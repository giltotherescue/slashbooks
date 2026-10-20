"""Synthetic regressions for report views, canonical handoffs and cash-flow purpose."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import unittest

from bookkeeping import cli, taxmap
from bookkeeping.accrual import enable
from bookkeeping.accrual.reports import CoverageError
from bookkeeping.entity import add_account, load_entity
from bookkeeping.reports import statements, workbook
from bookkeeping.reports.cashflow_statement import cash_flow
from test_accrual_cash_view import CashViewFixture, AR, AP, SERVICES, EQUIPMENT, SOFTWARE, LOAN, BANK
import test_accrual_acceptance

D = Decimal
JAN = (date(2026, 1, 1), date(2026, 1, 31))


class ReportViewIntegrityTests(CashViewFixture):
    def cash_default(self):
        self.item("invoice", "receivable", AR, "1000", "2026-01-05", "Customer", counter=SERVICES)
        enable.apply_set_view(self.entity, "cash")

    def test_default_cash_and_explicit_accrual_use_distinct_economic_events(self):
        self.cash_default()
        for basis in (None, "cash"):
            report = statements.profit_and_loss(self.root, *JAN, basis=basis)
            self.assertEqual(report.totals["net_income"], D("0.00"))
            self.assertEqual(report.meta["basis"], "cash")
        accrual = statements.profit_and_loss(self.root, *JAN, basis="accrual")
        self.assertEqual(accrual.totals["net_income"], D("1000.00"))
        self.assertEqual(accrual.meta["basis"], "accrual")
        self.settle("receipt", "2026-02-05", "400", [("invoice", "400")])
        feb = statements.profit_and_loss(self.root, date(2026, 2, 1), date(2026, 2, 28))
        self.assertEqual(feb.totals["net_income"], D("400.00"))
        for compute in (statements.balance_sheet, statements.trial_balance):
            self.assertEqual(compute(self.root, JAN[1]).meta["basis"], "accrual")

    def test_ask_honors_cash_default_but_reads_recorded_support(self):
        self.cash_default()
        self.assertEqual(statements.ask(self.root, "What was January 2026 profit?").data["net_income"], "0.00")
        self.assertEqual(statements.ask(self.root, "What was January 2026 accrual profit?").data["net_income"], "1000.00")
        answer = statements.ask(self.root, "Who owes us money in January 2026?")
        self.assertEqual(answer.intent, "aging")
        self.assertIn("1,000.00", answer.answer_text)
        self.assertEqual(answer.data["basis"], "accrual")

    def test_structured_and_comparative_formats_preserve_selected_basis(self):
        self.cash_default()
        for basis, expected in (("cash", "0.00"), ("accrual", "1000.00")):
            for flags in ([], ["--structure"], ["--compare", "previous"], ["--compare", "prior_year"]):
                with self.subTest(basis=basis, flags=flags):
                    out, err = StringIO(), StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = cli.main(["report", "pnl", "--entity", str(self.root), "--from", "2026-01-01", "--to", "2026-01-31", "--basis", basis, "--format", "json", *flags])
                    self.assertEqual(code, 0, err.getvalue())
                    payload = json.loads(out.getvalue())
                    self.assertEqual(payload["totals"]["net_income"], expected)
                    self.assertEqual(payload["meta"]["basis"], basis)

    def test_accountant_package_keeps_canonical_basis_and_support(self):
        self.cash_default()
        package = workbook.generate_accountant_package(self.root, *JAN, output_dir=self.root / "handoff", override=True)
        self.assertFalse(package.error, package.error)
        files = {path.name: path for path in package.output_dir.rglob("*.csv")}
        self.assertTrue(any("Outstanding" in name for name in files), files)
        cover = (package.output_dir / "csv" / "Cover.csv").read_text()
        self.assertIn("Accrual basis", cover)
        pnl = (package.output_dir / "csv" / "P-and-L.csv").read_text()
        self.assertIn("1000.00", pnl)
        checks = workbook.run_sanity_checks(self.root, *JAN)
        self.assertIn("accrual_support", [check.check for check in checks.checks])

    def test_tax_basis_uses_recorded_accrual_even_with_cash_default(self):
        self.cash_default()
        handoff = taxmap.build_handoff(self.root, *JAN, form="1120s")
        self.assertEqual(handoff["accounting_basis"], "accrual")
        self.assertIn(["Basis", "Accrual basis book balances computed by the Slashbooks engine"], taxmap._readme_table(handoff, {}))
        income = next(row for row in handoff["trial_balance"] if row["account"] == SERVICES)
        self.assertEqual(income["balance"], D("-1000.00"))
        manifest = taxmap.export_handoff(self.root, *JAN, form="1120s", output_dir=self.root / "tax", formats=("csv",))
        self.assertEqual(manifest["accounting_basis"], "accrual")
        self.assertIn("Basis: Accrual basis", (self.root / "tax/README.txt").read_text())

    def test_cash_only_tax_handoff_keeps_cash_basis_label(self):
        from test_taxmap import make_company
        parent = self.root / "cash-tax"
        parent.mkdir()
        root = make_company(parent)
        handoff = taxmap.build_handoff(root, *JAN)
        self.assertEqual(handoff["accounting_basis"], "cash")
        self.assertIn(["Basis", "Cash basis book balances computed by the Slashbooks engine"], taxmap._readme_table(handoff, {}))


class TaxCoverageIntegrityTests(unittest.TestCase):
    def setUp(self):
        fixture = test_accrual_acceptance.AccrualAcceptanceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.opening()
        self.fixture = fixture

    def test_unsupported_historical_windows_fail_before_any_artifact(self):
        for start, end in ((date(2026, 4, 1), date(2026, 6, 30)), (date(2026, 1, 1), date(2026, 5, 31))):
            with self.subTest(start=start):
                output = self.fixture.root / ("tax-" + start.isoformat())
                with self.assertRaises(CoverageError):
                    taxmap.export_handoff(self.fixture.root, start, end, form="1120s", output_dir=output)
                self.assertFalse(output.exists())
                with self.assertRaises(CoverageError):
                    taxmap.mapping_report(self.fixture.root, from_date=start, to_date=end, form="1120s")

    def test_supported_full_summary_exposes_historical_limit(self):
        handoff = taxmap.build_handoff(self.fixture.root, date(2026, 1, 1), date(2026, 7, 31), form="1120s")
        self.assertTrue(handoff["accounting"]["summary_included"])
        self.assertFalse(handoff["accounting"]["historical_detail_available"])
        self.assertEqual(handoff["accounting"]["coverage"]["source_as_of"], "2026-06-30")


class NoncashCashflowIntegrityTests(CashViewFixture):
    def check_totals(self, period, operating="0.00", investing="0.00", financing="0.00"):
        result = cash_flow(self.root, *period)
        self.assertEqual(result.totals["net_cash_operating"], D(operating))
        self.assertEqual(result.totals["net_cash_investing"], D(investing))
        self.assertEqual(result.totals["net_cash_financing"], D(financing))
        self.assertTrue(result.reconciliation["ties"], result.to_text())
        return result

    def test_unpaid_capital_bill_and_later_bank_payment(self):
        self.item("laptop", "payable", AP, "1200", "2026-01-05", "Supplier", counter=EQUIPMENT)
        jan = self.check_totals(JAN)
        self.assertEqual(jan.payload()["noncash_events"][0]["amount"], "1200.00")
        self.assertIn("Noncash investing and financing", jan.to_text())
        self.settle("payment", "2026-02-05", "-1200", [("laptop", "1200")])
        self.check_totals((date(2026, 2, 1), date(2026, 2, 28)), investing="-1200.00")
        self.check_totals((JAN[0], date(2026, 2, 28)), investing="-1200.00")

    def test_mixed_bill_partial_payments_split_capital_from_operating_to_the_cent(self):
        self.item("mixed", "payable", AP, "1000", "2026-01-05", "Supplier", lines=[(EQUIPMENT, "600"), (SOFTWARE, "400")])
        self.check_totals(JAN)
        for month, amount, cap, expense in ((2,"333.33","-199.99","-133.34"),(3,"333.33","-200.00","-133.33"),(4,"333.34","-200.01","-133.33")):
            day=f"2026-{month:02}-05"
            self.settle("p"+str(month), day, "-"+amount, [("mixed",amount)])
            self.check_totals((date(2026,month,1),date(2026,month,28)), operating=expense, investing=cap)
        self.check_totals((JAN[0],date(2026,4,30)), operating="-400.00", investing="-600.00")

    def test_financed_and_owner_contributed_assets_have_no_cash_activity(self):
        owner = "Equity:Owner-Contributions"
        add_account(self.root, owner, open_date="2026-01-01")
        self.entity = load_entity(self.root)
        self.journal("loan-equipment", "2026-01-05", [(EQUIPMENT,"1200"),(LOAN,"-1200")])
        self.journal("owner-equipment", "2026-01-06", [(EQUIPMENT,"800"),(owner,"-800")])
        result = self.check_totals(JAN)
        self.assertEqual(sum((D(row["amount"]) for row in result.meta["noncash_events"]),D(0)),D("2000"))

    def test_cashflow_uses_canonical_income_even_when_default_pnl_is_cash(self):
        self.item("invoice", "receivable", AR, "1000", "2026-01-05", "Customer", counter=SERVICES)
        enable.apply_set_view(self.entity,"cash")
        result = self.check_totals(JAN)
        self.assertEqual(result.totals["net_income"],D("1000.00"))
        self.assertEqual(result.meta["basis"],"accrual")

    def test_cash_downpayment_does_not_invent_loan_proceeds(self):
        self.journal("financed-purchase", "2026-01-05", [(EQUIPMENT,"1000"),(BANK,"-200"),(LOAN,"-800")])
        self.check_totals(JAN, investing="-200.00")

    def test_depreciation_addback_survives_non_cash_capital_adjustments(self):
        from test_accrual_cash_view import ACCUM, DEPRECIATION
        self.journal("contributed", "2026-01-05", [(EQUIPMENT,"1200"),(LOAN,"-1200")])
        self.journal("depreciation", "2026-01-31", [(DEPRECIATION,"100"),(ACCUM,"-100")])
        result = self.check_totals(JAN)
        self.assertEqual(result.totals["net_income"], D("-100.00"))
        self.assertEqual(next(row["amount"] for row in result.lines if row.get("account") == ACCUM), "100.00")

    def test_ambiguous_mixed_cash_purchase_exposes_only_actual_cash_for_classification(self):
        self.journal("mixed-purchase", "2026-01-05", [(EQUIPMENT,"600"),(SOFTWARE,"400"),(BANK,"-200"),(AP,"-800")])
        result = self.check_totals(JAN)
        self.assertEqual(result.totals["needs_classification"], D("-200.00"))
        self.assertEqual(result.totals["net_change_in_cash"], D("-200.00"))
        self.assertIn("cash_flow_mixed_purchase_allocation", [row["code"] for row in result.meta["unresolved_issues"]])

    def test_credit_on_mixed_capital_bill_does_not_reuse_original_payment_ratio(self):
        self.item("adjusted-mixed", "payable", AP, "1000", "2026-01-05", "Supplier", lines=[(EQUIPMENT,"600"),(SOFTWARE,"400")])
        self.adjust("credit-software", "adjusted-mixed", "credit", "400", "2026-01-10", SOFTWARE)
        self.settle("pay-capital", "2026-02-05", "-600", [("adjusted-mixed","600")])
        result=self.check_totals((date(2026,2,1),date(2026,2,28)))
        self.assertEqual(result.totals["needs_classification"],D("-600.00"))
        self.assertIn("cash_flow_adjusted_bill_allocation",[issue["code"] for issue in result.meta["unresolved_issues"]])


class OptionalTrackingPackageIntegrityTests(unittest.TestCase):
    def test_empty_optional_tracking_keeps_package_basis_consistent(self):
        import test_accrual_enable
        from bookkeeping.reports.cache import open_cache
        fixture=test_accrual_enable.Fixture(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.enable()
        as_of=date(2026,4,30)
        conn=open_cache(fixture.root)
        try:
            self.assertEqual(statements._recorded_basis(conn,as_of),"cash")
        finally: conn.close()
        self.assertEqual(statements._recorded_basis(load_entity(fixture.root),as_of),"cash")
        package=workbook.generate_accountant_package(fixture.root,date(2026,4,1),as_of,output_dir=fixture.work/"package",override=True)
        self.assertFalse(package.error,package.error)
        for name in ("Cover.csv","Summary.csv"):
            self.assertIn("Accounting basis,Cash basis",(package.output_dir/"csv"/name).read_text())
        self.assertFalse((package.output_dir/"csv/Outstanding-Balances.csv").exists())
        self.assertNotIn("accrual_support",[row.check for row in workbook.run_sanity_checks(fixture.root,date(2026,4,1),as_of).checks])
