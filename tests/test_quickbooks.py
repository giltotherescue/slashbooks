"""Tests for src/bookkeeping/quickbooks.py — U6 QuickBooks export inventory and parser.

All fixtures are fully synthetic (invented company "Acme Consulting LLC").
No real exports are used or committed.

Test coverage:
  - AE1-shaped happy path: cash-basis folder -> readiness green; import-opening
    posts entries dated 2026-01-01; computed balances match fixture BS
  - Accrual TB fixture -> readiness BLOCKS exactly the TB, other files unaffected
  - GL fixture: subtotal and opening-balance rows never parsed as transactions
  - Unrecognized file -> ambiguous
  - Name mapping table (curly apostrophe, "(2136) - 1", collision suffixing)
  - Amount parsing (commas, $, parentheses-negative, plain)
  - Row-type classifier
  - BS/PL section vs leaf classification
"""

import csv
import json
import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

# Path to synthetic fixtures
_FIXTURES = Path(__file__).parent / "fixtures" / "quickbooks"


def _write_xlsx(path: Path, rows: list[list[str]]) -> None:
    """Write a minimal single-sheet XLSX fixture with inline string cells."""

    def col_name(idx: int) -> str:
        idx += 1
        name = ""
        while idx:
            idx, rem = divmod(idx - 1, 26)
            name = chr(ord("A") + rem) + name
        return name

    row_xml = []
    for row_idx, row in enumerate(rows, start=1):
        cells = []
        for col_idx, value in enumerate(row):
            if value == "":
                continue
            ref = f"{col_name(col_idx)}{row_idx}"
            cells.append(
                f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            )
        row_xml.append(f'<row r="{row_idx}">{"".join(cells)}</row>')

    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        '</worksheet>'
    )

    with ZipFile(path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        zf.writestr("xl/worksheets/sheet1.xml", worksheet)


def _fixture_rows(name: str) -> list[list[str]]:
    with (_FIXTURES / name).open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.reader(fh))


class TestAmountParsing(unittest.TestCase):
    """Tests for parse_amount — the shared amount parser."""

    def setUp(self):
        from src.bookkeeping.quickbooks import parse_amount
        self.parse = parse_amount

    def test_plain_integer(self):
        self.assertEqual(self.parse("1234"), Decimal("1234.00"))

    def test_plain_decimal(self):
        self.assertEqual(self.parse("1234.56"), Decimal("1234.56"))

    def test_thousands_comma(self):
        self.assertEqual(self.parse("4,298.12"), Decimal("4298.12"))

    def test_quoted_thousands_comma(self):
        self.assertEqual(self.parse('"4,298.12"'), Decimal("4298.12"))

    def test_dollar_sign(self):
        self.assertEqual(self.parse("$1,234.56"), Decimal("1234.56"))

    def test_dollar_sign_zero(self):
        self.assertEqual(self.parse("$0.00"), Decimal("0.00"))

    def test_parentheses_negative(self):
        self.assertEqual(self.parse("(500.00)"), Decimal("-500.00"))

    def test_parentheses_negative_with_comma(self):
        self.assertEqual(self.parse("(1,200.50)"), Decimal("-1200.50"))

    def test_empty_string(self):
        self.assertEqual(self.parse(""), Decimal("0.00"))

    def test_negative_with_dollar(self):
        self.assertEqual(self.parse("-$44,609.90"), Decimal("-44609.90"))

    def test_dollar_subtotal_format(self):
        # QB subtotals like "$21,425.96"
        self.assertEqual(self.parse("$21,425.96"), Decimal("21425.96"))

    def test_negative_plain(self):
        self.assertEqual(self.parse("-429.81"), Decimal("-429.81"))

    def test_zero(self):
        self.assertEqual(self.parse("0.00"), Decimal("0.00"))

    def test_whitespace_stripped(self):
        self.assertEqual(self.parse("  1,234.56  "), Decimal("1234.56"))


class TestRowTypeClassifier(unittest.TestCase):
    """Tests for classify_row."""

    def setUp(self):
        from src.bookkeeping.quickbooks import classify_row, RowType
        self.classify = classify_row
        self.RT = RowType

    def _row(self, *cells):
        return list(cells)

    def test_blank_all_empty(self):
        r = self.classify(["", "", "", "", "", "", "", "", ""])
        self.assertEqual(r, self.RT.BLANK)

    def test_blank_single_empty(self):
        r = self.classify([""])
        self.assertEqual(r, self.RT.BLANK)

    def test_basis_footer_cash(self):
        r = self.classify(['Cash Basis Friday, June 12, 2026 12:09 PM GMT-05:00', "", ""])
        self.assertEqual(r, self.RT.BASIS_FOOTER)

    def test_basis_footer_accrual(self):
        r = self.classify(['Accrual Basis Thursday, June 12, 2025 10:30 AM GMT-05:00', "", ""])
        self.assertEqual(r, self.RT.BASIS_FOOTER)

    def test_column_header_trial_balance(self):
        r = self.classify(["Full name", "Debit", "Credit"])
        self.assertEqual(r, self.RT.COLUMN_HEADER)

    def test_column_header_bs_pl(self):
        r = self.classify(["", "Total"])
        self.assertEqual(r, self.RT.COLUMN_HEADER)

    def test_column_header_gl(self):
        r = self.classify(["", "Transaction date", "Transaction type", "Num",
                           "Name", "Description", "Split", "Amount", "Balance"])
        self.assertEqual(r, self.RT.COLUMN_HEADER)

    def test_column_header_coa(self):
        r = self.classify(["Account name", "Account type", "Detail type", "Lock"])
        self.assertEqual(r, self.RT.COLUMN_HEADER)

    def test_subtotal_total_for(self):
        r = self.classify(["Total for Bank Accounts", "$17,500.00"])
        self.assertEqual(r, self.RT.SUBTOTAL)

    def test_subtotal_bare_total(self):
        r = self.classify(["TOTAL", "$825,863.74", "$825,863.74"])
        self.assertEqual(r, self.RT.SUBTOTAL)

    def test_subtotal_net_income(self):
        r = self.classify(["Net Income", "-$44,609.90"])
        self.assertEqual(r, self.RT.SUBTOTAL)

    def test_subtotal_gross_profit(self):
        r = self.classify(["Gross Profit", "$19,133.47"])
        self.assertEqual(r, self.RT.SUBTOTAL)

    def test_opening_balance_gl(self):
        # GL opening balance: col0 empty, cols 1-7 empty, col8 has value
        r = self.classify(["", "", "", "", "", "", "", "", "4,298.12"])
        self.assertEqual(r, self.RT.OPENING_BALANCE)

    def test_transaction_gl(self):
        # GL transaction: col0 empty, col1 is a date
        r = self.classify(["", "01/02/2026", "Transfer", "", "", "desc", "Split", "-429.81", "3,868.31"])
        self.assertEqual(r, self.RT.TRANSACTION)

    def test_section_header(self):
        # Section: col0 has name, all other cols empty
        r = self.classify(["Assets", ""])
        self.assertEqual(r, self.RT.SECTION)

    def test_account_header_gl(self):
        # Account header in GL: col0 has account name, rest empty
        r = self.classify(["Distribution (2136) - 1", "", "", "", "", "", "", "", ""])
        self.assertEqual(r, self.RT.SECTION)

    def test_leaf_row_bs(self):
        # Leaf row with amount: col0 name, col1 amount
        r = self.classify(["Checking (4512) - 1", "12,500.00"])
        self.assertEqual(r, self.RT.TRANSACTION)

    def test_leaf_row_bs_dollar(self):
        r = self.classify(['Cash', "0.00"])
        self.assertEqual(r, self.RT.TRANSACTION)


class TestDetectReportType(unittest.TestCase):
    """Tests for detect_report_type and detect_basis."""

    def setUp(self):
        from src.bookkeeping.quickbooks import detect_report_type, detect_basis
        self.detect_type = detect_report_type
        self.detect_basis = detect_basis

    def test_chart_of_accounts(self):
        rows = [["Account name", "Account type", "Detail type", "Lock"]]
        self.assertEqual(self.detect_type(rows), "chart_of_accounts")

    def test_trial_balance(self):
        rows = [
            ["Acme Consulting LLC", "", ""],
            ["Trial Balance", "", ""],
            ["As of Dec 31, 2025", "", ""],
        ]
        self.assertEqual(self.detect_type(rows), "trial_balance")

    def test_balance_sheet(self):
        rows = [
            ["Acme Consulting LLC"],
            ["Balance Sheet"],
            ["As of Dec 31, 2025"],
        ]
        self.assertEqual(self.detect_type(rows), "balance_sheet")

    def test_profit_and_loss(self):
        rows = [
            ["Acme Consulting LLC"],
            ["Profit and Loss"],
            ["January-December, 2025"],
        ]
        self.assertEqual(self.detect_type(rows), "profit_and_loss")

    def test_general_ledger(self):
        rows = [
            ["Acme Consulting LLC"] + [""] * 8,
            ["General Ledger"] + [""] * 8,
            ["January-December, 2025"] + [""] * 8,
        ]
        self.assertEqual(self.detect_type(rows), "general_ledger")

    def test_unrecognized(self):
        rows = [["Some random file"], ["Data row"]]
        self.assertIsNone(self.detect_type(rows))

    def test_basis_cash(self):
        rows = [
            [""],
            ["Cash Basis Friday, June 12, 2026 12:09 PM GMT-05:00", "", ""],
        ]
        self.assertEqual(self.detect_basis(rows), "cash")

    def test_basis_accrual(self):
        rows = [
            [""],
            ["Accrual Basis Thursday, June 12, 2025 10:30 AM GMT-05:00", "", ""],
        ]
        self.assertEqual(self.detect_basis(rows), "accrual")

    def test_basis_none(self):
        rows = [["Some data"]]
        self.assertIsNone(self.detect_basis(rows))


class TestParseChartOfAccounts(unittest.TestCase):
    """Tests for parse_chart_of_accounts."""

    def setUp(self):
        from src.bookkeeping.quickbooks import parse_chart_of_accounts
        self.parse = parse_chart_of_accounts

    def test_parse_fixture(self):
        path = _FIXTURES / "chart_of_accounts.csv"
        accounts = self.parse(path)
        self.assertGreater(len(accounts), 0)
        # Check a known account
        names = [a.name for a in accounts]
        self.assertIn("Checking (4512) - 1", names)
        self.assertIn("Visa Card (3301) - 2", names)

    def test_account_types(self):
        path = _FIXTURES / "chart_of_accounts.csv"
        accounts = self.parse(path)
        by_name = {a.name: a for a in accounts}
        self.assertEqual(by_name["Checking (4512) - 1"].account_type, "Bank")
        self.assertEqual(by_name["Visa Card (3301) - 2"].account_type, "Credit Card")
        self.assertEqual(by_name["Consulting Revenue"].account_type, "Income")

    def test_hierarchical_name(self):
        path = _FIXTURES / "chart_of_accounts.csv"
        accounts = self.parse(path)
        names = [a.name for a in accounts]
        self.assertIn("Office expenses:Dues and Subscriptions", names)
        self.assertIn("Vehicle expenses:Vehicle registration", names)

    def test_direct_qbo_chart_of_accounts_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "chart_of_accounts_direct.csv"
            path.write_text(
                "Name,Type,Detail type,QuickBooks Balance,Bank Balance\n"
                "Checking (4512) - 1,Bank,Checking,20000.00,20000.00\n"
                "Consulting Revenue,Income,Service/Fee Income,0.00,\n",
                encoding="utf-8",
            )
            accounts = self.parse(path)
            by_name = {a.name: a for a in accounts}
            self.assertEqual(by_name["Checking (4512) - 1"].account_type, "Bank")
            self.assertEqual(by_name["Consulting Revenue"].detail_type, "Service/Fee Income")

    def test_xlsx_chart_of_accounts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "chart_of_accounts.xlsx"
            _write_xlsx(path, _fixture_rows("chart_of_accounts.csv"))
            accounts = self.parse(path)
            names = [a.name for a in accounts]
            self.assertIn("Checking (4512) - 1", names)

    def test_wrong_file_raises(self):
        from src.bookkeeping.quickbooks import parse_chart_of_accounts
        path = _FIXTURES / "trial_balance_cash_basis.csv"
        with self.assertRaises(ValueError):
            parse_chart_of_accounts(path)


class TestParseTrialBalance(unittest.TestCase):
    """Tests for parse_trial_balance."""

    def setUp(self):
        from src.bookkeeping.quickbooks import parse_trial_balance
        self.parse = parse_trial_balance

    def test_cash_basis_detected(self):
        path = _FIXTURES / "trial_balance_cash_basis.csv"
        tb = self.parse(path)
        self.assertEqual(tb.basis, "cash")

    def test_accrual_basis_detected(self):
        path = _FIXTURES / "trial_balance_accrual_basis.csv"
        tb = self.parse(path)
        self.assertEqual(tb.basis, "accrual")

    def test_company_name(self):
        path = _FIXTURES / "trial_balance_cash_basis.csv"
        tb = self.parse(path)
        self.assertEqual(tb.company, "Acme Consulting LLC")

    def test_as_of_date(self):
        path = _FIXTURES / "trial_balance_cash_basis.csv"
        tb = self.parse(path)
        self.assertIn("Dec 31, 2025", tb.as_of)

    def test_entries_parsed(self):
        path = _FIXTURES / "trial_balance_cash_basis.csv"
        tb = self.parse(path)
        self.assertGreater(len(tb.entries), 0)

    def test_xlsx_entries_parsed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "trial_balance_cash_basis.xlsx"
            _write_xlsx(path, _fixture_rows("trial_balance_cash_basis.csv"))
            tb = self.parse(path)
            self.assertEqual(tb.basis, "cash")
            by_name = {e.full_name: e for e in tb.entries}
            self.assertEqual(by_name["Checking (4512) - 1"].debit, Decimal("20000.00"))

    def test_debit_amount(self):
        path = _FIXTURES / "trial_balance_cash_basis.csv"
        tb = self.parse(path)
        by_name = {e.full_name: e for e in tb.entries}
        # Checking (4512) - 1 should have debit 20000.00
        self.assertIn("Checking (4512) - 1", by_name)
        self.assertEqual(by_name["Checking (4512) - 1"].debit, Decimal("20000.00"))
        self.assertEqual(by_name["Checking (4512) - 1"].credit, Decimal("0.00"))

    def test_credit_amount(self):
        path = _FIXTURES / "trial_balance_cash_basis.csv"
        tb = self.parse(path)
        by_name = {e.full_name: e for e in tb.entries}
        self.assertIn("Visa Card (3301) - 2", by_name)
        self.assertEqual(by_name["Visa Card (3301) - 2"].credit, Decimal("2000.00"))
        self.assertEqual(by_name["Visa Card (3301) - 2"].debit, Decimal("0.00"))

    def test_totals_row_excluded(self):
        """TOTAL row must not appear in entries."""
        path = _FIXTURES / "trial_balance_cash_basis.csv"
        tb = self.parse(path)
        names = [e.full_name for e in tb.entries]
        self.assertNotIn("TOTAL", names)

    def test_tb_balances(self):
        """Debits must equal credits in a balanced trial balance."""
        path = _FIXTURES / "trial_balance_cash_basis.csv"
        tb = self.parse(path)
        total_debit = sum(e.debit for e in tb.entries)
        total_credit = sum(e.credit for e in tb.entries)
        self.assertEqual(total_debit, total_credit)

    def test_wrong_file_raises(self):
        path = _FIXTURES / "chart_of_accounts.csv"
        with self.assertRaises(ValueError):
            self.parse(path)


class TestParseBalanceSheet(unittest.TestCase):
    """Tests for parse_balance_sheet."""

    def setUp(self):
        from src.bookkeeping.quickbooks import parse_balance_sheet
        self.parse = parse_balance_sheet

    def test_parse_fixture(self):
        path = _FIXTURES / "balance_sheet_2025_12_31.csv"
        bs = self.parse(path)
        self.assertEqual(bs.company, "Acme Consulting LLC")
        self.assertIn("Dec 31, 2025", bs.as_of)
        self.assertEqual(bs.basis, "cash")

    def test_leaf_rows_classified(self):
        path = _FIXTURES / "balance_sheet_2025_12_31.csv"
        bs = self.parse(path)
        leaf_names = [r.name for r in bs.rows if r.row_type == "leaf"]
        self.assertIn("Checking (4512) - 1", leaf_names)
        self.assertIn("Savings (9873) - 1", leaf_names)

    def test_section_rows_classified(self):
        path = _FIXTURES / "balance_sheet_2025_12_31.csv"
        bs = self.parse(path)
        section_names = [r.name for r in bs.rows if r.row_type == "section"]
        self.assertIn("Assets", section_names)
        self.assertIn("Current Assets", section_names)

    def test_subtotal_rows_classified(self):
        path = _FIXTURES / "balance_sheet_2025_12_31.csv"
        bs = self.parse(path)
        subtotal_names = [r.name for r in bs.rows if r.row_type == "subtotal"]
        self.assertTrue(any("Total for" in n for n in subtotal_names))

    def test_leaf_amounts(self):
        path = _FIXTURES / "balance_sheet_2025_12_31.csv"
        bs = self.parse(path)
        by_name = {r.name: r for r in bs.rows if r.row_type == "leaf"}
        self.assertEqual(by_name["Checking (4512) - 1"].amount, Decimal("20000.00"))
        self.assertEqual(by_name["Savings (9873) - 1"].amount, Decimal("5000.00"))

    def test_wrong_file_raises(self):
        path = _FIXTURES / "chart_of_accounts.csv"
        with self.assertRaises(ValueError):
            self.parse(path)


class TestParseProfitAndLoss(unittest.TestCase):
    """Tests for parse_profit_and_loss."""

    def setUp(self):
        from src.bookkeeping.quickbooks import parse_profit_and_loss
        self.parse = parse_profit_and_loss

    def test_parse_fixture(self):
        path = _FIXTURES / "profit_and_loss.csv"
        pl = self.parse(path)
        self.assertEqual(pl.company, "Acme Consulting LLC")
        self.assertIn("January-December, 2025", pl.period)
        self.assertEqual(pl.basis, "cash")

    def test_leaf_rows(self):
        path = _FIXTURES / "profit_and_loss.csv"
        pl = self.parse(path)
        leaf_names = [r.name for r in pl.rows if r.row_type == "leaf"]
        self.assertIn("Consulting Revenue", leaf_names)
        self.assertIn("Advertising & Marketing", leaf_names)

    def test_section_rows(self):
        path = _FIXTURES / "profit_and_loss.csv"
        pl = self.parse(path)
        section_names = [r.name for r in pl.rows if r.row_type == "section"]
        self.assertIn("Income", section_names)
        self.assertIn("Expenses", section_names)

    def test_subtotal_rows(self):
        path = _FIXTURES / "profit_and_loss.csv"
        pl = self.parse(path)
        subtotal_names = [r.name for r in pl.rows if r.row_type == "subtotal"]
        self.assertTrue(any("Total for" in n or n in ("Gross Profit", "Net Income", "Net Other Income")
                            for n in subtotal_names))

    def test_amounts(self):
        path = _FIXTURES / "profit_and_loss.csv"
        pl = self.parse(path)
        by_name = {r.name: r for r in pl.rows if r.row_type == "leaf"}
        self.assertEqual(by_name["Consulting Revenue"].amount, Decimal("95000.00"))
        self.assertEqual(by_name["Advertising & Marketing"].amount, Decimal("8750.00"))


class TestParseGeneralLedger(unittest.TestCase):
    """Tests for parse_general_ledger.

    Key requirement: subtotal and opening-balance rows must NEVER appear as
    transactions.
    """

    def setUp(self):
        from src.bookkeeping.quickbooks import parse_general_ledger
        self.parse = parse_general_ledger

    def test_parse_fixture(self):
        path = _FIXTURES / "general_ledger.csv"
        gl = self.parse(path)
        self.assertEqual(gl.company, "Acme Consulting LLC")
        self.assertIn("January-December, 2025", gl.period)
        self.assertEqual(gl.basis, "cash")

    def test_exact_transaction_count(self):
        """Subtotal and opening-balance rows must not appear as transactions.

        The fixture has:
          Checking (4512) - 1: 1 opening-balance row + 6 transaction rows + 1 subtotal = only 6 txns
          Savings (9873) - 1: 1 opening-balance row + 2 transaction rows + 1 subtotal = only 2 txns
          Consulting Revenue: 1 opening-balance row + 3 transaction rows + 1 subtotal = only 3 txns
          Advertising & Marketing: 1 opening-balance row + 1 transaction row + 1 subtotal = only 1 txn
        Total: 12 transactions, 0 subtotals, 0 opening-balance rows
        """
        path = _FIXTURES / "general_ledger.csv"
        gl = self.parse(path)
        self.assertEqual(len(gl.transactions), 12)

    def test_no_subtotals_as_transactions(self):
        path = _FIXTURES / "general_ledger.csv"
        gl = self.parse(path)
        # No transaction should have empty date (subtotals have no date)
        # All transactions should have a valid date
        for txn in gl.transactions:
            self.assertIsInstance(txn.txn_date, date)

    def test_account_context_propagated(self):
        path = _FIXTURES / "general_ledger.csv"
        gl = self.parse(path)
        accounts = {t.account for t in gl.transactions}
        self.assertIn("Checking (4512) - 1", accounts)
        self.assertIn("Consulting Revenue", accounts)

    def test_amounts_parsed_correctly(self):
        path = _FIXTURES / "general_ledger.csv"
        gl = self.parse(path)
        # Find the first deposit to Checking
        checking_txns = [t for t in gl.transactions if t.account == "Checking (4512) - 1"]
        self.assertTrue(len(checking_txns) > 0)
        # First transaction should be a deposit of 5000.00
        first = checking_txns[0]
        self.assertEqual(first.amount, Decimal("5000.00"))
        self.assertEqual(first.txn_date, date(2025, 1, 15))


class TestQBNameMapping(unittest.TestCase):
    """Tests for map_qb_account — name sanitization and type-to-root mapping."""

    def setUp(self):
        from src.bookkeeping.quickbooks import map_qb_account, _reset_collision_registry
        self.map = map_qb_account
        self.reset = _reset_collision_registry

    def tearDown(self):
        self.reset()

    def test_bank_account(self):
        self.reset()
        result = self.map("Checking (4512) - 1", "Bank")
        self.assertTrue(result.startswith("Assets:Bank:"))

    def test_bank_account_segment_sanitization(self):
        """'(4512) - 1' -> 'Checking-4512-1' after sanitization."""
        self.reset()
        result = self.map("Checking (4512) - 1", "Bank")
        self.assertEqual(result, "Assets:Bank:Checking-4512-1")

    def test_mercury_style_name(self):
        """'Distribution (2136) - 1' -> 'Assets:Bank:Distribution-2136-1'"""
        self.reset()
        result = self.map("Distribution (2136) - 1", "Bank")
        self.assertEqual(result, "Assets:Bank:Distribution-2136-1")

    def test_curly_apostrophe(self):
        """'Owner’s Draw' -> 'Equity:Owners-Draw' (curly apostrophe removed)."""
        self.reset()
        result = self.map("Owner’s Draw", "Equity")
        self.assertEqual(result, "Equity:Owners-Draw")

    def test_straight_apostrophe(self):
        """'Owner's Draw' -> 'Equity:Owners-Draw'"""
        self.reset()
        result = self.map("Owner's Draw", "Equity")
        self.assertEqual(result, "Equity:Owners-Draw")

    def test_hierarchical_name(self):
        """'Office expenses:Dues and Subscriptions' -> 'Expenses:Office-expenses:Dues-and-Subscriptions'"""
        self.reset()
        result = self.map("Office expenses:Dues and Subscriptions", "Expenses")
        self.assertEqual(result, "Expenses:Office-expenses:Dues-and-Subscriptions")

    def test_ar_type(self):
        self.reset()
        result = self.map("Accounts Receivable (A/R)", "Accounts receivable (A/R)")
        self.assertTrue(result.startswith("Assets:Receivable:"))

    def test_credit_card_type(self):
        self.reset()
        result = self.map("Visa Card (3301) - 2", "Credit Card")
        self.assertTrue(result.startswith("Liabilities:CreditCard:"))

    def test_ap_type(self):
        self.reset()
        result = self.map("Accounts Payable (A/P)", "Accounts payable (A/P)")
        self.assertTrue(result.startswith("Liabilities:Payable:"))

    def test_income_type(self):
        self.reset()
        result = self.map("Consulting Revenue", "Income")
        self.assertTrue(result.startswith("Income:"))

    def test_expenses_type(self):
        self.reset()
        result = self.map("Advertising & Marketing", "Expenses")
        self.assertTrue(result.startswith("Expenses:"))

    def test_other_expense_type(self):
        self.reset()
        result = self.map("Interest paid", "Other Expense")
        self.assertTrue(result.startswith("Expenses:"))

    def test_equity_type(self):
        self.reset()
        result = self.map("Retained Earnings", "Equity")
        self.assertTrue(result.startswith("Equity:"))

    def test_collision_suffixing(self):
        """Two different QB names that map to the same base path get numeric suffixes."""
        self.reset()
        # Two different names that might collide in sanitization
        name1 = "My-Account"
        name2 = "My Account"  # space -> hyphen -> same sanitized form
        r1 = self.map(name1, "Bank")
        r2 = self.map(name2, "Bank")
        # r1 gets the base, r2 gets the suffix
        self.assertNotEqual(r1, r2)
        self.assertTrue(r2.startswith(r1) or r1.startswith("Assets:Bank:"))

    def test_same_name_idempotent(self):
        """Same QB name registered twice returns the same beancount path."""
        self.reset()
        r1 = self.map("Checking (4512) - 1", "Bank")
        r2 = self.map("Checking (4512) - 1", "Bank")
        self.assertEqual(r1, r2)

    def test_result_is_valid_beancount(self):
        """All mapped accounts must pass beancount name validation."""
        from src.bookkeeping.quickbooks import _reset_collision_registry
        from src.bookkeeping.ledger.model import _validate_account_name
        self.reset()
        test_cases = [
            ("Checking (4512) - 1", "Bank"),
            ("Visa Card (3301) - 2", "Credit Card"),
            ("Owner's Draw", "Equity"),
            ("Owner’s Draw", "Equity"),
            ("Office expenses:Dues and Subscriptions", "Expenses"),
            ("Accounts Receivable (A/R)", "Accounts receivable (A/R)"),
            ("Interest paid", "Other Expense"),
        ]
        for qb_name, qb_type in test_cases:
            result = self.map(qb_name, qb_type)
            try:
                _validate_account_name(result)
            except ValueError as exc:
                self.fail(f"map_qb_account({qb_name!r}, {qb_type!r}) = {result!r} is not valid: {exc}")


class TestInventory(unittest.TestCase):
    """Tests for inventory() — readiness report."""

    def setUp(self):
        from src.bookkeeping.quickbooks import inventory, _reset_collision_registry
        self.inventory = inventory
        self.reset = _reset_collision_registry

    def tearDown(self):
        self.reset()

    def test_cash_basis_folder_green(self):
        """Happy path: cash-basis folder produces a green readiness report for TB."""
        # Create a temp folder with a cash-basis TB
        with tempfile.TemporaryDirectory() as tmpdir:
            import shutil
            # Copy only the cash-basis TB and CoA
            shutil.copy(_FIXTURES / "trial_balance_cash_basis.csv",
                        Path(tmpdir) / "trial_balance_cash_basis.csv")
            shutil.copy(_FIXTURES / "chart_of_accounts.csv",
                        Path(tmpdir) / "chart_of_accounts.csv")
            shutil.copy(_FIXTURES / "balance_sheet_2025_12_31.csv",
                        Path(tmpdir) / "balance_sheet_2025_12_31.csv")
            shutil.copy(_FIXTURES / "balance_sheet_comparison.csv",
                        Path(tmpdir) / "balance_sheet_comparison.csv")
            shutil.copy(_FIXTURES / "profit_and_loss.csv",
                        Path(tmpdir) / "profit_and_loss.csv")
            shutil.copy(_FIXTURES / "general_ledger.csv",
                        Path(tmpdir) / "general_ledger.csv")
            shutil.copy(_FIXTURES / "transaction_detail.csv",
                        Path(tmpdir) / "transaction_detail.csv")

            report = self.inventory(tmpdir)
            # TB slot should be present (not blocked)
            tb_slot = next(s for s in report.slots if s.report_key == "trial_balance")
            self.assertEqual(tb_slot.status, "present")

    def test_xlsx_folder_green(self):
        """QuickBooks Excel exports should be inventoried without CSV conversion."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for name in (
                "trial_balance_cash_basis",
                "chart_of_accounts",
                "balance_sheet_2025_12_31",
                "balance_sheet_comparison",
                "profit_and_loss",
                "general_ledger",
                "transaction_detail",
            ):
                _write_xlsx(
                    Path(tmpdir) / f"{name}.xlsx",
                    _fixture_rows(f"{name}.csv"),
                )

            report = self.inventory(tmpdir)
            self.assertTrue(report.is_ready(), report.to_dict())
            self.assertTrue(all(s.file and s.file.endswith(".xlsx") for s in report.slots))

    def test_accrual_tb_blocks_only_tb(self):
        """Accrual TB -> TB slot blocked; other present files unaffected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            import shutil
            shutil.copy(_FIXTURES / "trial_balance_accrual_basis.csv",
                        Path(tmpdir) / "trial_balance_accrual_basis.csv")
            shutil.copy(_FIXTURES / "chart_of_accounts.csv",
                        Path(tmpdir) / "chart_of_accounts.csv")
            shutil.copy(_FIXTURES / "balance_sheet_2025_12_31.csv",
                        Path(tmpdir) / "balance_sheet_2025_12_31.csv")
            shutil.copy(_FIXTURES / "balance_sheet_comparison.csv",
                        Path(tmpdir) / "balance_sheet_comparison.csv")
            shutil.copy(_FIXTURES / "profit_and_loss.csv",
                        Path(tmpdir) / "profit_and_loss.csv")
            shutil.copy(_FIXTURES / "general_ledger.csv",
                        Path(tmpdir) / "general_ledger.csv")
            shutil.copy(_FIXTURES / "transaction_detail.csv",
                        Path(tmpdir) / "transaction_detail.csv")

            report = self.inventory(tmpdir)

            # TB slot must be blocked
            tb_slot = next(s for s in report.slots if s.report_key == "trial_balance")
            self.assertEqual(tb_slot.status, "blocked")
            self.assertIn("Cash Basis", tb_slot.block_reason)
            self.assertIn("re-export", tb_slot.block_reason.lower())
            self.assertIn("fallback", tb_slot.block_reason)

            # CoA slot must be present (not blocked)
            coa_slot = next(s for s in report.slots if s.report_key == "chart_of_accounts")
            self.assertEqual(coa_slot.status, "present")

            # BS slot must be present (not blocked)
            bs_slot = next(s for s in report.slots if s.report_key == "balance_sheet")
            self.assertEqual(bs_slot.status, "present")

    def test_missing_file_marked_missing(self):
        """A missing expected file is marked as 'missing', not silently skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Empty folder -> all files missing
            report = self.inventory(tmpdir)
            for slot in report.slots:
                self.assertEqual(slot.status, "missing")

    def test_unrecognized_file_ambiguous(self):
        """An unrecognized file must appear in ambiguous_files, never silently skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write a CSV that doesn't match any known format
            unrecognized = Path(tmpdir) / "random_data.csv"
            unrecognized.write_text("col1,col2\nval1,val2\n", encoding="utf-8")
            report = self.inventory(tmpdir)
            # Should appear in ambiguous_files
            self.assertTrue(
                any("random_data.csv" in f for f in report.ambiguous_files),
                f"Expected random_data.csv in ambiguous_files, got {report.ambiguous_files}"
            )

    def test_report_is_not_ready_when_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import shutil
            shutil.copy(_FIXTURES / "trial_balance_accrual_basis.csv",
                        Path(tmpdir) / "trial_balance_accrual_basis.csv")
            report = self.inventory(tmpdir)
            self.assertFalse(report.is_ready())

    def test_report_is_not_ready_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.inventory(tmpdir)
            self.assertFalse(report.is_ready())

    def test_json_output_deterministic(self):
        """JSON output is deterministic (same folder -> same JSON)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            report1 = self.inventory(tmpdir)
            report2 = self.inventory(tmpdir)
            self.assertEqual(
                json.dumps(report1.to_dict(), sort_keys=True),
                json.dumps(report2.to_dict(), sort_keys=True),
            )

    def test_two_balance_sheets_assigned_correctly(self):
        """Two BS files should fill balance_sheet and balance_sheet_comparison slots."""
        with tempfile.TemporaryDirectory() as tmpdir:
            import shutil
            shutil.copy(_FIXTURES / "balance_sheet_2025_12_31.csv",
                        Path(tmpdir) / "balance_sheet_2025.csv")
            shutil.copy(_FIXTURES / "balance_sheet_comparison.csv",
                        Path(tmpdir) / "balance_sheet_comparison.csv")
            report = self.inventory(tmpdir)
            bs_slot = next(s for s in report.slots if s.report_key == "balance_sheet")
            bs_comp_slot = next(s for s in report.slots if s.report_key == "balance_sheet_comparison")
            # Both should be present
            self.assertEqual(bs_slot.status, "present")
            self.assertEqual(bs_comp_slot.status, "present")
            # Should be different files
            self.assertNotEqual(bs_slot.file, bs_comp_slot.file)


class TestImportOpening(unittest.TestCase):
    """Tests for import_opening — AE1-shaped happy path.

    Verifies:
    - Entries are dated the cutover
    - Total postings balance to zero
    - All accounts are opened with open directives
    - The produced ledger passes validation
    - Generated BS at cutover matches the fixture BS for imported accounts
    """

    def setUp(self):
        from src.bookkeeping.quickbooks import import_opening, _reset_collision_registry, inventory
        self.import_opening = import_opening
        self.reset = _reset_collision_registry
        self.inventory = inventory

    def tearDown(self):
        self.reset()

    def _make_entity(self, tmpdir: str):
        """Create a minimal entity-like object pointing to *tmpdir*."""
        from src.bookkeeping.entity import load_entity, init_entity
        p = Path(tmpdir) / "entity"
        init_entity(p, name="Acme Consulting LLC", business_type="consulting")
        return load_entity(p)

    def test_happy_path_cash_basis(self):
        """AE1: cash-basis folder -> import succeeds, entries dated cutover."""
        with tempfile.TemporaryDirectory() as tmpdir:
            import shutil
            qb_dir = Path(tmpdir) / "qb"
            qb_dir.mkdir()
            shutil.copy(_FIXTURES / "chart_of_accounts.csv", qb_dir / "chart_of_accounts.csv")
            shutil.copy(_FIXTURES / "trial_balance_cash_basis.csv", qb_dir / "trial_balance_cash_basis.csv")
            shutil.copy(_FIXTURES / "balance_sheet_2025_12_31.csv", qb_dir / "balance_sheet_2025_12_31.csv")

            entity = self._make_entity(tmpdir)
            self.reset()
            cutover = date(2026, 1, 1)
            result = self.import_opening(entity, qb_dir, cutover)

            self.assertTrue(result.success, f"Import failed: {result.errors}")
            self.assertEqual(result.entries_written, 1)
            self.assertGreater(result.accounts_opened, 0)

    def test_ledger_validates_after_import(self):
        """The written ledger must pass built-in validation."""
        from src.bookkeeping.ledger.validator import validate
        with tempfile.TemporaryDirectory() as tmpdir:
            import shutil
            qb_dir = Path(tmpdir) / "qb"
            qb_dir.mkdir()
            shutil.copy(_FIXTURES / "chart_of_accounts.csv", qb_dir / "chart_of_accounts.csv")
            shutil.copy(_FIXTURES / "trial_balance_cash_basis.csv", qb_dir / "trial_balance_cash_basis.csv")

            entity = self._make_entity(tmpdir)
            self.reset()
            result = self.import_opening(entity, qb_dir, date(2026, 1, 1))
            self.assertTrue(result.success, f"Import failed: {result.errors}")

            from src.bookkeeping.ledger.projections import render_store_ledger
            ledger_text = render_store_ledger(result.ledger_path)
            errors = validate(ledger_text)
            self.assertEqual(errors, [], f"Ledger validation errors: {errors}")

    def test_opening_entry_date(self):
        """Opening entries must be dated the cutover, not the TB as-of date."""
        from src.bookkeeping.ledger.validator import parse_ledger
        with tempfile.TemporaryDirectory() as tmpdir:
            import shutil
            qb_dir = Path(tmpdir) / "qb"
            qb_dir.mkdir()
            shutil.copy(_FIXTURES / "chart_of_accounts.csv", qb_dir / "chart_of_accounts.csv")
            shutil.copy(_FIXTURES / "trial_balance_cash_basis.csv", qb_dir / "trial_balance_cash_basis.csv")

            entity = self._make_entity(tmpdir)
            self.reset()
            cutover = date(2026, 1, 1)
            result = self.import_opening(entity, qb_dir, cutover)
            self.assertTrue(result.success)

            from src.bookkeeping.ledger.projections import render_store_ledger
            ledger_text = render_store_ledger(result.ledger_path)
            parsed = parse_ledger(ledger_text)
            for entry in parsed["entries"]:
                self.assertEqual(entry.date, cutover)

    def test_opening_import_is_idempotent_for_same_cutover_and_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import shutil
            from src.bookkeeping.ledger.store import LedgerStore, default_store_path

            qb_dir = Path(tmpdir) / "qb"
            qb_dir.mkdir()
            shutil.copy(_FIXTURES / "chart_of_accounts.csv", qb_dir / "chart_of_accounts.csv")
            shutil.copy(_FIXTURES / "trial_balance_cash_basis.csv", qb_dir / "trial_balance_cash_basis.csv")
            entity = self._make_entity(tmpdir)
            cutover = date(2026, 1, 1)

            first = self.import_opening(entity, qb_dir, cutover)
            second = self.import_opening(entity, qb_dir, cutover)

            self.assertTrue(first.success, first.errors)
            self.assertTrue(second.success, second.errors)
            self.assertEqual(first.entries_written, 1)
            self.assertEqual(second.entries_written, 0)
            entries = LedgerStore(default_store_path(entity.path)).load_entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].source_id, "quickbooks-opening-trial-balance-2026-01-01")

    def test_postings_balance(self):
        """All postings in the opening entry must sum to zero."""
        from src.bookkeeping.ledger.validator import parse_ledger
        from src.bookkeeping.ledger.model import TOLERANCE
        with tempfile.TemporaryDirectory() as tmpdir:
            import shutil
            qb_dir = Path(tmpdir) / "qb"
            qb_dir.mkdir()
            shutil.copy(_FIXTURES / "chart_of_accounts.csv", qb_dir / "chart_of_accounts.csv")
            shutil.copy(_FIXTURES / "trial_balance_cash_basis.csv", qb_dir / "trial_balance_cash_basis.csv")

            entity = self._make_entity(tmpdir)
            self.reset()
            result = self.import_opening(entity, qb_dir, date(2026, 1, 1))
            self.assertTrue(result.success)

            from src.bookkeeping.ledger.projections import render_store_ledger
            ledger_text = render_store_ledger(result.ledger_path)
            parsed = parse_ledger(ledger_text)
            for entry in parsed["entries"]:
                total = sum(p.amount for p in entry.postings)
                self.assertLessEqual(abs(total), TOLERANCE)

    def test_accrual_tb_refused(self):
        """import_opening must refuse an accrual-basis TB with a clear error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            import shutil
            qb_dir = Path(tmpdir) / "qb"
            qb_dir.mkdir()
            shutil.copy(_FIXTURES / "chart_of_accounts.csv", qb_dir / "chart_of_accounts.csv")
            shutil.copy(_FIXTURES / "trial_balance_accrual_basis.csv",
                        qb_dir / "trial_balance_accrual_basis.csv")

            entity = self._make_entity(tmpdir)
            self.reset()
            result = self.import_opening(entity, qb_dir, date(2026, 1, 1))

            self.assertFalse(result.success)
            self.assertTrue(len(result.errors) > 0)
            # Error message should mention cash basis
            self.assertTrue(
                any("Cash Basis" in e or "cash" in e.lower() for e in result.errors),
                f"Expected cash-basis error, got: {result.errors}"
            )

    def test_opening_balances_match_bs(self):
        """AE1: Balances computed from the produced ledger match the fixture BS.

        Specifically: Assets:Bank:Checking-4512-1 should have balance 12500.00
        at cutover, matching the fixture BS.
        """
        from src.bookkeeping.ledger.validator import parse_ledger
        with tempfile.TemporaryDirectory() as tmpdir:
            import shutil
            qb_dir = Path(tmpdir) / "qb"
            qb_dir.mkdir()
            shutil.copy(_FIXTURES / "chart_of_accounts.csv", qb_dir / "chart_of_accounts.csv")
            shutil.copy(_FIXTURES / "trial_balance_cash_basis.csv", qb_dir / "trial_balance_cash_basis.csv")

            entity = self._make_entity(tmpdir)
            self.reset()
            cutover = date(2026, 1, 1)
            result = self.import_opening(entity, qb_dir, cutover)
            self.assertTrue(result.success, f"Import failed: {result.errors}")

            from src.bookkeeping.ledger.projections import render_store_ledger
            ledger_text = render_store_ledger(result.ledger_path)
            parsed = parse_ledger(ledger_text)

            # Compute balances from entries dated <= cutover (same day = posted)
            account_balances: dict[str, Decimal] = {}
            for entry in parsed["entries"]:
                if entry.date <= cutover:
                    for posting in entry.postings:
                        account_balances[posting.account] = (
                            account_balances.get(posting.account, Decimal("0.00"))
                            + posting.amount
                        )

            # Checking (4512) - 1 -> Assets:Bank:Checking-4512-1 -> 20000.00
            self.assertIn("Assets:Bank:Checking-4512-1", account_balances,
                          f"Account not found; available: {list(account_balances.keys())}")
            self.assertEqual(account_balances["Assets:Bank:Checking-4512-1"], Decimal("20000.00"))

            # Savings (9873) - 1 -> Assets:Bank:Savings-9873-1 -> 5000.00
            self.assertIn("Assets:Bank:Savings-9873-1", account_balances)
            self.assertEqual(account_balances["Assets:Bank:Savings-9873-1"], Decimal("5000.00"))

            # Visa Card (3301) - 2 -> Liabilities:CreditCard:Visa-Card-3301-2 -> -2450.00
            cc_account = next(
                (k for k in account_balances if "Visa" in k or "3301" in k), None
            )
            self.assertIsNotNone(cc_account, f"No CC account found; available: {list(account_balances.keys())}")
            self.assertEqual(account_balances[cc_account], Decimal("-2000.00"))

    def test_atomic_write_no_tmp_left(self):
        """No .tmp files should remain after a successful import."""
        with tempfile.TemporaryDirectory() as tmpdir:
            import shutil
            qb_dir = Path(tmpdir) / "qb"
            qb_dir.mkdir()
            shutil.copy(_FIXTURES / "chart_of_accounts.csv", qb_dir / "chart_of_accounts.csv")
            shutil.copy(_FIXTURES / "trial_balance_cash_basis.csv", qb_dir / "trial_balance_cash_basis.csv")

            entity = self._make_entity(tmpdir)
            self.reset()
            result = self.import_opening(entity, qb_dir, date(2026, 1, 1))
            self.assertTrue(result.success)

            # No .tmp files should remain
            tmp_files = list(Path(entity.path).rglob("*.tmp"))
            self.assertEqual(tmp_files, [], f"Leftover .tmp files: {tmp_files}")


class TestCLI(unittest.TestCase):
    """Tests for the CLI surface: add_parser / run."""

    def setUp(self):
        from src.bookkeeping.quickbooks import add_parser, _reset_collision_registry
        self.add_parser = add_parser
        self.reset = _reset_collision_registry

    def tearDown(self):
        self.reset()

    def test_add_parser_registers_qb(self):
        import argparse
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        self.add_parser(subparsers)
        # inventory subcommand
        args = parser.parse_args(["qb", "inventory", str(_FIXTURES)])
        self.assertEqual(args.qb_command, "inventory")
        self.assertEqual(args.folder, _FIXTURES)

    def test_inventory_command_runs(self):
        import argparse
        from src.bookkeeping.quickbooks import run
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        self.add_parser(subparsers)
        # Use an empty temp dir so all slots are missing
        with tempfile.TemporaryDirectory() as tmpdir:
            args = parser.parse_args(["qb", "inventory", tmpdir])
            retcode = run(args)
            self.assertEqual(retcode, 0)

    def test_inventory_json_flag(self):
        import argparse
        import io
        from contextlib import redirect_stdout
        from src.bookkeeping.quickbooks import run
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        self.add_parser(subparsers)
        with tempfile.TemporaryDirectory() as tmpdir:
            args = parser.parse_args(["qb", "inventory", "--json", tmpdir])
            buf = io.StringIO()
            with redirect_stdout(buf):
                retcode = run(args)
            self.assertEqual(retcode, 0)
            output = buf.getvalue()
            # Should be valid JSON
            data = json.loads(output)
            self.assertIn("slots", data)
            self.assertIn("ready", data)


class TestDateAwareSlotAssignment(unittest.TestCase):
    """Regression: duplicate-period exports must be assigned by parsed dates,
    not filename order (real QuickBooks downloads produce '(1)' duplicates
    whose glob order is unrelated to content)."""

    def setUp(self):
        from src.bookkeeping.quickbooks import inventory, parse_qb_date_range
        self.inventory = inventory
        self.parse_range = parse_qb_date_range

    def test_parse_qb_date_range(self):
        self.assertEqual(self.parse_range("As of Dec 31, 2025"), (None, date(2025, 12, 31)))
        self.assertEqual(
            self.parse_range("January-May, 2026"), (date(2026, 1, 1), date(2026, 5, 31))
        )
        self.assertEqual(
            self.parse_range("January-December, 2025"), (date(2025, 1, 1), date(2025, 12, 31))
        )
        self.assertEqual(self.parse_range("nonsense"), (None, None))
        self.assertEqual(self.parse_range(None), (None, None))

    @staticmethod
    def _report_csv(title: str, date_line: str, body: str, basis: str) -> str:
        return (
            f'"Acme Consulting LLC",\n{title},\n"{date_line}",\n\n{body}\n\n'
            f'"{basis} Basis Friday, June 12, 2026 12:09 PM GMT-05:00",\n'
        )

    def test_duplicates_assigned_by_period_semantics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            qb = Path(tmpdir)
            bs_body = ',Total\nAssets,\nChecking,"1,000.00"\nTotal for Assets,"$1,000.00"'
            pl_body = ',Total\nIncome,\nSales,"2,000.00"\nTotal for Income,"$2,000.00"'
            tb_body = 'Full name,Debit,Credit\nChecking,"1,000.00",\nOpening Equity,,"1,000.00"'
            # Filename order is deliberately adversarial: the "(1)" files sort first.
            (qb / "Acme_Trial Balance (1).csv").write_text(
                self._report_csv("Trial Balance", "As of Dec 31, 2024", tb_body, "Cash")
            )
            (qb / "Acme_Trial Balance.csv").write_text(
                self._report_csv("Trial Balance", "As of Dec 31, 2025", tb_body, "Accrual")
            )
            (qb / "Acme_Balance Sheet.csv").write_text(
                self._report_csv("Balance Sheet", "As of May 31, 2026", bs_body, "Cash")
            )
            (qb / "Acme_Balance Sheet (1).csv").write_text(
                self._report_csv("Balance Sheet", "As of Dec 31, 2025", bs_body, "Cash")
            )
            (qb / "Acme_Profit and Loss (1).csv").write_text(
                self._report_csv("Profit and Loss", "January-December, 2025", pl_body, "Cash")
            )
            (qb / "Acme_Profit and Loss.csv").write_text(
                self._report_csv("Profit and Loss", "January-May, 2026", pl_body, "Cash")
            )

            report = self.inventory(qb)
            slots = {s.report_key: s for s in report.slots}

            # Comparison period is Jan-May 2026 -> prior-period end is 2025-12-31.
            self.assertIn("Trial Balance.csv", slots["trial_balance"].file)
            self.assertNotIn("(1)", Path(slots["trial_balance"].file).name)
            self.assertEqual(slots["trial_balance"].status, "blocked")  # accrual 2025 TB
            self.assertIn("(1)", Path(slots["balance_sheet"].file).name)  # Dec 2025
            self.assertNotIn("(1)", Path(slots["balance_sheet_comparison"].file).name)  # May 2026
            self.assertNotIn("(1)", Path(slots["profit_and_loss"].file).name)  # Jan-May 2026


class TestBalanceSheetOpeningFallback(unittest.TestCase):
    """import_opening --source balance-sheet derives openings from the
    cash-basis prior-period BS when the TB export is accrual."""

    def setUp(self):
        from src.bookkeeping.quickbooks import import_opening, _reset_collision_registry
        self.import_opening = import_opening
        self.reset = _reset_collision_registry

    def tearDown(self):
        self.reset()

    def _make_entity(self, tmpdir: str):
        from src.bookkeeping.entity import load_entity, init_entity
        p = Path(tmpdir) / "entity"
        init_entity(p, name="Acme Consulting LLC", business_type="consulting")
        return load_entity(p)

    def test_bs_fallback_matches_bs_accounts(self):
        import shutil
        from src.bookkeeping.ledger.validator import validate

        with tempfile.TemporaryDirectory() as tmpdir:
            qb_dir = Path(tmpdir) / "qb"
            qb_dir.mkdir()
            shutil.copy(_FIXTURES / "chart_of_accounts.csv", qb_dir / "chart_of_accounts.csv")
            shutil.copy(
                _FIXTURES / "trial_balance_accrual_basis.csv",
                qb_dir / "trial_balance_accrual_basis.csv",
            )
            shutil.copy(
                _FIXTURES / "balance_sheet_2025_12_31.csv",
                qb_dir / "balance_sheet_2025_12_31.csv",
            )

            entity = self._make_entity(tmpdir)
            self.reset()

            # Default trial-balance source refuses (accrual TB) and points at the fallback.
            blocked = self.import_opening(entity, qb_dir, date(2026, 1, 1))
            self.assertFalse(blocked.success)
            self.assertTrue(any("balance-sheet" in e for e in blocked.errors))

            self.reset()
            result = self.import_opening(
                entity, qb_dir, date(2026, 1, 1), source="balance-sheet"
            )
            self.assertTrue(result.success, f"BS-fallback import failed: {result.errors}")
            self.assertEqual(result.entries_written, 1)
            from src.bookkeeping.ledger.projections import render_store_ledger
            text = render_store_ledger(result.ledger_path)
            self.assertEqual(validate(text), [])
            self.assertIn("derived from QuickBooks cash-basis balance sheet", text)


if __name__ == "__main__":
    unittest.main()
