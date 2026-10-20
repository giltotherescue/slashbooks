from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.cli import main
from bookkeeping.ledger.model import Entry, Open, Posting
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.remote_contract import command_spec
from bookkeeping.taxmap import (
    CATALOG,
    DISCLAIMER,
    FORMS,
    PRIOR_EARNINGS_ACCOUNT,
    build_handoff,
    export_handoff,
    infer_form,
    map_account,
    mapping_report,
    update_settings,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tax_handoff"
UPDATE_GOLDEN = os.environ.get("UPDATE_GOLDEN") == "1"

ACCOUNTS = (
    "Assets:Bank:Checking",
    "Assets:Equipment",
    "Assets:Accumulated-Depreciation",
    "Assets:Loan-To-Shareholder",
    "Liabilities:CreditCard",
    "Liabilities:SBA-Loan",
    "Equity:Owner-Contributions",
    "Equity:Owner-Draws",
    "Equity:Opening-Balances",
    "Income:Consulting",
    "Income:Refunds",
    "Income:Interest",
    "Expenses:Software",
    "Expenses:Advertising",
    "Expenses:Meals",
    "Expenses:Officer-Salary",
    "Expenses:Payroll-Taxes",
    "Expenses:Rent",
    "Expenses:Depreciation",
    "Expenses:Charitable-Donations",
    "Expenses:Mystery",
    "Assets:Crypto-Wallet",
)


def make_company(root: Path, legal_structure: str = "S corporation") -> Path:
    company = root / "company"
    company.mkdir()
    (company / "entity.json").write_text(json.dumps({
        "name": "Synthetic Tax Co", "business_type": "consulting", "legal_structure": legal_structure,
    }) + "\n")
    store = LedgerStore(company / "ledger.sqlite")
    store.initialize()

    def entry(day: str, *legs: tuple[str, str], source: str) -> Entry:
        return Entry(date=date.fromisoformat(day), narration="Synthetic", meta=(("source-id", source),),
                     postings=tuple(Posting(account, Decimal(amount)) for account, amount in legs))

    with store.transaction() as conn:
        store.set_meta("canonical", "true", conn)
        store.set_meta("account_catalog", "sqlite", conn)
        store.insert_opens([Open(date(2025, 1, 1), account, ("USD",)) for account in ACCOUNTS], conn)
        store.insert_entries([
            # Prior year: opening equity and one year of activity.
            entry("2025-01-01", ("Assets:Bank:Checking", "5000.00"), ("Equity:Opening-Balances", "-5000.00"), source="p1"),
            entry("2025-06-01", ("Assets:Bank:Checking", "2000.00"), ("Income:Consulting", "-2000.00"), source="p2"),
            entry("2025-07-01", ("Expenses:Software", "300.00"), ("Assets:Bank:Checking", "-300.00"), source="p3"),
            entry("2025-12-01", ("Equity:Owner-Draws", "700.00"), ("Assets:Bank:Checking", "-700.00"), source="p4"),
            # Tax year 2026.
            entry("2026-01-15", ("Assets:Bank:Checking", "50000.00"), ("Income:Consulting", "-50000.00"), source="t1"),
            entry("2026-01-20", ("Income:Refunds", "500.00"), ("Assets:Bank:Checking", "-500.00"), source="t2"),
            entry("2026-02-01", ("Assets:Bank:Checking", "12.34"), ("Income:Interest", "-12.34"), source="t3"),
            entry("2026-02-10", ("Expenses:Software", "1200.00"), ("Liabilities:CreditCard", "-1200.00"), source="t4"),
            entry("2026-03-01", ("Expenses:Advertising", "800.00"), ("Liabilities:CreditCard", "-800.00"), source="t5"),
            entry("2026-03-05", ("Expenses:Meals", "240.00"), ("Liabilities:CreditCard", "-240.00"), source="t6"),
            entry("2026-04-01", ("Expenses:Officer-Salary", "20000.00"), ("Assets:Bank:Checking", "-20000.00"), source="t7"),
            entry("2026-04-01", ("Expenses:Payroll-Taxes", "1530.00"), ("Assets:Bank:Checking", "-1530.00"), source="t8"),
            entry("2026-05-01", ("Expenses:Rent", "6000.00"), ("Assets:Bank:Checking", "-6000.00"), source="t9"),
            entry("2026-06-01", ("Assets:Equipment", "3000.00"), ("Liabilities:SBA-Loan", "-3000.00"), source="t10"),
            entry("2026-12-31", ("Expenses:Depreciation", "600.00"), ("Assets:Accumulated-Depreciation", "-600.00"), source="t11"),
            entry("2026-07-01", ("Expenses:Charitable-Donations", "100.00"), ("Assets:Bank:Checking", "-100.00"), source="t12"),
            entry("2026-08-01", ("Expenses:Mystery", "75.00"), ("Assets:Bank:Checking", "-75.00"), source="t13"),
            entry("2026-09-01", ("Equity:Owner-Draws", "4000.00"), ("Assets:Bank:Checking", "-4000.00"), source="t14"),
            entry("2026-09-15", ("Assets:Bank:Checking", "1000.00"), ("Equity:Owner-Contributions", "-1000.00"), source="t15"),
            entry("2026-10-01", ("Assets:Loan-To-Shareholder", "250.00"), ("Assets:Bank:Checking", "-250.00"), source="t16"),
            entry("2026-11-01", ("Assets:Crypto-Wallet", "50.00"), ("Assets:Bank:Checking", "-50.00"), source="t17"),
            # After the tax year: must not appear.
            entry("2027-01-05", ("Assets:Bank:Checking", "999.00"), ("Income:Consulting", "-999.00"), source="n1"),
        ], conn)
    return company


YEAR = (date(2026, 1, 1), date(2026, 12, 31))


class FormInferenceTests(unittest.TestCase):
    def test_legal_structure_inference(self):
        cases = {
            "Single-member LLC": "schc", "Sole proprietorship": "schc", "LLC taxed as an S corporation": "1120s",
            "S-corp": "1120s", "Multi-member LLC": "1065", "General partnership": "1065",
            "C corporation": "1120", "Delaware corporation": "1120", "LLC": None, "": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(infer_form(text), expected)


class MappingTests(unittest.TestCase):
    def test_entity_types_map_the_same_account_to_their_own_lines(self):
        expected = {
            "Expenses:Advertising": {"schc": "schc/II/8", "1065": "1065/p1/21", "1120s": "1120s/p1/16", "1120": "1120/p1/22"},
            "Expenses:Officer-Salary": {"schc": None, "1065": None, "1120s": "1120s/p1/7", "1120": "1120/p1/12"},
            "Expenses:Guaranteed-Payments": {"schc": None, "1065": "1065/p1/10", "1120s": None, "1120": None},
            "Income:Interest": {"schc": "schc/none/personal", "1065": "1065/K/5", "1120s": "1120s/K/4", "1120": "1120/p1/5"},
            "Expenses:Charitable-Donations": {"schc": "schc/none/personal", "1065": "1065/K/13a",
                                              "1120s": "1120s/K/12a", "1120": "1120/p1/19"},
            "Equity:Owner-Draws": {"schc": "schc/none/owner_equity", "1065": "1065/M-2/6a",
                                   "1120s": "1120s/M-2/7", "1120": "1120/M-2/5a"},
            "Assets:Bank:Checking": {"schc": "schc/none/balance_sheet", "1065": "1065/L/1", "1120s": "1120s/L/1", "1120": "1120/L/1"},
            "Liabilities:CreditCard": {"schc": "schc/none/balance_sheet", "1065": "1065/L/17",
                                       "1120s": "1120s/L/18", "1120": "1120/L/18"},
            "Expenses:Payroll-Taxes": {"schc": "schc/II/23", "1065": "1065/p1/14", "1120s": "1120s/p1/12", "1120": "1120/p1/17"},
            "Expenses:Federal-Income-Tax": {"schc": "schc/none/nondeductible", "1065": "1065/M-1/nondeductible",
                                            "1120s": "1120s/M-1/nondeductible", "1120": "1120/M-1/nondeductible"},
        }
        for account, by_form in expected.items():
            for form, line in by_form.items():
                with self.subTest(account=account, form=form):
                    mapped = map_account(account, form)
                    self.assertEqual(mapped.line.code if mapped.line else None, line)
                    if line is None:
                        self.assertEqual(mapped.basis, "unmapped")

    def test_other_deductions_carry_statement_detail(self):
        mapped = map_account("Expenses:Software", "1120s")
        self.assertEqual((mapped.line.code, mapped.detail), ("1120s/p1/20", "Software"))
        travel = map_account("Expenses:Travel", "1065")
        self.assertEqual((travel.line.code, travel.detail), ("1065/p1/21", "Travel"))
        meals = map_account("Expenses:Meals", "schc")
        self.assertIn("deduction limit", meals.line.note)

    def test_every_concept_line_exists_in_the_catalog(self):
        from bookkeeping.taxmap import _CONCEPT_LINES, _OTHER_DEDUCTIONS

        for table in [*_CONCEPT_LINES.values(), _OTHER_DEDUCTIONS]:
            for form, code in table.items():
                self.assertIn(form, FORMS)
                if code is not None:
                    self.assertEqual(CATALOG[code].form, form)

    def test_unknown_accounts_stay_unmapped(self):
        for account in ("Expenses:Mystery", "Assets:Crypto-Wallet", "Equity:Something"):
            with self.subTest(account=account):
                self.assertEqual(map_account(account, "1120").basis, "unmapped")


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.company = make_company(Path(self.temp.name))

    def test_trial_balance_balances_and_carries_prior_earnings(self):
        handoff = build_handoff(self.company, *YEAR)
        self.assertEqual(handoff["form"], "1120s")
        self.assertEqual(handoff["form_basis"], "legal_structure")
        self.assertEqual(handoff["totals"]["difference"], Decimal("0.00"))
        rows = {row["account"]: row for row in handoff["trial_balance"]}
        self.assertEqual(rows[PRIOR_EARNINGS_ACCOUNT]["balance"], Decimal("-1700.00"))
        self.assertEqual(rows[PRIOR_EARNINGS_ACCOUNT]["mapping"].line.code, "1120s/L/24")
        self.assertEqual(rows["Income:Consulting"]["balance"], Decimal("-50000.00"))
        self.assertEqual(rows["Assets:Bank:Checking"]["beginning_balance"], Decimal("6000.00"))
        self.assertNotIn("Expenses:Officer-Salary", {r["account"] for r in handoff["unmapped"]})
        self.assertEqual(sorted(r["account"] for r in handoff["unmapped"]), ["Assets:Crypto-Wallet", "Expenses:Mystery"])
        summary = {(s["line"].code, s["detail"]): s["amount"] for s in handoff["summary"]}
        self.assertEqual(summary[("1120s/p1/1a", "")], Decimal("50000.00"))
        self.assertEqual(summary[("1120s/p1/1b", "")], Decimal("500.00"))
        self.assertEqual(summary[("1120s/L/10b", "")], Decimal("600.00"))
        self.assertEqual(summary[("1120s/p1/20", "Meals (full book amount)")], Decimal("240.00"))
        # Distributions are the period's change, not the cumulative draws balance.
        self.assertEqual(rows["Equity:Owner-Draws"]["balance"], Decimal("4700.00"))
        self.assertEqual(summary[("1120s/M-2/7", "")], Decimal("4000.00"))

    def test_overrides_are_stored_in_the_workspace_and_win(self):
        update_settings(self.company, form="1120s", account="Expenses:Mystery", line="1120s/p1/20")
        update_settings(self.company, account="Assets:Crypto-Wallet", line="1120s/L/14")
        config = json.loads((self.company / "entity.json").read_text())
        self.assertEqual(config["tax_handoff"]["line_overrides"]["1120s"]["Expenses:Mystery"], "1120s/p1/20")
        report = mapping_report(self.company, from_date=YEAR[0], to_date=YEAR[1])
        self.assertEqual(report["unmapped"], [])
        mystery = next(item for item in report["accounts"] if item["account"] == "Expenses:Mystery")
        self.assertEqual((mystery["basis"], mystery["detail"], mystery["review"]), ("override", "Mystery", False))
        # An override for another form does not leak.
        self.assertIn("Expenses:Mystery", mapping_report(self.company, form="1120")["unmapped"])
        update_settings(self.company, account="Expenses:Mystery", clear_line=True)
        self.assertIn("Expenses:Mystery", mapping_report(self.company)["unmapped"])

    def test_map_report_carries_number_return_type_and_ultratax_code(self):
        update_settings(self.company, account="Expenses:Mystery", account_number="6123", ultratax_code="UT-9")
        report = mapping_report(self.company)
        mystery = next(item for item in report["accounts"] if item["account"] == "Expenses:Mystery")
        self.assertEqual((mystery["account_number"], mystery["ultratax_code"]), ("6123", "UT-9"))
        for item in report["accounts"]:
            self.assertEqual(item["return_type"], report["form"])
            self.assertIn("account_number", item)
            self.assertIn("ultratax_code", item)
            self.assertIn("line", item)
        other = next(item for item in report["accounts"] if item["account"] != "Expenses:Mystery")
        self.assertIsNone(other["account_number"])
        self.assertIsNone(other["ultratax_code"])

    def test_settings_validation(self):
        with self.assertRaisesRegex(ValueError, "not a Form 1120-S line"):
            update_settings(self.company, account="Expenses:Mystery", line="1065/p1/21")
        with self.assertRaisesRegex(ValueError, "not in the account catalog"):
            update_settings(self.company, account="Expenses:Ghost", line="1120s/p1/20")
        with self.assertRaisesRegex(ValueError, "Unsupported return form"):
            update_settings(self.company, return_form="990")
        update_settings(self.company, return_form="1065")
        self.assertEqual(build_handoff(self.company, *YEAR)["form"], "1065")

    def test_unknown_form_requires_an_explicit_choice(self):
        config = json.loads((self.company / "entity.json").read_text())
        config["legal_structure"] = "LLC"
        (self.company / "entity.json").write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, "return form is unknown"):
            build_handoff(self.company, *YEAR)
        self.assertEqual(build_handoff(self.company, *YEAR, form="schc")["form"], "schc")

    def test_export_matches_golden_files_and_writes_vendor_layouts(self):
        update_settings(self.company, account="Expenses:Software", ultratax_code="80")
        update_settings(self.company, account="Assets:Bank:Checking", account_number="1001")
        out = Path(self.temp.name) / "out"
        manifest = export_handoff(self.company, *YEAR, output_dir=out, formats=["csv"])
        self.assertEqual(manifest["unmapped"], ["Assets:Crypto-Wallet", "Expenses:Mystery"])
        self.assertEqual(manifest["totals"]["difference"], "0.00")
        for name in manifest["files"]:
            golden = FIXTURES / name
            actual = (out / name).read_text(encoding="utf-8")
            if UPDATE_GOLDEN:
                golden.parent.mkdir(parents=True, exist_ok=True)
                golden.write_text(actual, encoding="utf-8")
            with self.subTest(file=name):
                self.assertEqual(actual, golden.read_text(encoding="utf-8"))
        with (out / "ultratax-tb-import-1120s-2026-01-01_2026-12-31.csv").open() as handle:
            ultratax = list(csv.reader(handle))
        self.assertEqual(ultratax[0], ["Account Number", "Account Description", "Unit", "Tax Code", "Amount"])
        self.assertIn(["1001", "Assets › Bank › Checking", "", "", "24507.34"], ultratax)
        software = next(row for row in ultratax if row[1] == "Expenses › Software")
        self.assertEqual(software[3:], ["80", "1200.00"])
        with (out / "lacerte-tbu-1120s-2026-01-01_2026-12-31.csv").open() as handle:
            types = {row[1]: row[2] for row in csv.reader(handle)}
        self.assertEqual((types["Equity › Owner-Draws"], types["Income › Consulting"]), ("L", "R"))

    def test_every_export_output_carries_the_draft_disclaimer(self):
        out = Path(self.temp.name) / "out"
        manifest = export_handoff(self.company, *YEAR, output_dir=out, formats=["csv"])
        self.assertEqual(manifest["disclaimer"], DISCLAIMER)
        self.assertIn("not tax advice", DISCLAIMER)
        stored = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["disclaimer"], DISCLAIMER)
        vendor = {"Lacerte TBU", "UltraTax CS Import"}
        self.assertIn("README.txt", manifest["files"])
        for name, info in manifest["files"].items():
            text = (out / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                if info["sheet"] in vendor:
                    # Vendor import layouts keep their header row first; the
                    # README and manifest in the same folder carry the note.
                    self.assertNotIn(DISCLAIMER, text)
                else:
                    self.assertEqual(text.splitlines()[0], DISCLAIMER)

    @unittest.skipUnless(importlib.util.find_spec("xlsxwriter"), "xlsx extra not installed")
    def test_xlsx_read_me_starts_with_the_disclaimer(self):
        import zipfile

        out = Path(self.temp.name) / "out"
        manifest = export_handoff(self.company, *YEAR, output_dir=out, formats=["xlsx"])
        self.assertEqual(manifest["disclaimer"], DISCLAIMER)
        with zipfile.ZipFile(out / "tax-handoff-1120s-2026-01-01_2026-12-31.xlsx") as archive:
            text = "".join(archive.read(n).decode() for n in archive.namelist() if n.endswith(".xml"))
        self.assertIn("Draft for review. Prepared from the data in these books only.", text)

    def test_exports_neutralize_formula_text_and_keep_numbers_numeric(self):
        import zipfile
        from unittest.mock import patch

        from bookkeeping import taxmap

        real = taxmap._ultratax_table

        def hostile(handoff):
            rows = real(handoff)
            rows[1][1] = '=HYPERLINK("http://x","y")'
            rows[2][1] = "@SUM(1+1)"
            rows[3][1] = "-2+3"
            return rows

        out = Path(self.temp.name) / "out"
        with patch.object(taxmap, "_ultratax_table", hostile):
            manifest = export_handoff(self.company, *YEAR, output_dir=out, formats=["csv", "xlsx"])
        for name in manifest["files"]:
            if not name.endswith(".csv"):
                continue
            with self.subTest(file=name):
                with (out / name).open(newline="", encoding="utf-8") as handle:
                    for row in csv.reader(handle):
                        for cell in row:
                            if cell.startswith(("=", "@", "\t", "\r")) or (cell.startswith("-") and not re.fullmatch(r"-[\d,]*\.?\d+", cell)):
                                self.fail(f"Formula text left in {name}: {cell!r}")
        with (out / "ultratax-tb-import-1120s-2026-01-01_2026-12-31.csv").open(newline="", encoding="utf-8") as handle:
            ultratax = list(csv.reader(handle))
        self.assertEqual([row[1] for row in ultratax[1:4]], ["'=HYPERLINK(\"http://x\",\"y\")", "'@SUM(1+1)", "'-2+3"])
        # A negative amount is still a plain number, not text with an apostrophe.
        self.assertIn("-50000.00", [row[4] for row in ultratax])
        if importlib.util.find_spec("xlsxwriter"):
            with zipfile.ZipFile(out / "tax-handoff-1120s-2026-01-01_2026-12-31.xlsx") as archive:
                sheets = [archive.read(n).decode() for n in archive.namelist() if n.startswith("xl/worksheets/")]
            self.assertTrue(sheets)
            for xml in sheets:
                self.assertNotIn("<f>", xml)

    def test_require_mapped_refuses_unmapped_accounts(self):
        with self.assertRaisesRegex(ValueError, "2 account\\(s\\) have no tax line"):
            export_handoff(self.company, *YEAR, output_dir=Path(self.temp.name) / "x", require_mapped=True)

    @unittest.skipUnless(importlib.util.find_spec("xlsxwriter"), "xlsx extra not installed")
    def test_xlsx_workbook_has_every_sheet(self):
        import zipfile

        out = Path(self.temp.name) / "out"
        manifest = export_handoff(self.company, *YEAR, output_dir=out, formats=["xlsx"])
        name = "tax-handoff-1120s-2026-01-01_2026-12-31.xlsx"
        self.assertIn(name, manifest["files"])
        with zipfile.ZipFile(out / name) as archive:
            workbook = archive.read("xl/workbook.xml").decode()
        for sheet in ("Read Me", "Trial Balance", "Tax Line Summary", "Unmapped Accounts", "Lacerte TBU",
                      "UltraTax CS Import"):
            self.assertIn(f'name="{sheet}"', workbook)

    def _large_accounting(self, accounts=400):
        chart = [{"account": f"Expenses:Category-{n:04d}", "account_type": "Expenses", "currency": "USD",
                  "detail_type": "", "open_date": "2025-12-31", "source_account": f"Expenses:Category-{n:04d}"}
                 for n in range(accounts)]
        return {"basis": "accrual", "currency": "USD", "requested_from": "2026-01-01", "requested_to": "2026-12-31",
                "summary_included": False, "unresolved_issues": [],
                "coverage": {"basis": "accrual", "chart": chart, "cutover_date": "2026-01-01", "id": "opening",
                             "summary_from": None, "summary_to": None, "mapping_hash": "0" * 64}}

    def test_read_me_shows_a_short_coverage_summary_and_manifest_keeps_the_full_block(self):
        from unittest.mock import patch

        accounting = self._large_accounting()
        out = Path(self.temp.name) / "big"
        with patch("bookkeeping.reports.statements._canonical_metadata", return_value=accounting):
            manifest = export_handoff(self.company, *YEAR, output_dir=out, formats=["csv"])
        text = (out / "README.txt").read_text(encoding="utf-8")
        self.assertNotIn("Category-0001", text)
        self.assertNotIn("mapping_hash", text)
        self.assertLess(len(text), 4000)
        line = next(row for row in text.splitlines() if row.startswith("Accounting coverage:"))
        self.assertIn("accrual basis", line)
        self.assertIn("cutover 2026-01-01", line)
        self.assertIn("historical detail available", line)
        self.assertEqual(manifest["accounting"], accounting)
        self.assertEqual(json.loads((out / "manifest.json").read_text())["accounting"], accounting)

    @unittest.skipUnless(importlib.util.find_spec("xlsxwriter"), "xlsx extra not installed")
    def test_xlsx_read_me_cell_stays_short_for_a_large_chart(self):
        import zipfile
        from unittest.mock import patch

        accounting = self._large_accounting(accounts=600)
        self.assertGreater(len(json.dumps(accounting)), 32767)
        out = Path(self.temp.name) / "bigx"
        with patch("bookkeeping.reports.statements._canonical_metadata", return_value=accounting):
            export_handoff(self.company, *YEAR, output_dir=out, formats=["xlsx"])
        with zipfile.ZipFile(out / "tax-handoff-1120s-2026-01-01_2026-12-31.xlsx") as archive:
            strings = archive.read("xl/sharedStrings.xml").decode()
        self.assertNotIn("Category-0001", strings)
        self.assertIn("Accounting coverage", strings)
        self.assertLess(max(len(piece) for piece in re.findall(r"<t[^>]*>([^<]*)</t>", strings)), 2000)

    def test_per_account_company_gets_a_per_account_basis_label(self):
        from unittest.mock import patch

        out = Path(self.temp.name) / "pa"
        accounting = {"basis": "accrual", "currency": "USD", "coverage": None, "requested_from": "2026-01-01",
                      "requested_to": "2026-12-31", "unresolved_issues": []}
        with patch("bookkeeping.taxmap.discipline", return_value="per_account"), \
                patch("bookkeeping.taxmap.tracking_start", return_value="2026-04-01"), \
                patch("bookkeeping.reports.statements._canonical_metadata", return_value=accounting):
            handoff = build_handoff(self.company, *YEAR)
            export_handoff(self.company, *YEAR, output_dir=out, formats=["csv"])
        self.assertIn("Cash basis, accrual tracking for selected accounts from 2026-04-01", handoff["basis_label"])
        text = (out / "README.txt").read_text(encoding="utf-8")
        self.assertIn("Basis: Cash basis, accrual tracking for selected accounts from 2026-04-01", text)
        self.assertNotIn("Accrual basis book balances", text)

    def test_per_account_company_without_accrual_records_keeps_the_plain_label(self):
        from unittest.mock import patch

        # Turning tracking on, with no accrual record yet, changes no export.
        with patch("bookkeeping.taxmap.discipline", return_value="per_account"), \
                patch("bookkeeping.taxmap.tracking_start", return_value="2026-04-01"):
            handoff = build_handoff(self.company, *YEAR)
        self.assertEqual(handoff["accounting_basis"], "cash")
        self.assertIsNone(handoff["basis_label"])

    def test_cash_company_keeps_the_plain_basis_label(self):
        handoff = build_handoff(self.company, *YEAR)
        self.assertIsNone(handoff["basis_label"])

    def test_each_supported_form_exports_a_balanced_trial_balance(self):
        for form in FORMS:
            with self.subTest(form=form):
                handoff = build_handoff(self.company, *YEAR, form=form)
                self.assertEqual(handoff["totals"]["difference"], Decimal("0.00"))
                self.assertTrue(all(s["line"].form == form for s in handoff["summary"]))


class TaxCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.company = make_company(Path(self.temp.name), legal_structure="Single-member LLC")

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_cli_round_trip(self):
        code, output = self.run_cli("tax", "lines", "--form", "schc", "--format", "json")
        self.assertEqual(code, 0)
        self.assertIn("schc/II/24b", {line["code"] for line in json.loads(output)})
        code, output = self.run_cli("tax", "map", "--entity", str(self.company), "--year", "2026", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["form"], "schc")
        code, _ = self.run_cli("tax", "set", "--entity", str(self.company), "--account", "Expenses:Mystery",
                               "--line", "schc/II/27b")
        self.assertEqual(code, 0)
        out = Path(self.temp.name) / "export"
        code, output = self.run_cli("tax", "export", "--entity", str(self.company), "--year", "2026",
                                    "--output-dir", str(out), "--formats", "csv")
        self.assertEqual(code, 0)
        self.assertIn("2 unmapped account(s): Assets:Crypto-Wallet, Expenses:Officer-Salary", output)
        self.assertEqual(output.splitlines()[0], DISCLAIMER)
        self.assertTrue((out / "manifest.json").is_file())
        code, output = self.run_cli("tax", "map", "--entity", str(self.company), "--year", "2026")
        self.assertEqual((code, output.splitlines()[0]), (0, DISCLAIMER))
        code, output = self.run_cli("tax", "map", "--entity", str(self.company), "--format", "json")
        self.assertEqual(json.loads(output)["disclaimer"], DISCLAIMER)

    def test_remote_contract_classifies_tax_commands(self):
        self.assertFalse(command_spec(["tax", "export", "--entity", "@entity", "--year", "2026"])["mutates"])
        self.assertFalse(command_spec(["tax", "map", "--entity", "@entity"])["mutates"])
        self.assertTrue(command_spec(["tax", "set", "--entity", "@entity", "--return-form", "1065"])["mutates"])


if __name__ == "__main__":
    unittest.main()
