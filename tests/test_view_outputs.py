"""Every report output can be produced on either view, and the default comes from the default view.

The proof company is the golden cash company with accrual turned on at 2026-04-01, opening items for
an invoice (900.00) and a bill (250.00), and one new unpaid bill of 400.00 dated 2026-04-15.
Hand-computed expectations:

* April, accrual view: expenses 400.00 (the new bill), revenue 0.00. The opening items carry no income or expense.
* April, cash view: the bill is unpaid, so expenses 0.00.
* January to March: both views equal the original cash books, because no accrual record exists before the start.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_accrual_enable import AP, D, Fixture, books  # noqa: E402

from bookkeeping.accrual import items, journals  # noqa: E402
from bookkeeping.accrual.reports import CoverageError  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.reports import statements  # noqa: E402


class TrackedCompany(Fixture):
    """Cash company, accrual turned on at D, default view as chosen by the test."""

    def make(self, default_view: str = "cash", records: bool = True) -> None:
        self.enable()
        if default_view == "accrual":
            self.set_view("accrual")
        if records:
            self.apply_opening(self.OPEN_ITEMS)
            entity = load_entity(self.root)
            evidence = journals.intake_evidence(entity, {"schema_version": 1, "source": {
                "namespace": "example.synthetic", "account_id": "co", "object_id": "bill-2", "version": "1"},
                "currency": "USD", "observed_facts": {"document": "bill-2"}})["id"]
            request = {"operation_id": "create-bill-2", "id": "bill-2", "kind": "payable", "account": AP, "currency": "USD",
                       "original_amount": "400.00", "as_of": "2026-04-15", "counterparty": "Granite Insurance",
                       "origin": {"mode": "new", "source_id": "bill-2-src", "counter_account": "Expenses:Insurance"},
                       "evidence_ids": [evidence]}
            items.apply_item(entity, items.preview_item(entity, request))

    def totals(self, result) -> tuple:
        return tuple(section["total"] for section in result.sections)


class ProfitAndLossViews(TrackedCompany):
    def test_cash_view_with_records_is_the_derived_cash_view(self) -> None:
        self.make("cash")
        explicit = statements.profit_and_loss(self.root, date(2026, 4, 1), date(2026, 4, 30), basis="cash")
        self.assertEqual(explicit.meta["view"], "cash")
        self.assertEqual(self.totals(explicit), (Decimal("0.00"), Decimal("0.00")))
        # The command line names the default view, so a person gets the same derived cash view without a flag.
        code, out, err = books("report", "pnl", "--from", "2026-04-01", "--to", "2026-04-30", "--format", "json", "--entity", str(self.root))
        self.assertEqual(code, 0, err)
        self.assertEqual(out["meta"]["view"], "cash")
        self.assertEqual(out["totals"]["net_income"], "0.00")
        # The library call with no view is the books as recorded: the engine's own readers (the cash flow statement) need that.
        self.assertEqual(self.totals(statements.profit_and_loss(self.root, date(2026, 4, 1), date(2026, 4, 30))), (Decimal("0.00"), Decimal("400.00")))

    def test_accrual_view_on_a_cash_default_company(self) -> None:
        self.make("cash")
        april = statements.profit_and_loss(self.root, date(2026, 4, 1), date(2026, 4, 30), basis="accrual")
        self.assertEqual(self.totals(april), (Decimal("0.00"), Decimal("400.00")))
        self.assertEqual((april.meta["basis"], april.meta["view"]), ("accrual", "accrual"))
        self.assertIn("Accounting basis: accrual", april.to_text())
        self.assertEqual(april.sections[0]["label"], "Revenue")

    def test_accrual_view_before_the_start_names_the_start(self) -> None:
        self.make("cash")
        with self.assertRaises(CoverageError) as caught:
            statements.profit_and_loss(self.root, date(2026, 1, 1), date(2026, 3, 31), basis="accrual")
        self.assertIn(D, str(caught.exception))

    def test_default_accrual_company_gets_the_cash_view_on_request(self) -> None:
        self.make("accrual")
        april = statements.profit_and_loss(self.root, date(2026, 4, 1), date(2026, 4, 30))
        self.assertEqual(self.totals(april), (Decimal("0.00"), Decimal("400.00")))
        cash = statements.profit_and_loss(self.root, date(2026, 4, 1), date(2026, 4, 30), basis="cash")
        self.assertEqual(self.totals(cash), (Decimal("0.00"), Decimal("0.00")))
        self.assertEqual(cash.meta["view"], "cash")

    def test_cash_books_with_no_records_have_one_view(self) -> None:
        self.make("cash", records=False)
        plain = statements.profit_and_loss(self.root, date(2026, 1, 1), date(2026, 3, 31))
        self.assertNotIn("view", plain.meta)
        self.assertEqual(statements.profit_and_loss(self.root, date(2026, 1, 1), date(2026, 3, 31), basis="cash").to_json(), plain.to_json())

    def test_cash_books_that_never_enabled_accrual_refuse_the_accrual_view(self) -> None:
        with self.assertRaises(ValueError) as caught:
            statements.profit_and_loss(self.root, date(2026, 1, 1), date(2026, 3, 31), basis="accrual")
        self.assertIn("Cash books keep no data for an accrual view", str(caught.exception))


class ProofCashCompanyThenABill(TrackedCompany):
    """Cash company, accrual turned on, then one unpaid bill: the cash view does not move, the accrual view shows the bill."""

    APRIL = (date(2026, 4, 1), date(2026, 4, 30))

    def rows(self, result) -> dict:
        found = {row["label"]: row["amount"] for section in result.sections for row in section["rows"]}
        found["totals"] = self.totals(result)
        return found

    def add_bill(self) -> None:
        self.apply_opening(self.OPEN_ITEMS)
        entity = load_entity(self.root)
        evidence = journals.intake_evidence(entity, {"schema_version": 1, "source": {
            "namespace": "example.synthetic", "account_id": "co", "object_id": "bill-2", "version": "1"},
            "currency": "USD", "observed_facts": {"document": "bill-2"}})["id"]
        items.apply_item(entity, items.preview_item(entity, {
            "operation_id": "create-bill-2", "id": "bill-2", "kind": "payable", "account": AP, "currency": "USD",
            "original_amount": "400.00", "as_of": "2026-04-15", "counterparty": "Granite Insurance",
            "origin": {"mode": "new", "source_id": "bill-2-src", "counter_account": "Expenses:Insurance"}, "evidence_ids": [evidence]}))

    def test_cash_view_is_unchanged_by_the_bill_and_the_accrual_view_shows_it(self) -> None:
        self.enable()
        before = self.rows(statements.profit_and_loss(self.root, *self.APRIL, basis="cash"))
        self.assertEqual(before["totals"], (Decimal("0.00"), Decimal("0.00")))
        recorded = statements.profit_and_loss(self.root, *self.APRIL, basis="cash")
        self.add_bill()
        after = statements.profit_and_loss(self.root, *self.APRIL, basis="cash")
        self.assertEqual(after.meta["view"], "cash")
        self.assertEqual(self.rows(after), self.rows(recorded))
        accrual = statements.profit_and_loss(self.root, *self.APRIL, basis="accrual")
        self.assertEqual(self.rows(accrual), {"Expenses › Insurance": Decimal("400.00"), "totals": (Decimal("0.00"), Decimal("400.00"))})

    def test_the_default_view_cash_company_gets_the_same_answers_on_every_statement(self) -> None:
        self.enable()
        self.add_bill()
        # Accrual view: allowed for a cash default view once capability is 1; the payable shows on the balance sheet.
        sheet = statements.balance_sheet(self.root, date(2026, 4, 30), view="accrual")
        self.assertIn("Liabilities › Payable › Accounts-Payable         650.00", sheet.to_text())
        self.assertEqual(statements.trial_balance(self.root, date(2026, 4, 30), view="accrual").meta["view"], "accrual")
        self.assertEqual(statements.general_ledger(self.root, *self.APRIL, view="accrual").meta["view"], "accrual")
        # Before the tracking start: the existing coverage error names the first date, on every statement.
        for call, args in ((statements.profit_and_loss, (date(2026, 1, 1), date(2026, 3, 31))),
                           (statements.general_ledger, (date(2026, 1, 1), date(2026, 3, 31)))):
            with self.assertRaises(CoverageError) as caught:
                call(self.root, *args, **({"basis": "accrual"} if call is statements.profit_and_loss else {"view": "accrual"}))
            self.assertEqual(caught.exception.first_date, D)
        # Cash view of a balance statement: refused once accrual records exist, whatever the default view.
        for call in (statements.balance_sheet, statements.trial_balance):
            with self.assertRaises(ValueError) as caught:
                call(self.root, date(2026, 4, 30), view="cash")
            self.assertIn("has no cash view", str(caught.exception))
        # The default call (no view named) still reads the recorded books.
        self.assertIn("Liabilities › Payable › Accounts-Payable         650.00", statements.balance_sheet(self.root, date(2026, 4, 30)).to_text())

    def test_capability_zero_keeps_the_recorded_books_byte_for_byte(self) -> None:
        plain = statements.profit_and_loss(self.root, date(2026, 1, 1), date(2026, 3, 31)).to_json()
        self.assertEqual(statements.profit_and_loss(self.root, date(2026, 1, 1), date(2026, 3, 31), basis="cash").to_json(), plain)
        self.assertEqual(statements.balance_sheet(self.root, date(2026, 3, 31), view="cash").to_json(),
                         statements.balance_sheet(self.root, date(2026, 3, 31)).to_json())
        with self.assertRaises(ValueError) as caught:
            statements.balance_sheet(self.root, date(2026, 3, 31), view="accrual")
        self.assertIn("Cash books keep no data for an accrual view", str(caught.exception))


class CanonicalReportsCrossTheTrackingStart(TrackedCompany):
    """Cash default, tracking start 2026-04-01, opening items and one new bill. Before the start the journal is the cash books,
    so no canonical report may raise a coverage error for a range that crosses it. Only an explicit accrual view refuses."""

    FROM, TO = "2026-01-01", "2026-06-30"

    def test_every_canonical_report_succeeds_over_a_range_that_crosses_the_start(self) -> None:
        self.make("cash")
        entity = str(self.root)
        for name, argv in (("general ledger", ["report", "general-ledger", "--from", self.FROM, "--to", self.TO]),
                           ("default P&L", ["report", "pnl", "--from", self.FROM, "--to", self.TO]),
                           ("balance sheet", ["report", "balance-sheet", "--as-of", self.TO]),
                           ("trial balance", ["report", "trial-balance", "--as-of", self.TO]),
                           ("cash flow", ["report", "cashflow", "--from", self.FROM, "--to", self.TO])):
            with self.subTest(report=name):
                code, out, err = books(*argv, "--entity", entity)
                self.assertEqual(code, 0, err or out)
                self.assertNotIn("Accrual figures start on", err)
        code, out, err = books("report", "cashflow", "--from", self.FROM, "--to", self.TO, "--format", "json", "--entity", entity)
        self.assertTrue(out["reconciliation"]["ties"], out["reconciliation"])

    def test_the_default_pnl_is_the_derived_cash_view_and_the_library_default_is_the_recorded_books(self) -> None:
        self.make("cash")
        code, out, err = books("report", "pnl", "--from", self.FROM, "--to", self.TO, "--format", "json", "--entity", str(self.root))
        self.assertEqual((code, out["meta"]["view"]), (0, "cash"))
        recorded = statements.profit_and_loss(self.root, date.fromisoformat(self.FROM), date.fromisoformat(self.TO))
        self.assertEqual(recorded.sections[1]["total"] - Decimal(out["sections"][1]["total"]), Decimal("400.00"))

    def test_the_workbook_and_the_tax_handoff_succeed(self) -> None:
        self.make("cash")
        entity = str(self.root)
        out = self.work / "export"
        code, text, err = books("export", "--entity", entity, "--from", self.FROM, "--to", self.TO, "--override", "--output-dir", str(out))
        self.assertEqual(code, 0, err)
        self.assertIn("Cash view (derived", (out / "csv" / "Cover.csv").read_text())
        books("tax", "set", "--entity", entity, "--return-form", "schc")
        tax = self.work / "tax"
        code, text, err = books("tax", "export", "--entity", entity, "--from", self.FROM, "--to", self.TO, "--output-dir", str(tax))
        self.assertEqual(code, 0, err)
        readme = (tax / "README.txt").read_text()
        self.assertNotIn("Accrual basis book balances", readme)
        self.assertNotIn("Cash basis book balances", readme)
        self.assertIn("cash entries before 2026-04-01, accrual records from 2026-04-01", readme)

    def test_only_an_explicit_accrual_view_refuses_a_range_that_starts_before_the_start(self) -> None:
        self.make("cash")
        entity = str(self.root)
        for argv in (["report", "pnl", "--from", self.FROM, "--to", self.TO, "--basis", "accrual"],
                     ["report", "general-ledger", "--from", self.FROM, "--to", self.TO, "--basis", "accrual"],
                     ["report", "balance-sheet", "--as-of", "2026-03-31", "--basis", "accrual"]):
            with self.subTest(argv=argv):
                code, out, err = books(*argv, "--entity", entity)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("Accrual figures start on 2026-04-01", err)

    def test_a_company_that_only_turned_accrual_on_keeps_the_original_tax_label(self) -> None:
        self.enable()
        entity = str(self.root)
        books("tax", "set", "--entity", entity, "--return-form", "schc")
        tax = self.work / "tax"
        code, text, err = books("tax", "export", "--entity", entity, "--from", self.FROM, "--to", self.TO, "--output-dir", str(tax))
        self.assertEqual(code, 0, err)
        # No accrual record exists, so every entry is a cash entry and the original label is still true.
        self.assertIn("Cash basis book balances computed by the Slashbooks engine", (tax / "README.txt").read_text())


class BalanceStatementViews(TrackedCompany):
    def test_accrual_view_labels_the_balance_sheet_and_refuses_before_the_start(self) -> None:
        self.make("cash")
        sheet = statements.balance_sheet(self.root, date(2026, 4, 30), view="accrual")
        self.assertEqual(sheet.meta["view"], "accrual")
        self.assertIn("Accounting basis: accrual", sheet.to_text())
        with self.assertRaises(CoverageError):
            statements.balance_sheet(self.root, date(2026, 3, 31), view="accrual")
        with self.assertRaises(CoverageError):
            statements.trial_balance(self.root, date(2026, 3, 31), view="accrual")

    def test_balances_have_no_cash_view(self) -> None:
        self.make("accrual")
        for call in (statements.balance_sheet, statements.trial_balance):
            with self.subTest(call=call.__name__):
                with self.assertRaises(ValueError) as caught:
                    call(self.root, date(2026, 4, 30), view="cash")
                self.assertIn("has no cash view", str(caught.exception))
        code, out, err = books("report", "balance-sheet", "--as-of", "2026-04-30", "--basis", "cash", "--entity", str(self.root))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("has no cash view", err)

    def test_the_default_view_is_unchanged_when_the_flag_is_absent_or_equal(self) -> None:
        self.make("accrual")
        plain = statements.balance_sheet(self.root, date(2026, 4, 30)).to_json()
        self.assertEqual(statements.balance_sheet(self.root, date(2026, 4, 30), view="accrual").to_json(), plain)

    def test_cli_refuses_a_view_with_structure_or_compare(self) -> None:
        self.make("cash")
        for extra in (["--structure", "--format", "json"], ["--compare", "previous"]):
            code, out, err = books("report", "pnl", "--from", D, "--to", "2026-04-30", "--basis", "accrual", *extra, "--entity", str(self.root))
            self.assertEqual((code, out), (1, ""), extra)
            self.assertIn("--basis cannot be combined", err)

    def test_coverage_hint_repeats_the_view(self) -> None:
        self.make("cash")
        code, out, err = books("report", "balance-sheet", "--as-of", "2026-03-31", "--basis", "accrual", "--entity", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("--basis accrual --as-of 2026-04-01", err)


class AskRouting(TrackedCompany):
    TODAY = "2026-08-05"

    def ask(self, question: str):
        return books("ask", "--entity", str(self.root), question, "--today", self.TODAY)

    def test_cash_net_income_uses_the_cash_view_before_the_start(self) -> None:
        self.make("accrual")
        code, out, err = self.ask("What was our cash net income in March?")
        self.assertEqual(code, 0, err)
        self.assertIn("Answer covers: cash basis (derived from accrual books)", out)
        code, out, err = self.ask("What was our net income in March?")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("The first available date is 2026-04-01.", err)

    def test_accrual_wording_uses_the_accrual_view_and_no_wording_uses_the_default(self) -> None:
        self.make("cash")
        code, out, err = self.ask("What was our accrual net income in April?")
        self.assertEqual(code, 0, err)
        self.assertIn("Answer covers: accrual basis", out)
        self.assertIn("Net income from 2026-04-01 to 2026-04-30: -400.00", out)
        code, out, err = self.ask("What was our net income in April?")
        self.assertEqual(code, 0, err)
        self.assertIn("Answer covers: cash basis (derived from accrual books)", out)
        self.assertIn("Cash-basis net income from 2026-04-01 to 2026-04-30: 0.00", out)
        code, out, err = self.ask("What was our accrual net income in March?")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("2026-04-01", err)

    def test_balance_questions_use_the_accrual_records(self) -> None:
        self.make("cash")
        code, out, err = self.ask("What do we owe?")
        self.assertEqual(code, 0, err)
        self.assertIn("What we owe (payables): 650.00", out)
        code, out, err = self.ask("What do we owe on a cash basis?")
        self.assertEqual(code, 0, err)
        self.assertIn("Not available: the cash-basis view covers profit and loss only", out)

    def test_existing_wording_is_unchanged_for_a_cash_company(self) -> None:
        # A company that never turned accrual on keeps the original routing and refusal text.
        code, out, err = self.ask("What was our net income in March on an accrual basis?")
        self.assertEqual(code, 0, err)
        self.assertIn("Not available: you asked for the accrual basis", out)
        code, out, err = self.ask("What was our cash net income in March?")
        self.assertEqual(code, 0, err)
        self.assertIn("Answer covers: cash basis, from 2026-03-01 to 2026-03-31.", out)


class WorkbookViews(TrackedCompany):
    def export(self, *argv: str):
        out = self.work / "export"
        code, text, err = books("export", "--entity", str(self.root), "--output-dir", str(out), *argv)
        return code, text, err, out / "csv"

    def test_cash_view_exports_the_profit_and_loss_and_names_the_view(self) -> None:
        self.make("cash")
        code, text, err, csv_dir = self.export("--from", D, "--to", "2026-04-30", "--override")
        self.assertEqual(code, 0, err)
        names = sorted(path.name for path in csv_dir.iterdir())
        self.assertIn("P-and-L.csv", names)
        for left_out in ("Balance-Sheet.csv", "Trial-Balance.csv", "Summary.csv", "Checks.csv", "Outstanding-Balances.csv"):
            self.assertNotIn(left_out, names)
        cover = (csv_dir / "Cover.csv").read_text()
        self.assertIn("Cash view (derived from the accrual records", cover)
        self.assertIn("Not shown on the cash view", cover)
        pnl = (csv_dir / "P-and-L.csv").read_text()
        self.assertIn("Cash basis (derived from accrual books)", pnl)

    def test_cash_view_refuses_a_sheet_list_that_asks_for_balances(self) -> None:
        self.make("accrual")
        code, text, err, _ = self.export("--from", D, "--to", "2026-04-30", "--basis", "cash", "--sheets", "pnl,balance-sheet")
        self.assertEqual(code, 1)
        self.assertIn("The cash view has no Balance Sheet sheet", err)

    def test_accrual_view_on_a_cash_default_company_adds_the_accrual_sheets(self) -> None:
        self.make("cash")
        code, text, err, csv_dir = self.export("--from", D, "--to", "2026-04-30", "--basis", "accrual", "--override")
        self.assertEqual(code, 0, err)
        names = sorted(path.name for path in csv_dir.iterdir())
        for shown in ("Balance-Sheet.csv", "Outstanding-Balances.csv", "Receivables-Payables-Aging.csv"):
            self.assertIn(shown, names)
        self.assertIn("Accrual view (accrual figures start on 2026-04-01)", (csv_dir / "Cover.csv").read_text())
        self.assertIn("Report metadata,Basis,Accrual basis", (csv_dir / "P-and-L.csv").read_text())

    def test_accrual_view_before_the_start_is_refused_with_the_start_date(self) -> None:
        self.make("cash")
        code, text, err, _ = self.export("--from", "2026-01-01", "--to", "2026-03-31", "--basis", "accrual", "--override")
        self.assertEqual(code, 1)
        self.assertIn("2026-04-01", err)

    def test_cash_books_that_never_enabled_accrual_refuse_the_accrual_export(self) -> None:
        code, text, err, _ = self.export("--from", "2026-01-01", "--to", "2026-03-31", "--basis", "accrual")
        self.assertEqual(code, 1)
        self.assertIn("Cash books keep no data for an accrual view", err)


class CompareViews(TrackedCompany):
    """The reference folder is empty: the proof here is which view is asked for, refused or labelled."""

    def folder(self) -> Path:
        path = self.work / "qb"
        path.mkdir(exist_ok=True)
        return path

    def compare(self, view=None, start=D, end="2026-04-30"):
        from bookkeeping import compare as compare_module
        return compare_module.compare_period(load_entity(self.root), self.folder(), date.fromisoformat(start), date.fromisoformat(end), view=view)

    def test_the_default_view_report_has_no_view_field(self) -> None:
        self.make("cash")
        self.assertNotIn("view", self.compare().to_dict())
        self.assertNotIn("view", self.compare(view="cash").to_dict())

    def test_the_cash_view_compares_the_profit_and_loss_only(self) -> None:
        self.make("accrual")
        report = self.compare(view="cash")
        self.assertEqual(report.to_dict()["view"], "cash")
        text = " ".join(report.errors)
        self.assertIn("The cash view has no balance sheet", text)
        self.assertIn("The cash view has no trial balance", text)

    def test_the_accrual_view_before_the_start_is_an_error_not_an_empty_result(self) -> None:
        self.make("cash")
        report = self.compare(view="accrual", start="2026-01-01", end="2026-03-31")
        self.assertEqual(report.to_dict()["view"], "accrual")
        self.assertTrue(any("2026-04-01" in message for message in report.errors), report.errors)

    def test_a_view_other_than_the_default_saves_no_dispositions(self) -> None:
        from bookkeeping import compare as compare_module
        self.make("accrual")
        self.compare(view="cash")
        self.assertEqual(compare_module.load_differences(load_entity(self.root)), [])

    def test_cash_books_that_never_enabled_accrual_refuse_the_accrual_view(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.compare(view="accrual", start="2026-01-01", end="2026-03-31")
        self.assertIn("Cash books keep no data for an accrual view", str(caught.exception))
        code, out, err = books("compare", "period", "--entity", str(self.root), "--qb-folder", str(self.folder()),
                               "--from", "2026-01-01", "--to", "2026-03-31", "--basis", "accrual")
        self.assertEqual(code, 1)
        self.assertIn("Cash books keep no data", err)


class MixedSeries(TrackedCompany):
    """Risk 5: a monthly series that crosses the tracking start never mixes views silently."""

    def test_refuse_mode_names_the_start(self) -> None:
        self.make("accrual")
        with self.assertRaises(CoverageError) as caught:
            statements.profit_and_loss_by_month(self.root, date(2026, 2, 1), date(2026, 5, 31))
        self.assertIn("2026-04-01", str(caught.exception))

    def test_label_mode_names_the_view_of_every_month(self) -> None:
        self.make("accrual")
        series = statements.profit_and_loss_by_month(self.root, date(2026, 2, 1), date(2026, 5, 31), pre_tracking="label")
        self.assertEqual([(m["month"], m["view"], m["available"]) for m in series],
                         [("2026-02", "cash", True), ("2026-03", "cash", True), ("2026-04", "accrual", True), ("2026-05", "accrual", True)])
        self.assertEqual(series[0]["requested_view"], "accrual")
        self.assertIn("Accrual figures start on 2026-04-01", series[0]["reason"])
        self.assertNotIn("requested_view", series[2])
        # April, accrual view: the 400.00 bill is an expense.
        self.assertEqual(series[2]["statement"].sections[1]["total"], Decimal("400.00"))

    def test_a_partial_first_month_is_labelled_like_a_whole_month(self) -> None:
        self.make("accrual")
        series = statements.profit_and_loss_by_month(self.root, date(2026, 3, 15), date(2026, 4, 30), pre_tracking="label")
        self.assertEqual([(m["from_date"], m["view"], m["available"]) for m in series],
                         [("2026-03-15", "cash", True), ("2026-04-01", "accrual", True)])

    def test_a_month_that_contains_the_start_is_unavailable_not_mixed(self) -> None:
        self.enable("2026-04-10")
        self.set_view("accrual")
        series = statements.profit_and_loss_by_month(self.root, date(2026, 3, 1), date(2026, 5, 31), pre_tracking="label")
        self.assertEqual([(m["month"], m["view"], m["available"]) for m in series],
                         [("2026-03", "cash", True), ("2026-04", "accrual", False), ("2026-05", "accrual", True)])
        self.assertIn("2026-04-10", series[1]["reason"])
        self.assertIn("No figure is guessed.", series[1]["reason"])

    def test_the_cash_view_series_never_refuses(self) -> None:
        self.make("accrual")
        series = statements.profit_and_loss_by_month(self.root, date(2026, 2, 1), date(2026, 5, 31), view="cash")
        self.assertTrue(all(m["available"] and m["view"] == "cash" for m in series))

    def test_a_bad_mode_is_refused(self) -> None:
        self.make("accrual")
        with self.assertRaises(ValueError):
            statements.profit_and_loss_by_month(self.root, date(2026, 4, 1), date(2026, 4, 30), pre_tracking="guess")


class ImportedSummaryPeriod(unittest.TestCase):
    """Risk 8: the cash view is unavailable for subperiods of an imported QuickBooks summary."""

    def test_cash_view_says_so_and_never_guesses(self) -> None:
        import tempfile
        import golden_companies
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "qb"
            golden_companies.build_quickbooks(root)
            series = statements.profit_and_loss_by_month(root, date(2026, 5, 1), date(2026, 7, 31), view="cash", pre_tracking="label")
            self.assertEqual([(m["month"], m["available"]) for m in series], [("2026-05", False), ("2026-06", False), ("2026-07", True)])
            for month in series[:2]:
                self.assertIsNone(month["statement"])
                self.assertIn("The cash view is unavailable for", month["reason"])
                self.assertIn("2026-07-01", month["reason"])
                self.assertIn("No figure is guessed.", month["reason"])
            with self.assertRaises(CoverageError):
                statements.profit_and_loss_by_month(root, date(2026, 5, 1), date(2026, 7, 31), view="cash")


class BundleCarriesTheThreeFacts(TrackedCompany):
    """Capability, tracking start and default view travel together, or the bundle is refused."""

    def bundle(self, name: str = "company.zip"):
        from bookkeeping import migration_bundle
        return migration_bundle.build_bundle(self.root, self.work / name)

    def identity(self) -> dict:
        from bookkeeping import migration_bundle
        result = self.bundle()
        self.assertEqual(result["status"], "exported", result)
        return migration_bundle.load_bundle(self.work / "company.zip")["manifest"]["source_entity"]

    def test_cash_view_with_accrual_on_names_both(self) -> None:
        self.make("cash", records=False)
        identity = self.identity()
        self.assertEqual((identity["basis"], identity["accrual_capability_version"], identity["accrual_tracking_start"]), ("cash", 1, D))

    def test_accrual_view_with_a_tracking_start_names_all_three(self) -> None:
        self.make("accrual", records=False)
        identity = self.identity()
        self.assertEqual((identity["basis"], identity["accrual_capability_version"], identity["accrual_tracking_start"]), ("accrual", 1, D))

    def test_a_company_that_never_enabled_accrual_gains_no_new_identity_field(self) -> None:
        identity = self.identity()
        self.assertEqual(identity["basis"], "cash")
        self.assertNotIn("accrual_capability_version", identity)
        self.assertNotIn("accrual_tracking_start", identity)

    def test_the_bundle_validates_after_a_view_change(self) -> None:
        from bookkeeping import migration_bundle
        self.make("accrual")
        result = self.bundle()
        self.assertEqual(result["status"], "exported", result)
        self.assertEqual(migration_bundle.validate_bundle(self.work / "company.zip")["status"], "validated")

    def edit_config(self, **changes) -> None:
        path = self.root / "entity.json"
        config = json.loads(path.read_text())
        for key, value in changes.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    def test_combinations_the_bundle_cannot_carry_are_refused(self) -> None:
        cases = {
            "tracking_start_without_capability": ({"accrual_capability_version": 0}, "unsupported_tracking_start"),
            "bad_tracking_date": ({"accrual_tracking_start": "2026-13-45"}, "unsupported_tracking_start"),
            "tracking_start_differs_from_the_ledger": ({"accrual_tracking_start": "2026-05-01"}, "tracking_start_mismatch"),
            "tracking_start_missing_from_the_company_file": ({"accrual_tracking_start": None}, "tracking_start_mismatch"),
            "unknown_capability": ({"accrual_capability_version": 2}, "unsupported_basis"),
        }
        for name, (changes, code) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.make("cash", records=False)
                self.edit_config(**changes)
                result = self.bundle()
                self.assertEqual(result["status"], "blocked", result)
                self.assertIn(code, result["blockers"])
                self.assertFalse((self.work / "company.zip").exists())

    def test_capability_without_the_accrual_ledger_is_refused(self) -> None:
        # A company file that claims capability 1 while the ledger is still schema 1 cannot be carried.
        self.edit_config(accrual_capability_version=1)
        result = self.bundle()
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("unsupported_schema", result["blockers"])


if __name__ == "__main__":
    unittest.main()
