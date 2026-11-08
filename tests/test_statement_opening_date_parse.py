"""The statement-opening skip reads each row date as a date, never as text.

A posted row on the account of a recorded statement opening is skipped only when
its date is a valid day before the opening day. A row with no date, a date that
is not ISO, or an impossible date is an import error. It is not skipped, not
marked seen, and not written. A corrected retry imports it normally.
All data is synthetic.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.cli import main as books_main  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.ledger.importer import import_transactions  # noqa: E402
from bookkeeping.ledger.model import Open  # noqa: E402
from bookkeeping.ledger.staging import StagingStore  # noqa: E402
from bookkeeping.ledger.store import LedgerStore, default_store_path  # noqa: E402
from bookkeeping.statement_opening import opening_dates, record_opening  # noqa: E402

CHECKING = "Assets:Bank:Checking"
OPENING_DAY = "2026-08-01"


def _make_entity(root: Path) -> Path:
    entity_dir = root / "co"
    (entity_dir / "staging").mkdir(parents=True)
    config = {"name": "Synthetic Co", "business_type": "consulting",
              "bank_account_mappings": {"feed-chk": CHECKING}}
    (entity_dir / "entity.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
    (entity_dir / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n", encoding="utf-8")
    return entity_dir


def _row(source_id: str, day: object) -> dict:
    row = {"id": source_id, "amount": "-5.00", "description": "FEED ROW", "account": "Checking",
           "accountId": "feed-chk", "currency": "USD"}
    if day is not None:
        row["date"] = day
    return row


def _queue_ids(entity_dir: Path) -> list[str]:
    path = entity_dir / "staging" / "pending-categorization.json"
    return sorted(str(item["id"]) for item in json.loads(path.read_text(encoding="utf-8"))) if path.exists() else []


class OpeningSkipDateParseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name))
        store = LedgerStore(default_store_path(self.entity))
        store.initialize()
        with store.transaction() as conn:
            store.insert_opens([Open(date=date(2026, 1, 1), account=CHECKING)], conn)
        record = record_opening(self.entity, account=CHECKING, on=OPENING_DAY, amount="1000.00")
        self.assertEqual(record["status"], "recorded", record)
        self.assertEqual(opening_dates(self.entity), {CHECKING: date(2026, 8, 1)})

    def tearDown(self):
        self._tmp.cleanup()

    def run_import(self, rows: list[dict], session: str = "feed-test"):
        return import_transactions(load_entity(self.entity), rows, session_id=session)

    def is_seen(self, source_id: str) -> bool:
        return StagingStore(self.entity / "staging").is_seen(source_id)

    def run_cli(self, rows: list[dict]) -> tuple[int, str, str]:
        path = Path(self._tmp.name) / "rows.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = books_main(["ingest", str(path), "--entity", str(self.entity), "--source", "synthetic"])
        return code, out.getvalue(), err.getvalue()

    # --- Reproductions from the draft evaluation ----------------------------------

    def test_a_row_with_no_date_is_an_error_not_a_skip(self):
        result = self.run_import([_row("no-date", None)])
        self.assertEqual(result.before_opening, [])
        self.assertTrue(result.errors)

    def test_a_row_after_the_opening_with_a_non_iso_date_is_not_dropped(self):
        result = self.run_import([_row("us-date", "08/10/2026")])
        self.assertEqual(result.before_opening, [])
        self.assertTrue(result.errors)

    # --- More invalid dates -------------------------------------------------------

    def test_an_impossible_date_is_an_error_not_a_skip(self):
        result = self.run_import([_row("bad-day", "2026-02-30")])
        self.assertEqual(result.before_opening, [])
        self.assertEqual(len(result.errors), 1)
        self.assertIn("bad-day", result.errors[0])

    def test_invalid_rows_are_not_seen_not_queued_and_not_posted(self):
        self.run_import([_row("no-date", None), _row("us-date", "08/10/2026"), _row("bad-day", "2026-02-30")])
        for source_id in ("no-date", "us-date", "bad-day"):
            self.assertFalse(self.is_seen(source_id), source_id)
        self.assertEqual(_queue_ids(self.entity), [])
        store = LedgerStore(default_store_path(self.entity))
        self.assertFalse({"no-date", "us-date", "bad-day"} & {r.source_id for r in store.load_source_refs()})

    def test_a_mixed_batch_imports_the_valid_rows_and_reports_the_invalid_rows(self):
        result = self.run_import([
            _row("before", "2026-07-20"), _row("on", OPENING_DAY), _row("after", "2026-08-10"),
            _row("no-date", None), _row("us-date", "08/10/2026"), _row("bad-day", "2026-02-30"),
        ])
        self.assertEqual([item["source_id"] for item in result.before_opening], ["before"])
        self.assertEqual(_queue_ids(self.entity), ["after", "on"])
        self.assertEqual(len(result.errors), 3)

    def test_a_timestamp_still_uses_its_day(self):
        result = self.run_import([_row("before", "2026-07-31T23:59:59Z"), _row("on", "2026-08-01T00:00:00Z"),
                                  _row("after", "2026-08-10T12:00:00Z")])
        self.assertEqual([item["source_id"] for item in result.before_opening], ["before"])
        self.assertEqual(_queue_ids(self.entity), ["after", "on"])
        self.assertEqual(result.errors, [])

    def test_a_corrected_retry_imports_the_row(self):
        first = self.run_import([_row("fix-me", "08/10/2026")], "s1")
        self.assertTrue(first.errors)
        retry = self.run_import([_row("fix-me", "2026-08-10")], "s2")
        self.assertEqual(retry.errors, [])
        self.assertEqual(retry.skipped_duplicate, 0)
        self.assertEqual(_queue_ids(self.entity), ["fix-me"])

    # --- The CLI prints the error and exits non-zero -----------------------------------

    def test_the_cli_reports_invalid_dates_and_exits_non_zero(self):
        code, out, _err = self.run_cli([_row("no-date", None), _row("us-date", "08/10/2026"),
                                        _row("bad-day", "2026-02-30"), _row("after", "2026-08-10")])
        self.assertEqual(code, 1, out)
        self.assertIn("errors:", out)
        for source_id in ("no-date", "us-date", "bad-day"):
            self.assertIn(source_id, out)
        self.assertNotIn("before opening", out)
        self.assertEqual(_queue_ids(self.entity), ["after"])

    def test_the_cli_retry_with_corrected_dates_exits_zero(self):
        code, _out, _err = self.run_cli([_row("fix-me", "08/10/2026")])
        self.assertEqual(code, 1)
        code, out, _err = self.run_cli([_row("fix-me", "2026-08-10")])
        self.assertEqual(code, 0, out)
        self.assertEqual(_queue_ids(self.entity), ["fix-me"])


if __name__ == "__main__":
    unittest.main()
