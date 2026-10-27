"""Tests for `books statement record-opening`.

All fixtures under tests/fixtures/statements/ are synthetic.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping import statement_import as si  # noqa: E402
from bookkeeping import tieout  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore, default_store_path  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402
from bookkeeping.reports.cache import open_cache  # noqa: E402
from bookkeeping.reports.cashflow_statement import opening_entries  # noqa: E402
from bookkeeping.reports.statements import _account_balances_as_of  # noqa: E402
from bookkeeping.statement_opening import StatementOpeningError, record_opening  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "statements"
CHECKING = "Assets:Bank:Checking"
CARD = "Liabilities:CreditCard:Visa"


def _make_entity(root: Path) -> Path:
    entity_dir = root / "co"
    (entity_dir / "staging").mkdir(parents=True)
    config = {"name": "Synthetic Co", "business_type": "consulting"}
    (entity_dir / "entity.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
    (entity_dir / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n", encoding="utf-8")
    return entity_dir


class RecordOpeningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def imp(self, name: str, profile: str, account: str = CHECKING, **kwargs) -> dict:
        si.save_profile(self.entity, FIXTURES / name, name=profile, ledger_account=account, overrides={})
        result = si.import_file(self.entity, FIXTURES / name, profile=profile, ts="2026-02-01T00:00:00Z", **kwargs)
        return result["statements"][0]["statement"]

    def balances(self, day: str) -> dict:
        conn = open_cache(self.entity)
        try:
            return _account_balances_as_of(conn, date.fromisoformat(day))
        finally:
            conn.close()

    def test_first_statement_opening_is_recorded_once_against_opening_equity(self):
        record = self.imp("chase_like_checking.csv", "checking", period_start="2026-01-01", period_end="2026-01-31")
        self.assertEqual(record["opening_balance"], "1000.00")
        sid = record["statement_id"]

        preview = record_opening(self.entity, sid, dry_run=True)
        self.assertEqual(preview["status"], "dry_run")
        self.assertEqual(preview["date"], "2026-01-01")
        self.assertEqual(preview["postings"], [{"account": CHECKING, "amount": "1000.00"},
                                               {"account": "Equity:Opening-Balances", "amount": "-1000.00"}])
        self.assertEqual(self.balances("2026-01-31").get(CHECKING, Decimal(0)), Decimal(0))

        done = record_opening(self.entity, sid)
        self.assertEqual(done["status"], "recorded")
        balances = self.balances("2026-01-01")
        self.assertEqual(balances[CHECKING], Decimal("1000.00"))
        self.assertEqual(balances["Equity:Opening-Balances"], Decimal("-1000.00"))
        self.assertEqual(self.balances("2025-12-31").get(CHECKING, Decimal(0)), Decimal(0))

        again = record_opening(self.entity, sid)
        self.assertEqual(again["status"], "already_recorded")
        with LedgerStore(default_store_path(self.entity)).connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)

        # The cash flow statement takes it as a beginning balance; the statement tie-out leaves it out of the movement.
        conn = open_cache(self.entity)
        try:
            self.assertEqual(len(opening_entries(conn, date(2026, 1, 1), date(2026, 1, 31))), 1)
        finally:
            conn.close()
        postings = tieout.load_postings(self.entity, [CHECKING])[CHECKING]
        period = tieout.period_tieout(account=CHECKING, statement_id=sid, start=date(2026, 1, 1),
                                      end=date(2026, 1, 31), opening=Decimal("1000.00"),
                                      closing=Decimal("1000.00"), postings=postings, waiting=tieout.Series([]))
        self.assertEqual(period["status"], "matches")

    def test_card_opening_uses_the_ledger_sign(self):
        record = self.imp("card_debit_credit.csv", "card", account=CARD, period_start="2026-01-01",
                          period_end="2026-01-31", opening_balance="1000.00", closing_balance="769.99")
        done = record_opening(self.entity, record["statement_id"])
        self.assertEqual(done["status"], "recorded")
        self.assertEqual(done["postings"][0], {"account": CARD, "amount": "-1000.00"})
        self.assertEqual(self.balances("2026-01-01")[CARD], Decimal("-1000.00"))

    def test_only_the_first_statement_of_an_account_carries_the_opening(self):
        first = self.imp("chase_like_checking.csv", "checking", period_start="2026-01-01", period_end="2026-01-31")
        later = self.imp("chase_like_checking_later.csv", "checking-later", period_start="2026-01-15",
                         period_end="2026-02-14", opening_balance="3191.00", closing_balance="3067.26")
        refused = record_opening(self.entity, later["statement_id"])
        self.assertEqual((refused["status"], refused["reason"]), ("not_eligible", "not_first_statement"))
        self.assertEqual(record_opening(self.entity, first["statement_id"])["status"], "recorded")

    def test_entries_before_the_statement_refuse(self):
        record = self.imp("chase_like_checking.csv", "checking", period_start="2026-01-01", period_end="2026-01-31")
        store = LedgerStore(default_store_path(self.entity))
        store.initialize()
        with store.transaction() as conn:
            store.insert_opens([Open(date=date(2025, 12, 1), account=CHECKING),
                                Open(date=date(2025, 12, 1), account="Expenses:Software")], conn)
            store.insert_entries([Entry(date=date(2025, 12, 20), narration="Earlier", meta=(("source-id", "t-1"),),
                                        postings=(Posting(CHECKING, Decimal("-10.00")),
                                                  Posting("Expenses:Software", Decimal("10.00"))))], conn)
        refused = record_opening(self.entity, record["statement_id"])
        self.assertEqual((refused["status"], refused["reason"]), ("not_eligible", "books_before_statement"))
        self.assertEqual(refused["first_posting"], "2025-12-20")

    def test_unconfirmed_period_has_no_saved_statement(self):
        record = self.imp("chase_like_checking.csv", "checking")
        self.assertFalse(record["period_confirmed"])
        with self.assertRaises(StatementOpeningError) as caught:
            record_opening(self.entity, record["statement_id"])
        self.assertEqual(caught.exception.code, "statement_not_found")

    def test_cli_and_remote_contract(self):
        record = self.imp("chase_like_checking.csv", "checking", period_start="2026-01-01", period_end="2026-01-31")
        argv = ["statement", "record-opening", "--entity", str(self.entity), "--statement-id", record["statement_id"]]
        self.assertFalse(command_spec([*argv, "--dry-run"])["mutates"])
        self.assertTrue(command_spec(argv)["mutates"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(argv), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "recorded")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["statement", "record-opening", "--entity", str(self.entity),
                                   "--statement-id", "missing"]), 1)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], "statement_not_found")


if __name__ == "__main__":
    unittest.main()
