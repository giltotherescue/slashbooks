#!/usr/bin/env python3
"""Time ``queue confirm-group`` of many items and ``queue confirm`` of one item, for companies of growing size.

Run it from a checkout of the engine::

    python3 benchmarks/bench_confirm_group.py --sizes 5000,50000 --group 256 --repeat 3
    python3 benchmarks/bench_confirm_group.py --src /path/to/other/checkout/src   (to compare two versions)

For each size it builds a synthetic company (posted transactions from 200 payees, decided items in month archives,
``--group`` open proposals for one category and a few single proposals for another). Each run copies that company,
times one ``queue confirm`` of a single item, then one ``queue confirm-group`` of the whole group, through the CLI.
All data is synthetic.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

PARSER = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
PARSER.add_argument("--sizes", default="5000,50000", help="comma separated numbers of posted transactions")
PARSER.add_argument("--group", type=int, default=256, help="open proposals in the group (default 256)")
PARSER.add_argument("--repeat", type=int, default=3, help="runs for each size (default 3)")
PARSER.add_argument("--src", default=str(Path(__file__).resolve().parents[1] / "src"),
                    help="the src folder of the engine to measure (default: this checkout)")
ARGS = PARSER.parse_args()
sys.path.insert(0, ARGS.src)

from bookkeeping import cli, queue  # noqa: E402

GROUP_CATEGORY = "Expenses:Software"
SINGLE_CATEGORY = "Expenses:Fees"


def run(argv: list[str]) -> str:
    sink = io.StringIO()
    with redirect_stdout(sink), redirect_stderr(sink):
        code = cli.main(argv)
    if code != 0:
        raise SystemExit(f"command failed: {argv}\n{sink.getvalue()[-600:]}")
    return sink.getvalue()


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
    payees = [payee_name(i) for i in range(200)]
    context = {name: {"canonical_category": GROUP_CATEGORY, "confirmed_count": 9, "last_confirmed_date": "2026-01-01",
                      "last_operation_hash": "0" * 64, "notes": "", "reset": False} for name in payees}
    (entity / "learned-context").mkdir(exist_ok=True)
    (entity / "learned-context" / "counterparties.json").write_text(json.dumps(context), encoding="utf-8")
    rows = [{"id": f"b-{i:06d}", "date": f"2026-{1 + (i % 9):02d}-{1 + (i % 27):02d}", "amount": f"-{10 + (i % 90)}.{i % 100:02d}",
             "description": payees[i % len(payees)], "accountName": "Operating Checking", "accountType": "checking"}
            for i in range(size)]
    rows += [{"id": f"new-{i}", "date": "2026-10-05", "amount": f"-{12 + i % 7}.00",
              "description": f"NEW PAYEE {chr(65 + i % 26)}{chr(65 + (i // 26) % 26)}",
              "accountName": "Operating Checking", "accountType": "checking"} for i in range(ARGS.group)]
    rows += [{"id": f"solo-{i}", "date": "2026-10-06", "amount": "-7.00", "description": "SOLO PAYEE",
              "accountName": "Operating Checking", "accountType": "checking"} for i in range(ARGS.repeat)]
    source = root / "in.json"
    source.write_text(json.dumps(rows), encoding="utf-8")
    run(["ingest", str(source), "--entity", str(entity), "--session-id", "bench"])
    posted = sqlite3.connect(entity / "ledger.sqlite").execute("SELECT COUNT(*) FROM source_transactions").fetchone()[0]
    if posted < size:
        raise SystemExit(f"only {posted} of {size} transactions were posted: the learned context did not match")
    archived = size // 2
    months = [f"{2024 + (m // 12)}-{1 + (m % 12):02d}" for m in range(23)] + ["2026-10"]
    folder = entity / "review-queue" / "archive"
    folder.mkdir(parents=True, exist_ok=True)
    per_month = max(1, archived // len(months))
    for month in months:
        items = {f"a-{month}-{n:05d}.json": {"source_id": f"a-{month}-{n:05d}", "status": "confirmed", "date": f"{month}-15",
                                                "amount": "-1.00", "description": "ARCHIVED", "confirmed_category": GROUP_CATEGORY}
                 for n in range(per_month)}
        (folder / f"{month}.json").write_bytes(queue._archive_bytes(dict(sorted(items.items()))))
    group = [part for i in range(ARGS.group) for part in ("--source-id", f"new-{i}")]
    run(["queue", "propose-group", "--entity", str(entity), *group, "--category", GROUP_CATEGORY, "--reasoning", "Bench."])
    for i in range(ARGS.repeat):
        run(["queue", "propose", "--entity", str(entity), "--source-id", f"solo-{i}", "--category", SINGLE_CATEGORY,
             "--reasoning", "Bench."])
    return entity


def timed(argv: list[str]) -> tuple[float, str]:
    time.sleep(0.05)
    start = time.perf_counter()
    output = run(argv)
    return time.perf_counter() - start, output


def main() -> int:
    table = []
    for size in [int(part) for part in ARGS.sizes.split(",")]:
        with tempfile.TemporaryDirectory() as tmp:
            start = time.perf_counter()
            base = build(Path(tmp), size)
            print(f"[{size}] built in {time.perf_counter() - start:.1f} s", flush=True)
            singles, groups = [], []
            for attempt in range(ARGS.repeat):
                entity = Path(tmp) / f"run-{attempt}"
                shutil.copytree(base, entity, symlinks=True)
                single, _ = timed(["queue", "confirm", "--entity", str(entity), "--item", f"solo-{attempt}"])
                group, output = timed(["queue", "confirm-group", "--entity", str(entity), "--category", GROUP_CATEGORY])
                if f"Confirmed {ARGS.group} item" not in output:
                    raise SystemExit(f"unexpected output: {output}")
                singles.append(single * 1000)
                groups.append(group)
                print(f"[{size}] run {attempt + 1}: confirm one {single * 1000:.0f} ms, "
                      f"confirm-group of {ARGS.group} {group:.2f} s", flush=True)
                shutil.rmtree(entity)
            table.append((size, singles, groups))
    print()
    print(f"| Transactions | Confirm one (ms), runs | Median | Confirm-group of {ARGS.group} (s), runs | Median |")
    print("|---|---|---|---|---|")
    for size, singles, groups in table:
        print(f"| {size:,} | {' / '.join(f'{v:.0f}' for v in singles)} | {statistics.median(singles):.0f} | "
              f"{' / '.join(f'{v:.2f}' for v in groups)} | {statistics.median(groups):.2f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
