"""A statement opening must not stack on another opening of the same account.

All fixtures are synthetic. The QuickBooks snapshot opens ``Assets:Bank:Checking`` with 100.00.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date, timedelta
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

    def use_cutover(self, cutover: date) -> None:
        """Write a coherent synthetic snapshot whose period starts on ``cutover`` (prior-day opening reports)."""
        text = SNAPSHOT.read_text()
        if cutover != date(2026, 1, 1):
            moved = {"2026-01-01": cutover.isoformat(), "2025-12-31": (cutover - timedelta(days=1)).isoformat(),
                     "2026-01-31": (cutover + timedelta(days=30)).isoformat(),
                     "2026-01-15": (cutover + timedelta(days=5)).isoformat(),
                     "2026-02-01": (cutover + timedelta(days=31)).isoformat()}
            for old, new in moved.items():
                text = text.replace(old, new)
        self.source.write_text(text, encoding="utf-8")

    def qb_opening(self, cutover: date = date(2026, 1, 1)) -> dict:
        self.use_cutover(cutover)
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

    def write_entries(self, entries, opens=()) -> None:
        """Write entries the way the engine does, with audit events, so ledger integrity stays valid."""
        store = LedgerStore(default_store_path(self.entity))
        with store.transaction(immediate=True) as conn:
            store.append_audit_event("intent", {"session_id": "test-opening", "entries": len(entries)}, conn)
            store.insert_opens(list(opens), conn)
            store.insert_entries(list(entries), conn)
            for item in entries:
                store.append_audit_event("entry-written", {"session_id": "test-opening",
                                                           "source_id": item.source_id or "reversal"}, conn)
            store.append_audit_event("ledger-store-sealed", {"session_id": "test-opening", "entries": len(entries),
                                                             "source_ids": [], "store_sha256": store.content_digest(conn)}, conn)

    def entry_count(self) -> int:
        with LedgerStore(default_store_path(self.entity)).connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def reverse_qb_opening(self, source: str = "quickbooks-opening") -> None:
        store = LedgerStore(default_store_path(self.entity))
        original = next(e for e in store.load_entries() if dict(e.meta).get("import-source") == source)
        # Like a real reversal, it keeps the metadata of the original and adds ``reverses``.
        meta = tuple((k, v) for k, v in original.meta if k != "source-id")
        reversal = Entry(date=original.date, narration="Reversal of opening",
                         meta=meta + (("reverses", original.source_id),),
                         postings=tuple(Posting(p.account, -p.amount, p.currency) for p in original.postings))
        self.write_entries([reversal])

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
        self.assertTrue(any(CHECKING in e and "opening balance" in e for e in result["opening"]["errors"]))
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

    # A statement opening first, then a QuickBooks opening: the rule is per account, on any date.
    def qb_result(self, apply: bool = True, cutover: date = date(2026, 1, 1)) -> dict:
        self.use_cutover(cutover)
        entity = load_entity(self.entity)
        preview = sync.sync_opening(entity, self.source, cutover, chart_decision="preserve")
        if not apply:
            return preview
        return sync.sync_opening(entity, self.source, cutover, apply=True, chart_decision="preserve",
                                 chart_plan_hash=preview["chart"]["plan_hash"])

    def test_quickbooks_opening_before_a_statement_opening_date_is_refused(self):
        done = record_opening(self.entity, account=CHECKING, on="2026-02-01", amount="140.00")
        self.assertEqual(done["status"], "recorded")
        before = self.entry_count()
        preview = self.qb_result(apply=False)
        self.assertFalse(preview["opening"]["success"])
        self.assertTrue(any(CHECKING in e for e in preview["opening"]["errors"]))
        result = self.qb_result()
        self.assertFalse(result["applied"])
        self.assertEqual(self.entry_count(), before)
        self.assertEqual(self.checking_total(), Decimal("140.00"))

    def test_quickbooks_opening_for_another_account_is_allowed(self):
        store = LedgerStore(default_store_path(self.entity))
        with store.transaction() as conn:
            store.insert_opens([Open(date=date(2025, 1, 1), account=SAVINGS)], conn)
        self.assertEqual(record_opening(self.entity, account=SAVINGS, on="2026-02-01", amount="75.00")["status"],
                         "recorded")
        self.assertTrue(self.qb_result()["applied"])
        self.assertEqual(self.checking_total(), Decimal("100.00"))
        self.assertEqual(self.checking_total(SAVINGS), Decimal("75.00"))

    def test_reversed_statement_opening_frees_the_account_for_quickbooks(self):
        record_opening(self.entity, account=CHECKING, on="2026-02-01", amount="140.00")
        self.reverse_qb_opening("statement-opening")
        self.assertTrue(self.qb_result()["applied"])
        self.assertEqual(self.checking_total(), Decimal("100.00"))

    def test_a_zero_statement_opening_line_is_an_opening(self):
        # Supported representation: a committed entry with import-source statement-opening and a posting line to
        # the account, even of 0.00. The presence of the line counts, not the sum.
        zero = Entry(date=date(2026, 2, 1), narration="Zero opening",
                     meta=(("source-id", "statement-opening-zero"), ("import-source", "statement-opening")),
                     postings=(Posting(CHECKING, Decimal("0.00")), Posting("Equity:Opening-Balances", Decimal("0.00"))))
        self.write_entries([zero], opens=[Open(date=date(2026, 2, 1), account="Equity:Opening-Balances")])
        result = self.qb_result()
        self.assertFalse(result["applied"])
        self.assertEqual(self.checking_total(), Decimal("0.00"))

    def test_mid_month_quickbooks_opening_first_blocks_a_statement_opening_either_side(self):
        mid = date(2026, 3, 15)
        self.assertTrue(self.qb_opening(mid)["applied"])
        for day in ("2026-03-01", "2026-03-15", "2026-03-20", "2026-04-01"):
            refused = record_opening(self.entity, account=CHECKING, on=day, amount="140.00")
            self.assertEqual((refused["status"], refused["reason"]), ("not_eligible", "opening_exists_other_source"), day)
        self.assertEqual(self.checking_total(), Decimal("100.00"))

    def test_mid_month_statement_opening_first_blocks_a_quickbooks_opening_before_and_after(self):
        self.assertEqual(record_opening(self.entity, account=CHECKING, on="2026-03-20", amount="140.00")["status"],
                         "recorded")
        for cutover in (date(2026, 3, 15), date(2026, 3, 20), date(2026, 4, 1)):
            result = self.qb_result(cutover=cutover)
            self.assertFalse(result["applied"], cutover)
        self.assertEqual(self.checking_total(), Decimal("140.00"))
        self.assertEqual(self.entry_count(), 1)

    def test_checks_run_under_the_entity_write_lock_when_the_write_happens(self):
        from bookkeeping import quickbooks as qb
        from bookkeeping.ledger import importer
        held = []
        real = qb.active_opening_for_account

        def spy(*args, **kwargs):
            held.append(self.entity.resolve() in importer._held_entity_locks.get())
            return real(*args, **kwargs)

        with unittest.mock.patch.object(qb, "active_opening_for_account", spy):
            record_opening(self.entity, account=CHECKING, on="2026-02-01", amount="140.00")
            self.assertEqual(held, [True], "statement opening")
        self.assertEqual(self.checking_total(), Decimal("140.00"))

    def test_quickbooks_apply_checks_the_account_again_under_the_lock(self):
        from bookkeeping import quickbooks as qb
        from bookkeeping.ledger import importer
        held = []
        real = qb.active_opening_for_account

        def spy(*args, **kwargs):
            held.append(self.entity.resolve() in importer._held_entity_locks.get())
            return real(*args, **kwargs)

        with unittest.mock.patch.object(qb, "active_opening_for_account", spy):
            self.assertTrue(self.qb_result()["applied"])
        self.assertTrue(held and held[-1], "the last check before the write runs under the lock")


class ReversalIdentityTests(unittest.TestCase):
    """Unit tests of the exact-reversal rule on entries in memory."""

    @staticmethod
    def entry(source_id, day, amount, meta=(), account=CHECKING, source="statement-opening"):
        return Entry(date=day, narration="n", meta=tuple(([("source-id", source_id)] if source_id else [])
                                                         + [("import-source", source)] + list(meta)),
                     postings=(Posting(account, Decimal(amount)), Posting("Equity:Opening-Balances", -Decimal(amount))))

    def found(self, entries, currency="USD", sources=("statement-opening",)):
        from bookkeeping import quickbooks as qb
        return qb.active_opening_for_account(None, CHECKING, currency, sources, entries)

    D = date(2026, 2, 1)

    def test_a_unique_exact_reversal_frees_the_account(self):
        original = self.entry("o1", self.D, "5.00")
        reversal = self.entry(None, self.D, "-5.00", meta=[("reverses", "o1")])
        self.assertIsNone(self.found([original, reversal]))

    def test_malformed_or_partial_reversals_leave_the_opening_active(self):
        original = self.entry("o1", self.D, "5.00")
        for bad in (self.entry(None, self.D, "-4.00", meta=[("reverses", "o1")]),          # partial amount
                    self.entry(None, date(2026, 2, 2), "-5.00", meta=[("reverses", "o1")]),  # other day
                    self.entry(None, self.D, "5.00", meta=[("reverses", "o1")]),            # same sign
                    self.entry(None, self.D, "-5.00", meta=[("reverses", "missing")])):      # no original
            self.assertIsNotNone(self.found([original, bad]))

    def test_an_ambiguous_original_or_a_double_claim_fails_closed(self):
        one, two = self.entry("o1", self.D, "5.00"), self.entry("o1", self.D, "5.00")
        reversal = self.entry(None, self.D, "-5.00", meta=[("reverses", "o1")])
        self.assertIsNotNone(self.found([one, two, reversal]))
        original = self.entry("o1", self.D, "5.00")
        again = self.entry(None, self.D, "-5.00", meta=[("reverses", "o1")])
        self.assertIsNotNone(self.found([original, reversal, again]))

    def test_an_entry_cannot_reverse_itself_and_a_zero_entry_is_still_an_opening(self):
        selfish = self.entry("z1", self.D, "0.00", meta=[("reverses", "z1")])
        self.assertIsNone(self.found([selfish]))  # a reversal entry is not an opening
        zero = self.entry("z1", self.D, "0.00")
        self.assertIsNotNone(self.found([zero]))
        self.assertIsNotNone(self.found([zero, self.entry("z2", self.D, "0.00", meta=[("reverses", "z2")])]))

    def test_the_feed_id_alias_is_one_identity_and_a_reversal_of_a_reversal_does_not_count(self):
        original = self.entry("banksync:abc", self.D, "5.00")
        self.assertIsNone(self.found([original, self.entry(None, self.D, "-5.00", meta=[("reverses", "abc")])]))
        reversal = self.entry("r1", self.D, "-5.00", meta=[("reverses", "o1")])
        undo = self.entry(None, self.D, "5.00", meta=[("reverses", "r1")])
        base = self.entry("o1", self.D, "5.00")
        self.assertIsNone(self.found([base, reversal, undo]))
        # The reversal of a reversal does not restore the first reversal as an opening or reverse it again.

    def test_currency_and_account_are_part_of_the_identity(self):
        original = self.entry("o1", self.D, "5.00")
        self.assertIsNone(self.found([original], currency="EUR"))
        self.assertIsNone(self.found([self.entry("o2", self.D, "5.00", account=SAVINGS)]))
        self.assertIsNone(self.found([original], sources=("quickbooks-opening",)))


if __name__ == "__main__":
    unittest.main()
