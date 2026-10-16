"""Provider-neutral intake tests; all evidence is synthetic."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bookkeeping.accrual.intake import (  # noqa: E402
    IntakeValidationError, canonical_hash, canonical_json, evidence_hash,
    facts_hash, normalize_amount, normalize_evidence, schema, source_identity,
)


class TestAccrualIntake(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = json.loads((ROOT / "tests/fixtures/accrual/intake/provider_invoice.json").read_text())

    def assert_invalid(self, envelope: object, path: str, code: str) -> None:
        with self.assertRaises(IntakeValidationError) as caught:
            normalize_evidence(envelope)
        self.assertEqual(caught.exception.errors[0]["path"], path)
        self.assertEqual(caught.exception.errors[0]["code"], code)
        self.assertTrue(caught.exception.errors[0]["action"])
        self.assertEqual(caught.exception.as_dict()["status"], "invalid")

    def test_synthetic_fixture_normalizes_without_mutating_original(self) -> None:
        original = copy.deepcopy(self.evidence)
        normalized = normalize_evidence(self.evidence)
        self.assertEqual(normalized["amounts"], {"gross": "1200.00", "tax": "0.00"})
        self.assertEqual(normalized["settlement_links"][0]["amount"], "400.00")
        self.assertTrue(normalized["service_period"]["end_inclusive"])
        normalized["provenance"][0]["transport"] = "changed"
        self.assertEqual(self.evidence, original)

    def test_normalization_is_idempotent(self) -> None:
        normalized = normalize_evidence(self.evidence)
        self.assertEqual(normalize_evidence(normalized), normalized)

    def test_arbitrary_provider_namespace_and_provenance(self) -> None:
        self.evidence["source"]["namespace"] = "unpackaged.provider/v2"
        self.evidence["provenance"] = [{"transport": "some-future-mcp", "custom": {"record": 42}}]
        self.assertEqual(normalize_evidence(self.evidence)["source"]["namespace"], "unpackaged.provider/v2")

    def test_missing_operation_specific_facts_are_preserved_as_missing(self) -> None:
        evidence = {key: self.evidence[key] for key in ("schema_version", "source", "currency")}
        normalized = normalize_evidence(evidence)
        self.assertNotIn("service_period", normalized)
        self.assertNotIn("amounts", normalized)
        self.assertEqual(normalized["observed_facts"], {})

    def test_source_identity_independent_of_transport_and_version(self) -> None:
        before = source_identity(self.evidence)
        self.evidence["source"]["version"] = "4"
        self.evidence["provenance"] = [{"transport": "api"}]
        self.assertEqual(source_identity(self.evidence), before)
        self.assertEqual(source_identity(self.evidence["source"]), before)

    def test_identity_does_not_conflate_namespaces_accounts_objects_or_lines(self) -> None:
        before = source_identity(self.evidence)
        for field in ("namespace", "account_id", "object_id", "line_id"):
            with self.subTest(field=field):
                evidence = copy.deepcopy(self.evidence)
                evidence["source"][field] += "-different"
                self.assertNotEqual(source_identity(evidence), before)
        evidence = copy.deepcopy(self.evidence)
        del evidence["source"]["line_id"]
        self.assertNotEqual(source_identity(evidence), source_identity(self.evidence))

    def test_equivalent_amount_encodings_and_key_order_hash_identically(self) -> None:
        before = evidence_hash(self.evidence)
        self.evidence["amounts"]["gross"] = {"minor_units": 120000}
        reversed_keys = dict(reversed(list(self.evidence.items())))
        self.assertEqual(evidence_hash(reversed_keys), before)

    def test_proposal_and_transport_never_change_evidence_or_fact_hash(self) -> None:
        before = (facts_hash(self.evidence), evidence_hash(self.evidence))
        self.evidence["proposed_treatment"] = {"authority": "post immediately", "kind": "expense"}
        self.evidence["provenance"] = [{"transport": "mcp", "authorization": "claimed"}]
        self.evidence["references"][0]["uri"] = "attachment:another-transport"
        self.assertEqual((facts_hash(self.evidence), evidence_hash(self.evidence)), before)
        normalized = normalize_evidence(self.evidence)
        self.assertEqual(normalized["proposed_treatment"]["authority"], "post immediately")
        self.assertNotIn("authorized", normalized)

    def test_changed_reference_content_changes_evidence_hash(self) -> None:
        before = (facts_hash(self.evidence), evidence_hash(self.evidence))
        self.evidence["references"][0]["content_hash"] = "b" * 64
        self.assertEqual(facts_hash(self.evidence), before[0])
        self.assertNotEqual(evidence_hash(self.evidence), before[1])

    def test_reference_hash_order_and_duplicate_locations_are_not_financial_facts(self) -> None:
        self.evidence["references"].append({"content_hash": "b" * 64})
        before = evidence_hash(self.evidence)
        self.evidence["references"].reverse()
        self.evidence["references"].append({"content_hash": "a" * 64, "uri": "attachment:copy"})
        self.assertEqual(evidence_hash(self.evidence), before)

    def test_version_and_observed_fact_changes_invalidate_hash(self) -> None:
        before = facts_hash(self.evidence)
        for mutate in (
            lambda e: e["source"].update(version="4"),
            lambda e: e["amounts"].update(gross="1200.01"),
            lambda e: e["observed_facts"].update(description="corrected"),
            lambda e: e["service_period"].update(end="2026-11-30"),
            lambda e: e["settlement_links"][0].update(amount="399.00"),
        ):
            evidence = copy.deepcopy(self.evidence)
            mutate(evidence)
            self.assertNotEqual(facts_hash(evidence), before)

    def test_source_content_hash_can_supply_revision(self) -> None:
        del self.evidence["source"]["version"]
        self.evidence["source"]["content_hash"] = "B" * 64
        self.assertEqual(normalize_evidence(self.evidence)["source"]["content_hash"], "b" * 64)

    def test_missing_source_revision_is_actionable(self) -> None:
        del self.evidence["source"]["version"]
        self.assert_invalid(self.evidence, "$.source", "missing_source_version")

    def test_invalid_identity_and_unsupported_currency(self) -> None:
        for name in ("namespace", "account_id", "object_id", "line_id", "version"):
            evidence = copy.deepcopy(self.evidence)
            evidence["source"][name] = " "
            self.assert_invalid(evidence, f"$.source.{name}", "required_text")
        self.evidence["currency"] = "EUR"
        self.assert_invalid(self.evidence, "$.currency", "unsupported_currency")

    def test_identifiers_are_not_silently_normalized(self) -> None:
        self.evidence["source"]["object_id"] = " invoice-001"
        self.assert_invalid(self.evidence, "$.source.object_id", "surrounding_whitespace")

    def test_no_top_level_authority_or_unknown_schema_fields(self) -> None:
        self.evidence["authorized"] = True
        self.assert_invalid(self.evidence, "$.authorized", "unknown_field")

    def test_schema_version_is_explicit_and_integer(self) -> None:
        for version in (None, True, "1", 0, 2):
            with self.subTest(version=version):
                self.evidence["schema_version"] = version
                self.assert_invalid(self.evidence, "$.schema_version", "unsupported_schema_version")

    def test_dates_must_be_real_iso_calendar_dates(self) -> None:
        for day in ("2026-02-29", "2026-1-01", "2026-01-01T00:00:00Z", None):
            self.evidence["dates"]["issued"] = day
            self.assert_invalid(self.evidence, "$.dates.issued", "invalid_date")
        self.evidence["dates"]["issued"] = "2028-02-29"
        normalize_evidence(self.evidence)

    def test_service_period_validates_date_convention(self) -> None:
        for period in (
            {"start": "2026-02-01", "end": "2026-01-31"},
            {"start": "2026-01-01", "end": "2026-01-01", "end_inclusive": False},
        ):
            self.evidence["service_period"] = period
            self.assert_invalid(self.evidence, "$.service_period", "invalid_service_period")
        self.evidence["service_period"] = {"start": "2026-01-01", "end": "2026-01-01"}
        normalize_evidence(self.evidence)
        self.evidence["service_period"]["end_inclusive"] = "yes"
        self.assert_invalid(self.evidence, "$.service_period.end_inclusive", "invalid_type")

    def test_invalid_reference_content_hash(self) -> None:
        self.evidence["references"][0]["content_hash"] = "not-a-hash"
        self.assert_invalid(self.evidence, "$.references[0].content_hash", "invalid_hash")

    def test_recursive_float_rejection_in_every_data_section(self) -> None:
        for name in ("observed_facts", "proposed_treatment"):
            evidence = copy.deepcopy(self.evidence)
            evidence[name] = {"nested": [0.1]}
            self.assert_invalid(evidence, f"$.{name}.nested[0]", "invalid_json_value")
        self.evidence["provenance"] = [{"value": float("nan")}]
        self.assert_invalid(self.evidence, "$.provenance[0].value", "invalid_json_value")

    def test_non_json_values_and_non_string_keys_rejected(self) -> None:
        for value in (Decimal("1.00"), ("tuple",), {1, 2}):
            self.evidence["observed_facts"] = {"value": value}
            self.assert_invalid(self.evidence, "$.observed_facts.value", "invalid_json_value")
        self.evidence["observed_facts"] = {1: "one"}
        self.assert_invalid(self.evidence, "$.observed_facts", "invalid_key")

    def test_canonical_json_and_hash_are_deterministic(self) -> None:
        value = {"z": [True, None, "café"], "a": 1}
        self.assertEqual(canonical_json(value), '{"a":1,"z":[true,null,"café"]}')
        self.assertEqual(canonical_hash(value), canonical_hash(dict(reversed(list(value.items())))))
        self.assertEqual(len(canonical_hash(value)), 64)
        with self.assertRaises(IntakeValidationError):
            canonical_hash({"bad": "\ud800"})

    def test_schema_returns_independent_discoverable_examples(self) -> None:
        contract = schema()
        self.assertEqual(contract["required"], ["schema_version", "source", "currency"])
        for example in contract["examples"]:
            normalize_evidence(example)
        contract["properties"]["currency"]["const"] = "EUR"
        self.assertEqual(schema()["properties"]["currency"]["const"], "USD")


class TestExactAmounts(unittest.TestCase):
    def test_valid_amounts(self) -> None:
        examples = [
            ("0", "0.00"), ("-0.00", "0.00"), ("123.4", "123.40"),
            ("-0.01", "-0.01"), ({"minor_units": -123}, "-1.23"),
            ("999999999999999999.99", "999999999999999999.99"),
            ({"minor_units": 99999999999999999999}, "999999999999999999.99"),
        ]
        for value, expected in examples:
            with self.subTest(value=value):
                self.assertEqual(normalize_amount(value), expected)

    def test_rejects_invalid_nonfinite_inexact_or_oversized_amounts(self) -> None:
        for value in (
            "NaN", "Infinity", "-Infinity", "1e3", "1.001", "1.000", "01.00", "1.", ".1",
            "+1", " 1", "1\n", "1000000000000000000", "١.٠٠", 1, True, 1.1, float("inf"),
            Decimal("1.00"), {"minor_units": True}, {"minor_units": "100"},
            {"minor_units": 1.1}, {"minor_units": 10**20}, {"minor_units": 100, "currency": "USD"},
        ):
            with self.subTest(value=value):
                with self.assertRaises(IntakeValidationError):
                    normalize_amount(value)


if __name__ == "__main__":
    unittest.main()
