"""The shared importer skips rows dated before a recorded statement opening.

These tests record the safe behavior of that skip (``ledger/importer.py``, the ``openings`` lookup in
``import_transactions``): a posted row on the account of a live statement opening, dated before the opening day,
is not imported, because the opening balance already includes it. The row is listed in
``ImportResult.before_opening``. Rows with a missing, impossible or non-ISO date are covered by
``tests/test_statement_opening_date_parse.py``. All data is synthetic.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.ledger.importer import import_transactions  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore, default_store_path  # noqa: E402
from bookkeeping.statement_opening import opening_dates, record_opening  # noqa: E402

CHECKING = "Assets:Bank:Checking"
SAVINGS = "Assets:Bank:Savings"
OPENING_DAY = "2026-08-01"


def _make_entity(root: Path) -> Path:
    entity_dir = root / "co"
    (entity_dir / "staging").mkdir(parents=True)
    config = {"name": "Synthetic Co", "business_type": "consulting",
              "bank_account_mappings": {"feed-chk": CHECKING, "feed-sav": SAVINGS}}
    (entity_dir / "entity.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
    (entity_dir / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n", encoding="utf-8")
    return entity_dir


def _row(source_id: str, day: object, account_id: str = "feed-chk", **extra: object) -> dict:
    row = {"id": source_id, "amount": "-5.00", "description": "FEED ROW", "account": "Checking",
           "accountId": account_id, "currency": "USD", **extra}
    if day is not None:
        row["date"] = day
    return row


def _queue_ids(entity_dir: Path) -> list[str]:
    path = entity_dir / "staging" / "pending-categorization.json"
    return [str(item["id"]) for item in json.loads(path.read_text(encoding="utf-8"))] if path.exists() else []


class StatementOpeningSkipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name))
        store = LedgerStore(default_store_path(self.entity))
        store.initialize()
        with store.transaction() as conn:
            store.insert_opens([Open(date=date(2026, 1, 1), account=CHECKING),
                                Open(date=date(2026, 1, 1), account=SAVINGS)], conn)
        record = record_opening(self.entity, account=CHECKING, on=OPENING_DAY, amount="1000.00")
        self.assertEqual(record["status"], "recorded", record)
        self.assertEqual(opening_dates(self.entity), {CHECKING: date(2026, 8, 1)})

    def tearDown(self):
        self._tmp.cleanup()

    def run_import(self, rows: list[dict], session: str = "feed-test"):
        return import_transactions(load_entity(self.entity), rows, session_id=session)

    def ledger_source_ids(self) -> set[str]:
        store = LedgerStore(default_store_path(self.entity))
        return {ref.source_id for ref in store.load_source_refs()}

    def test_row_before_the_opening_is_listed_and_not_imported(self):
        result = self.run_import([_row("feed-0", "2026-07-20"), _row("feed-1", OPENING_DAY),
                                  _row("feed-2", "2026-08-10")])
        self.assertEqual(result.before_opening, [{"source_id": "feed-0", "date": "2026-07-20", "account": CHECKING,
                                                  "opening_date": OPENING_DAY}])
        # A row on the opening day is activity of the first period, so it is imported.
        self.assertEqual(_queue_ids(self.entity), ["feed-1", "feed-2"])
        self.assertNotIn("feed-0", self.ledger_source_ids())

    def test_skip_is_not_remembered_so_a_resync_skips_the_row_again(self):
        first = self.run_import([_row("feed-0", "2026-07-20")], "s1")
        again = self.run_import([_row("feed-0", "2026-07-20")], "s2")
        self.assertEqual(len(first.before_opening), 1)
        self.assertEqual(len(again.before_opening), 1)
        self.assertEqual(_queue_ids(self.entity), [])

    def test_other_accounts_are_not_affected(self):
        result = self.run_import([_row("sav-0", "2026-07-20", account_id="feed-sav")])
        self.assertEqual(result.before_opening, [])
        self.assertEqual(_queue_ids(self.entity), ["sav-0"])

    def test_a_reversed_opening_no_longer_skips(self):
        store = LedgerStore(default_store_path(self.entity))
        with store.connection() as conn:
            opening = conn.execute("SELECT source_id FROM entries WHERE metadata_json LIKE '%statement-opening%'"
                                   ).fetchone()[0]
        with store.transaction() as conn:
            store.insert_entries([Entry(date=date(2026, 8, 1), narration="Reverse opening",
                                        meta=(("source-id", opening + "-rev"), ("reverses", opening)),
                                        postings=(Posting(CHECKING, Decimal("-1000.00")),
                                                  Posting("Equity:Opening-Balances", Decimal("1000.00"))))], conn)
        self.assertEqual(opening_dates(self.entity), {})
        result = self.run_import([_row("feed-0", "2026-07-20")])
        self.assertEqual(result.before_opening, [])

    def test_a_pending_row_is_not_skipped(self):
        result = self.run_import([_row("feed-p", "2026-07-20", pending=True)])
        self.assertEqual(result.before_opening, [])
        self.assertEqual(result.new_pending, 1)

    def test_only_statement_openings_count_not_other_opening_entries(self):
        store = LedgerStore(default_store_path(self.entity))
        with store.transaction() as conn:
            store.insert_entries([Entry(date=date(2026, 8, 1), narration="Opening from another tool",
                                        meta=(("source-id", "qb-open-sav"), ("import-source", "quickbooks-opening")),
                                        postings=(Posting(SAVINGS, Decimal("50.00")),
                                                  Posting("Equity:Opening-Balances", Decimal("-50.00"))))], conn)
        self.assertNotIn(SAVINGS, opening_dates(self.entity))
        result = self.run_import([_row("sav-0", "2026-07-20", account_id="feed-sav")])
        self.assertEqual(result.before_opening, [])


if __name__ == "__main__":
    unittest.main()
