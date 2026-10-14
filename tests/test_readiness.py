from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.readiness import DependencyVersions, assess_review_readiness
from bookkeeping.reconcile import reconcile
from test_queue import _make_entity


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.entity = _make_entity(Path(self.temp.name))
        config_path = self.entity.path / "entity.json"
        config = json.loads(config_path.read_text())
        config["declared_sources"] = [{
            "name": "Synthetic checking",
            "account": "Assets:Bank:Checking",
            "coverage_from": "2026-09-01",
            "coverage_to": "2026-09-30",
        }]
        config_path.write_text(json.dumps(config, sort_keys=True) + "\n")
        store = LedgerStore(self.entity.path / "ledger.sqlite")
        with store.transaction() as conn:
            store.insert_entries([Entry(
                date=date(2026, 9, 15), narration="Synthetic receipt", flag="*",
                meta=(("source-id", "synthetic-receipt"),),
                postings=(
                    Posting("Assets:Bank:Checking", Decimal("100.00"), "USD"),
                    Posting("Income:Consulting", Decimal("-100.00"), "USD"),
                ),
            )], conn)
        self.reconcile_complete()
        self.dependencies = DependencyVersions(17, 9, 4, 3)

    def reconcile_complete(self):
        return reconcile(
            self.entity.path, "Assets:Bank:Checking", Decimal("100.00"), date(2026, 9, 30),
            source_opening_balance=Decimal("0.00"),
            source_transaction_total=Decimal("100.00"),
            source_snapshot_at="2026-10-01T12:00:00Z",
            source_through="2026-09-30",
            source_evidence_refs=["provider-record:statement-2026-09"],
            source_evidence_hashes={"provider-record:statement-2026-09": "a" * 64},
        )

    def assess(self, dependencies=None, period="2026-09", as_of=date(2026, 10, 13)):
        return assess_review_readiness(
            self.entity.path, period, dependencies or self.dependencies, as_of=as_of,
        ).to_document()

    def test_complete_period_is_ready_with_exact_report_totals(self):
        first = self.assess()
        second = self.assess()
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "ready_for_review")
        self.assertEqual(first["blockers"], [])
        self.assertRegex(first["package_id"], r"^[0-9a-f]{64}$")
        pnl = next(report for report in first["reports"] if report["kind"] == "pnl")
        self.assertEqual(pnl["totals"], {"net_income": "100.00"})
        self.assertFalse(pnl["provisional"])

    def test_missing_opening_statement_evidence_and_unreconciled_balance_block(self):
        reconcile(
            self.entity.path, "Assets:Bank:Checking", Decimal("90.00"), date(2026, 9, 30),
            source_snapshot_at="2026-10-01T12:00:00Z", source_through="2026-09-30",
        )
        result = self.assess()
        codes = {item["code"] for item in result["blockers"]}
        self.assertIn("opening_evidence_missing", codes)
        self.assertIn("reconciliation_not_clean", codes)
        self.assertIn("source_evidence_provenance_missing", codes)
        self.assertTrue(all(report["provisional"] for report in result["reports"]))

    def test_stored_clean_status_cannot_override_tampered_reconciliation_math(self):
        path = self.entity.path / "reports" / "reconciliation.json"
        records = json.loads(path.read_text())
        records[0]["source_balance"] = "999.00"
        records[0]["status"] = "clean"
        records[0]["source_integrity_residual"] = "0.00"
        path.write_text(json.dumps(records) + "\n")
        result = self.assess()
        self.assertIn("reconciliation_not_clean", {item["code"] for item in result["blockers"]})

    def test_zero_reconciliation_cannot_make_an_unknown_source_account_ready(self):
        config_path = self.entity.path / "entity.json"
        config = json.loads(config_path.read_text())
        config["declared_sources"].append({
            "name": "Unknown bank",
            "account": "Assets:Bank:Ghost",
            "coverage_from": "2026-09-01",
            "coverage_to": "2026-09-30",
        })
        config_path.write_text(json.dumps(config) + "\n")
        reconcile(
            self.entity.path, "Assets:Bank:Ghost", Decimal("0.00"), date(2026, 9, 30),
            source_opening_balance=Decimal("0.00"),
            source_transaction_total=Decimal("0.00"),
            source_snapshot_at="2026-10-01T12:00:00Z",
            source_through="2026-09-30",
            source_evidence_refs=["provider-record:ghost-2026-09"],
            source_evidence_hashes={"provider-record:ghost-2026-09": "b" * 64},
        )
        result = self.assess()
        invalid = [item for item in result["blockers"] if item["code"] == "source_account_invalid"]
        self.assertEqual(invalid[0]["refs"], ["Assets:Bank:Ghost"])
        self.assertEqual(result["state"], "blocked")

    def test_source_snapshot_must_be_timezone_aware_and_available_by_as_of(self):
        path = self.entity.path / "reports" / "reconciliation.json"
        for timestamp in ("2026-10-01T12:00:00", "2026-10-14T12:00:00Z"):
            with self.subTest(timestamp=timestamp):
                records = json.loads(path.read_text())
                records[0]["source_snapshot_at"] = timestamp
                path.write_text(json.dumps(records) + "\n")
                result = self.assess()
                self.assertIn("source_statement_incomplete", {item["code"] for item in result["blockers"]})

    def test_source_coverage_queue_duplicates_and_questions_are_period_sensitive(self):
        config_path = self.entity.path / "entity.json"
        config = json.loads(config_path.read_text())
        config["declared_sources"][0]["coverage_to"] = "2026-09-15"
        config_path.write_text(json.dumps(config) + "\n")
        (self.entity.path / "staging" / "pending-categorization.json").write_text(json.dumps([
            {"id": "prior", "date": "2026-08-31"},
            {"id": "current", "date": "2026-09-12"},
        ]))
        (self.entity.path / "staging" / "duplicate-candidates.json").write_text(json.dumps([
            {"source_id": "dup", "status": "duplicate-candidate", "transaction": {"date": "2026-09-20"}},
        ]))
        (self.entity.path / "reports" / "open-questions.json").write_text(json.dumps([
            {"source_id": "question", "period": "2026-09", "status": "open"},
            {"source_id": "answered", "period": "2026-09", "status": "answered"},
        ]))
        result = self.assess()
        codes = {item["code"] for item in result["blockers"]}
        self.assertEqual({"source_coverage_incomplete", "unresolved_period_work", "unresolved_questions"} - codes, set())
        work = next(item for item in result["blockers"] if item["code"] == "unresolved_period_work")
        self.assertEqual(work["refs"], ["current", "dup"])

    def test_changed_source_reproposal_state_remains_unresolved_without_staging_row(self):
        queue_path = self.entity.path / "review-queue" / "changed-source.json"
        queue_path.write_text(json.dumps({
            "source_id": "changed-source",
            "date": "2026-09-12",
            "status": "requires-reproposal",
            "proposed_category": None,
            "suggested_category": "Expenses:Software",
            "requires_human_review": True,
        }) + "\n")
        result = self.assess()
        work = next(item for item in result["blockers"] if item["code"] == "unresolved_period_work")
        self.assertEqual(work["refs"], ["changed-source"])
        self.assertEqual(result["state"], "blocked")

    def test_current_and_future_periods_never_claim_final_readiness(self):
        for period, as_of, code in [
            ("2026-10", date(2026, 10, 13), "period_current"),
            ("2026-11", date(2026, 10, 13), "period_future"),
        ]:
            with self.subTest(period=period):
                result = self.assess(period=period, as_of=as_of)
                self.assertEqual(result["state"], "blocked")
                self.assertIn(code, {item["code"] for item in result["blockers"]})

    def test_all_host_dependency_versions_invalidate_package_without_ledger_change(self):
        original = self.assess()
        for field in ("books_revision", "source_revision", "reply_revision", "policy_version"):
            changed = self.assess(replace(self.dependencies, **{field: getattr(self.dependencies, field) + 1}))
            self.assertNotEqual(changed["package_id"], original["package_id"], field)
            self.assertEqual(changed["evidence_fingerprint"], original["evidence_fingerprint"])

    def test_assessment_does_not_write_company_state(self):
        before = {str(path.relative_to(self.entity.path)): path.read_bytes()
                  for path in self.entity.path.rglob("*") if path.is_file()}
        self.assess()
        after = {str(path.relative_to(self.entity.path)): path.read_bytes()
                 for path in self.entity.path.rglob("*") if path.is_file()}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
