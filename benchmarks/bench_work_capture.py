"""Benchmark of ``work.capture``: the cost of the capture around one small write, at three sizes of the books.

All data is synthetic. For each size the script builds a company folder with that many posted transactions (ledger
entries, bank rows and seen ids) and a tenth as many pending transactions, then it runs one small write
(``queue propose`` then ``queue confirm`` of one pending row) inside a work capture. It times only the capture:
the time to enter the block and the time to leave it, not the command.

    cold         the index file ``.change-capture.sqlite`` does not exist yet (the first capture builds it)
    warm         the index is current (the normal case after one capture)
    full         ``incremental=False``: two full snapshots, as before

The books hold 400 decided review items for each month, so a larger size is a longer history. The capture reads the
archive of each month that a command touches (the month of the newest items), never the older months.

Run:  python3 benchmarks/bench_work_capture.py            (sizes 1000, 10000, 50000)
      python3 benchmarks/bench_work_capture.py --sizes 1000 10000 --repeat 3
Each line prints the path that the capture took (``incremental``, ``saved`` or ``full``) and the time in ms.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bookkeeping import change_capture, cli, queue, work  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402

CHECKING = "Assets:Bank:Operating-Checking"


PER_MONTH = 400  # Decided review items for each month. The queue keeps one archive file for each month.


def day_of(number: int) -> str:
    month = number // PER_MONTH
    return f"{2000 + month // 12}-{month % 12 + 1:02d}-{number % 27 + 1:02d}"


def run_cli(*argv: str) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return cli.main(list(argv))


def build(entity: Path, size: int) -> None:
    """A company folder with ``size`` posted transactions and ``size // 10 + 20`` pending ones."""
    assert run_cli("entity", "init", str(entity), "--name", "Benchmark Studio") == 0
    pending_count = size // 10 + 20
    conn = sqlite3.connect(entity / "ledger.sqlite")
    try:
        accounts = {name for (name,) in conn.execute("SELECT name FROM accounts")}
        for name in (CHECKING, "Expenses:Software"):
            if name not in accounts:
                conn.execute("INSERT INTO accounts (name, currency, open_date) VALUES (?, 'USD', '2020-01-01')", (name,))
        with conn:
            for number in range(size):
                day = day_of(number)
                source_id = f"bench-{number:06d}"
                cursor = conn.execute(
                    "INSERT INTO entries (date, narration, source_id) VALUES (?, 'synthetic row', ?)", (day, source_id))
                conn.executemany(
                    "INSERT INTO postings (entry_id, account, amount) VALUES (?, ?, ?)",
                    [(cursor.lastrowid, CHECKING, "-10.00"), (cursor.lastrowid, "Expenses:Software", "10.00")])
                conn.execute(
                    "INSERT INTO source_transactions (id, payload_json, status, imported_at) VALUES (?, ?, 'posted', ?)",
                    (source_id, json.dumps({"id": source_id, "date": day, "amount": "-10.00"}), day + "T00:00:00Z"))
    finally:
        conn.close()
    # Every posted transaction was decided in the review queue. The queue moves decided items into month archives.
    folder = entity / "review-queue"
    folder.mkdir(exist_ok=True)
    for number in range(size):
        source_id = f"bench-{number:06d}"
        item = {"source_id": source_id, "status": "confirmed", "proposal_type": "category", "amount": "-10.00",
                "date": day_of(number), "proposed_category": "Expenses:Software",
                "confirmed_category": "Expenses:Software", "reasoning": "Synthetic history."}
        (folder / f"{source_id}.json").write_text(json.dumps(item, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    queue.compact_closed_items(load_entity(entity))
    staging = entity / "staging"
    staging.mkdir(exist_ok=True)
    pending = [{"id": f"pend-{n:06d}", "date": "2026-03-01", "amount": "-12.00", "description": f"SYNTHETIC {n}",
                "accountName": "Operating Checking", "accountType": "checking"} for n in range(pending_count)]
    (staging / "pending-categorization.json").write_text(json.dumps(pending), encoding="utf-8")
    seen = [f"bench-{n:06d}" for n in range(size)] + [row["id"] for row in pending]
    (staging / "seen-ids.json").write_text(json.dumps(seen), encoding="utf-8")


def write(entity: Path, source_id: str) -> None:
    assert run_cli("queue", "propose", "--entity", str(entity), "--source-id", source_id,
                   "--category", "Expenses:Software", "--reasoning", "Synthetic.") == 0
    assert run_cli("queue", "confirm", "--entity", str(entity), "--item", source_id) == 0


def timed(entity: Path, source_id: str, incremental: bool) -> tuple[str, float, int]:
    """Run one write inside a capture. Returns the path, the time of the capture in ms and the event count."""
    time.sleep(0.03)  # A host runs commands more than one clock tick apart; the capture waits for the tick otherwise.
    manager = work.capture(entity, incremental=incremental)
    start = time.perf_counter()
    holder = manager.__enter__()
    entered = time.perf_counter()
    try:
        write(entity, source_id)
    finally:
        left = time.perf_counter()
        manager.__exit__(None, None, None)
        done = time.perf_counter()
    return holder.path, ((entered - start) + (done - left)) * 1000, len(holder.events)


def measure(size: int, repeat: int) -> list[tuple[str, str, float]]:
    rows: list[tuple[str, str, float]] = []
    with tempfile.TemporaryDirectory() as tmp:
        entity = Path(tmp) / "co"
        build(entity, size)
        counter = iter(range(size // 10 + 20))
        index = entity / change_capture.INDEX_NAME

        def source() -> str:
            return f"pend-{next(counter):06d}"

        for kind in ("cold", "warm", "full"):
            samples, path = [], ""
            for attempt in range(repeat + (1 if kind == "warm" else 0)):
                if kind == "cold":
                    for leftover in entity.glob(change_capture.INDEX_NAME + "*"):
                        leftover.unlink()
                path, ms, events = timed(entity, source(), incremental=kind != "full")
                assert events, "the write gave no work event"
                if kind == "warm" and attempt == 0:
                    continue  # The first capture after a cold start builds the index.
                samples.append(ms)
            rows.append((kind, path, statistics.median(samples)))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000, 50000])
    parser.add_argument("--repeat", type=int, default=5, help="captures to time for each cell (the median is shown)")
    args = parser.parse_args()
    print(f"{'transactions':>12}  {'mode':<5}  {'path':<12}  {'capture ms (median)':>20}")
    for size in args.sizes:
        for kind, path, ms in measure(size, args.repeat):
            print(f"{size:>12}  {kind:<5}  {path:<12}  {ms:>20.1f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
