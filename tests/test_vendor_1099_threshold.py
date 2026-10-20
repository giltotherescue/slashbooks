"""The Vendor 1099 sheet uses the federal threshold for the year of each payment.

$600 applies to payments made through 2025 and $2,000 to payments made in 2026
(Public Law 119-21, section 70433; IRS "Am I required to file a Form 1099 or
other information return?").
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.bookkeeping.reports.workbook import _build_vendor_1099_rows, threshold_1099_for_year
from tests.test_workbook import _make_entity

HEADER = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2024-01-01 open Assets:Bank:Checking USD
2024-01-01 open Expenses:Contractors USD
"""


def _payment(day: str, vendor: str, amount: str, n: int) -> str:
    return (
        f'\n{day} * "{vendor}" "Work"\n  source-id: "p{n}"\n'
        f"  Expenses:Contractors  {amount} USD\n  Assets:Bank:Checking  -{amount} USD\n"
    )


class Vendor1099ThresholdTest(unittest.TestCase):
    def _rows(self, payments, start, end, strip_override=True):
        text = HEADER + "".join(_payment(d, v, a, i) for i, (d, v, a) in enumerate(payments))
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp), text)
            if strip_override:
                import json
                cfg = json.loads((entity / "entity.json").read_text())
                cfg.pop("vendor_1099_threshold", None)
                (entity / "entity.json").write_text(json.dumps(cfg))
            rows = _build_vendor_1099_rows(entity, start, end, None)
        return {r[0]: r for r in rows[1:] if r and r[0] and not r[0].startswith("Note")}, rows[0]

    def test_threshold_table(self):
        self.assertEqual(threshold_1099_for_year(2024), Decimal("600.00"))
        self.assertEqual(threshold_1099_for_year(2025), Decimal("600.00"))
        self.assertEqual(threshold_1099_for_year(2026), Decimal("2000.00"))

    def test_2025_uses_600(self):
        by, header = self._rows([("2025-06-01", "V", "1500.00")], date(2025, 1, 1), date(2025, 12, 31))
        self.assertEqual(by["V"][2], "Yes")
        self.assertIn("600.00", header[2])

    def test_2026_uses_2000(self):
        by, header = self._rows([("2026-06-01", "V", "1500.00")], date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(by["V"][2], "No")
        self.assertIn("2000.00", header[2])

    def test_same_vendor_listed_in_2025_not_2026(self):
        a, _ = self._rows([("2025-03-01", "V", "1000.00")], date(2025, 1, 1), date(2025, 12, 31))
        b, _ = self._rows([("2026-03-01", "V", "1000.00")], date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual((a["V"][2], b["V"][2]), ("Yes", "No"))

    def test_boundaries(self):
        cases = [("2026", "1999.99", "No"), ("2026", "2000.00", "Yes"),
                 ("2025", "599.99", "No"), ("2025", "600.00", "Yes")]
        for year, amount, expected in cases:
            with self.subTest(year=year, amount=amount):
                by, _ = self._rows([(f"{year}-05-01", "V", amount)], date(int(year), 1, 1), date(int(year), 12, 31))
                self.assertEqual(by["V"][2], expected)

    def test_report_spanning_year_boundary(self):
        # 1,000 in December 2025 (over 600) and 1,000 in January 2026 (under 2,000).
        # Years are not added together: the 2025 payments alone meet $600.
        by, header = self._rows(
            [("2025-12-15", "A", "1000.00"), ("2026-01-15", "A", "1000.00"),
             ("2025-12-20", "B", "500.00"), ("2026-01-20", "B", "1500.00"),
             ("2026-01-10", "C", "2000.00")],
            date(2025, 12, 1), date(2026, 1, 31))
        self.assertEqual(by["A"][2], "Yes")   # 2025 total 1000 >= 600
        self.assertEqual(by["B"][2], "No")    # 2025: 500 < 600; 2026: 1500 < 2000
        self.assertEqual(by["C"][2], "Yes")   # 2026: 2000 >= 2000
        self.assertEqual(Decimal(by["B"][1]), Decimal("2000.00"))  # combined net still shown
        self.assertIn("payment year", header[2])
        self.assertIn("2025: 1000.00 vs 600.00", by["A"][5])
        self.assertIn("2026: 1000.00 vs 2000.00", by["A"][5])

    def test_entity_override_applies_to_every_year(self):
        text = HEADER + _payment("2026-02-01", "V", "700.00", 1)
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp), text)  # entity.json sets 600
            rows = _build_vendor_1099_rows(entity, date(2026, 1, 1), date(2026, 12, 31), Decimal("600"))
        self.assertEqual(rows[1][2], "Yes")


if __name__ == "__main__":
    unittest.main()
