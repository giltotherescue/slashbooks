"""An opening answer carries the exact recorded opening facts, apart from the candidate statement fields.

All fixtures are synthetic.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from bookkeeping.ledger.books_policy_check import reviewed_entry  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore, default_store_path  # noqa: E402
from bookkeeping.statement_opening import record_opening  # noqa: E402
import test_statement_opening_overlap as _overlap  # noqa: E402
from bookkeeping import statement_import as si  # noqa: E402

_Base = _overlap.StatementOpeningOverlapTests
CHECKING = _overlap.CHECKING
FIXTURES = _overlap.FIXTURES

CARD = "Liabilities:CreditCard:Visa"


class RecordedOpeningFactsTests(unittest.TestCase):
    setUp = _Base.setUp
    use_cutover = _Base.use_cutover
    qb_opening = _Base.qb_opening
    statement = _Base.statement
    write_entries = _Base.write_entries
    reverse_qb_opening = _Base.reverse_qb_opening
    checking_total = _Base.checking_total

    def later_statement(self) -> str:
        """A synthetic statement for 2026-01-15 to 2026-02-14 that prints an opening of 140.00."""
        name = "chase_like_checking_later.csv"
        si.save_profile(self.entity, FIXTURES / name, name="later", ledger_account=CHECKING, overrides={})
        result = si.import_file(self.entity, FIXTURES / name, profile="later", ts="2026-02-15T00:00:00Z",
                                period_start="2026-01-15", period_end="2026-02-14", opening_balance="140.00")
        return result["statements"][0]["statement"]["statement_id"]

    def open_card(self) -> None:
        store = LedgerStore(default_store_path(self.entity))
        with store.transaction() as conn:
            store.insert_opens([Open(date=date(2025, 1, 1), account=CARD)], conn)

    def assert_facts(self, answer, *, amount, day, import_source, opening_source=None, account=CHECKING):
        facts = answer["recorded_opening"]
        self.assertEqual((facts["date"], facts["account"], facts["amount"], facts["currency"], facts["import_source"],
                          facts["opening_source"]), (day, account, amount, "USD", import_source, opening_source))
        self.assertEqual(facts["entry"]["date"], day)
        self.assertEqual(facts["source_id"], dict(map(tuple, facts["entry"]["meta"]))["source-id"])
        entry_lines = {p["account"]: p["amount"] for p in facts["entry"]["postings"]}
        self.assertIn(account, entry_lines)
        self.assertEqual(len(facts["entry_fingerprint"]), 64)

    def test_typed_opening_then_a_later_statement_with_a_printed_opening(self):
        preview = record_opening(self.entity, account=CHECKING, on="2026-01-01", amount="100.00", dry_run=True)
        self.assertEqual(record_opening(self.entity, account=CHECKING, on="2026-01-01", amount="100.00")["status"],
                         "recorded")
        sid = self.later_statement()
        answer = record_opening(self.entity, sid)
        self.assertEqual((answer["status"], answer["reason"]), ("not_eligible", "opening_exists"))
        self.assert_facts(answer, amount="100.00", day="2026-01-01", import_source="statement-opening",
                          opening_source="person")
        # The recorded entry is the entry the preview proposed.
        self.assertEqual(answer["recorded_opening"]["entry_fingerprint"], preview["proposed_entry_fingerprint"])
        self.assertEqual(answer["recorded_opening"]["entry"], preview["proposed_entry"])
        # The candidate fields stay apart.
        self.assertEqual((answer["candidate_date"], answer["printed_amount"], answer["candidate_source"]),
                         ("2026-01-15", "140.00", "printed"))
        self.assertEqual(self.checking_total(), Decimal("100.00"))

    def test_replace_to_a_later_day_still_names_the_recorded_opening(self):
        record_opening(self.entity, account=CHECKING, on="2026-01-01", amount="100.00")
        sid = self.later_statement()
        answer = record_opening(self.entity, sid, replace=True)
        self.assertEqual(answer["reason"], "replace_not_earlier")
        self.assert_facts(answer, amount="100.00", day="2026-01-01", import_source="statement-opening",
                          opening_source="person")

    def test_a_card_opening_is_shown_as_money_owed_and_the_entry_keeps_the_ledger_sign(self):
        self.open_card()
        record_opening(self.entity, account=CARD, on="2026-01-01", amount="150.00")
        answer = record_opening(self.entity, account=CARD, on="2026-01-15", amount="200.00")
        self.assertEqual(answer["reason"], "opening_exists")
        self.assert_facts(answer, amount="150.00", day="2026-01-01", import_source="statement-opening",
                          opening_source="person", account=CARD)
        lines = {p["account"]: p["amount"] for p in answer["recorded_opening"]["entry"]["postings"]}
        self.assertEqual(Decimal(str(lines[CARD])), Decimal("-150.00"))
        self.assertEqual(answer["candidate_amount"], "200.00")

    def test_a_zero_opening_is_a_recorded_opening(self):
        zero = Entry(date=date(2026, 1, 1), narration="Zero opening",
                     meta=(("source-id", "statement-opening-zero"), ("import-source", "statement-opening")),
                     postings=(Posting(CHECKING, Decimal("0.00")), Posting("Equity:Opening-Balances", Decimal("0.00"))))
        self.write_entries([zero], opens=[Open(date=date(2026, 1, 1), account="Equity:Opening-Balances")])
        answer = record_opening(self.entity, account=CHECKING, on="2026-01-15", amount="140.00")
        self.assertEqual(answer["reason"], "opening_exists")
        self.assert_facts(answer, amount="0.00", day="2026-01-01", import_source="statement-opening")

    def test_an_exact_replay_is_already_recorded_with_the_same_facts(self):
        sid = self.statement(start="2026-01-01")
        self.assertEqual(record_opening(self.entity, sid)["status"], "recorded")
        again = record_opening(self.entity, sid)
        self.assertEqual(again["status"], "already_recorded")
        self.assert_facts(again, amount="140.00", day="2026-01-01", import_source="statement-opening",
                          opening_source="printed")
        self.assertEqual(again["recorded_opening"]["statement_id"], sid)
        self.assertEqual(again["candidate_date"], "2026-01-01")

    def test_a_reversed_opening_leaves_no_recorded_opening_and_the_statement_opening_records(self):
        record_opening(self.entity, account=CHECKING, on="2026-01-01", amount="100.00")
        self.reverse_qb_opening("statement-opening")
        sid = self.statement(start="2026-01-01")
        done = record_opening(self.entity, sid)
        self.assertEqual(done["status"], "recorded")
        self.assertNotIn("recorded_opening", done)
        self.assertEqual(self.checking_total(), Decimal("140.00"))

    def test_a_quickbooks_opening_is_the_recorded_source(self):
        self.assertTrue(self.qb_opening()["applied"])
        sid = self.later_statement()
        answer = record_opening(self.entity, sid)
        self.assertEqual(answer["reason"], "opening_exists_other_source")
        self.assert_facts(answer, amount="100.00", day="2026-01-01", import_source="quickbooks-opening")
        self.assertEqual(answer["opening_entry"]["import_source"], "quickbooks-opening")
        store = LedgerStore(default_store_path(self.entity))
        entry = next(e for e in store.load_entries() if e.source_id == answer["recorded_opening"]["source_id"])
        self.assertEqual(answer["recorded_opening"]["entry_fingerprint"],
                         reviewed_entry(entry)["proposed_entry_fingerprint"])
        self.assertEqual((answer["candidate_date"], answer["printed_amount"]), ("2026-01-15", "140.00"))


del _Base, _overlap  # keep unittest from collecting the borrowed test class

if __name__ == "__main__":
    unittest.main()
