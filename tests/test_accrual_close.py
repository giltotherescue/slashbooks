"""Real local close/reopen, stale readiness, and dated reversal proofs."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from bookkeeping.accrual import journals, items, reports, schedules
from bookkeeping.accrual.store import load_record
from bookkeeping.entity import load_entity
from bookkeeping.readiness import assess_review_readiness, local_dependency_versions
from bookkeeping.reconcile import reconcile
from bookkeeping.preparation_runtime import PreparationDenied
from tests import test_accrual_reports as fixtures


class AccrualCloseTests(unittest.TestCase):
    setUp = fixtures.AccrualReportsTests.setUp
    change = fixtures.AccrualReportsTests.change
    preview = fixtures.AccrualReportsTests.preview
    opening = fixtures.AccrualReportsTests.opening
    july = fixtures.AccrualReportsTests.july
    settle = fixtures.AccrualReportsTests.settle
    journal = fixtures.AccrualReportsTests.journal
    evidence_for = fixtures.AccrualReportsTests.evidence_for

    def ready(self):
        config = self.root / "entity.json"
        value = json.loads(config.read_text())
        value["declared_sources"] = [{"name": "Synthetic checking", "account": "Assets:Bank:Checking", "coverage_from": "2026-07-01", "coverage_to": "2026-07-31"}]
        config.write_text(json.dumps(value))
        self.entity = load_entity(self.root)
        reconcile(self.root, "Assets:Bank:Checking", Decimal("9600.00"), date(2026, 7, 31),
            source_opening_balance=Decimal("10000.00"), source_transaction_total=Decimal("-400.00"),
            source_snapshot_at="2026-08-01T12:00:00Z", source_through="2026-07-31",
            source_evidence_refs=["synthetic:statement-july"], source_evidence_hashes={"synthetic:statement-july": "a" * 64})
        result = assess_review_readiness(self.root, "2026-07", local_dependency_versions(self.root), as_of=date(2026, 8, 2))
        self.assertEqual(result.state, "ready_for_review", result.to_document())
        return result

    def close(self, assessment, operation_id="close-july"):
        payload = {"operation_id": operation_id, "period": "2026-07", "action": "close", "as_of": "2026-08-02",
                   "reason": "Reviewed synthetic July books", "readiness_package_id": assessment.package_id}
        return reports.preview_period(self.entity, payload)

    def test_real_close_blocks_financial_routes_and_reopen_requires_fresh_readiness(self):
        self.opening()
        self.july()
        assessment = self.ready()
        plan = self.close(assessment)
        receipt = reports.apply_period(self.entity, plan)
        self.assertEqual(reports.apply_period(self.entity, plan), receipt)
        self.assertEqual(load_record(self.store, "periods", "2026-07")["status"], "closed")
        with self.assertRaisesRegex(ValueError, "closed"):
            self.journal("closed-depreciation", "2026-07-31", [("Expenses:Operating-Costs", "10.00"), ("Assets:Fixed:Accumulated-Depreciation", "-10.00")])
        with self.assertRaisesRegex(ValueError, "closed"):
            self.settle("closed-receipt", "customer-open", "100.00", "2026-07-31")
        support = {"operation_id": "closed-support", "account": "Assets:Fixed:Equipment", "as_of": "2026-07-31", "currency": "USD",
                   "details": [{"id": "asset", "balance": "2400.00"}], "evidence_ids": [self.evidence]}
        with self.assertRaisesRegex(ValueError, "closed"):
            reports.preview_support(self.entity, support)
        reopen = reports.preview_period(self.entity, {"operation_id": "reopen-july", "period": "2026-07", "action": "reopen",
            "as_of": "2026-08-02", "reason": "Correct explicit provider depreciation"})
        reports.apply_period(self.entity, reopen)
        self.assertEqual(load_record(self.store, "periods", "2026-07")["status"], "open")
        self.journal("correct-depreciation", "2026-07-31", [("Expenses:Operating-Costs", "10.00"), ("Assets:Fixed:Accumulated-Depreciation", "-10.00")])
        with self.assertRaisesRegex(ValueError, "readiness has unresolved"):
            self.close(assessment, "stale-close")
        support.update(operation_id="corrected-depreciation-support", account="Assets:Fixed:Accumulated-Depreciation",
                       details=[{"id": "depreciation", "balance": "-450.00"}], supersedes="external-support:july-depreciation-support")
        reports.apply_support(self.entity, reports.preview_support(self.entity, support))
        with self.assertRaisesRegex(ValueError, "readiness changed"):
            self.close(assessment, "stale-close-after-repair")
        current = self.ready()
        self.assertNotEqual(current.package_id, assessment.package_id)
        reports.apply_period(self.entity, self.close(current, "reclose-july"))

    def test_support_or_evidence_only_mutation_invalidates_readiness(self):
        self.opening()
        self.july()
        before = self.ready()
        count = self.store.counts().entries
        self.evidence_for("additional-source-provenance", "2026-07-31")
        after = self.ready()
        self.assertEqual(count, self.store.counts().entries)
        self.assertNotEqual(before.to_document()["evidence_fingerprint"], after.to_document()["evidence_fingerprint"])
        with self.assertRaisesRegex(ValueError, "readiness changed"):
            self.close(before)

    def test_adjust_preparation_never_grants_close_or_reopen_authority(self):
        self.opening()
        self.july()
        assessment = self.ready()
        plan = self.close(assessment)
        with patch("bookkeeping.preparation_runtime.is_preparation", return_value=True):
            with self.assertRaises(PreparationDenied):
                reports.apply_period(self.entity, plan)
            with self.assertRaises(PreparationDenied):
                self.close(assessment)
        self.assertIsNone(load_record(self.store, "periods", "2026-07"))

    def test_future_posted_reversal_does_not_clear_earlier_due_or_balance(self):
        self.opening()
        request = {"operation_id": "reverse-payroll-origin", "id": "reverse-payroll", "kind": "accrual", "account": "Liabilities:Other:Payroll-Payable",
                   "original_amount": "50.00", "as_of": "2026-07-15", "currency": "USD", "settlement_strategy": "reverse", "reversal_date": "2026-08-01",
                   "origin": {"mode": "new", "source_id": "reverse-payroll-provider", "counter_account": "Expenses:Operating-Costs"},
                   "evidence_ids": [self.evidence_for("reverse-payroll-provider")]}
        items.apply_item(self.entity, items.preview_item(self.entity, request))
        journals.apply_correction(self.entity, journals.preview_correction(self.entity, {"id": "late-payroll-reversal", "original_id": "reverse-payroll-origin",
            "date": "2026-09-01", "description": "Late explicitly dated reversal"}, reverse_only=True))
        august = reports.report(self.entity, "outstanding", as_of="2026-08-31")
        self.assertEqual(next(r["remaining_as_of"] for r in august["rows"] if r["id"] == "reverse-payroll"), "50.00")
        self.assertTrue(any(i["code"] == "reversal_due" for i in august["issues"]))
        september = reports.report(self.entity, "outstanding", as_of="2026-09-01")
        self.assertEqual(next(r["remaining_as_of"] for r in september["rows"] if r["id"] == "reverse-payroll"), "0.00")
        self.assertFalse(any(i["code"] == "reversal_due" for i in september["issues"]))
