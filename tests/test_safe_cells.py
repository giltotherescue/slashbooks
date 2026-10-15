"""Formula-injection protection for CSV and XLSX output."""
from __future__ import annotations

import csv
import importlib.util
import io
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.reports import workbook
from bookkeeping.safe_cells import is_numeric_text, neutralize_cell, neutralize_row

ATTACKS = [
    '=HYPERLINK("http://evil.example","x")',
    "+1+1",
    "-2+3",
    "@SUM(A1)",
    "\t=1+1",
    "\r=1+1",
    "  =1+1",
    "-",
]


class NeutralizeTests(unittest.TestCase):
    def test_every_trigger_is_neutralized(self):
        for text in ATTACKS:
            with self.subTest(text=text):
                self.assertEqual(neutralize_cell(text), "'" + text)

    def test_numbers_stay_numbers(self):
        for value in ["12", "-12.50", "+3", "-1,234.50", "0.00", "-$3.00", ".5", 12, -1.5, Decimal("-4.20"), None, True]:
            with self.subTest(value=value):
                self.assertEqual(neutralize_cell(value), value)

    def test_ordinary_text_is_not_changed(self):
        for text in ["", "Acme Co", "A=B", "Net - income", "(120.00)", "'=1", "2026-01-01", "-,", "-."]:
            with self.subTest(text=text):
                expected = "'" + text if text in {"-,", "-."} else text
                self.assertEqual(neutralize_cell(text), expected)

    def test_numeric_text_check(self):
        self.assertTrue(is_numeric_text("-1,234.50"))
        self.assertFalse(is_numeric_text("-1+1"))
        self.assertFalse(is_numeric_text("-"))
        self.assertFalse(is_numeric_text("1,23"))

    def test_row_helper(self):
        self.assertEqual(neutralize_row(["=1", "-5.00", "x"]), ["'=1", "-5.00", "x"])


class WriterTests(unittest.TestCase):
    def test_workbook_csv_writer_neutralizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.csv"
            workbook._write_csv(path, [["Name", "Amount"], ["=cmd|' /C calc'!A0", "-10.00"]])
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(rows[1], ["'=cmd|' /C calc'!A0", "-10.00"])

    def test_cashflow_csv_neutralizes_free_text(self):
        from bookkeeping import cashflow

        stream = io.StringIO(newline="")
        writer = cashflow._SafeWriter(csv.writer(stream))
        writer.writerow(["warning", "=1+1"])
        self.assertEqual(stream.getvalue().strip(), "warning,'=1+1")

    @unittest.skipUnless(importlib.util.find_spec("xlsxwriter"), "xlsx extra not installed")
    def test_xlsx_never_stores_text_as_a_formula(self):
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.xlsx"
            rows = [["=HYPERLINK(\"x\")", "Amount"]] + [[text, "-10.50"] for text in ATTACKS]
            workbook._write_xlsx(path, [workbook.XlsxSheet(name="Data", rows=rows, numeric_columns=frozenset({1}))])
            with ZipFile(path) as archive:
                sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        self.assertEqual(sheet.findall(".//m:f", ns), [])
        text_cells = [c for c in sheet.findall(".//m:c", ns) if c.get("r", "").startswith("A")]
        self.assertTrue(text_cells)
        self.assertTrue(all(c.get("t") == "s" for c in text_cells))
        self.assertIn("=HYPERLINK(\"x\")", {"".join(si.itertext()) for si in strings})
        numbers = [c.find("m:v", ns).text for c in sheet.findall(".//m:c", ns) if c.get("r", "").startswith("B") and c.get("r") != "B1"]
        self.assertEqual(set(numbers), {"-10.50"})


if __name__ == "__main__":
    unittest.main()
