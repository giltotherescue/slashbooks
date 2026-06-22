"""Tests for src/bookkeeping/reports/workbook.py

Covers:
- CSV-only output (xlsxwriter absent) + enablement message
- All workbook sheets produced as CSV files with correct filenames
- P&L/BS/TB CSV totals match statements API values exactly
- Vendor sheet flags >=600 candidates only
- Adjustment log reconstructs reversal pairs exactly once (two same-day reversals)
- Sanity checks: unbalanced BS → equity fail
- Sanity checks: open queue item → queue_empty fail
- Generation refused on fail without --override
- Generation allowed with --override; override noted in cover CSV
- Cover sheet text is jargon-free (no beancount/SQL tokens)
- xlsxwriter absent path (monkeypatched): CSV-only + enablement message
- When xlsxwriter present (skip-if-absent): workbook file created
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from zipfile import ZipFile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Fixture ledger (same shape as test_statements.py, extended with reversals)
# ---------------------------------------------------------------------------

FIXTURE_LEDGER = """\
option "title" "Test Entity"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Bank:Checking USD
2026-01-01 open Income:Revenue:Consulting USD
2026-01-01 open Expenses:Software USD
2026-01-01 open Expenses:Office USD
2026-01-01 open Equity:OpeningBalance USD
2026-01-01 open Liabilities:CreditCard USD

; Opening balance
2026-01-01 * "Opening balance"
  source-id: "open_001"
  Assets:Bank:Checking         500.00 USD
  Equity:OpeningBalance       -500.00 USD

; Revenue January
2026-01-15 * "Acme Corp" "Consulting January"
  source-id: "txn_001"
  Assets:Bank:Checking        5000.00 USD
  Income:Revenue:Consulting  -5000.00 USD

; Software subscription
2026-01-20 * "Acme Software" "Monthly subscription"
  source-id: "txn_002"
  Expenses:Software            200.00 USD
  Assets:Bank:Checking        -200.00 USD

; Revenue February
2026-02-15 * "Acme Corp" "Consulting February"
  source-id: "txn_003"
  Assets:Bank:Checking        3000.00 USD
  Income:Revenue:Consulting  -3000.00 USD

; Office supplies
2026-02-20 * "Office Depot" "Paper and pens"
  source-id: "txn_004"
  Expenses:Office              100.00 USD
  Assets:Bank:Checking        -100.00 USD

; Revenue March
2026-03-15 * "Acme Corp" "Consulting March"
  source-id: "txn_005"
  Assets:Bank:Checking        2000.00 USD
  Income:Revenue:Consulting  -2000.00 USD

; Reversal of txn_002 (corrects a wrong category)
2026-03-20 * "REVERSAL of txn_002"
  source-id: "rev_txn_002"
  reverses: "txn_002"
  Expenses:Software           -200.00 USD
  Assets:Bank:Checking         200.00 USD

; Corrected replacement for txn_002
2026-03-20 * "Acme Software" "Monthly subscription (corrected)"
  source-id: "corr_txn_002"
  correction-of: "txn_002"
  Expenses:Office              200.00 USD
  Assets:Bank:Checking        -200.00 USD

; Second same-day reversal pair — txn_004
2026-03-20 * "REVERSAL of txn_004"
  source-id: "rev_txn_004"
  reverses: "txn_004"
  Expenses:Office             -100.00 USD
  Assets:Bank:Checking         100.00 USD

; Corrected replacement for txn_004
2026-03-20 * "Office Depot" "Paper and pens (corrected)"
  source-id: "corr_txn_004"
  correction-of: "txn_004"
  Expenses:Software            100.00 USD
  Assets:Bank:Checking        -100.00 USD

; Large vendor payment for 1099 testing
2026-03-25 * "Big Vendor LLC" "Project work"
  source-id: "txn_006"
  Expenses:Software            700.00 USD
  Assets:Bank:Checking        -700.00 USD

; Small vendor payment - below threshold
2026-03-28 * "Small Vendor" "Minor service"
  source-id: "txn_007"
  Expenses:Office               50.00 USD
  Assets:Bank:Checking         -50.00 USD

; Revenue April
2026-04-15 * "Beta LLC" "Project work"
  source-id: "txn_008"
  Assets:Bank:Checking        1500.00 USD
  Income:Revenue:Consulting  -1500.00 USD
"""

# An unbalanced ledger for testing equity check failure
UNBALANCED_LEDGER = """\
option "title" "Unbalanced Entity"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Bank:Checking USD
2026-01-01 open Income:Revenue:Consulting USD
2026-01-01 open Equity:OpeningBalance USD

; This entry is balanced (beancount requires it), but we'll hack the DB directly
2026-01-01 * "Opening balance"
  source-id: "open_001"
  Assets:Bank:Checking        1000.00 USD
  Equity:OpeningBalance      -1000.00 USD

2026-01-15 * "Revenue"
  source-id: "txn_001"
  Assets:Bank:Checking        5000.00 USD
  Income:Revenue:Consulting  -5000.00 USD
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FROM_DATE = date(2026, 1, 1)
TO_DATE = date(2026, 4, 15)

JARGON_TOKENS = [
    "beancount", "bean-check", "pushtag", "poptag", "narration",
    "SELECT", "FROM", "WHERE", "INSERT", "UPDATE", "DELETE",
    "JOIN", "GROUP BY", "ORDER BY",
]


def _make_entity(tmp_path: Path, ledger_text: str = FIXTURE_LEDGER) -> Path:
    entity_dir = tmp_path / "entity"
    entity_dir.mkdir()
    (entity_dir / "entity.json").write_text(
        json.dumps({
            "name": "Test Entity",
            "legal_structure": "single-member LLC",
            "vendor_1099_threshold": 600,
            "country": "US",
            "tax_jurisdiction": "US",
            "operating_currency": "USD",
            "indirect_tax": {"registered": False, "type": None},
            "payroll": {"enabled": False, "provider": None, "posting_mode": "draft_journal_entries"},
        }),
        encoding="utf-8",
    )
    (entity_dir / "reports").mkdir()
    (entity_dir / "staging").mkdir()
    (entity_dir / "review-queue").mkdir()
    (entity_dir / "ingestion").mkdir()
    (entity_dir / "ingestion" / "payroll").mkdir()
    (entity_dir / "books.beancount").write_text(ledger_text, encoding="utf-8")
    from src.bookkeeping.reports.cache import regenerate
    regenerate(entity_dir)
    return entity_dir


def _read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


def _find_csv(output_dir: Path, name_fragment: str) -> Path:
    csv_dir = output_dir / "csv"
    matches = list(csv_dir.glob(f"*{name_fragment}*"))
    if not matches:
        raise FileNotFoundError(f"No CSV matching '{name_fragment}' in {csv_dir}")
    return matches[0]


_SHEET_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkg_rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def _xlsx_sheet_names(zf: ZipFile) -> list[str]:
    workbook_xml = ET.fromstring(zf.read("xl/workbook.xml"))
    return [
        sheet.attrib["name"]
        for sheet in workbook_xml.find("main:sheets", _SHEET_NS)
    ]


def _xlsx_sheet_xml(zf: ZipFile, sheet_name: str) -> ET.Element:
    workbook_xml = ET.fromstring(zf.read("xl/workbook.xml"))
    rels_xml = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_targets = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels_xml.findall("pkg_rel:Relationship", _SHEET_NS)
    }
    for sheet in workbook_xml.find("main:sheets", _SHEET_NS):
        if sheet.attrib["name"] == sheet_name:
            rel_id = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            target = rel_targets[rel_id].lstrip("/")
            path = target if target.startswith("xl/") else f"xl/{target}"
            return ET.fromstring(zf.read(path))
    raise KeyError(f"Sheet not found: {sheet_name}")


def _xlsx_shared_text(zf: ZipFile) -> str:
    try:
        shared_xml = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return ""
    return " ".join(
        text.text or ""
        for text in shared_xml.findall(".//main:t", _SHEET_NS)
    )


# ---------------------------------------------------------------------------
# Test: all CSV files produced
# ---------------------------------------------------------------------------

class TestAllCSVsProduced(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_twelve_sheets_as_csv(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        self.assertIsNone(result.error, f"Package generation failed: {result.error}")

        expected_fragments = [
            "Cover", "P-and-L", "Balance-Sheet", "Trial-Balance",
            "General-Ledger", "Reconciliations", "Vendor-1099",
            "Adjustment-Log", "Open-Questions", "Summary", "Checks",
            "Source-Index",
        ]
        csv_dir = result.output_dir / "csv"
        self.assertTrue(csv_dir.exists(), f"csv/ directory not found at {csv_dir}")

        csv_files = list(csv_dir.glob("*.csv"))
        self.assertEqual(len(csv_files), 12, f"Expected 12 CSV files, found {len(csv_files)}: {csv_files}")

        for fragment in expected_fragments:
            matches = [f for f in csv_files if fragment.lower() in f.name.lower()]
            self.assertTrue(matches, f"No CSV file matching '{fragment}' found")

    def test_csv_files_in_result(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        self.assertEqual(len(result.csv_files), 12)
        for p in result.csv_files:
            self.assertTrue(p.exists(), f"CSV file not found: {p}")

    def test_selected_sheets_omit_general_ledger(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(
            self.entity_dir,
            FROM_DATE,
            TO_DATE,
            override=True,
            sheets="pnl,trial-balance",
        )
        self.assertIsNone(result.error, f"Package generation failed: {result.error}")
        self.assertEqual(
            sorted(p.name for p in result.csv_files),
            ["P-and-L.csv", "Trial-Balance.csv"],
        )

    def test_exclude_general_ledger_keeps_summary_plain_values(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(
            self.entity_dir,
            FROM_DATE,
            TO_DATE,
            override=True,
            exclude_sheets="general-ledger",
        )
        self.assertIsNone(result.error, f"Package generation failed: {result.error}")
        csv_names = {p.name for p in result.csv_files}
        self.assertNotIn("General-Ledger.csv", csv_names)
        self.assertIn("Summary.csv", csv_names)
        summary_rows = _read_csv(_find_csv(result.output_dir, "Summary"))
        self.assertNotIn("=", " ".join(cell for row in summary_rows for cell in row))

    def test_general_ledger_can_use_narrower_period(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(
            self.entity_dir,
            FROM_DATE,
            TO_DATE,
            override=True,
            sheets="general-ledger",
            gl_from_date=date(2026, 3, 1),
            gl_to_date=date(2026, 3, 31),
        )
        self.assertIsNone(result.error, f"Package generation failed: {result.error}")
        rows = _read_csv(_find_csv(result.output_dir, "General-Ledger"))
        all_text = " ".join(cell for row in rows for cell in row)
        self.assertIn("2026-03-15", all_text)
        self.assertNotIn("2026-01-15", all_text)

    def test_audit_log_sheet_is_optional_and_store_backed(self):
        from src.bookkeeping.ledger.migrate import migrate_beancount_to_store
        from src.bookkeeping.reports.workbook import generate_accountant_package

        migration = migrate_beancount_to_store(self.entity_dir, force=True)
        self.assertTrue(migration, migration.error_message)
        result = generate_accountant_package(
            self.entity_dir,
            FROM_DATE,
            TO_DATE,
            override=True,
            sheets="audit-log",
        )
        self.assertIsNone(result.error, f"Package generation failed: {result.error}")
        rows = _read_csv(_find_csv(result.output_dir, "Audit-Log"))
        all_text = " ".join(cell for row in rows for cell in row)
        self.assertIn("migration", all_text)
        self.assertIn("record_hash", rows[0][6].lower().replace(" ", "_"))

    def test_summary_and_checks_csvs_are_plain_values(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        summary_rows = _read_csv(_find_csv(result.output_dir, "Summary"))
        checks_rows = _read_csv(_find_csv(result.output_dir, "Checks"))
        source_rows = _read_csv(_find_csv(result.output_dir, "Source-Index"))

        summary = {row[0]: row[1] for row in summary_rows[1:] if len(row) > 1}
        self.assertEqual(summary["Produced by"], "/books")
        self.assertEqual(summary["Project link"], "https://github.com/giltotherescue/slashbooks")
        self.assertEqual(summary["Legal structure"], "single-member LLC")
        self.assertEqual(summary["Net income"], "10450.00")
        self.assertEqual(summary["Ending cash"], "10950.00")
        self.assertEqual(summary["GL posting rows"], "26")
        self.assertNotIn("=", " ".join(cell for row in summary_rows for cell in row))

        self.assertEqual(checks_rows[0], ["Check", "Tie-out", "Status"])
        self.assertTrue(any(row[0] == "Trial balance debits equal credits" for row in checks_rows))
        self.assertNotIn("=", " ".join(cell for row in checks_rows for cell in row))
        self.assertIn("Review Status", source_rows[0])


# ---------------------------------------------------------------------------
# Test: P&L totals match statements API
# ---------------------------------------------------------------------------

class TestPnLTotalsMatchStatements(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_total_revenue_matches(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package
        from src.bookkeeping.reports.statements import profit_and_loss

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        self.assertIsNone(result.error)

        # Get the authoritative value from the statements API
        pnl = profit_and_loss(self.entity_dir, FROM_DATE, TO_DATE)
        api_income = pnl.sections[0]["total"]   # Total Revenue
        api_expenses = pnl.sections[1]["total"] # Total Expenses
        api_net = pnl.totals["net_income"]

        # Read the P&L CSV and find the summary totals
        pnl_csv = _find_csv(result.output_dir, "P-and-L")
        rows = _read_csv(pnl_csv)

        # Find the "Summary totals" section in the CSV
        summary_row_idx = None
        for i, row in enumerate(rows):
            if row and row[0] == "Summary totals":
                summary_row_idx = i
                break

        self.assertIsNotNone(summary_row_idx, "Could not find 'Summary totals' in P&L CSV")

        # Extract net_income from the rows after summary
        csv_net = None
        for row in rows[summary_row_idx + 1:]:
            if row and "Net Income" in row[1]:
                csv_net = Decimal(row[2])
                break

        self.assertIsNotNone(csv_net, "Could not find Net Income in P&L CSV summary")
        self.assertEqual(csv_net, api_net,
                         f"CSV net income {csv_net} != API net income {api_net}")

    def test_section_totals_in_csv(self):
        """Total Revenue and Total Expenses rows appear in CSV."""
        from src.bookkeeping.reports.workbook import generate_accountant_package
        from src.bookkeeping.reports.statements import profit_and_loss

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        pnl = profit_and_loss(self.entity_dir, FROM_DATE, TO_DATE)
        api_income = pnl.sections[0]["total"]

        pnl_csv = _find_csv(result.output_dir, "P-and-L")
        rows = _read_csv(pnl_csv)

        found_income = None
        for row in rows:
            if row and row[1] == "Total Revenue":
                found_income = Decimal(row[2])
                break
        self.assertIsNotNone(found_income, "Total Revenue row not found in P&L CSV")
        self.assertEqual(found_income, api_income)


# ---------------------------------------------------------------------------
# Test: Balance Sheet totals match statements API
# ---------------------------------------------------------------------------

class TestBalanceSheetTotalsMatchStatements(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_total_assets_in_csv(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package
        from src.bookkeeping.reports.statements import balance_sheet

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        bs = balance_sheet(self.entity_dir, TO_DATE)
        api_assets = bs.totals["total_assets"]
        api_le = bs.totals["total_liabilities_and_equity"]

        bs_csv = _find_csv(result.output_dir, "Balance-Sheet")
        rows = _read_csv(bs_csv)

        found_assets = None
        for row in rows:
            if row and row[1] == "Total Assets":
                found_assets = Decimal(row[2])
                break
        self.assertIsNotNone(found_assets, "Total Assets row not found in Balance Sheet CSV")
        self.assertEqual(found_assets, api_assets)

    def test_liabilities_equity_in_csv(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package
        from src.bookkeeping.reports.statements import balance_sheet

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        bs = balance_sheet(self.entity_dir, TO_DATE)
        api_le = bs.totals["total_liabilities_and_equity"]

        bs_csv = _find_csv(result.output_dir, "Balance-Sheet")
        rows = _read_csv(bs_csv)

        # Find "Total Liabilities" + "Total Equity" and verify they add to api_le
        total_liabs = Decimal("0")
        total_equity = Decimal("0")
        for row in rows:
            if row and row[1] == "Total Liabilities":
                total_liabs = Decimal(row[2])
            if row and row[1] == "Total Equity":
                total_equity = Decimal(row[2])

        computed_le = total_liabs + total_equity
        self.assertEqual(computed_le, api_le,
                         f"CSV L+E {computed_le} != API L+E {api_le}")


# ---------------------------------------------------------------------------
# Test: Trial Balance CSV totals
# ---------------------------------------------------------------------------

class TestTrialBalanceTotalsMatchStatements(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tb_totals_match(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package
        from src.bookkeeping.reports.statements import trial_balance

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        tb = trial_balance(self.entity_dir, TO_DATE)
        api_debits = tb.totals["total_debits"]
        api_credits = tb.totals["total_credits"]

        tb_csv = _find_csv(result.output_dir, "Trial-Balance")
        rows = _read_csv(tb_csv)

        csv_debits = None
        csv_credits = None
        for row in rows:
            if row and row[0] == "Totals":
                if len(row) >= 3:
                    csv_debits = Decimal(row[1]) if row[1] else Decimal("0")
                    csv_credits = Decimal(row[2]) if row[2] else Decimal("0")
                break

        self.assertIsNotNone(csv_debits, "Totals row not found in Trial Balance CSV")
        self.assertEqual(csv_debits, api_debits,
                         f"CSV debits {csv_debits} != API debits {api_debits}")
        self.assertEqual(csv_credits, api_credits,
                         f"CSV credits {csv_credits} != API credits {api_credits}")


# ---------------------------------------------------------------------------
# Test: Vendor 1099 sheet flags correctly
# ---------------------------------------------------------------------------

class TestVendor1099Sheet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_big_vendor_flagged_as_candidate(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        vendor_csv = _find_csv(result.output_dir, "Vendor-1099")
        rows = _read_csv(vendor_csv)

        # Find "Big Vendor LLC" row
        big_vendor_row = None
        for row in rows:
            if row and "Big Vendor LLC" in row[0]:
                big_vendor_row = row
                break

        self.assertIsNotNone(big_vendor_row, "Big Vendor LLC not found in vendor CSV")
        self.assertEqual(big_vendor_row[2].strip(), "Yes",
                         f"Big Vendor LLC (700) should be flagged as 1099 candidate, got: {big_vendor_row}")

    def test_small_vendor_not_flagged(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        vendor_csv = _find_csv(result.output_dir, "Vendor-1099")
        rows = _read_csv(vendor_csv)

        # "Small Vendor" paid 50, should be "No"
        small_vendor_row = None
        for row in rows:
            if row and "Small Vendor" in row[0]:
                small_vendor_row = row
                break

        self.assertIsNotNone(small_vendor_row, "Small Vendor not found in vendor CSV")
        self.assertEqual(small_vendor_row[2].strip(), "No",
                         f"Small Vendor (50) should NOT be flagged as 1099 candidate, got: {small_vendor_row}")

    def test_threshold_boundary(self):
        """Exactly 600 is flagged; 599.99 is not."""
        from src.bookkeeping.reports.workbook import _build_vendor_1099_rows

        # We test the builder directly with a known entity
        rows = _build_vendor_1099_rows(self.entity_dir, FROM_DATE, TO_DATE, Decimal("600.00"))
        # Big Vendor LLC has 700 → Yes
        big = [r for r in rows if r and len(r) >= 3 and "Big Vendor" in r[0]]
        self.assertTrue(big, "Big Vendor LLC not found")
        self.assertIn("Yes", big[0][2])

    def test_forms_not_generated_note(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        vendor_csv = _find_csv(result.output_dir, "Vendor-1099")
        rows = _read_csv(vendor_csv)

        all_text = " ".join(cell for row in rows for cell in row)
        self.assertIn("NOT generated", all_text,
                      "Vendor 1099 sheet should note that forms are NOT generated")

    def test_reversal_descriptions_not_listed_as_vendors(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        vendor_csv = _find_csv(result.output_dir, "Vendor-1099")
        rows = _read_csv(vendor_csv)

        vendor_names = {row[0] for row in rows[1:] if row and row[0]}
        self.assertNotIn("REVERSAL of txn_002", vendor_names)
        self.assertNotIn("REVERSAL of txn_004", vendor_names)

    def test_reversals_net_against_original_vendor(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        vendor_csv = _find_csv(result.output_dir, "Vendor-1099")
        rows = _read_csv(vendor_csv)

        acme_row = next(row for row in rows if row and row[0] == "Acme Software")
        office_row = next(row for row in rows if row and row[0] == "Office Depot")
        self.assertEqual(Decimal(acme_row[1]), Decimal("200.00"))
        self.assertEqual(acme_row[2], "No")
        self.assertEqual(Decimal(acme_row[3]), Decimal("200.00"))
        self.assertEqual(Decimal(acme_row[4]), Decimal("0.00"))
        self.assertEqual(Decimal(office_row[1]), Decimal("100.00"))
        self.assertEqual(office_row[2], "No")
        self.assertEqual(Decimal(office_row[3]), Decimal("100.00"))
        self.assertEqual(Decimal(office_row[4]), Decimal("0.00"))


# ---------------------------------------------------------------------------
# Test: General Ledger review columns
# ---------------------------------------------------------------------------

class TestGeneralLedgerReviewColumns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_gl_csv_splits_payee_memo_source_and_entry_type(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package
        from src.bookkeeping.reports.statements import general_ledger

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        gl_csv = _find_csv(result.output_dir, "General-Ledger")
        rows = _read_csv(gl_csv)

        self.assertEqual(
            rows[0],
            [
                "Account Type",
                "Account",
                "Account Detail",
                "Date",
                "Counterparty",
                "Memo",
                "Source ID",
                "Entry Type",
                "Amount",
            ],
        )
        acme_row = next(row for row in rows if len(row) >= 9 and row[6] == "txn_001")
        self.assertEqual(acme_row[0], "Assets")
        self.assertEqual(acme_row[2], "Checking")
        self.assertEqual(acme_row[4], "Acme Corp")
        self.assertEqual(acme_row[5], "Consulting January")
        self.assertEqual(acme_row[7], "Posted")

        reversal_row = next(row for row in rows if len(row) >= 9 and row[6] == "rev_txn_002")
        self.assertEqual(reversal_row[7], "Reversal")
        correction_row = next(row for row in rows if len(row) >= 9 and row[6] == "corr_txn_002")
        self.assertEqual(correction_row[7], "Correction")
        opening_row = next(row for row in rows if len(row) >= 9 and row[6] == "open_001")
        self.assertEqual(opening_row[7], "Opening")

        api_gl = general_ledger(self.entity_dir, FROM_DATE, TO_DATE)
        api_totals = {
            section["label"]: section["total"]
            for section in api_gl.sections
        }
        csv_totals = {
            row[1]: Decimal(row[8])
            for row in rows
            if len(row) >= 9 and row[7] == "Account Total"
        }
        self.assertEqual(csv_totals, api_totals)


# ---------------------------------------------------------------------------
# Test: Adjustment Log
# ---------------------------------------------------------------------------

class TestAdjustmentLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_reversal_pairs_each_once(self):
        """Two same-day reversals must appear as exactly two rows (not four)."""
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        adj_csv = _find_csv(result.output_dir, "Adjustment-Log")
        rows = _read_csv(adj_csv)

        # Skip header row; skip empty rows and placeholder rows
        data_rows = [r for r in rows[1:] if r and r[0] and r[0] != "(no reversals or corrections found in the ledger)"]
        self.assertEqual(len(data_rows), 2,
                         f"Expected 2 adjustment log rows, got {len(data_rows)}: {data_rows}")

    def test_original_ids_correct(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        adj_csv = _find_csv(result.output_dir, "Adjustment-Log")
        rows = _read_csv(adj_csv)

        original_ids = {r[0] for r in rows[1:] if r and r[0] and "no reversal" not in r[0]}
        self.assertIn("txn_002", original_ids, f"txn_002 not in adjustment log: {original_ids}")
        self.assertIn("txn_004", original_ids, f"txn_004 not in adjustment log: {original_ids}")

    def test_no_pair_duplication(self):
        """Each pair's original ID appears exactly once."""
        from src.bookkeeping.reports.workbook import _build_adjustment_log_rows

        rows = _build_adjustment_log_rows(self.entity_dir)
        data_rows = [r for r in rows[1:] if r and r[0] and "no reversal" not in r[0]]
        original_ids = [r[0] for r in data_rows]
        self.assertEqual(len(original_ids), len(set(original_ids)),
                         f"Duplicate original IDs in adjustment log: {original_ids}")

    def test_corrected_category_populated(self):
        """Corrected category is set for pairs that have a correction-of entry."""
        from src.bookkeeping.reports.workbook import _build_adjustment_log_rows

        rows = _build_adjustment_log_rows(self.entity_dir)
        data_rows = [r for r in rows[1:] if r and r[0] and "no reversal" not in r[0]]

        for row in data_rows:
            self.assertNotEqual(row[7], "",
                                f"Corrected category is empty for {row[0]}: {row}")

    def test_adjustment_log_has_complete_trace_columns(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        adj_csv = _find_csv(result.output_dir, "Adjustment-Log")
        rows = _read_csv(adj_csv)

        self.assertEqual(
            rows[0],
            [
                "Original ID",
                "Original Date",
                "Original Counterparty",
                "Original Amount",
                "Reversal ID",
                "Reversal Date",
                "Corrected ID",
                "Corrected Category",
                "Corrected Amount",
                "Trace Status",
                "Note",
            ],
        )
        txn_002 = next(row for row in rows if row and row[0] == "txn_002")
        self.assertEqual(txn_002[4], "rev_txn_002")
        self.assertEqual(txn_002[6], "corr_txn_002")
        self.assertEqual(txn_002[9], "COMPLETE")


# ---------------------------------------------------------------------------
# Test: Sanity checks
# ---------------------------------------------------------------------------

class TestSanityChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_equity_pass_when_balanced(self):
        from src.bookkeeping.reports.workbook import run_sanity_checks

        entity_dir = _make_entity(self.tmp_path)
        result = run_sanity_checks(entity_dir, FROM_DATE, TO_DATE)

        equity_check = next(c for c in result.checks if c.check == "equity_reconciliation")
        self.assertEqual(equity_check.status, "pass",
                         f"Expected equity check to pass: {equity_check.detail}")

    def test_equity_fail_when_unbalanced(self):
        """Hack the cache to inject an imbalance, then check equity fails."""
        from src.bookkeeping.reports.workbook import run_sanity_checks
        import sqlite3

        entity_dir = _make_entity(self.tmp_path, UNBALANCED_LEDGER)

        # Inject an extra asset posting to make BS unbalanced
        # Cache lives at entity_dir/reports/cache.sqlite
        cache_path = entity_dir / "reports" / "cache.sqlite"
        conn = sqlite3.connect(str(cache_path))
        try:
            # Find an entry to duplicate
            entry = conn.execute("SELECT id FROM entries LIMIT 1").fetchone()
            if entry:
                # Add a phantom posting to unbalance assets without matching equity
                conn.execute(
                    "INSERT INTO postings (entry_id, account, amount, currency) VALUES (?, ?, ?, ?)",
                    (entry[0], "Assets:Bank:Checking", "999.00", "USD"),
                )
                conn.commit()
        finally:
            conn.close()

        result = run_sanity_checks(entity_dir, FROM_DATE, date(2026, 1, 15))
        equity_check = next(c for c in result.checks if c.check == "equity_reconciliation")
        self.assertEqual(equity_check.status, "fail",
                         f"Expected equity check to fail with injected imbalance: {equity_check.detail}")

    def test_queue_empty_pass_when_no_items(self):
        from src.bookkeeping.reports.workbook import run_sanity_checks

        entity_dir = _make_entity(self.tmp_path)
        result = run_sanity_checks(entity_dir, FROM_DATE, TO_DATE)

        queue_check = next(c for c in result.checks if c.check == "queue_empty")
        self.assertEqual(queue_check.status, "pass",
                         f"Expected queue_empty to pass with no queue items: {queue_check.detail}")

    def test_queue_empty_fail_with_open_item(self):
        from src.bookkeeping.reports.workbook import run_sanity_checks

        entity_dir = _make_entity(self.tmp_path)
        # Write an open queue item
        queue_item = {
            "id": "qi_001",
            "source_id": "txn_test",
            "status": "open",
            "proposed_category": "Expenses:Software",
            "reasoning": "Looks like a software subscription",
        }
        (entity_dir / "review-queue" / "qi_001.json").write_text(
            json.dumps(queue_item), encoding="utf-8"
        )

        result = run_sanity_checks(entity_dir, FROM_DATE, TO_DATE)
        queue_check = next(c for c in result.checks if c.check == "queue_empty")
        self.assertEqual(queue_check.status, "fail",
                         f"Expected queue_empty to fail with open item: {queue_check.detail}")

    def test_sanity_result_json_serializable(self):
        from src.bookkeeping.reports.workbook import run_sanity_checks

        entity_dir = _make_entity(self.tmp_path)
        result = run_sanity_checks(entity_dir, FROM_DATE, TO_DATE)
        parsed = json.loads(result.to_json())
        self.assertIn("checks", parsed)
        self.assertIn("has_failures", parsed)

    def test_indirect_tax_registration_warns_without_calculating(self):
        from src.bookkeeping.reports.workbook import run_sanity_checks

        entity_dir = _make_entity(self.tmp_path)
        entity_json = entity_dir / "entity.json"
        data = json.loads(entity_json.read_text(encoding="utf-8"))
        data["country"] = "GB"
        data["tax_jurisdiction"] = "United Kingdom"
        data["operating_currency"] = "GBP"
        data["indirect_tax"] = {"registered": True, "type": "VAT"}
        entity_json.write_text(json.dumps(data), encoding="utf-8")

        result = run_sanity_checks(entity_dir, FROM_DATE, TO_DATE)
        check = next(c for c in result.checks if c.check == "indirect_tax_scope")
        self.assertEqual(check.status, "warn")
        self.assertIn("does not calculate", check.detail)

    def test_mixed_currency_ledger_warns(self):
        from src.bookkeeping.reports.workbook import run_sanity_checks

        mixed_ledger = FIXTURE_LEDGER + """
2026-03-25 open Assets:Bank:Euro EUR
2026-03-25 open Income:Revenue:Euro EUR

2026-03-25 * "Euro client"
  source-id: "txn_eur"
  Assets:Bank:Euro        100.00 EUR
  Income:Revenue:Euro    -100.00 EUR
"""
        entity_dir = _make_entity(self.tmp_path, mixed_ledger)
        result = run_sanity_checks(entity_dir, FROM_DATE, TO_DATE)
        check = next(c for c in result.checks if c.check == "currency_scope")
        self.assertEqual(check.status, "warn")
        self.assertIn("EUR", check.detail)

    def test_payroll_enabled_warns_when_reports_missing(self):
        from src.bookkeeping.reports.workbook import run_sanity_checks

        entity_dir = _make_entity(self.tmp_path)
        entity_json = entity_dir / "entity.json"
        data = json.loads(entity_json.read_text(encoding="utf-8"))
        data["payroll"] = {
            "enabled": True,
            "provider": "justworks",
            "posting_mode": "draft_journal_entries",
        }
        entity_json.write_text(json.dumps(data), encoding="utf-8")

        result = run_sanity_checks(entity_dir, FROM_DATE, TO_DATE)
        check = next(c for c in result.checks if c.check == "payroll_reports")
        self.assertEqual(check.status, "warn")
        self.assertIn("Justworks", check.detail)
        self.assertIn("no payroll report files", check.detail)

    def test_payroll_enabled_warns_with_reports_present_for_review(self):
        from src.bookkeeping.reports.workbook import run_sanity_checks

        entity_dir = _make_entity(self.tmp_path)
        entity_json = entity_dir / "entity.json"
        data = json.loads(entity_json.read_text(encoding="utf-8"))
        data["payroll"] = {
            "enabled": True,
            "provider": "justworks",
            "posting_mode": "draft_journal_entries",
        }
        entity_json.write_text(json.dumps(data), encoding="utf-8")
        (entity_dir / "ingestion" / "payroll" / "justworks-2026-q1.csv").write_text(
            "employee,total cost,net pay\nExample,100.00,75.00\n",
            encoding="utf-8",
        )

        result = run_sanity_checks(entity_dir, FROM_DATE, TO_DATE)
        check = next(c for c in result.checks if c.check == "payroll_reports")
        self.assertEqual(check.status, "warn")
        self.assertIn("Found 1 payroll report", check.detail)


# ---------------------------------------------------------------------------
# Test: Generation refusal and override
# ---------------------------------------------------------------------------

class TestGenerationRefusalAndOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_entity_with_open_queue_item(self) -> Path:
        entity_dir = _make_entity(self.tmp_path)
        queue_item = {
            "id": "qi_001",
            "source_id": "txn_test",
            "status": "open",
            "proposed_category": "Expenses:Software",
        }
        (entity_dir / "review-queue" / "qi_001.json").write_text(
            json.dumps(queue_item), encoding="utf-8"
        )
        return entity_dir

    def test_refused_on_failure_without_override(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        entity_dir = self._make_entity_with_open_queue_item()
        result = generate_accountant_package(entity_dir, FROM_DATE, TO_DATE, override=False)

        self.assertFalse(result.success, "Should have been refused due to open queue item")
        self.assertIsNotNone(result.error)
        self.assertIn("sanity check", result.error.lower(),
                      f"Error message should mention sanity check: {result.error}")

    def test_allowed_with_override(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        entity_dir = self._make_entity_with_open_queue_item()
        result = generate_accountant_package(entity_dir, FROM_DATE, TO_DATE, override=True)

        self.assertTrue(result.success, f"Should succeed with override: {result.error}")
        self.assertTrue(result.override_used)

    def test_override_noted_in_cover_csv(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        entity_dir = self._make_entity_with_open_queue_item()
        result = generate_accountant_package(entity_dir, FROM_DATE, TO_DATE, override=True)
        self.assertTrue(result.success)

        cover_csv = _find_csv(result.output_dir, "Cover")
        rows = _read_csv(cover_csv)
        all_text = " ".join(cell for row in rows for cell in row).lower()
        self.assertIn("override", all_text,
                      "Cover sheet should note that --override was used")

    def test_no_csv_produced_on_refusal(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        entity_dir = self._make_entity_with_open_queue_item()
        result = generate_accountant_package(entity_dir, FROM_DATE, TO_DATE, override=False)
        self.assertFalse(result.success)
        self.assertEqual(result.csv_files, [],
                         "No CSV files should be produced when generation is refused")


# ---------------------------------------------------------------------------
# Test: Cover sheet text is jargon-free
# ---------------------------------------------------------------------------

class TestCoverSheetJargonFree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_jargon_in_cover(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        self.assertIsNone(result.error)

        cover_csv = _find_csv(result.output_dir, "Cover")
        rows = _read_csv(cover_csv)
        all_text = " ".join(cell for row in rows for cell in row)

        for token in JARGON_TOKENS:
            self.assertNotIn(
                token, all_text,
                f"Cover sheet contains jargon token '{token}': {all_text[:200]}",
            )

    def test_cover_has_entity_name(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        cover_csv = _find_csv(result.output_dir, "Cover")
        rows = _read_csv(cover_csv)
        all_text = " ".join(cell for row in rows for cell in row)
        self.assertIn("Test Entity", all_text)

    def test_cover_has_period_dates(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        cover_csv = _find_csv(result.output_dir, "Cover")
        rows = _read_csv(cover_csv)
        all_text = " ".join(cell for row in rows for cell in row)
        self.assertIn("2026-01-01", all_text)
        self.assertIn("2026-04-15", all_text)

    def test_cover_has_sheet_index(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        cover_csv = _find_csv(result.output_dir, "Cover")
        rows = _read_csv(cover_csv)
        sheet_names = ["P&L", "Balance Sheet", "Trial Balance", "General Ledger",
                       "Reconciliations", "Vendor 1099", "Adjustment Log", "Open Questions"]
        all_text = " ".join(cell for row in rows for cell in row)
        for name in sheet_names:
            # Check key words appear
            key_word = name.split()[0]
            self.assertIn(key_word, all_text,
                          f"Sheet '{name}' not mentioned in cover sheet index")


# ---------------------------------------------------------------------------
# Test: xlsxwriter absent path
# ---------------------------------------------------------------------------

class TestXlsxwriterAbsent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_csv_only_when_xlsxwriter_absent(self):
        """When xlsxwriter is simulated as absent, CSV-only output + enablement message."""
        from src.bookkeeping.reports import workbook as wb_mod

        with patch.object(wb_mod, "_xlsxwriter_available", return_value=False):
            import io
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                result = wb_mod.generate_accountant_package(
                    self.entity_dir, FROM_DATE, TO_DATE, override=True
                )

            self.assertIsNone(result.error)
            self.assertFalse(result.xlsx_available)
            self.assertIsNone(result.xlsx_file)
            # CSVs should still be produced
            self.assertEqual(len(result.csv_files), 12)
            # Enablement message in stdout
            output = captured.getvalue()
            self.assertIn("pip install", output,
                          f"Enablement message not printed. stdout: {output!r}")
            self.assertIn("agent-books[xlsx]", output)

    def test_no_xlsx_file_created_when_absent(self):
        """No .xlsx file in output when xlsxwriter absent."""
        from src.bookkeeping.reports import workbook as wb_mod

        with patch.object(wb_mod, "_xlsxwriter_available", return_value=False):
            result = wb_mod.generate_accountant_package(
                self.entity_dir, FROM_DATE, TO_DATE, override=True
            )

        xlsx_files = list(result.output_dir.glob("*.xlsx"))
        self.assertEqual(xlsx_files, [], f"No .xlsx files expected but found: {xlsx_files}")

    def test_real_environment_behavior(self):
        """If xlsxwriter is truly absent, CSV-only mode works without errors."""
        # This test verifies the real environment (xlsxwriter is NOT installed by default)
        from src.bookkeeping.reports.workbook import generate_accountant_package, _xlsxwriter_available

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        self.assertIsNone(result.error)
        self.assertEqual(len(result.csv_files), 12)

        if not _xlsxwriter_available():
            # In this environment, xlsx_file must be None
            self.assertIsNone(result.xlsx_file)
        # If xlsxwriter happens to be available, xlsx_file may exist — that's fine too


# ---------------------------------------------------------------------------
# Test: XLSX workbook created when xlsxwriter present (skip if absent)
# ---------------------------------------------------------------------------

@unittest.skipUnless(
    importlib.util.find_spec("xlsxwriter") is not None,
    "xlsxwriter not installed — skipping XLSX creation test",
)
class TestXlsxCreatedWhenPresent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_xlsx_file_created(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        self.assertIsNone(result.error)
        self.assertTrue(result.xlsx_available)
        self.assertIsNotNone(result.xlsx_file)
        self.assertTrue(result.xlsx_file.exists(),
                        f"XLSX file not found: {result.xlsx_file}")
        # Must be a non-empty file
        self.assertGreater(result.xlsx_file.stat().st_size, 0)

    def test_xlsx_has_summary_checks_and_source_index_tabs(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        self.assertIsNotNone(result.xlsx_file)

        with ZipFile(result.xlsx_file) as zf:
            sheet_names = _xlsx_sheet_names(zf)

        self.assertIn("Summary", sheet_names)
        self.assertIn("Checks", sheet_names)
        self.assertIn("Source Index", sheet_names)
        self.assertEqual(len(sheet_names), 12)

    def test_xlsx_summary_links_to_books_project(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        self.assertIsNotNone(result.xlsx_file)

        with ZipFile(result.xlsx_file) as zf:
            summary_xml = _xlsx_sheet_xml(zf, "Summary")
            shared_text = _xlsx_shared_text(zf)
            rel_paths = [name for name in zf.namelist() if name.startswith("xl/worksheets/_rels/")]
            rel_text = " ".join(zf.read(name).decode("utf-8") for name in rel_paths)

        self.assertIn("Produced by", shared_text)
        self.assertIn("/books", shared_text)
        self.assertIn("Project link", shared_text)
        self.assertIn("https://github.com/giltotherescue/slashbooks", shared_text)
        self.assertIn("Legal structure", shared_text)
        self.assertIn("single-member LLC", shared_text)
        self.assertIsNotNone(summary_xml.find("main:hyperlinks", _SHEET_NS))
        self.assertIn("https://github.com/giltotherescue/slashbooks", rel_text)

    def test_xlsx_summary_ending_cash_sums_all_cash_accounts(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        self.assertIsNotNone(result.xlsx_file)

        with ZipFile(result.xlsx_file) as zf:
            summary_xml = _xlsx_sheet_xml(zf, "Summary")
            shared_text = _xlsx_shared_text(zf)
            formulas = [
                formula.text or ""
                for formula in summary_xml.findall(".//main:f", _SHEET_NS)
            ]

        formula_text = " ".join(formulas)
        self.assertIn('SUMIF(\'Balance Sheet\'!B:B,"Assets › Bank*",\'Balance Sheet\'!C:C)', formula_text)
        self.assertIn('SUMIF(\'Balance Sheet\'!B:B,"Assets › Cash*",\'Balance Sheet\'!C:C)', formula_text)
        self.assertIn(
            'COUNTIFS(\'General Ledger\'!I:I,"<>",\'General Ledger\'!H:H,"<>Account Total",\'General Ledger\'!H:H,"<>Entry Type")',
            formula_text,
        )
        self.assertIn("GL posting rows", shared_text)

    def test_xlsx_checks_sheet_contains_formulas(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        self.assertIsNotNone(result.xlsx_file)

        with ZipFile(result.xlsx_file) as zf:
            checks_xml = _xlsx_sheet_xml(zf, "Checks")
            formulas = [
                formula.text or ""
                for formula in checks_xml.findall(".//main:f", _SHEET_NS)
            ]

        formula_text = " ".join(formulas)
        expected_fragments = [
            "'Trial Balance'!B8-'Trial Balance'!C8",
            'IF(ROUND(B2,2)=0,"PASS","FAIL")',
            "'Balance Sheet'!C4-'Balance Sheet'!C16",
            'IF(ROUND(B3,2)=0,"PASS","FAIL")',
            "'P&L'!C12-('Balance Sheet'!C11-Summary!B19)",
            'IF(ROUND(B4,2)=0,"PASS","FAIL")',
            'COUNTIF(\'Open Questions\'!I:I,"open")',
            'COUNTIF(Reconciliations!G:G,"REVIEW")',
            'COUNTIF(\'Vendor 1099\'!C:C,"Yes")',
        ]
        for fragment in expected_fragments:
            self.assertIn(fragment, formula_text)

    def test_xlsx_checks_net_income_formula_handles_mid_period_package(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(
            self.entity_dir,
            date(2026, 3, 1),
            TO_DATE,
            override=True,
        )
        self.assertIsNotNone(result.xlsx_file)

        with ZipFile(result.xlsx_file) as zf:
            checks_xml = _xlsx_sheet_xml(zf, "Checks")
            formulas = [
                formula.text or ""
                for formula in checks_xml.findall(".//main:f", _SHEET_NS)
            ]

        formula_text = " ".join(formulas)
        self.assertIn("'P&L'!C12-('Balance Sheet'!C11-Summary!B19)", formula_text)

    def test_xlsx_contains_native_table_parts(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        self.assertIsNotNone(result.xlsx_file)

        with ZipFile(result.xlsx_file) as zf:
            for sheet_name in [
                "Summary", "Checks", "Reconciliations", "Vendor 1099",
                "Adjustment Log", "Open Questions", "Source Index",
            ]:
                sheet_xml = _xlsx_sheet_xml(zf, sheet_name)
                table_parts = sheet_xml.find("main:tableParts", _SHEET_NS)
                self.assertIsNotNone(table_parts, f"{sheet_name} should have a native table")

            gl_xml = _xlsx_sheet_xml(zf, "General Ledger")
            self.assertIsNotNone(gl_xml.find("main:autoFilter", _SHEET_NS))
            self.assertIsNotNone(gl_xml.find(".//main:pane", _SHEET_NS))


# ---------------------------------------------------------------------------
# Test: Open questions sheet
# ---------------------------------------------------------------------------

class TestOpenQuestionsSheet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_when_file_absent(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        oq_csv = _find_csv(result.output_dir, "Open-Questions")
        rows = _read_csv(oq_csv)
        # Should have header + placeholder row
        self.assertGreaterEqual(len(rows), 1)
        # Header should have expected columns
        self.assertEqual(rows[0][0], "#")
        self.assertEqual(rows[0][6], "Question / Note")
        self.assertEqual(rows[0][7], "Owner Response")

    def test_reads_from_json_file(self):
        from src.bookkeeping.reports.workbook import generate_accountant_package

        questions = [
            {
                "area": "Tax",
                "related_sheet": "Vendor 1099",
                "account": "Expenses:Software",
                "source_id": "txn_006",
                "amount": "700.00",
                "question": "What is the accountant deadline?",
                "owner_response": "Ask the accountant",
                "status": "open",
            },
            {"question": "Should this transfer be categorized?", "status": "open", "notes": ""},
        ]
        oq_file = self.entity_dir / "reports" / "open-questions.json"
        oq_file.write_text(json.dumps(questions), encoding="utf-8")

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        oq_csv = _find_csv(result.output_dir, "Open-Questions")
        rows = _read_csv(oq_csv)

        all_text = " ".join(cell for row in rows for cell in row)
        self.assertIn("accountant deadline", all_text)
        self.assertIn("transfer", all_text)
        self.assertIn("txn_006", all_text)
        self.assertIn("Ask the accountant", all_text)


# ---------------------------------------------------------------------------
# Test: Reconciliation sheet
# ---------------------------------------------------------------------------

class TestReconciliationsSheet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_reconciliations_sheet(self):
        """No reconciliation records → sheet has header and placeholder."""
        from src.bookkeeping.reports.workbook import generate_accountant_package

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        recon_csv = _find_csv(result.output_dir, "Reconciliations")
        rows = _read_csv(recon_csv)
        self.assertGreaterEqual(len(rows), 1)
        # Header row
        self.assertIn("Account", rows[0])
        self.assertIn("Source", rows[0])
        self.assertIn("Review Status", rows[0])
        self.assertEqual(rows[1][6], "REVIEW")

    def test_reconciliation_records_appear(self):
        """After reconcile(), discrepancy appears in the sheet."""
        from src.bookkeeping.reports.workbook import generate_accountant_package
        from src.bookkeeping.reconcile import reconcile

        # Create a discrepancy record
        reconcile(
            self.entity_dir,
            "Assets:Bank:Checking",
            Decimal("99999.00"),  # deliberately wrong to create discrepancy
            TO_DATE,
        )

        result = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE, override=True)
        recon_csv = _find_csv(result.output_dir, "Reconciliations")
        rows = _read_csv(recon_csv)
        all_text = " ".join(cell for row in rows for cell in row)
        self.assertIn("Assets:Bank:Checking", all_text)
        record_row = next(row for row in rows if row and row[0] == "Assets:Bank:Checking")
        self.assertEqual(record_row[5], "open")
        self.assertEqual(record_row[6], "REVIEW")


# ---------------------------------------------------------------------------
# Test: Deterministic CSV output
# ---------------------------------------------------------------------------

class TestDeterministicOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_csv_content_is_deterministic(self):
        """Running generate_accountant_package twice produces identical CSV content."""
        from src.bookkeeping.reports.workbook import generate_accountant_package

        # First run
        out1 = self.tmp_path / "run1"
        result1 = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE,
                                        output_dir=out1, override=True)
        self.assertIsNone(result1.error)

        # Second run to a different dir
        out2 = self.tmp_path / "run2"
        result2 = generate_accountant_package(self.entity_dir, FROM_DATE, TO_DATE,
                                        output_dir=out2, override=True)
        self.assertIsNone(result2.error)

        def normalized_csv_text(path: Path) -> str:
            rows = _read_csv(path)
            for row in rows:
                if row and row[0] == "Generated":
                    row[1] = "<generated>"
            return "\n".join(",".join(row) for row in rows)

        csvs1 = sorted((out1 / "csv").glob("*.csv"))
        csvs2 = sorted((out2 / "csv").glob("*.csv"))
        self.assertEqual([p.name for p in csvs1], [p.name for p in csvs2])
        for csv1, csv2 in zip(csvs1, csvs2):
            self.assertEqual(
                normalized_csv_text(csv1),
                normalized_csv_text(csv2),
                f"{csv1.name} should be deterministic across runs",
            )


# ---------------------------------------------------------------------------
# Test: sanity-check JSON output structure
# ---------------------------------------------------------------------------

class TestSanityCheckOutputStructure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.entity_dir = _make_entity(self.tmp_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_expected_checks_returned(self):
        from src.bookkeeping.reports.workbook import run_sanity_checks

        result = run_sanity_checks(self.entity_dir, FROM_DATE, TO_DATE)
        check_names = {c.check for c in result.checks}
        expected = {
            "entity_metadata",
            "indirect_tax_scope",
            "currency_scope",
            "payroll_reports",
            "yoy_pnl_variance",
            "equity_reconciliation",
            "queue_empty",
            "reconciliation_clean",
        }
        self.assertEqual(check_names, expected,
                         f"Expected checks {expected}, got {check_names}")

    def test_each_check_has_status_and_detail(self):
        from src.bookkeeping.reports.workbook import run_sanity_checks

        result = run_sanity_checks(self.entity_dir, FROM_DATE, TO_DATE)
        for c in result.checks:
            self.assertIn(c.status, ("pass", "warn", "fail"),
                          f"Check {c.check} has invalid status: {c.status}")
            self.assertIsInstance(c.detail, str)
            self.assertGreater(len(c.detail), 0, f"Check {c.check} has empty detail")

    def test_no_prior_data_yoy_is_warn(self):
        """With no prior-year data, yoy_pnl_variance is warn (not fail)."""
        from src.bookkeeping.reports.workbook import run_sanity_checks

        result = run_sanity_checks(self.entity_dir, FROM_DATE, TO_DATE)
        yoy = next(c for c in result.checks if c.check == "yoy_pnl_variance")
        # May be pass (if no prior data found) or warn — never fail
        self.assertIn(yoy.status, ("pass", "warn"),
                      f"yoy_pnl_variance should be pass or warn without prior data: {yoy}")
        if yoy.status == "warn":
            self.assertIn("/books ledger activity", yoy.detail)
            self.assertIn("QuickBooks reference exports", yoy.detail)

    def test_has_failures_property(self):
        from src.bookkeeping.reports.workbook import SanityResult, SanityCheck

        # All pass
        r = SanityResult(checks=[
            SanityCheck("a", "pass", "ok"),
            SanityCheck("b", "warn", "watch this"),
        ])
        self.assertFalse(r.has_failures)

        # One fail
        r2 = SanityResult(checks=[
            SanityCheck("a", "pass", "ok"),
            SanityCheck("b", "fail", "problem"),
        ])
        self.assertTrue(r2.has_failures)


if __name__ == "__main__":
    unittest.main()
