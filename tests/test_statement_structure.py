"""Structured statement output: section, subtotal and total rows (schema version 2)."""
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
from bookkeeping.cli import main
from bookkeeping.reports.cache import regenerate
from bookkeeping.reports.statements import balance_sheet, profit_and_loss
from bookkeeping.reports.structure import build_structure, is_cost_of_sales

LEDGER = """\
option "title" "Test Entity"
option "operating_currency" "USD"

2026-01-01 open Assets:Bank:Checking USD
2026-01-01 open Income:Services USD
2026-01-01 open Expenses:Cost-of-Sales:Hosting USD
2026-01-01 open Expenses:Software USD
2026-01-01 open Equity:Owner-Contributions USD
2026-01-01 open Liabilities:CreditCard USD

2026-01-01 * "Owner puts money in"
  Assets:Bank:Checking         1000.00 USD
  Equity:Owner-Contributions  -1000.00 USD

2026-01-10 * "Client" "Work"
  Assets:Bank:Checking         5000.00 USD
  Income:Services             -5000.00 USD

2026-01-12 * "Host" "Servers"
  Expenses:Cost-of-Sales:Hosting  800.00 USD
  Assets:Bank:Checking           -800.00 USD

2026-01-20 * "Tool" "Software"
  Expenses:Software             200.00 USD
  Liabilities:CreditCard       -200.00 USD
"""


def make_entity(root: Path, ledger: str = LEDGER) -> Path:
    entity = root / "entity"
    entity.mkdir()
    (entity / "entity.json").write_text('{"name": "Test Entity"}')
    (entity / "reports").mkdir()
    (entity / "staging").mkdir()
    (entity / "books.beancount").write_text(ledger, encoding="utf-8")
    regenerate(entity)
    return entity


def by_key(lines):
    return {line["key"]: line for line in lines}


class StructureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = make_entity(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_pnl_has_gross_profit_when_cost_of_sales_exists(self):
        result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 1, 31))
        structure = build_structure(result)
        lines = by_key(structure["lines"])
        self.assertEqual(structure["version"], 2)
        self.assertEqual(lines["total_revenue"]["amount"], "5000.00")
        self.assertEqual(lines["total_cost_of_sales"]["amount"], "800.00")
        self.assertEqual(lines["gross_profit"]["amount"], "4200.00")
        self.assertEqual(lines["total_expenses"]["amount"], "200.00")
        self.assertEqual(lines["net_income"]["amount"], "4000.00")
        self.assertEqual(lines["net_income"]["type"], "total")
        self.assertEqual(lines["gross_profit"]["type"], "subtotal")
        keys = [l["key"] for l in structure["lines"] if l["type"] in {"subtotal", "total"}]
        self.assertEqual(keys, ["total_revenue", "total_cost_of_sales", "gross_profit", "total_expenses", "net_income"])
        # Gross profit minus the remaining expenses is the engine's net income.
        self.assertEqual(Decimal(lines["gross_profit"]["amount"]) - Decimal(lines["total_expenses"]["amount"]),
                         result.totals["net_income"])

    def test_pnl_account_rows_carry_ledger_account_and_section(self):
        structure = build_structure(profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 1, 31)))
        hosting = next(l for l in structure["lines"] if l.get("account") == "Expenses:Cost-of-Sales:Hosting")
        self.assertEqual(hosting["section"], "cost_of_sales")
        self.assertEqual(hosting["amount"], "800.00")
        software = next(l for l in structure["lines"] if l.get("account") == "Expenses:Software")
        self.assertEqual(software["section"], "expenses")

    def test_pnl_without_cost_of_sales_has_no_gross_profit(self):
        ledger = LEDGER.replace("Expenses:Cost-of-Sales:Hosting", "Expenses:Hosting")
        with tempfile.TemporaryDirectory() as tmp:
            entity = make_entity(Path(tmp), ledger)
            lines = by_key(build_structure(profit_and_loss(entity, date(2026, 1, 1), date(2026, 1, 31)))["lines"])
        self.assertNotIn("gross_profit", lines)
        self.assertNotIn("total_cost_of_sales", lines)
        self.assertEqual(lines["total_expenses"]["amount"], "1000.00")
        self.assertEqual(lines["net_income"]["amount"], "4000.00")

    def test_balance_sheet_totals(self):
        result = balance_sheet(self.entity, date(2026, 1, 31))
        structure = build_structure(result)
        lines = by_key(structure["lines"])
        self.assertEqual(lines["total_assets"]["amount"], "5200.00")
        self.assertEqual(lines["total_liabilities"]["amount"], "200.00")
        self.assertEqual(lines["total_equity"]["amount"], "5000.00")
        self.assertEqual(lines["total_liabilities_and_equity"]["amount"], "5200.00")
        self.assertEqual(lines["total_liabilities_and_equity"]["type"], "total")
        self.assertEqual(lines["net_income_current_period"]["type"], "computed")
        self.assertTrue(structure["balanced"])
        self.assertEqual(structure["difference"], "0.00")

    def test_legacy_fields_unchanged(self):
        """The structure is added next to the legacy fields; it replaces none of them."""
        result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 1, 31))
        from bookkeeping.reports.structure import structured_payload
        payload = structured_payload(result)
        legacy = json.loads(result.to_json())
        for key, value in legacy.items():
            self.assertEqual(payload[key], value)
        self.assertEqual(payload["schema_version"], 2)

    def test_cost_of_sales_detection(self):
        self.assertTrue(is_cost_of_sales("Expenses:COGS"))
        self.assertTrue(is_cost_of_sales("Expenses:Cost-of-Goods-Sold:Materials"))
        self.assertFalse(is_cost_of_sales("Expenses:Operating-Costs"))
        self.assertFalse(is_cost_of_sales("Income:COGS"))


class StructureCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = make_entity(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_default_json_has_no_structure(self):
        code, out = self.run_cli("report", "pnl", "--entity", str(self.entity), "--from", "2026-01-01",
                                 "--to", "2026-01-31", "--format", "json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertNotIn("structure", payload)
        self.assertNotIn("schema_version", payload)

    def test_structure_flag_adds_structure(self):
        code, out = self.run_cli("report", "pnl", "--entity", str(self.entity), "--from", "2026-01-01",
                                 "--to", "2026-01-31", "--format", "json", "--structure")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["schema_version"], 2)
        self.assertIn("gross_profit", by_key(payload["structure"]["lines"]))

    def test_balance_sheet_structure_flag(self):
        code, out = self.run_cli("report", "balance-sheet", "--entity", str(self.entity), "--as-of", "2026-01-31",
                                 "--format", "json", "--structure")
        self.assertEqual(code, 0)
        self.assertIn("total_liabilities_and_equity", by_key(json.loads(out)["structure"]["lines"]))

    def test_text_output_ignores_structure(self):
        base = ["report", "pnl", "--entity", str(self.entity), "--from", "2026-01-01", "--to", "2026-01-31"]
        self.assertEqual(self.run_cli(*base), self.run_cli(*base, "--structure"))


if __name__ == "__main__":
    unittest.main()
