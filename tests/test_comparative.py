"""Comparative periods: --compare previous|prior_year."""
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
from bookkeeping.reports.comparative import (
    add_months, compare_balance_sheet, compare_cashflow, compare_pnl, pct, prior_date, prior_range,
)
from test_statement_structure import make_entity

D = Decimal

LEDGER = """\
option "operating_currency" "USD"

2025-01-01 open Assets:Bank:Checking USD
2025-01-01 open Income:Services USD
2025-01-01 open Expenses:Cost-of-Sales:Hosting USD
2025-01-01 open Expenses:Software USD
2025-01-01 open Expenses:Marketing USD
2025-01-01 open Expenses:Office USD
2025-01-01 open Equity:Owner-Contributions USD
2025-01-01 open Liabilities:CreditCard USD

2025-01-01 * "Start"
  Assets:Bank:Checking         5000.00 USD
  Equity:Owner-Contributions  -5000.00 USD

2025-01-10 * "Client"
  Assets:Bank:Checking         1000.00 USD
  Income:Services             -1000.00 USD

2025-01-11 * "Host"
  Expenses:Cost-of-Sales:Hosting  300.00 USD
  Assets:Bank:Checking           -300.00 USD

2025-01-12 * "Tool"
  Expenses:Software             100.00 USD
  Assets:Bank:Checking         -100.00 USD

2025-01-13 * "Ads"
  Expenses:Marketing             70.00 USD
  Assets:Bank:Checking          -70.00 USD

2026-01-10 * "Client"
  Assets:Bank:Checking         2000.00 USD
  Income:Services             -2000.00 USD

2026-01-11 * "Host"
  Expenses:Cost-of-Sales:Hosting  500.00 USD
  Assets:Bank:Checking           -500.00 USD

2026-01-12 * "Tool"
  Expenses:Software             100.00 USD
  Assets:Bank:Checking         -100.00 USD

2026-01-13 * "Paper"
  Expenses:Office                50.00 USD
  Liabilities:CreditCard        -50.00 USD
"""


def line(result, key):
    return next(l for l in result.lines if l["key"] == key)


class DateRuleTests(unittest.TestCase):
    def test_whole_month_goes_to_previous_month(self):
        self.assertEqual(prior_range(date(2026, 3, 1), date(2026, 3, 31), "previous"),
                         (date(2026, 2, 1), date(2026, 2, 28)))

    def test_whole_quarter_and_year(self):
        self.assertEqual(prior_range(date(2026, 4, 1), date(2026, 6, 30), "previous"),
                         (date(2026, 1, 1), date(2026, 3, 31)))
        self.assertEqual(prior_range(date(2026, 1, 1), date(2026, 12, 31), "previous"),
                         (date(2025, 1, 1), date(2025, 12, 31)))

    def test_other_range_shifts_by_its_length(self):
        self.assertEqual(prior_range(date(2026, 3, 10), date(2026, 3, 19), "previous"),
                         (date(2026, 2, 28), date(2026, 3, 9)))

    def test_prior_year_keeps_month_end(self):
        self.assertEqual(prior_range(date(2028, 2, 1), date(2028, 2, 29), "prior_year"),
                         (date(2027, 2, 1), date(2027, 2, 28)))
        self.assertEqual(prior_range(date(2026, 3, 5), date(2026, 3, 20), "prior_year"),
                         (date(2025, 3, 5), date(2025, 3, 20)))

    def test_balance_sheet_dates(self):
        self.assertEqual(prior_date(date(2026, 3, 31), "previous"), date(2026, 2, 28))
        self.assertEqual(prior_date(date(2026, 3, 15), "previous"), date(2026, 2, 15))
        self.assertEqual(prior_date(date(2026, 1, 31), "previous"), date(2025, 12, 31))
        self.assertEqual(prior_date(date(2026, 6, 30), "prior_year"), date(2025, 6, 30))
        self.assertEqual(add_months(date(2026, 5, 31), -1), date(2026, 4, 30))

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            prior_range(date(2026, 1, 1), date(2026, 1, 31), "nope")


class PercentTests(unittest.TestCase):
    def test_rounding_rule(self):
        self.assertEqual(pct(D("1"), D("3")), D("33.33"))
        self.assertEqual(pct(D("2"), D("3")), D("66.67"))
        self.assertEqual(pct(D("1"), D("800")), D("0.13"))      # 0.125 rounds up
        self.assertEqual(pct(D("-1"), D("800")), D("-0.13"))    # half away from zero
        self.assertIsNone(pct(D("5"), D("0")))


class ComparativeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = make_entity(Path(self._tmp.name), LEDGER)

    def tearDown(self):
        self._tmp.cleanup()


class PnlTests(ComparativeBase):
    def test_prior_year(self):
        r = compare_pnl(self.entity, date(2026, 1, 1), date(2026, 1, 31), "prior_year")
        self.assertTrue(r.prior_info["has_data"])
        self.assertEqual((r.prior_info["from_date"], r.prior_info["to_date"]), ("2025-01-01", "2025-01-31"))
        revenue = line(r, "total_revenue")
        self.assertEqual((revenue["amount"], revenue["prior_amount"], revenue["change"], revenue["change_pct"]),
                         ("2000.00", "1000.00", "1000.00", "100.00"))
        gp = line(r, "gross_profit")
        self.assertEqual((gp["amount"], gp["prior_amount"], gp["change"], gp["change_pct"]),
                         ("1500.00", "700.00", "800.00", "114.29"))
        self.assertEqual((gp["pct_of_income"], gp["prior_pct_of_income"]), ("75.00", "70.00"))
        self.assertEqual(line(r, "total_revenue")["pct_of_income"], "100.00")
        net = line(r, "net_income")
        self.assertEqual((net["amount"], net["prior_amount"], net["change"]), ("1350.00", "530.00", "820.00"))
        self.assertEqual(r.prior_totals["net_income"], "530.00")

    def test_prior_only_and_current_only_accounts_are_lines(self):
        r = compare_pnl(self.entity, date(2026, 1, 1), date(2026, 1, 31), "prior_year")
        marketing = line(r, "Expenses:Marketing")
        self.assertEqual((marketing["amount"], marketing["prior_amount"], marketing["change"]),
                         ("0.00", "70.00", "-70.00"))
        office = line(r, "Expenses:Office")
        self.assertEqual((office["amount"], office["prior_amount"], office["change_pct"]), ("50.00", "0.00", None))
        keys = [l["key"] for l in r.lines]
        self.assertLess(keys.index("Expenses:Marketing"), keys.index("total_expenses"))
        self.assertGreater(keys.index("Expenses:Marketing"), keys.index("gross_profit"))

    def test_prior_only_account_keeps_account_order(self):
        r = compare_pnl(self.entity, date(2026, 1, 1), date(2026, 1, 31), "prior_year")
        expense_keys = [l["key"] for l in r.lines if l.get("section") == "expenses" and l["type"] == "account"]
        self.assertEqual(expense_keys, sorted(expense_keys))
        self.assertEqual(expense_keys, ["Expenses:Marketing", "Expenses:Office", "Expenses:Software"])

    def test_prior_total_equals_sum_of_prior_lines(self):
        r = compare_pnl(self.entity, date(2026, 1, 1), date(2026, 1, 31), "prior_year")
        total = sum((D(l["prior_amount"]) for l in r.lines if l.get("section") == "expenses" and l["type"] == "account"),
                    D("0.00"))
        self.assertEqual(str(total), line(r, "total_expenses")["prior_amount"])

    def test_gross_profit_when_only_the_prior_period_has_cost_of_sales(self):
        ledger = LEDGER.replace("2026-01-11 * \"Host\"\n  Expenses:Cost-of-Sales:Hosting  500.00 USD\n  Assets:Bank:Checking           -500.00 USD",
                                "2026-01-11 * \"Host\"\n  Expenses:Software  500.00 USD\n  Assets:Bank:Checking -500.00 USD")
        with tempfile.TemporaryDirectory() as tmp:
            entity = make_entity(Path(tmp), ledger)
            r = compare_pnl(entity, date(2026, 1, 1), date(2026, 1, 31), "prior_year")
        gp = line(r, "gross_profit")
        self.assertEqual((gp["amount"], gp["prior_amount"]), ("2000.00", "700.00"))

    def test_no_prior_data_is_stated_and_nulls_everywhere(self):
        r = compare_pnl(self.entity, date(2025, 1, 1), date(2025, 1, 31), "previous")
        self.assertFalse(r.prior_info["has_data"])
        self.assertIn("no entries", r.prior_info["note"])
        self.assertIsNone(r.prior_totals)
        for l in r.lines:
            if l["type"] != "section":
                self.assertIsNone(l["prior_amount"])
                self.assertIsNone(l["change"])
                self.assertIsNone(l["change_pct"])
        self.assertEqual(line(r, "total_revenue")["amount"], "1000.00")
        self.assertIn("No prior period data", r.to_text())

    def test_zero_revenue_gives_null_pct_of_income(self):
        r = compare_pnl(self.entity, date(2026, 2, 1), date(2026, 2, 28), "previous")
        self.assertIsNone(line(r, "net_income")["pct_of_income"])

    def test_json_shape(self):
        payload = json.loads(compare_pnl(self.entity, date(2026, 1, 1), date(2026, 1, 31), "prior_year").to_json())
        self.assertEqual(payload["kind"], "pnl")
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["comparison"]["mode"], "prior_year")
        self.assertTrue(payload["comparison"]["prior"]["has_data"])
        self.assertIn("ROUND_HALF_UP", payload["comparison"]["rounding"])
        self.assertEqual(payload["totals"]["net_income"], "1350.00")
        self.assertEqual(payload["prior_totals"]["net_income"], "530.00")
        self.assertIn("sections", payload)


class BalanceSheetTests(ComparativeBase):
    def test_previous_month_end(self):
        r = compare_balance_sheet(self.entity, date(2026, 1, 31), "prior_year")
        self.assertEqual(r.prior_info["to_date"], "2025-01-31")
        bank = line(r, "Assets:Bank:Checking")
        # 2025-01-31: 5000 + 1000 - 300 - 100 - 70 = 5530. 2026-01-31: 5530 + 2000 - 500 - 100 = 6930.
        self.assertEqual((bank["amount"], bank["prior_amount"], bank["change"]), ("6930.00", "5530.00", "1400.00"))
        total = line(r, "total_liabilities_and_equity")
        self.assertEqual(total["amount"], line(r, "total_assets")["amount"])
        self.assertEqual(total["prior_amount"], line(r, "total_assets")["prior_amount"])
        self.assertNotIn("pct_of_income", bank)

    def test_no_prior_data(self):
        r = compare_balance_sheet(self.entity, date(2025, 1, 31), "prior_year")
        self.assertFalse(r.prior_info["has_data"])
        self.assertIsNone(line(r, "total_assets")["prior_amount"])


class CashflowTests(ComparativeBase):
    def test_prior_year(self):
        r = compare_cashflow(self.entity, date(2026, 1, 1), date(2026, 1, 31), "prior_year")
        self.assertTrue(r.ties)
        net = line(r, "net_change_in_cash")
        # 2026: 2000 - 500 - 100 = 1400. 2025: 5000 + 1000 - 300 - 100 - 70 = 5530.
        self.assertEqual((net["amount"], net["prior_amount"], net["change"]), ("1400.00", "5530.00", "-4130.00"))
        self.assertEqual(line(r, "beginning_cash")["amount"], "5530.00")
        self.assertEqual(line(r, "beginning_cash")["prior_amount"], "0.00")

    def test_no_prior_data(self):
        r = compare_cashflow(self.entity, date(2025, 1, 1), date(2025, 1, 31), "previous")
        self.assertFalse(r.prior_info["has_data"])
        self.assertTrue(r.ties)


class CliTests(ComparativeBase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_commands_accept_compare(self):
        base = ["--entity", str(self.entity), "--format", "json", "--compare", "prior_year"]
        for rc, extra in (("pnl", ["--from", "2026-01-01", "--to", "2026-01-31"]),
                          ("balance-sheet", ["--as-of", "2026-01-31"]),
                          ("cashflow", ["--from", "2026-01-01", "--to", "2026-01-31"])):
            code, out = self.run_cli("report", rc, *extra, *base)
            self.assertEqual(code, 0, rc)
            self.assertEqual(json.loads(out)["comparison"]["mode"], "prior_year")

    def test_text_comparison(self):
        code, out = self.run_cli("report", "pnl", "--entity", str(self.entity), "--from", "2026-01-01",
                                 "--to", "2026-01-31", "--compare", "previous")
        self.assertEqual(code, 0)
        self.assertIn("compared with", out)
        self.assertIn("% income", out)

    def test_without_compare_nothing_changes(self):
        code, out = self.run_cli("report", "pnl", "--entity", str(self.entity), "--from", "2026-01-01",
                                 "--to", "2026-01-31", "--format", "json")
        payload = json.loads(out)
        self.assertNotIn("comparison", payload)
        self.assertNotIn("structure", payload)

    def test_invalid_compare_value_is_rejected(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("report", "pnl", "--entity", str(self.entity), "--from", "2026-01-01",
                         "--to", "2026-01-31", "--compare", "quarter")


if __name__ == "__main__":
    unittest.main()
