"""A statement opening must not stack on another opening of the same account.

All fixtures are synthetic. The QuickBooks snapshot opens ``Assets:Bank:Checking`` with 100.00.
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

from bookkeeping import quickbooks_sync as sync  # noqa: E402
from bookkeeping import statement_import as si  # noqa: E402
from bookkeeping.entity import init_entity, load_entity  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore, default_store_path  # noqa: E402
from bookkeeping.statement_opening import record_opening  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "statements"
SNAPSHOT = ROOT / "tests" / "fixtures" / "quickbooks_api" / "snapshot.json"
CHECKING = "Assets:Bank:Checking"
SAVINGS = "Assets:Bank:Savings"


class StatementOpeningOverlapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.entity = Path(self._tmp.name) / "books"
        init_entity(self.entity, name="Synthetic Example Books")
        self.source = Path(self._tmp.name) / "snapshot.json"
        self.source.write_text(SNAPSHOT.read_text(), encoding="utf-8")

    def qb_opening(self, cutover: date = date(2026, 1, 1)) -> dict:
        entity = load_entity(self.entity)
        preview = sync.sync_opening(entity, self.source, cutover, chart_decision="preserve")
        return sync.sync_opening(entity, self.source, cutover, apply=True, chart_decision="preserve",
                                 chart_plan_hash=preview["chart"]["plan_hash"])

    def statement(self, account: str = CHECKING, profile: str = "checking", start: str = "2025-12-15") -> str:
        name = "chase_like_checking.csv"
        si.save_profile(self.entity, FIXTURES / name, name=profile, ledger_account=account, overrides={})
        result = si.import_file(self.entity, FIXTURES / name, profile=profile, ts="2026-02-01T00:00:00Z",
                                period_start=start, period_end="2026-01-31", opening_balance="140.00")
        self.assertEqual(result["status"], "imported")
        return result["statements"][0]["statement"]["statement_id"]

    def checking_total(self, account: str = CHECKING) -> Decimal:
        with LedgerStore(default_store_path(self.entity)).connection() as conn:
            return sum((Decimal(str(r[0])) for r in conn.execute(
                "SELECT amount FROM postings WHERE account = ?", (account,))), Decimal("0.00"))

    def entry_count(self) -> int:
        with LedgerStore(default_store_path(self.entity)).connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def reverse_qb_opening(self, day: date = date(2026, 1, 1)) -> None:
        store = LedgerStore(default_store_path(self.entity))
        original = next(e for e in store.load_entries() if dict(e.meta).get("import-source") == "quickbooks-opening")
        reversal = Entry(date=original.date, narration="Reversal of QuickBooks opening",
                         meta=(("source-id", "reversal-qb-opening"), ("reverses", original.source_id)),
                         postings=tuple(Posting(p.account, -p.amount, p.currency) for p in original.postings))
        with store.transaction() as conn:
            store.insert_entries([reversal], conn)

    def test_statement_opening_is_refused_when_the_account_has_a_quickbooks_opening(self):
        self.assertTrue(self.qb_opening()["applied"])
        sid = self.statement()
        before = self.entry_count()
        refused = record_opening(self.entity, sid)
        self.assertEqual((refused["status"], refused["reason"]), ("not_eligible", "opening_exists_other_source"))
        self.assertIn("2026-01-01", refused["message"])
        self.assertEqual(self.entry_count(), before)
        self.assertEqual(self.checking_total(), Decimal("100.00"))

    def test_refusal_holds_for_replace_dry_run_and_a_later_start_day(self):
        self.qb_opening()
        sid = self.statement()
        for kwargs in ({"replace": True}, {"dry_run": True}):
            refused = record_opening(self.entity, sid, **kwargs)
            self.assertEqual(refused["reason"], "opening_exists_other_source")
        later = record_opening(self.entity, account=CHECKING, on="2026-02-01", amount="140.00", replace=True)
        self.assertEqual(later["reason"], "opening_exists_other_source")
        self.assertEqual(self.checking_total(), Decimal("100.00"))

    def test_a_mid_month_start_day_before_or_after_the_quickbooks_opening_is_refused(self):
        self.qb_opening()
        for day in ("2025-12-15", "2026-01-15"):
            typed = record_opening(self.entity, account=CHECKING, on=day, amount="140.00")
            self.assertEqual((typed["status"], typed["reason"]), ("not_eligible", "opening_exists_other_source"))
        self.assertEqual(self.checking_total(), Decimal("100.00"))

    def test_the_statement_rows_still_import_after_the_refusal(self):
        self.qb_opening()
        sid = self.statement()
        record_opening(self.entity, sid)
        again = si.import_file(self.entity, FIXTURES / "chase_like_checking.csv", profile="checking",
                               ts="2026-02-02T00:00:00Z", period_start="2025-12-15", period_end="2026-01-31",
                               opening_balance="140.00")
        self.assertEqual(again["status"], "imported")

    def test_a_different_account_still_gets_its_statement_opening(self):
        self.qb_opening()
        store = LedgerStore(default_store_path(self.entity))
        with store.transaction() as conn:
            store.insert_opens([Open(date=date(2025, 1, 1), account=SAVINGS)], conn)
        sid = self.statement(account=SAVINGS, profile="savings")
        done = record_opening(self.entity, sid)
        self.assertEqual(done["status"], "recorded")
        self.assertEqual(self.checking_total(SAVINGS), Decimal("140.00"))
        self.assertEqual(self.checking_total(), Decimal("100.00"))

    def test_a_statement_opening_still_blocks_a_quickbooks_opening(self):
        sid = self.statement()
        self.assertEqual(record_opening(self.entity, sid)["status"], "recorded")
        entity = load_entity(self.entity)
        result = sync.sync_opening(entity, self.source, date(2026, 1, 1), apply=True, chart_decision="preserve")
        self.assertFalse(result["applied"])
        self.assertTrue(any("overlap" in e for e in result["opening"]["errors"]))
        self.assertEqual(self.checking_total(), Decimal("140.00"))

    def test_the_statement_only_path_is_unchanged_and_replays_exactly(self):
        sid = self.statement()
        self.assertEqual(record_opening(self.entity, sid)["status"], "recorded")
        self.assertEqual(record_opening(self.entity, sid)["status"], "already_recorded")
        self.assertEqual(self.checking_total(), Decimal("140.00"))
        self.assertEqual(self.entry_count(), 1)

    def test_a_reversed_quickbooks_opening_no_longer_blocks_and_retry_works(self):
        self.qb_opening()
        sid = self.statement()
        refused = record_opening(self.entity, sid)
        self.assertEqual((refused["status"], refused["reason"]), ("not_eligible", "opening_exists_other_source"))
        self.reverse_qb_opening()
        done = record_opening(self.entity, sid)
        self.assertEqual(done["status"], "recorded")
        self.assertEqual(self.checking_total(), Decimal("140.00"))


if __name__ == "__main__":
    unittest.main()
