#!/usr/bin/env python3
"""Time one entry insert, one ``source_exists`` and a 1,000-row ``books ingest``, for companies of growing size.

Run it from a checkout of the engine, and again with ``--src`` of another checkout to compare two versions::

    python3 benchmarks/bench_ledger_insert.py --sizes 5000,50000 --work /some/folder
    python3 benchmarks/bench_ledger_insert.py --sizes 5000,50000 --work /some/folder --src ../other/src

For each size it builds a synthetic company once (posted bank rows with learned payees) in ``--work`` and keeps it
there for the next run. Each measure runs on a fresh copy, so one run does not change the next. The copy keeps the
append-only trigger text of the engine that built it when the engine under test accepts that text as valid, so use one
``--work`` folder for each engine whose installed trigger text differs.

* ``insert``: ``LedgerStore.insert_entries`` of 1,000 entries in one transaction (rolled back), time per entry.
* ``exists``: ``LedgerStore.source_exists`` for an id that is not there and for one that is, time per call.
* ``ingest``: ``books ingest`` of 1,000 new rows that all post.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import statistics
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path

PARSER = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
PARSER.add_argument("--sizes", default="5000,50000", help="comma separated numbers of posted transactions")
PARSER.add_argument("--work", required=True, help="a folder for the companies; it is kept between runs")
PARSER.add_argument("--repeat", type=int, default=3, help="runs of each measure; the median is printed (default 3)")
PARSER.add_argument("--src", default=str(Path(__file__).resolve().parents[1] / "src"),
                    help="the src folder of the engine to measure (default: this checkout)")
ARGS = PARSER.parse_args()
sys.path.insert(0, ARGS.src)

from bookkeeping import cli  # noqa: E402
from bookkeeping.ledger.model import Entry, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402

PAYEES = [f"VENDOR {chr(ord('A') + i % 26)}{chr(ord('A') + i // 26)}" for i in range(50)]


def run(argv: list[str]) -> None:
    sink = io.StringIO()
    with redirect_stdout(sink), redirect_stderr(sink):
        code = cli.main(argv)
    if code != 0:
        raise SystemExit(f"command failed: {argv}\n{sink.getvalue()[-400:]}")


def rows(prefix: str, count: int) -> list[dict]:
    return [{"id": f"{prefix}-{i:06d}", "date": f"2026-{1 + (i % 9):02d}-{1 + (i % 27):02d}",
             "amount": f"-{10 + (i % 90)}.{i % 100:02d}", "description": PAYEES[i % len(PAYEES)],
             "accountName": "Operating Checking", "accountType": "checking"} for i in range(count)]


def build(size: int) -> Path:
    entity = Path(ARGS.work) / f"co-{size}"
    if (entity / "ledger.sqlite").is_file():
        return entity
    run(["entity", "init", str(entity), "--name", "Bench Studio"])
    context = {name: {"canonical_category": "Expenses:Software", "confirmed_count": 9, "last_confirmed_date": "2026-01-01",
                      "last_operation_hash": "0" * 64, "notes": "", "reset": False} for name in PAYEES}
    (entity / "learned-context").mkdir(exist_ok=True)
    (entity / "learned-context" / "counterparties.json").write_text(json.dumps(context), encoding="utf-8")
    source = Path(ARGS.work) / f"in-{size}.json"
    source.write_text(json.dumps(rows("b", size)), encoding="utf-8")
    run(["ingest", str(source), "--entity", str(entity), "--session-id", "bench"])
    return entity


def fresh(entity: Path) -> Path:
    copy = entity.with_name(entity.name + "-run")
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(entity, copy)
    return copy


def measure(entity: Path) -> dict[str, float]:
    copy = fresh(entity)
    store = LedgerStore(copy / "ledger.sqlite")
    store.initialize()
    entries = [Entry(date=date(2026, 10, 5), narration="x", meta=(("source-id", f"ins-{i}"),),
                     postings=(Posting("Expenses:Software", Decimal("1")), Posting("Assets:Bank:Checking", Decimal("-1"))))
               for i in range(1000)]
    conn = store.connect()
    start = time.perf_counter()
    store.insert_entries(entries, conn)
    insert = (time.perf_counter() - start) / len(entries)
    conn.rollback()
    conn.close()
    start = time.perf_counter()
    for i in range(200):
        store.source_exists(f"missing-{i}")
    missing = (time.perf_counter() - start) / 200
    start = time.perf_counter()
    for i in range(200):
        store.source_exists(f"b-{i:06d}")
    found = (time.perf_counter() - start) / 200
    source = copy / "new.json"
    source.write_text(json.dumps(rows("new", 1000)), encoding="utf-8")
    start = time.perf_counter()
    run(["ingest", str(source), "--entity", str(copy), "--session-id", "bench-new"])
    ingest = time.perf_counter() - start
    posted = store.counts().entries
    shutil.rmtree(copy)
    return {"insert_ms": insert * 1000, "exists_missing_ms": missing * 1000, "exists_found_ms": found * 1000,
            "ingest_s": ingest, "entries_after": posted}


def main() -> int:
    Path(ARGS.work).mkdir(parents=True, exist_ok=True)
    print(f"engine: {ARGS.src}")
    print(f"{'size':>7} {'insert ms/entry':>16} {'exists miss ms':>15} {'exists hit ms':>14} {'ingest 1000 s':>14}")
    for size in (int(value) for value in ARGS.sizes.split(",")):
        entity = build(size)
        runs = [measure(entity) for _ in range(ARGS.repeat)]
        med = {key: statistics.median(run[key] for run in runs) for key in runs[0]}
        print(f"{size:>7} {med['insert_ms']:>16.3f} {med['exists_missing_ms']:>15.3f} {med['exists_found_ms']:>14.3f} "
              f"{med['ingest_s']:>14.2f}   (entries after ingest: {int(med['entries_after'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
