"""Card-funded prepaids use statement reconciliation, not duplicate control support."""
from datetime import date
from decimal import Decimal
import json

from bookkeeping.accrual import reports, schedules
from bookkeeping.entity import add_account, load_entity
from bookkeeping.readiness import assess_review_readiness, local_dependency_versions
from bookkeeping.reconcile import reconcile
from tests.test_accrual_schedules import AccrualFixture, BANK, PREPAID, EXPENSE, AP


class CardReadinessTests(AccrualFixture):
    def setUp(self):
        super().setUp()
        self.card = 'Liabilities:CreditCard:Synthetic'
        add_account(self.root, self.card, open_date='2000-01-01')
        request = self.schedule(origin={'mode': 'new', 'source_id': 'annual-card-purchase', 'funding_account': self.card})
        schedules.apply_schedule(self.entity, schedules.preview_schedule(self.entity, request))
        schedules.apply_due(self.entity, schedules.preview_due(self.entity, '2026-01-31', operation_id='january-recognition'))

    def configure_sources(self, *, include_card=True, coverage_to='2026-01-31'):
        sources = [{'name': 'Synthetic checking', 'account': BANK, 'coverage_from': '2026-01-01', 'coverage_to': '2026-01-31'}]
        if include_card:
            sources.append({'name': 'Synthetic card', 'account': self.card, 'coverage_from': '2026-01-01', 'coverage_to': coverage_to})
        path = self.root / 'entity.json'
        config = json.loads(path.read_text())
        config['declared_sources'] = sources
        path.write_text(json.dumps(config))
        self.entity = load_entity(self.root)

    def reconcile_source(self, account, amount, identity):
        reconcile(self.root, account, Decimal(amount), date(2026, 1, 31),
            source_opening_balance=Decimal('0.00'), source_transaction_total=Decimal(amount),
            source_snapshot_at='2026-02-01T12:00:00Z', source_through='2026-01-31',
            source_evidence_refs=['synthetic:statement-' + identity], source_evidence_hashes={'synthetic:statement-' + identity: 'a' * 64})

    def assess(self):
        return assess_review_readiness(self.root, '2026-01', local_dependency_versions(self.root), as_of=date(2026, 2, 2)).to_document()

    def test_card_needs_declared_coverage_and_clean_statement_but_not_external_control_support(self):
        # Another fully reconciled source cannot hide an undeclared card balance.
        self.configure_sources(include_card=False)
        self.reconcile_source(BANK, '0.00', 'bank')
        assessment = self.assess()
        self.assertEqual(assessment['state'], 'blocked')
        self.assertIn(self.card, {row['account'] for row in assessment['sources']})
        self.assertTrue(any(row['code'] == 'source_coverage_incomplete' and row['refs'] == [self.card] for row in assessment['blockers']))
        self.assertTrue(any(row['code'] == 'reconciliation_missing' and row['refs'] == [self.card] for row in assessment['blockers']))
        self.assertFalse(any(row['code'] == 'control_balance_unreconciled' and self.card in row['refs'] for row in assessment['blockers']))

        self.configure_sources(coverage_to='2026-01-30')
        self.reconcile_source(self.card, '-1200.00', 'card')
        self.assertIn('source_coverage_incomplete', {row['code'] for row in self.assess()['blockers']})
        self.configure_sources()
        self.reconcile_source(self.card, '-1199.00', 'incorrect-card')
        self.assertIn('reconciliation_not_clean', {row['code'] for row in self.assess()['blockers']})
        self.reconcile_source(self.card, '-1200.00', 'card')
        ready = self.assess()
        self.assertEqual(ready['state'], 'ready_for_review', ready['blockers'])
        self.assertEqual(ready['blockers'], [])
        rows = reports.report(self.entity, 'reconciliation', as_of='2026-01-31')['rows']
        self.assertNotIn(self.card, {row['account'] for row in rows})
        self.assertEqual(next(row for row in rows if row['account'] == PREPAID)['status'], 'reconciled')

    def test_card_with_zero_ending_balance_still_requires_current_period_reconciliation(self):
        self.post('card-payment', '2026-01-15', [(self.card, '1200.00'), (BANK, '-1200.00')])
        self.configure_sources(include_card=False)
        self.reconcile_source(BANK, '-1200.00', 'bank')
        self.assertEqual(self.balances()[self.card], Decimal(0))
        assessment = self.assess()
        self.assertIn(self.card, {row['account'] for row in assessment['sources']})
        self.assertTrue(any(row['code'] == 'reconciliation_missing' and row['refs'] == [self.card] for row in assessment['blockers']))

    def test_coverage_account_cannot_redirect_a_required_or_mapped_card_to_clean_bank(self):
        self.configure_sources(include_card=False)
        self.reconcile_source(BANK, '0.00', 'bank')
        before = self.store.content_digest()
        for mapped in (False, True):
            with self.subTest(mapped=mapped):
                path = self.root / 'entity.json'
                config = json.loads(path.read_text())
                source = 'synthetic-card-feed' if mapped else self.card
                config['bank_account_mappings'] = {source: self.card} if mapped else {}
                config['source_coverage'] = {source: {'coverage_from': '2026-01-01', 'coverage_to': '2026-01-31',
                                                    'account': BANK, 'source': 'redirected-bank'}}
                path.write_text(json.dumps(config))
                assessment = self.assess()
                self.assertEqual(assessment['state'], 'blocked')
                card_sources = [row for row in assessment['sources'] if row['account'] == self.card]
                self.assertEqual(len(card_sources), 1)
                self.assertEqual(card_sources[0]['source'], source)
                self.assertTrue(any(row['code'] == 'reconciliation_missing' and row['refs'] == [self.card]
                                    for row in assessment['blockers']))
                self.assertEqual(self.store.content_digest(), before)

    def test_general_payables_and_card_prefix_lookalikes_are_ledger_only_not_cards(self):
        lookalike = 'Liabilities:CreditCardboard'
        add_account(self.root, lookalike, open_date='2000-01-01')
        self.post('other-liabilities', '2026-01-15', [(EXPENSE, '100.00'), (AP, '-50.00'), (lookalike, '-50.00')])
        rows = reports.report(self.entity, 'reconciliation', as_of='2026-01-31')['rows']
        by_account = {row['account']: row for row in rows}
        self.assertEqual(by_account[AP]['status'], 'ledger_only')
        self.assertEqual(by_account[lookalike]['status'], 'ledger_only')
        self.assertNotIn(self.card, by_account)
