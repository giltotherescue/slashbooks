from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.connectors.payroll import provider_spec, normalize_provider  # noqa: E402


class TestPayrollProviderSpecs(unittest.TestCase):
    def test_justworks_is_report_first(self) -> None:
        spec = provider_spec("justworks")
        self.assertEqual(spec.display_name, "Justworks")
        self.assertEqual(spec.first_import_mode, "custom_report_csv")

    def test_known_api_placeholders(self) -> None:
        self.assertEqual(provider_spec("gusto").future_api_shape, "async_general_ledger_report_api")
        self.assertEqual(provider_spec("deel").future_api_shape, "global_payroll_results_api")
        self.assertEqual(provider_spec("adp").future_api_shape, "product_specific_payroll_output_api")

    def test_unknown_provider_normalizes_to_other(self) -> None:
        self.assertEqual(normalize_provider("rippling"), "other")
        self.assertEqual(provider_spec("rippling").display_name, "Other payroll provider")
