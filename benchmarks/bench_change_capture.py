#!/usr/bin/env python3
"""Time the change set capture of one approve and one categorize, for companies of growing size.

Run it from a checkout of the engine::

    python3 benchmarks/bench_change_capture.py --sizes 1000,10000,50000

For each size it builds a synthetic company (posted transactions, a learned context, a few open items and many decided
items that sit in month archives), then runs two commands inside ``changeset.capture`` and prints, for each capture, the
path that it took (``incremental`` or ``full-comparison``, the parts that it read in full and why) and the time of the
capture. The time is the time of entering and leaving ``capture``; the time of the command itself is printed apart.
The exit code is 1 when a capture fell back, or when the warm capture of the largest company is not within a small
constant of the smallest one (``--factor`` times, plus ``--slack-ms``).
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import statistics
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

PARSER = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
PARSER.add_argument("--sizes", default="1000,10000,50000", help="comma separated numbers of posted transactions")
PARSER.add_argument("--archived-months", type=int, default=24, help="month archives of decided items (default 24)")
PARSER.add_argument("--archived-share", type=float, default=0.5,
                    help="decided items in the archives, as a share of the transactions (default 0.5)")
PARSER.add_argument("--payees", type=int, default=200,
                    help="distinct payees in the learned context (default 200; a larger number makes the file larger)")
PARSER.add_argument("--repeat", type=int, default=3, help="warm captures to time for each command (default 3)")
PARSER.add_argument("--factor", type=float, default=3.0)
PARSER.add_argument("--slack-ms", type=float, default=15.0)
PARSER.add_argument("--src", default=str(Path(__file__).resolve().parents[1] / "src"),
                    help="the src folder of the engine to measure (default: this checkout)")
ARGS = PARSER.parse_args()
sys.path.insert(0, ARGS.src)

from bookkeeping import changeset, cli, queue  # noqa: E402


def run(argv: list[str]) -> int:
    sink = io.StringIO()
    with redirect_stdout(sink), redirect_stderr(sink):
        code = cli.main(argv)
    if code != 0:
        raise SystemExit(f"command failed: {argv}\n{sink.getvalue()[-400:]}")
    return code


def payee_name(i: int) -> str:
    """A payee name without digits: the engine drops digit runs when it matches a payee to its learned context."""
    letters = ""
    for _ in range(4):
        letters += chr(ord("A") + i % 26)
        i //= 26
    return f"VENDOR {letters}"


def build(root: Path, size: int) -> Path:
    entity = root / "co"
    run(["entity", "init", str(entity), "--name", "Bench Studio"])
    payees = [payee_name(i) for i in range(ARGS.payees)]
    context = {name: {"canonical_category": "Expenses:Software", "confirmed_count": 9, "last_confirmed_date": "2026-01-01",
                      "last_operation_hash": "0" * 64, "notes": "", "reset": False} for name in payees}
    (entity / "learned-context").mkdir(exist_ok=True)
    (entity / "learned-context" / "counterparties.json").write_text(json.dumps(context), encoding="utf-8")
    rows = [{"id": f"b-{i:06d}", "date": f"2026-{1 + (i % 9):02d}-{1 + (i % 27):02d}", "amount": f"-{10 + (i % 90)}.{i % 100:02d}",
             "description": payees[i % len(payees)], "accountName": "Operating Checking", "accountType": "checking"}
            for i in range(size)]
    rows += [{"id": f"new-{i}", "date": "2026-10-05", "amount": "-12.00", "description": "NEW PAYEE",
              "accountName": "Operating Checking", "accountType": "checking"} for i in range(2 * ARGS.repeat + 2)]
    source = root / "in.json"
    source.write_text(json.dumps(rows), encoding="utf-8")
    run(["ingest", str(source), "--entity", str(entity), "--session-id", "bench"])
    import sqlite3
    posted = sqlite3.connect(entity / "ledger.sqlite").execute("SELECT COUNT(*) FROM source_transactions").fetchone()[0]
    if posted < size:
        raise SystemExit(f"only {posted} of {size} transactions were posted: the learned context did not match")
    # Decided items in month archives, written in the engine's own archive format. The last month is the month of
    # the items that the benchmark decides, so its archive is as large as the others.
    archived = int(size * ARGS.archived_share)
    months = [f"{2024 + (m // 12)}-{1 + (m % 12):02d}" for m in range(ARGS.archived_months - 1)] + ["2026-10"]
    folder = entity / "review-queue" / "archive"
    folder.mkdir(parents=True, exist_ok=True)
    per_month = max(1, archived // len(months))
    for month in months:
        items = {f"a-{month}-{n:05d}.json": {"source_id": f"a-{month}-{n:05d}", "status": "confirmed", "date": f"{month}-15",
                                                "amount": "-1.00", "description": "ARCHIVED", "confirmed_category": "Expenses:Software"}
                 for n in range(per_month)}
        (folder / f"{month}.json").write_bytes(queue._archive_bytes(dict(sorted(items.items()))))
    return entity


def timed(entity: Path, argv: list[str], revision: int) -> tuple[float, float, dict]:
    time.sleep(0.05)  # A real host starts a command later than the clock tick of the last write.
    manager = changeset.capture(entity, books_revision=revision + 1, base_revision=revision)
    t0 = time.perf_counter()
    receipt = manager.__enter__()
    t1 = time.perf_counter()
    run(argv)
    t2 = time.perf_counter()
    manager.__exit__(None, None, None)
    t3 = time.perf_counter()
    document = receipt.change_set
    if not document["complete"]:
        raise SystemExit(f"incomplete change set: {document['reason']}")
    return (t1 - t0) + (t3 - t2), t2 - t1, getattr(receipt, "path_report", {"mode": "unknown"})


def describe(report: dict) -> str:
    notes = [*report.get("fallbacks", [])]
    rebuilt = report.get("rebuilt", [])
    text = report.get("mode", "unknown")
    if notes:
        text += " FALLBACK: " + "; ".join(notes)
    if rebuilt:
        text += " (index rebuilt: " + ", ".join(rebuilt) + ")"
    return text


def main() -> int:
    sizes = [int(part) for part in ARGS.sizes.split(",")]
    failed = False
    table = []
    for size in sizes:
        with tempfile.TemporaryDirectory() as tmp:
            entity = build(Path(tmp), size)
            revision = 0
            cold, _, report = timed(entity, ["queue", "list", "--entity", str(entity)], revision)
            print(f"[{size}] cold capture {cold * 1000:.1f} ms: {describe(report)}")
            approve, categorize, commands = [], [], []
            for i in range(ARGS.repeat):
                for kind, bucket in (("approve", approve), ("categorize", categorize)):
                    item = f"new-{2 * i + (0 if kind == 'approve' else 1)}"
                    run(["queue", "propose", "--entity", str(entity), "--source-id", item, "--category",
                         "Expenses:Software", "--reasoning", "Benchmark."])
                    argv = (["queue", "confirm", "--entity", str(entity), "--item", item] if kind == "approve" else
                            ["queue", "correct", "--entity", str(entity), "--item", item, "--category", "Expenses:Fees"])
                    capture_time, command_time, report = timed(entity, argv, revision)
                    revision += 1
                    bucket.append(capture_time * 1000)
                    commands.append(command_time * 1000)
                    print(f"[{size}] {kind} capture {capture_time * 1000:.1f} ms "
                          f"(the command took {command_time * 1000:.0f} ms): {describe(report)}")
                    if report.get("mode") == "incremental" and report.get("fallbacks"):
                        failed = True
                    elif report.get("mode") not in ("incremental", "unknown"):
                        failed = True
            table.append((size, int(size * ARGS.archived_share), cold * 1000, statistics.median(approve),
                          statistics.median(categorize), statistics.median(commands)))
    print()
    print("| Transactions | Archived items | Cold capture (ms) | Approve capture (ms) | Categorize capture (ms) | Command (ms) |")
    print("|---|---|---|---|---|---|")
    for size, archived, cold, approve, categorize, command in table:
        print(f"| {size:,} | {archived:,} | {cold:.1f} | {approve:.1f} | {categorize:.1f} | {command:.0f} |")
    base = max(table[0][3], table[0][4])
    for row in table[1:]:
        worst = max(row[3], row[4])
        if worst > ARGS.factor * base + ARGS.slack_ms:
            print(f"FAIL: the warm capture at {row[0]:,} ({worst:.1f} ms) is not within {ARGS.factor}x + "
                  f"{ARGS.slack_ms} ms of the one at {table[0][0]:,} ({base:.1f} ms)")
            failed = True
    print("FAIL: a capture fell back" if failed else "PASS: every capture was incremental and flat")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
