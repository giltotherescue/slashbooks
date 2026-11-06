"""Account register: ordering, currencies, backdated entries, splits, presentation sign, CLI."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bookkeeping.cli import main
from bookkeeping.reports.register import (
    SPLIT_MARKER, RegisterEntry, RegisterPosting, account_register, entity_register, natural_sign, running_balances,
)
from test_statement_structure import make_entity

D = Decimal
FIXTURE = Path(__file__).parent / "fixtures" / "register" / "register.beancount"


def entry(key, day, *postings, description="x"):
    return RegisterEntry(key, date.fromisoformat(day), description,
                         tuple(RegisterPosting(a, D(v), c) for a, v, c in postings))


class RegisterTests(unittest.TestCase):
    def test_orders_by_date_and_keeps_given_order_on_a_day(self):
        entries = [
            entry("1", "2026-01-02", ("Assets:A", "10.00", "USD"), ("Income:I", "-10.00", "USD")),
            entry("3", "2026-01-03", ("Assets:A", "5.00", "USD"), ("Income:I", "-5.00", "USD")),
            entry("2", "2026-01-03", ("Assets:A", "1.00", "USD"), ("Income:I", "-1.00", "USD")),
        ]
        reg = account_register(entries, "Assets:A")
        self.assertEqual([line.entry_key for line in reg.lines], ["1", "3", "2"])
        self.assertEqual([str(line.balance) for line in reg.lines], ["10.00", "15.00", "16.00"])

    def test_backdated_entry_changes_later_balances(self):
        entries = [
            entry("1", "2026-01-10", ("Assets:A", "100.00", "USD"), ("Income:I", "-100.00", "USD")),
            entry("2", "2026-01-05", ("Assets:A", "7.00", "USD"), ("Income:I", "-7.00", "USD")),
        ]
        reg = account_register(entries, "Assets:A")
        self.assertEqual([(line.entry_key, str(line.balance)) for line in reg.lines], [("2", "7.00"), ("1", "107.00")])

    def test_one_running_balance_for_each_currency(self):
        entries = [
            entry("1", "2026-01-01", ("Assets:A", "10.00", "USD"), ("Equity:E", "-10.00", "USD")),
            entry("2", "2026-01-02", ("Assets:A", "20.00", "EUR"), ("Equity:E", "-20.00", "EUR")),
            entry("3", "2026-01-03", ("Assets:A", "5.00", "USD"), ("Equity:E", "-5.00", "USD")),
        ]
        reg = account_register(entries, "Assets:A")
        self.assertEqual([(line.currency, str(line.balance)) for line in reg.lines],
                         [("USD", "10.00"), ("EUR", "20.00"), ("USD", "15.00")])
        self.assertEqual({c: str(v) for c, v in reg.closing.items()}, {"USD": "15.00", "EUR": "20.00"})

    def test_split_is_the_other_account_or_a_marker(self):
        entries = [
            entry("1", "2026-01-01", ("Assets:A", "-3.00", "USD"), ("Expenses:X", "3.00", "USD")),
            entry("2", "2026-01-02", ("Assets:A", "-3.00", "USD"), ("Expenses:X", "1.00", "USD"),
                  ("Expenses:Y", "2.00", "USD")),
            entry("3", "2026-01-03", ("Assets:A", "-3.00", "USD"), ("Expenses:X", "1.00", "USD"),
                  ("Expenses:X", "2.00", "USD")),
        ]
        self.assertEqual([line.split for line in account_register(entries, "Assets:A").lines],
                         ["Expenses:X", SPLIT_MARKER, "Expenses:X"])

    def test_natural_sign_for_each_account_type(self):
        self.assertEqual([natural_sign(a) for a in ("Assets:A", "Expenses:E", "Liabilities:L", "Equity:Q", "Income:I")],
                         [1, 1, -1, -1, -1])
        entries = [entry("1", "2026-01-01", ("Liabilities:L", "-40.00", "USD"), ("Expenses:E", "40.00", "USD"))]
        owed = account_register(entries, "Liabilities:L", presentation="natural").lines[0]
        self.assertEqual((str(owed.balance), str(owed.change), str(owed.amount)), ("40.00", "40.00", "-40.00"))
        self.assertEqual(str(account_register(entries, "Liabilities:L").lines[0].balance), "-40.00")
        self.assertEqual(str(account_register(entries, "Expenses:E", presentation="natural").lines[0].balance), "40.00")
        income = [entry("1", "2026-01-01", ("Assets:A", "9.00", "USD"), ("Income:I", "-9.00", "USD"))]
        self.assertEqual(str(account_register(income, "Income:I", presentation="natural").lines[0].balance), "9.00")
        self.assertEqual(str(account_register(income, "Assets:A", presentation="natural").lines[0].balance), "9.00")
        with self.assertRaises(ValueError):
            account_register(income, "Assets:A", presentation="other")

    def test_empty_account(self):
        reg = account_register([entry("1", "2026-01-01", ("Assets:A", "1.00", "USD"), ("Equity:E", "-1.00", "USD"))],
                               "Assets:None")
        self.assertEqual((reg.lines, dict(reg.opening), dict(reg.closing)), ((), {}, {}))
        self.assertEqual(account_register([], "Assets:A").lines, ())

    def test_date_range_has_an_opening_balance(self):
        entries = [entry(str(i), f"2026-01-0{i}", ("Assets:A", "10.00", "USD"), ("Equity:E", "-10.00", "USD"))
                   for i in range(1, 6)]
        reg = account_register(entries, "Assets:A", from_date=date(2026, 1, 3), to_date=date(2026, 1, 4))
        self.assertEqual({c: str(v) for c, v in reg.opening.items()}, {"USD": "20.00"})
        self.assertEqual([str(line.balance) for line in reg.lines], ["30.00", "40.00"])
        self.assertEqual({c: str(v) for c, v in reg.closing.items()}, {"USD": "40.00"})
        after = account_register(entries, "Assets:A", from_date=date(2027, 1, 1))
        self.assertEqual((after.lines, {c: str(v) for c, v in after.opening.items()}), ((), {"USD": "50.00"}))

    def test_running_balances_for_all_accounts_match_each_register(self):
        entries = [
            entry("a", "2026-01-02", ("Assets:A", "10.00", "USD"), ("Liabilities:L", "-10.00", "USD")),
            entry("b", "2026-01-01", ("Assets:A", "1.00", "EUR"), ("Equity:E", "-1.00", "EUR")),
            entry("c", "2026-01-03", ("Assets:A", "2.00", "USD"), ("Assets:A", "3.00", "USD"), ("Equity:E", "-5.00", "USD")),
        ]
        for presentation in ("ledger", "natural"):
            every = running_balances(entries, presentation=presentation)
            for account in ("Assets:A", "Liabilities:L", "Equity:E"):
                reg = account_register(entries, account, presentation=presentation)
                flat = [(key, b.posting_index, b.balance) for (key, acct), bs in every.items() if acct == account for b in bs]
                self.assertCountEqual(flat, [(line.entry_key, line.posting_index, line.balance) for line in reg.lines])
        self.assertEqual([(b.posting_index, str(b.balance)) for b in running_balances(entries)[("c", "Assets:A")]],
                         [(0, "12.00"), (1, "15.00")])


class RegisterLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = make_entity(Path(self._tmp.name), FIXTURE.read_text(encoding="utf-8"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_checking_register_from_a_ledger(self):
        reg = entity_register(self.entity, "Assets:Bank:Checking")
        self.assertEqual([(str(l.date), str(l.balance), l.split) for l in reg.lines], [
            ("2026-01-02", "5000.00", "Equity:Owner-Contributions"),
            ("2026-01-05", "5100.00", "Income:Services"),
            ("2026-01-10", "6300.00", "Income:Services"),
            ("2026-01-15", "6000.00", SPLIT_MARKER),
            ("2026-01-20", "5960.00", "Liabilities:CreditCard"),
            ("2026-01-25", "5950.00", "Liabilities:CreditCard"),
        ])

    def test_card_owed_is_positive_in_natural_presentation(self):
        reg = entity_register(self.entity, "Liabilities:CreditCard")
        self.assertEqual([str(l.balance) for l in reg.lines], ["40.00", "0.00", "-10.00"])
        ledger = entity_register(self.entity, "Liabilities:CreditCard", presentation="ledger")
        self.assertEqual([str(l.balance) for l in ledger.lines], ["-40.00", "0.00", "10.00"])

    def test_unused_account_is_empty_and_unknown_account_is_an_error(self):
        self.assertEqual(entity_register(self.entity, "Assets:Unused").lines, ())
        with self.assertRaises(ValueError):
            entity_register(self.entity, "Assets:Missing")

    def test_cli_json_and_error(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["report", "register", "--entity", str(self.entity), "--account", "Liabilities:CreditCard",
                         "--from", "2026-01-15", "--format", "json"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["opening"], {"USD": "40.00"})
        self.assertEqual([l["balance"] for l in data["lines"]], ["0.00", "-10.00"])
        self.assertEqual(set(data["lines"][0]), {"entry_id", "posting_index", "date", "description", "account",
                                                  "currency", "amount", "change", "split", "balance"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["report", "register", "--entity", str(self.entity), "--account", "Assets:Missing"])
        self.assertEqual(code, 1)
        self.assertIn("Unknown account", err.getvalue())


if __name__ == "__main__":
    unittest.main()
