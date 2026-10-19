"""Statement of cash flows, indirect method."""
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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bookkeeping.cli import main
from bookkeeping.reports.cashflow_statement import build_cash_flow, cash_flow, classify
from bookkeeping.reports.statements import balance_sheet
from test_statement_structure import make_entity

D = Decimal

CASH_BOOK = """\
option "title" "Cash Co"
option "operating_currency" "USD"

2026-01-01 open Assets:Bank:Checking USD
2026-01-01 open Assets:Fixed:Equipment USD
2026-01-01 open Assets:Processors:Stripe USD
2026-01-01 open Income:Services USD
2026-01-01 open Expenses:Software USD
2026-01-01 open Equity:Owner-Contributions USD
2026-01-01 open Equity:Owner-Draws USD
2026-01-01 open Liabilities:CreditCard USD
2026-01-01 open Liabilities:Loans:Bank-Loan USD

2026-01-01 * "Owner puts money in"
  Assets:Bank:Checking         10000.00 USD
  Equity:Owner-Contributions  -10000.00 USD

2026-01-05 * "Client" "Work"
  Assets:Bank:Checking          5000.00 USD
  Income:Services              -5000.00 USD

2026-01-08 * "Laptop" "Equipment"
  Assets:Fixed:Equipment        2000.00 USD
  Assets:Bank:Checking         -2000.00 USD

2026-01-10 * "Bank" "Loan proceeds"
  Assets:Bank:Checking          3000.00 USD
  Liabilities:Loans:Bank-Loan  -3000.00 USD

2026-01-15 * "Tool" "Software on the card"
  Expenses:Software              400.00 USD
  Liabilities:CreditCard        -400.00 USD

2026-01-20 * "Owner" "Draw"
  Equity:Owner-Draws            1000.00 USD
  Assets:Bank:Checking         -1000.00 USD

2026-01-25 * "Stripe" "Payout in transit"
  Assets:Processors:Stripe       300.00 USD
  Income:Services               -300.00 USD

2026-02-05 * "Card" "Card payment"
  Liabilities:CreditCard         150.00 USD
  Assets:Bank:Checking          -150.00 USD
"""


def cash_book_entity(root: Path) -> Path:
    return make_entity(root, CASH_BOOK)


def line_map(result):
    return {l["key"]: l for l in result.lines}


class CashBookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = cash_book_entity(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_january_groups(self):
        r = cash_flow(self.entity, date(2026, 1, 1), date(2026, 1, 31))
        lines = line_map(r)
        # Net income: 5000 + 300 - 400 = 4900.
        self.assertEqual(lines["net_income"]["amount"], "4900.00")
        # Operating: net income plus the card balance that grew by 400.
        self.assertEqual(lines["Liabilities:CreditCard"]["amount"], "400.00")
        self.assertEqual(r.totals["net_cash_operating"], D("5300.00"))
        # Investing: equipment bought for 2000.
        self.assertEqual(r.totals["net_cash_investing"], D("-2000.00"))
        # Financing: contribution 10000, loan 3000, draw -1000.
        self.assertEqual(lines["Liabilities:Loans:Bank-Loan"]["amount"], "3000.00")
        self.assertEqual(lines["Equity:Owner-Draws"]["amount"], "-1000.00")
        self.assertEqual(r.totals["net_cash_financing"], D("12000.00"))
        # The processor balance is not guessed: it has its own group.
        self.assertEqual(r.totals["needs_classification"], D("-300.00"))
        self.assertEqual([n["account"] for n in r.needs_classification], ["Assets:Processors:Stripe"])
        self.assertEqual(r.totals["net_change_in_cash"], D("15000.00"))

    def test_ties_to_the_balance_sheet_to_the_cent(self):
        for start, end in ((date(2026, 1, 1), date(2026, 1, 31)), (date(2026, 2, 1), date(2026, 2, 28)),
                           (date(2026, 1, 1), date(2026, 2, 28))):
            r = cash_flow(self.entity, start, end)
            bs = balance_sheet(self.entity, end)
            bs_cash = sum((row["amount"] for row in bs.sections[0]["rows"] if "Bank" in row["label"]), D("0.00"))
            self.assertTrue(r.reconciliation["ties"])
            self.assertEqual(r.reconciliation["difference"], "0.00")
            self.assertEqual(r.totals["ending_cash"], bs_cash)
            self.assertEqual(r.totals["beginning_cash"] + r.totals["net_change_in_cash"], bs_cash)

    def test_second_period_begins_at_prior_ending_cash(self):
        january = cash_flow(self.entity, date(2026, 1, 1), date(2026, 1, 31))
        february = cash_flow(self.entity, date(2026, 2, 1), date(2026, 2, 28))
        self.assertEqual(february.totals["beginning_cash"], january.totals["ending_cash"])
        self.assertEqual(february.totals["net_change_in_cash"], D("-150.00"))
        self.assertEqual(line_map(february)["Liabilities:CreditCard"]["amount"], "-150.00")

    def test_empty_period_has_zero_change_and_ties(self):
        r = cash_flow(self.entity, date(2025, 1, 1), date(2025, 1, 31))
        self.assertEqual(r.totals["net_change_in_cash"], D("0.00"))
        self.assertTrue(r.reconciliation["ties"])

    def test_json_shape(self):
        r = cash_flow(self.entity, date(2026, 1, 1), date(2026, 1, 31))
        payload = json.loads(r.to_json())
        self.assertEqual(payload["kind"], "cashflow")
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["method"], "indirect")
        self.assertEqual([s["label"] for s in payload["sections"]],
                         ["Operating activities", "Investing activities", "Financing activities", "Needs classification"])
        self.assertEqual(payload["totals"]["net_change_in_cash"], "15000.00")
        self.assertTrue(payload["reconciliation"]["ties"])
        self.assertEqual(payload["reconciliation"]["cash_accounts"][0]["account"], "Assets:Bank:Checking")
        self.assertEqual(payload["structure"]["lines"][-1]["key"], "ending_cash")

    def test_text_has_reconciliation_and_classification_notice(self):
        text = cash_flow(self.entity, date(2026, 1, 1), date(2026, 1, 31)).to_text()
        self.assertIn("The statement ties to the balance sheet cash accounts.", text)
        self.assertIn("Needs classification", text)
        self.assertIn("Processors", text)
        for token in ["beancount", "SELECT", "sqlite", "Assets:Bank:"]:
            self.assertNotIn(token, text)

    def test_no_classification_group_when_everything_is_known(self):
        ledger = CASH_BOOK.replace("Assets:Processors:Stripe", "Assets:Bank:Stripe-Balance")
        with tempfile.TemporaryDirectory() as tmp:
            entity = make_entity(Path(tmp), ledger)
            r = cash_flow(entity, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(r.needs_classification, [])
        self.assertNotIn("needs_classification", line_map(r))


class AccrualAccountTests(unittest.TestCase):
    def test_receivable_payable_prepaid_deferred_and_depreciation(self):
        ledger = """\
option "operating_currency" "USD"

2026-01-01 open Assets:Bank:Checking USD
2026-01-01 open Assets:Receivable:Accounts-Receivable USD
2026-01-01 open Assets:Other:Prepaid-Expenses USD
2026-01-01 open Assets:Fixed:Equipment USD
2026-01-01 open Assets:Fixed:Accumulated-Depreciation USD
2026-01-01 open Liabilities:Payable:Accounts-Payable USD
2026-01-01 open Liabilities:Other:Deferred-Revenue USD
2026-01-01 open Income:Services USD
2026-01-01 open Expenses:Software USD
2026-01-01 open Expenses:Depreciation USD
2026-01-01 open Equity:Owner-Contributions USD

2026-01-01 * "Start"
  Assets:Bank:Checking          9000.00 USD
  Assets:Fixed:Equipment        1000.00 USD
  Equity:Owner-Contributions  -10000.00 USD

2026-01-10 * "Invoice sent"
  Assets:Receivable:Accounts-Receivable  2000.00 USD
  Income:Services                       -2000.00 USD

2026-01-12 * "Annual license paid"
  Assets:Other:Prepaid-Expenses  600.00 USD
  Assets:Bank:Checking          -600.00 USD

2026-01-15 * "Bill received"
  Expenses:Software              500.00 USD
  Liabilities:Payable:Accounts-Payable  -500.00 USD

2026-01-20 * "Customer pays ahead"
  Assets:Bank:Checking           800.00 USD
  Liabilities:Other:Deferred-Revenue  -800.00 USD

2026-01-31 * "Depreciation"
  Expenses:Depreciation          100.00 USD
  Assets:Fixed:Accumulated-Depreciation  -100.00 USD
"""
        with tempfile.TemporaryDirectory() as tmp:
            entity = make_entity(Path(tmp), ledger)
            r = cash_flow(entity, date(2026, 1, 2), date(2026, 1, 31))
        lines = line_map(r)
        # Net income: 2000 - 500 - 100 = 1400.
        self.assertEqual(lines["net_income"]["amount"], "1400.00")
        self.assertEqual(lines["Assets:Receivable:Accounts-Receivable"]["amount"], "-2000.00")
        self.assertEqual(lines["Assets:Other:Prepaid-Expenses"]["amount"], "-600.00")
        self.assertEqual(lines["Liabilities:Payable:Accounts-Payable"]["amount"], "500.00")
        self.assertEqual(lines["Liabilities:Other:Deferred-Revenue"]["amount"], "800.00")
        self.assertEqual(lines["Assets:Fixed:Accumulated-Depreciation"]["amount"], "100.00")
        self.assertIn("Depreciation and amortization", lines["Assets:Fixed:Accumulated-Depreciation"]["label"])
        # 1400 - 2000 - 600 + 500 + 800 + 100 = 200.
        self.assertEqual(r.totals["net_cash_operating"], D("200.00"))
        self.assertEqual(r.totals["net_cash_investing"], D("0.00"))
        self.assertEqual(r.needs_classification, [])
        self.assertTrue(r.reconciliation["ties"])
        self.assertEqual(r.totals["net_change_in_cash"], D("200.00"))


class ClassificationTests(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(classify("Assets:Bank:Checking", "bank")[0], "cash")
        self.assertEqual(classify("Liabilities:CreditCard", "credit_card")[0], "operating")
        self.assertEqual(classify("Assets:Fixed:Equipment", "fixed_asset")[0], "investing")
        self.assertEqual(classify("Equity:Owner-Draws", "equity")[0], "financing")
        self.assertEqual(classify("Liabilities:Other:Term-Loan", "other_liability")[0], "financing")

    def test_ambiguous_accounts_are_visible_not_guessed(self):
        self.assertEqual(classify("Liabilities:Other:Sales-Tax", "other_liability")[0], "needs_classification")
        self.assertEqual(classify("Assets:Uncategorized:Bank", "other_asset")[0], "needs_classification")
        self.assertEqual(classify("Equity:Retained-Earnings", "equity")[0], "needs_classification")


class ReconciliationTests(unittest.TestCase):
    def test_difference_is_reported_when_the_ledger_does_not_balance(self):
        # One-sided data: cash rose 100 but nothing else moved.
        r = build_cash_flow(date(2026, 1, 1), date(2026, 1, 31), D("0.00"),
                            {"Assets:Bank:Checking": D("0.00")}, {"Assets:Bank:Checking": D("100.00")},
                            {"Assets:Bank:Checking": "bank"})
        self.assertFalse(r.reconciliation["ties"])
        self.assertEqual(r.reconciliation["difference"], "-100.00")
        self.assertIn("DIFFERENCE: -100.00", r.to_text())

    def test_command_fails_with_the_difference(self):
        with tempfile.TemporaryDirectory() as tmp:
            entity = cash_book_entity(Path(tmp))
            bad = build_cash_flow(date(2026, 1, 1), date(2026, 1, 31), D("0.00"),
                                  {"Assets:Bank:Checking": D("0.00")}, {"Assets:Bank:Checking": D("100.00")},
                                  {"Assets:Bank:Checking": "bank"})
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("bookkeeping.reports.cashflow_statement.cash_flow", return_value=bad), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["report", "cashflow", "--entity", str(entity), "--from", "2026-01-01",
                             "--to", "2026-01-31", "--format", "json"])
        self.assertEqual(code, 1)
        self.assertIn("Difference: -100.00", err.getvalue())
        self.assertFalse(json.loads(out.getvalue())["reconciliation"]["ties"])


class CliTests(unittest.TestCase):
    def test_cli_json_and_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            entity = cash_book_entity(Path(tmp))
            for fmt in ("json", "text"):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = main(["report", "cashflow", "--entity", str(entity), "--from", "2026-01-01",
                                 "--to", "2026-01-31", "--format", fmt])
                self.assertEqual(code, 0)
                self.assertIn("15000.00" if fmt == "json" else "Statement of Cash Flows", out.getvalue())

    def test_start_after_end_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            entity = cash_book_entity(Path(tmp))
            with self.assertRaises(ValueError):
                cash_flow(entity, date(2026, 2, 1), date(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
