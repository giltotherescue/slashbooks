"""Synthetic paid-fact oracles for year-specific 1099 preparation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bookkeeping.tax1099 import export_cpa_package, prepare_1099, verify_preparation


def payload(year=2026, category="nonemployee_compensation", amount="2000.00"):
    address = {"line1": "100 Example Lane", "city": "Austin", "region": "TX", "postal_code": "78701", "country": "US"}
    return {"tax_year": year, "payer_profile": {"id": "payer", "legal_name": "Example Payer", "address": address, "phone": "512-555-0100", "verified": True, "tin_status": "verified"},
            "recipients": [{"party_id": "vendor", "legal_name": "Example Vendor", "address": address, "tax_classification": "individual", "classification_verified": True, "w9_status": "verified", "tin_status": "verified"}],
            "payments": [{"id": "pay-a", "party_id": "vendor", "date": f"{year}-02-15", "currency": "USD", "gross_amount": amount, "cash_amount": amount, "federal_withholding": "0.00", "payment_method": "check", "direction": "pay", "source": {"source_id": "source-a", "bank_account": "checking", "mode": "bank"}, "evidence_ids": ["evidence-a"], "allocations": [{"amount": amount, "tax_category": category, "classification_verified": True}]}],
            "coverage": {"complete": True, "opening_payments_complete": True, "start_date": f"{year}-01-01", "end_date": f"{year}-12-31"}}


class Tax1099Tests(unittest.TestCase):
    def test_supported_years_and_exact_thresholds(self):
        for year, threshold, box in [(2025, "600.00", "1"), (2026, "2000.00", "1a")]:
            p = payload(year, amount=threshold)
            r = prepare_1099(p)
            self.assertTrue(r["ready"])
            self.assertEqual(r["forms"][0]["boxes"], {box: threshold})
            self.assertEqual(prepare_1099(p), r)
            p["payments"][0].update(gross_amount=str(float(threshold)-.01), cash_amount=str(float(threshold)-.01))
            p["payments"][0]["allocations"][0]["amount"] = p["payments"][0]["gross_amount"]
            self.assertEqual(prepare_1099(p)["forms"], [])
        for year in [2024, 2027, "2026", True]:
            with self.assertRaises(ValueError): prepare_1099(payload(year))

    def test_special_thresholds_and_corporate_exceptions(self):
        for category, amount, form, box, corporate in [("royalties", "10.00", "1099-MISC", "2", False), ("attorney_gross_proceeds", "600.00", "1099-MISC", "10", True), ("legal_services", "2000.00", "1099-NEC", "1a", True), ("medical_healthcare", "2000.00", "1099-MISC", "6", True)]:
            p = payload(category=category, amount=amount)
            if corporate: p["recipients"][0]["tax_classification"] = "s_corporation"
            r = prepare_1099(p)
            self.assertEqual(r["forms"][0]["form"], form)
            self.assertEqual(r["forms"][0]["boxes"], {box: amount})
        p = payload(); p["recipients"][0]["tax_classification"] = "c_corporation"
        self.assertEqual(prepare_1099(p)["forms"], [])

    def test_rails_and_missing_facts_fail_closed(self):
        for rail, qualifies, included, ready in [("card", None, False, True), ("payment_network", True, False, True), ("payment_network", False, True, True), ("payment_network", None, False, False), ("unknown", None, False, False)]:
            p = payload(); p["payments"][0]["payment_method"] = rail
            if qualifies is not None: p["payments"][0]["network_qualifies_1099k"] = qualifies
            r = prepare_1099(p)
            self.assertEqual(bool(r["forms"]), included); self.assertEqual(r["ready"], ready)
        for field, value in [("w9_status", "missing"), ("tin_status", "missing"), ("tax_classification", "foreign"), ("classification_verified", False)]:
            p = payload(); p["recipients"][0][field] = value
            self.assertFalse(prepare_1099(p)["ready"])
        p = payload(); p["payments"][0]["allocations"][0]["classification_verified"] = False
        self.assertEqual(prepare_1099(p)["payments"][0]["status"], "blocked")
        p["payments"][0]["payment_method"] = "card"
        self.assertFalse(prepare_1099(p)["ready"])
        self.assertEqual(prepare_1099(p)["totals"]["blocked"], "2000.00")

    def test_duplicates_conflicts_and_order_independent_conflict(self):
        p = payload(); duplicate = copy.deepcopy(p["payments"][0]); duplicate["id"] = "alias"
        p["payments"].append(duplicate)
        r = prepare_1099(p)
        self.assertEqual(r["forms"][0]["boxes"]["1a"], "2000.00")
        self.assertEqual(r["totals"]["included"], "2000.00")
        duplicate["source"]["source_id"] = "banksync:" + p["payments"][0]["source"]["source_id"]
        self.assertEqual(prepare_1099(p)["totals"]["included"], "2000.00")
        duplicate["allocations"][0]["tax_category"] = "rent"
        for rows in [p["payments"], list(reversed(p["payments"]))]:
            p["payments"] = rows; r = prepare_1099(p)
            self.assertFalse(r["ready"]); self.assertEqual(r["forms"], [])
            self.assertTrue(all(row["status"] == "blocked" for row in r["payments"]))

    def test_same_year_refunds_and_cross_year_block(self):
        p = payload(amount="3000.00"); refund = copy.deepcopy(p["payments"][0])
        refund.update(id="refund", direction="receive", refund_of="pay-a", date="2026-03-01", gross_amount="500.00", cash_amount="500.00", source={"source_id": "refund-source"})
        refund["allocations"][0]["amount"] = "500.00"; p["payments"].append(refund)
        self.assertEqual(prepare_1099(p)["forms"][0]["boxes"], {"1a": "2500.00"})
        p["payments"][0]["date"] = "2025-12-31"
        self.assertFalse(prepare_1099(p)["ready"])
        self.assertIn("prior_year_refund_requires_review", [i["code"] for i in prepare_1099(p)["issues"]])

    def test_withholding_forces_form_and_gross_not_cash(self):
        p = payload(amount="100.00"); p["payments"][0].update(cash_amount="76.00", federal_withholding="24.00")
        r = prepare_1099(p)
        self.assertEqual(r["forms"][0]["boxes"], {"1a": "100.00", "4": "24.00"})
        p["payments"][0]["payment_method"] = "card"
        self.assertFalse(prepare_1099(p)["ready"])

    def test_coverage_currency_and_unpaid_items(self):
        p = payload()
        self.assertEqual(prepare_1099(p)["forms"][0]["boxes"]["1a"], "2000.00")
        p["payments"] = []
        self.assertEqual(prepare_1099(p)["forms"], [])
        p["coverage"]["opening_payments_complete"] = False
        self.assertFalse(prepare_1099(p)["ready"])

    def test_typed_numeric_source_ids_without_free_text_tins(self):
        p = payload(); p["payments"][0]["source"]["source_id"] = "345" + "678" + "901"
        p["payments"][0]["evidence_ids"] = ["987" + "654" + "321"]
        r = prepare_1099(p); self.assertTrue(r["ready"]); verify_preparation(r)
        for key in ["legal_name", "phone"]:
            changed = copy.deepcopy(p); changed["payer_profile"][key] = "123" + "456" + "789"
            with self.assertRaises(ValueError): prepare_1099(changed)
        p["payments"][0]["source"]["source_id"] = "123" + "-45-" + "6789"
        with self.assertRaises(ValueError): prepare_1099(p)

    def test_unknown_tax_fields_are_not_silently_discarded(self):
        for path, key in [("payment", "state_tax_amount"), ("allocation", "tax_exempt"), ("profile", "foreign_tax_status"), ("source", "withholding_amount")]:
            p = payload()
            target = {"payment":p["payments"][0], "allocation":p["payments"][0]["allocations"][0], "profile":p["recipients"][0], "source":p["payments"][0]["source"]}[path]
            target[key] = "1.00"
            with self.assertRaisesRegex(ValueError, "Unsupported fields"): prepare_1099(p)

    def test_excluded_payee_does_not_require_recipient_tin_or_w9(self):
        for category, amount in [("not_reportable", "5000.00"), ("nonemployee_compensation", "100.00")]:
            p = payload(category=category, amount=amount)
            p["recipients"][0].update(w9_status="missing", tin_status="missing")
            r = prepare_1099(p)
            self.assertEqual(r["forms"], []); self.assertTrue(r["ready"])
        p = payload(amount="100.00")
        p["recipients"][0]["tax_classification"] = "unknown"
        self.assertFalse(prepare_1099(p)["ready"])
        self.assertEqual(prepare_1099(p)["totals"]["blocked"], "100.00")
        p = payload(); p["payments"][0]["currency"] = "CAD"
        self.assertFalse(prepare_1099(p)["ready"])

    def test_money_errors_and_sensitive_input(self):
        for amount in [True, float("nan"), "NaN", "Infinity", "1.001", "-1.00"]:
            p = payload(); p["payments"][0]["gross_amount"] = amount
            with self.assertRaises(ValueError): prepare_1099(p)
        for key in ["tin", "ssn", "ein", "recipient_tin", "payerTaxID", "tax_identifier", "socialSecurityNumber", "FEIN"]:
            p = payload(); p["recipients"][0][key] = "protected"
            with self.assertRaisesRegex(ValueError, "Protected"): prepare_1099(p)

    def test_hash_mutation_and_private_export(self):
        p = prepare_1099(payload()); verify_preparation(p)
        changed = copy.deepcopy(p); changed["forms"][0]["boxes"]["1a"] = "2.00"
        with self.assertRaises(ValueError): verify_preparation(changed)
        with tempfile.TemporaryDirectory() as root:
            result = export_cpa_package(p, Path(root)/"package")
            self.assertFalse(result["protected_profiles_included"])
            self.assertEqual((Path(root)/"package"/"preparation.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((Path(root)/"package"/"preparation.json").read_text()), p)
            with self.assertRaises(FileExistsError): export_cpa_package(p, Path(root)/"package")

    def test_csv_formula_cells_and_export_failure_cleanup(self):
        p = payload(); p["recipients"][0]["legal_name"] = " =UNTRUSTED()"
        p["recipients"][0]["party_id"] = "+vendor"
        p["payments"][0]["party_id"] = "+vendor"
        prep = prepare_1099(p)
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)/"safe"
            export_cpa_package(prep, target)
            csv_text = (target/"form-totals.csv").read_text()
            self.assertIn("'+vendor", csv_text); self.assertIn("' =UNTRUSTED()", csv_text)
            with patch("bookkeeping.tax1099.os.fsync", side_effect=OSError("synthetic disk failure")):
                with self.assertRaises(OSError): export_cpa_package(prep, Path(root)/"failed")
            self.assertEqual(sorted(p.name for p in Path(root).iterdir()), ["safe"])

    def test_privileged_cpa_export_requires_matching_transient_profiles(self):
        prep = prepare_1099(payload())
        protected = {"payer": {"id": "payer", "tin": "12" + "-" + "3456789"}, "recipients": {"vendor": {"party_id": "vendor", "tin": "987" + "-" + "65" + "-" + "4321"}}}
        with tempfile.TemporaryDirectory() as root:
            wrong = copy.deepcopy(protected); wrong["recipients"]["vendor"]["party_id"] = "other"
            with self.assertRaises(ValueError): export_cpa_package(prep, Path(root)/"wrong", wrong)
            self.assertEqual(list(Path(root).iterdir()), [])
            result = export_cpa_package(prep, Path(root)/"protected", protected)
            self.assertTrue(result["protected_profiles_included"])
            text = json.dumps(result) + (Path(root)/"protected"/"preparation.json").read_text()
            self.assertNotIn(protected["payer"]["tin"], text)
            stored = json.loads((Path(root)/"protected"/"protected-tax-profiles.json").read_text())
            self.assertEqual(stored, protected)
            self.assertEqual((Path(root)/"protected"/"protected-tax-profiles.json").stat().st_mode & 0o777, 0o600)

    def test_mixed_allocations_reconcile_included_excluded_blocked(self):
        p = payload(2025, amount="800.00")
        p["recipients"][0]["tax_classification"] = "s_corporation"
        p["payments"][0]["allocations"] = [
            {"amount": "700.00", "tax_category": "legal_services", "classification_verified": True},
            {"amount": "100.00", "tax_category": "rent", "classification_verified": True}]
        r = prepare_1099(p)
        self.assertEqual(r["totals"], {"included": "700.00", "excluded": "100.00", "blocked": "0.00"})
        self.assertEqual(r["forms"][0]["boxes"], {"1": "700.00"})
        p["payments"][0]["allocations"][1]["tax_category"] = "unknown"
        r = prepare_1099(p)
        self.assertEqual(r["totals"], {"included": "700.00", "excluded": "0.00", "blocked": "100.00"})
        self.assertFalse(r["ready"])
        self.assertEqual(r["forms"][0]["boxes"], {"1": "700.00"})
        self.assertFalse(r["forms"][0]["ready"])

    def test_state_withholding_and_refund_category_overflow_block(self):
        p = payload(); p["payments"][0]["state_withholding"] = "10.00"
        self.assertIn("state_withholding_not_supported", [i["code"] for i in prepare_1099(p)["issues"]])
        p = payload(amount="3000.00")
        p["payments"][0]["allocations"] = [{"amount": "2000.00", "tax_category": "legal_services", "classification_verified": True}, {"amount": "1000.00", "tax_category": "rent", "classification_verified": True}]
        refund = copy.deepcopy(p["payments"][0]); refund.update(id="refund", direction="receive", refund_of="pay-a", date="2026-03-01", gross_amount="2000.00", cash_amount="2000.00", source={"source_id": "refund-source"})
        refund["allocations"][0]["amount"] = "500.00"; refund["allocations"][1]["amount"] = "1500.00"
        p["payments"].append(refund)
        self.assertIn("refund_category_exceeds_original", [i["code"] for i in prepare_1099(p)["issues"]])


if __name__ == "__main__": unittest.main()
