"""Opening balances are a starting point for the cash flow statement, never a cash flow."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bookkeeping.accrual.reports import CoverageError
from bookkeeping.reports.cashflow_statement import classify, cash_flow
from bookkeeping.reports.comparative import compare_cashflow
from tests import test_accrual_reports as accrual_fixture

D = Decimal
QB_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "quickbooks"


class AccrualCashFlowTests(unittest.TestCase):
    """Reuses the accrual fixture: opening as of 2026-06-30, cutover 2026-07-01."""

    _base = accrual_fixture.AccrualReportsTests
    change = accrual_fixture.fixtures.AccrualMigrationTests.change
    preview = accrual_fixture.fixtures.AccrualMigrationTests.preview
    opening = _base.opening
    evidence_for = _base.evidence_for
    journal = _base.journal
    settle = _base.settle
    july = _base.july

    def setUp(self):
        accrual_fixture.fixtures.AccrualMigrationTests.setUp(self)
        self.opening()
        self.july()

    def test_ytd_inside_the_fiscal_summary_is_refused(self):
        with self.assertRaises(CoverageError) as caught:
            cash_flow(self.root, date(2026, 1, 1), date(2026, 7, 31))
        self.assertEqual(caught.exception.first_date, "2026-07-01")
        self.assertEqual(caught.exception.field, "from")

    def test_post_cutover_period_starts_from_the_opening_balances(self):
        r = cash_flow(self.root, date(2026, 7, 1), date(2026, 7, 31))
        lines = {l["key"]: l for l in r.lines}
        self.assertEqual(r.totals["beginning_cash"], D("10000.00"))
        # The opening equipment, retained earnings and 400 of opening depreciation are balances, not flows.
        self.assertNotIn("Assets:Fixed:Equipment", lines)
        self.assertEqual(r.totals["net_cash_investing"], D("0.00"))
        self.assertEqual(r.totals["net_cash_financing"], D("0.00"))
        self.assertEqual(lines["Assets:Fixed:Accumulated-Depreciation"]["amount"], "40.00")
        self.assertEqual(r.totals["net_change_in_cash"], D("-400.00"))
        self.assertEqual(r.totals["ending_cash"], D("9600.00"))
        self.assertTrue(r.reconciliation["ties"])

    def test_second_half_previous_comparison_marks_the_summary_prior_as_no_data(self):
        r = compare_cashflow(self.root, date(2026, 7, 1), date(2026, 12, 31), "previous")
        self.assertFalse(r.prior_info["has_data"])
        self.assertIn("coverage", r.prior_info["note"])
        net = next(l for l in r.lines if l["key"] == "net_change_in_cash")
        self.assertIsNone(net["prior_amount"])

    def test_period_that_ends_before_the_opening_is_refused_by_coverage(self):
        with self.assertRaises(CoverageError):
            cash_flow(self.root, date(2026, 6, 30), date(2026, 7, 31))


class QuickBooksOpeningTests(unittest.TestCase):
    def setUp(self):
        from src.bookkeeping.quickbooks import _reset_collision_registry, import_opening
        self.reset = _reset_collision_registry
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        qb = tmp / "qb"
        qb.mkdir()
        shutil.copy(QB_FIXTURES / "chart_of_accounts.csv", qb / "chart_of_accounts.csv")
        shutil.copy(QB_FIXTURES / "trial_balance_cash_basis.csv", qb / "trial_balance_cash_basis.csv")
        from src.bookkeeping.entity import init_entity, load_entity
        init_entity(tmp / "entity", name="Acme Consulting LLC", business_type="consulting")
        self.entity = load_entity(tmp / "entity")
        result = import_opening(self.entity, qb, date(2026, 1, 1))
        self.assertTrue(result.success, result.errors)
        self.path = Path(self.entity.path)

    def tearDown(self):
        self.reset()
        self._tmp.cleanup()

    def test_opening_on_the_cutover_date_is_the_beginning_balance(self):
        r = cash_flow(self.path, date(2026, 1, 1), date(2026, 1, 31))
        self.assertGreater(r.totals["beginning_cash"], D("0.00"))
        self.assertEqual(r.totals["net_change_in_cash"], D("0.00"))
        self.assertEqual(r.totals["ending_cash"], r.totals["beginning_cash"])
        self.assertTrue(all("pening" not in l["key"] for l in r.lines))
        self.assertEqual(r.totals["net_cash_financing"], D("0.00"))
        self.assertTrue(r.reconciliation["ties"])

    def test_period_that_contains_a_later_opening_is_refused(self):
        with self.assertRaises(CoverageError) as caught:
            cash_flow(self.path, date(2025, 12, 1), date(2026, 1, 31))
        self.assertEqual(caught.exception.first_date, "2026-01-01")


class OpeningEquityClassificationTests(unittest.TestCase):
    def test_opening_balance_equity_is_never_financing(self):
        for name in ("Equity:Opening-Balances", "Equity:OpeningBalance"):
            self.assertEqual(classify(name, "equity")[0], "needs_classification")


if __name__ == "__main__":
    unittest.main()
