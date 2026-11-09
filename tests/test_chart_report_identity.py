"""Economic proof contracts retain baseline hashes when display identity is added.

Hashes were captured from public synthetic report goldens at ef7e5d7 before the
identity fields existed. They cover P&L, BS, TB and GL across all three companies.
"""
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import unittest

from bookkeeping.reports.statements import StatementResult

FIXTURES = Path(__file__).parent / "fixtures"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ChartReportIdentityTests(unittest.TestCase):
    def test_all_baseline_report_hashes_survive_new_fields_and_display_changes(self):
        proofs = json.loads((FIXTURES / "chart_identity/legacy-proof-hashes.json").read_text())["sha256"]
        kinds = set()
        for path, expected in proofs.items():
            with self.subTest(path=path):
                payload = json.loads((FIXTURES / "golden" / path).read_text())
                kinds.add(payload["kind"])
                result = StatementResult(payload["kind"], date.fromisoformat(payload["from_date"]) if payload["from_date"] else None,
                                         date.fromisoformat(payload["to_date"]) if payload["to_date"] else None,
                                         payload["sections"], payload["totals"], payload["meta"])
                self.assertEqual(digest(json.loads(result.to_json(identity_fields=False))), expected)
                for section in result.sections:
                    if section.get("account"):
                        section["label"] = section["display_name"] = "Renamed section — exact text"
                    for row in section.get("rows", []):
                        if row.get("account"):
                            row["label"] = row["display_name"] = "Renamed row — exact text"
                        if row.get("computed"):
                            self.assertNotIn("account", row)
                for row in result.meta.get("raw_rows", []):
                    if row.get("account"):
                        row["label"] = row["display_name"] = "Renamed trial balance row"
                self.assertEqual(digest(json.loads(result.to_json(identity_fields=False))), expected)
        self.assertEqual(kinds, {"pnl", "balance_sheet", "trial_balance", "general_ledger"})

    def test_derived_cash_special_rows_keep_existing_semantics(self):
        old = StatementResult("pnl", date(2026, 1, 1), date(2026, 1, 31), sections=[
            {"label": "Revenue", "rows": [{"label": "Income › Services", "amount": "10.00", "indent": 1},
                                           {"label": "Unapplied receipts", "special": "unapplied_in", "amount": "2.00", "indent": 1}]}])
        expected = old.to_json()
        current = deepcopy(old)
        current.sections[0]["rows"][0].update(account="Income:Services", display_name="Professional services", label="Professional services")
        self.assertEqual(current.to_json(identity_fields=False), expected)
