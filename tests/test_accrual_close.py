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


class MidmonthCutoverReadinessTests(unittest.TestCase):
    fiscal_start = "2026-01-01"
    change = fixtures.AccrualReportsTests.change
    settle = fixtures.AccrualReportsTests.settle
    evidence_for = fixtures.AccrualReportsTests.evidence_for

    def setUp(self):
        fixtures.AccrualReportsTests.setUp(self)
        for path in self.exports.glob('*.csv'):
            path.write_text(path.read_text().replace('June 30, 2026', 'July 14, 2026').replace('January 1 -', f'January {int(self.fiscal_start[-2:])} -'))
        for schedule in self.support['schedules']:
            schedule['method'] = 'actual_day'
        config = json.loads((self.root / 'entity.json').read_text())
        config['cutover_date'] = '2026-07-15'
        (self.root / 'entity.json').write_text(json.dumps(config))
        self.entity = load_entity(self.root)
        from bookkeeping.accrual import migration
        migration.apply(self.entity, migration.preview(self.entity, self.exports, '2026-07-15',
            fiscal_start=self.fiscal_start, supporting=self.support))

    def prepare(self, *, recognize=True):
        self.settle('partial-customer-receipt', 'customer-open', '400.00', '2026-07-20')
        self.settle('partial-vendor-payment', 'vendor-open', '-200.00', '2026-07-21')
        if recognize:
            schedules.apply_due(self.entity, schedules.preview_due(self.entity, '2026-08-31', operation_id='partial-and-august-recognition'))
        config = json.loads((self.root / 'entity.json').read_text())
        config['declared_sources'] = [{'name': 'Synthetic checking', 'account': 'Assets:Bank:Checking',
            'coverage_from': '2026-07-15', 'coverage_to': '2026-08-31'}]
        (self.root / 'entity.json').write_text(json.dumps(config))
        self.entity = load_entity(self.root)
        for month, opening, activity in [(7, '10000.00', '200.00'), (8, '10200.00', '0.00')]:
            reconcile(self.root, 'Assets:Bank:Checking', Decimal('10200.00'), date(2026, month, 31),
                source_opening_balance=Decimal(opening), source_transaction_total=Decimal(activity),
                source_snapshot_at='2026-09-01T12:00:00Z', source_through=f'2026-{month:02d}-31',
                source_evidence_refs=[f'synthetic:statement-{month}'],
                source_evidence_hashes={f'synthetic:statement-{month}': 'a' * 64})

    def assess(self, period='2026-07'):
        return assess_review_readiness(self.root, period, local_dependency_versions(self.root), as_of=date(2026, 9, 2)).to_document()

    def test_partial_activity_is_explicit_reviewed_and_ytd_prefix_counted_once(self):
        from bookkeeping.reports import statements
        self.prepare()
        july = self.assess()
        self.assertEqual(july['state'], 'ready_for_review', july)
        self.assertEqual((july['activity_from'], july['activity_to'], july['partial_post_cutover'], july['imported_summary_through']),
                         ('2026-07-15', '2026-07-31', True, '2026-07-14'))
        self.assertEqual(july['sources'][0]['requested_from'], '2026-07-15')
        self.assertTrue(all(row['period_from'] == '2026-07-15' for row in july['reports']))
        august = self.assess('2026-08')
        self.assertEqual(august['state'], 'ready_for_review', august)
        self.assertEqual((august['activity_from'], august['partial_post_cutover']), ('2026-08-01', False))
        activity = statements.profit_and_loss(self.root, date(2026, 7, 15), date(2026, 8, 31))
        ytd = statements.profit_and_loss(self.root, date.fromisoformat(self.fiscal_start), date(2026, 8, 31))
        # July recognizes 90 revenue and 60 expense; August recognizes
        # 164.11 and 109.41. Residual cents stay in the final installment.
        self.assertEqual(activity.totals['net_income'], Decimal('84.70'))
        self.assertEqual(ytd.totals['net_income'], Decimal('6884.70'))
        self.assertTrue(ytd.meta['summary_included'])
        with self.assertRaises(reports.CoverageError):
            statements.profit_and_loss(self.root, date(2026, 7, 1), date(2026, 7, 31))
        request = {'operation_id': 'close-reviewed-partial', 'period': '2026-07', 'action': 'close', 'as_of': '2026-09-02',
                   'reason': 'Review exact initial activity interval', 'readiness_package_id': july['package_id']}
        reports.apply_period(self.entity, reports.preview_period(self.entity, request))
        self.assertEqual(load_record(self.store, 'periods', '2026-07')['status'], 'closed')

    def test_source_coverage_opening_reconciliation_and_questions_are_not_skipped(self):
        self.prepare()
        path = self.root / 'reports/reconciliation.json'
        original = path.read_text()
        rows = json.loads(original)
        july = next(row for row in rows if row['as_of'] == '2026-07-31')
        # This remains a balanced full-window pair, but starts before cutover.
        july.update(source_opening_balance='9900.00', source_transaction_total='300.00')
        path.write_text(json.dumps(rows))
        self.assertIn('cutover_opening_mismatch', {row['code'] for row in self.assess()['blockers']})
        path.write_text(json.dumps([row for row in rows if row['as_of'] != '2026-07-31']))
        self.assertIn('reconciliation_missing', {row['code'] for row in self.assess()['blockers']})
        path.write_text(original)
        config_path = self.root / 'entity.json'
        config = json.loads(config_path.read_text()); config['declared_sources'][0]['coverage_from'] = '2026-07-16'
        config_path.write_text(json.dumps(config))
        self.assertIn('source_coverage_incomplete', {row['code'] for row in self.assess()['blockers']})
        config['declared_sources'][0]['coverage_from'] = '2026-07-15'; config_path.write_text(json.dumps(config))
        before = self.assess()
        (self.root / 'reports/open-questions.json').write_text(json.dumps([{'date': '2026-07-20', 'source_id': 'partial-question'}]))
        self.assertIn('unresolved_questions', {row['code'] for row in self.assess()['blockers']})
        (self.root / 'reports/open-questions.json').write_text('[]')
        self.evidence_for('new-partial-evidence')
        with self.assertRaisesRegex(ValueError, 'readiness changed'):
            reports.preview_period(self.entity, {'operation_id': 'stale-partial-close', 'period': '2026-07', 'action': 'close',
                'as_of': '2026-09-02', 'reason': 'Stale partial review', 'readiness_package_id': before['package_id']})

    def test_unposted_initial_partial_recognition_blocks_readiness(self):
        self.prepare(recognize=False)
        self.assertIn('recognition_due', {row['code'] for row in self.assess()['blockers']})
