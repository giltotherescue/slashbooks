"""Bank rows that clear long after QuickBooks recorded the money, and the review of held rows.

Synthetic data only. The history is built with the same helper as the fiscal-year tests."""
from datetime import date, timedelta
from decimal import Decimal
import json
import unittest
from unittest import mock

from bookkeeping.ledger.importer import import_transactions
from bookkeeping.ledger.store import LedgerStore, default_store_path
from bookkeeping import quickbooks_history as qh
from bookkeeping.quickbooks_history import (HISTORY_BANK_LAG_DAYS, list_buffer_decisions, release_due_buffer,
                                            resolve_buffer_decision, sync_transactions)

from test_quickbooks_history_fiscal import FiscalCases

END = date(2026, 2, 28)


class LagCases(FiscalCases):
    def history(self, events):
        self.build(first='2026-01-01', last='2026-02-28', opening=self.BASE, events=events)
        plan = sync_transactions(self.entity, self.path)
        self.assertTrue(plan['ready'], plan['blockers'])
        sync_transactions(self.entity, self.path, apply=True, plan_hash=plan['plan_hash'])
        self.entity.entity_config['bank_account_mappings'] = {'feed-1': 'Assets:Bank:Checking', 'feed-2': 'Assets:Bank:Savings'}

    def row(self, name, day, amount, feed='feed-1'):
        return {'id': 'plaid:' + name, 'date': day, 'amount': amount, 'description': 'Synthetic bank row ' + name,
                'accountId': feed, 'accountName': 'Synthetic', 'pending': False}

    def ingest(self, *rows):
        return import_transactions(self.entity, list(rows), 'lag-test', categorizer=lambda t: ('Income:Sales', 'auto'))

    def state(self, name):
        sid = 'plaid:' + name
        held = [c for c in json.loads((self.entity.staging_dir / 'duplicate-candidates.json').read_text())
                if c['source_id'] == sid and c['status'] == 'duplicate-candidate'] \
            if (self.entity.staging_dir / 'duplicate-candidates.json').exists() else []
        if held:
            return 'held'
        if any(e.source_id == sid for e in LedgerStore(default_store_path(self.entity.path)).load_entries()):
            return 'posted'
        pending = self.entity.staging_dir / 'pending-categorization.json'
        if pending.exists() and any(t.get('id') == sid for t in json.loads(pending.read_text())):
            return 'pending'
        return 'none'

    def held(self, name):
        return next(r for r in list_buffer_decisions(self.entity) if r['source_id'] == 'plaid:' + name)

    def day(self, base, lag):
        return (date.fromisoformat(base) + timedelta(days=lag)).isoformat()

    DEPOSIT = [('2026-02-27', {'1': '30', '4': '-30'})]

    def test_the_named_constant(self):
        self.assertEqual(HISTORY_BANK_LAG_DAYS, 45)

    def test_same_amount_row_after_a_lag_of_4_10_30_and_45_days_is_held_and_never_released_by_time(self):
        self.history(self.DEPOSIT)
        for lag in (4, 10, 30, 45):
            with self.subTest(lag=lag):
                self.ingest(self.row('lag%d' % lag, self.day('2026-02-27', lag), '30'))
                self.assertEqual(self.state('lag%d' % lag), 'held')
                row = self.held('lag%d' % lag)
                self.assertEqual(row['hold_rule'], 'unmatched_history_leg')
                leg, = row['candidate_legs']
                self.assertEqual((leg['date'], leg['amount'], leg['type'], leg['account']),
                                 ('2026-02-27', '30', 'JournalEntry', 'Assets:Bank:Checking'))
                self.assertEqual(leg['days_before_row'], lag)
                self.assertIn('doc_number', leg);self.assertIn('name', leg)
        due = release_due_buffer(self.entity, now='2027-01-01T00:00:00Z')
        self.assertEqual(due['released'], [])
        self.assertEqual(len(due['needs_review']), 4)

    def test_row_46_days_after_the_leg_goes_the_normal_path(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('lag46', self.day('2026-02-27', 46), '30'))
        self.assertNotEqual(self.state('lag46'), 'held')
        self.assertIn(self.state('lag46'), {'posted', 'pending'})

    def test_row_with_no_same_amount_leg_after_the_settlement_days_goes_the_normal_path_at_once(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('other', '2026-03-20', '31.07'))
        self.assertIn(self.state('other'), {'posted', 'pending'})

    def test_opposite_direction_is_not_a_candidate(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('opposite', '2026-03-20', '-30'))
        self.assertIn(self.state('opposite'), {'posted', 'pending'})

    def test_outstanding_check_clearing_after_the_period_is_held(self):
        self.history([('2026-02-20', {'5': '75', '1': '-75'})])
        self.ingest(self.row('check', '2026-03-15', '-75'))
        self.assertEqual(self.state('check'), 'held')
        self.assertEqual(self.held('check')['candidate_legs'][0]['amount'], '-75')

    def test_two_same_amount_legs_and_one_bank_row_need_a_person_and_each_leg_matches_once(self):
        self.history([('2026-02-10', {'1': '30', '4': '-30'}), ('2026-02-20', {'1': '30', '4': '-30'})])
        self.ingest(self.row('first', '2026-03-05', '30'))
        row = self.held('first')
        self.assertEqual({c['date'] for c in row['candidate_legs']}, {'2026-02-10', '2026-02-20'})
        self.assertEqual(release_due_buffer(self.entity, now='2027-01-01T00:00:00Z')['released'], [])
        first_leg = next(c['source_id'] for c in row['candidate_legs'] if c['date'] == '2026-02-10')
        resolve_buffer_decision(self.entity, 'plaid:first', 'history-match', first_leg)
        # One leg is left, so the next row is still a possible duplicate; the matched leg is not a candidate.
        self.ingest(self.row('second', '2026-03-06', '30'))
        self.assertEqual([c['date'] for c in self.held('second')['candidate_legs']], ['2026-02-20'])
        with self.assertRaisesRegex(ValueError, 'unmatched'):
            resolve_buffer_decision(self.entity, 'plaid:second', 'history-match', first_leg)
        resolve_buffer_decision(self.entity, 'plaid:second', 'history-match',
                                self.held('second')['candidate_legs'][0]['source_id'])
        self.ingest(self.row('third', '2026-03-07', '30'))
        self.assertIn(self.state('third'), {'posted', 'pending'})

    def test_one_leg_and_two_same_amount_rows_the_second_goes_normal_after_the_first_matches(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('one', '2026-03-05', '30'))
        leg = self.held('one')['candidate_legs'][0]['source_id']
        resolve_buffer_decision(self.entity, 'plaid:one', 'history-match', leg)
        self.assertEqual(self.state('one'), 'none')
        self.ingest(self.row('two', '2026-03-06', '30'))
        self.assertIn(self.state('two'), {'posted', 'pending'})
        self.assertEqual(sum(e.source_id == 'plaid:one' for e in LedgerStore(default_store_path(self.entity.path)).load_entries()), 0)

    def test_two_rows_in_one_batch_are_both_held_until_a_person_decides(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('a', '2026-03-05', '30'), self.row('b', '2026-03-06', '30'))
        self.assertEqual((self.state('a'), self.state('b')), ('held', 'held'))

    def test_a_leg_matched_by_a_row_inside_the_period_is_not_a_candidate(self):
        self.history([('2026-02-20', {'1': '30', '4': '-30'})])
        self.ingest(self.row('inside', '2026-02-21', '30'))
        self.assertEqual(self.held('inside')['hold_rule'], 'inside_period')
        resolve_buffer_decision(self.entity, 'plaid:inside', 'history-match', self.held('inside')['candidate_legs'][0]['source_id'])
        self.ingest(self.row('after', '2026-03-10', '30'))
        self.assertIn(self.state('after'), {'posted', 'pending'})

    def test_a_row_inside_the_period_with_no_candidate_waits_for_a_person(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('inside', '2026-02-10', '8.88'))
        self.assertEqual(self.state('inside'), 'held')
        self.assertEqual(release_due_buffer(self.entity, now='2027-01-01T00:00:00Z'),
                         {'released': [], 'needs_review': ['plaid:inside'], 'errors': []})

    def test_transfer_between_two_mapped_accounts_matches_each_leg_by_account_and_direction(self):
        self.history([('2026-02-26', {'1': '-20', '11': '20'})])
        self.ingest(self.row('out', '2026-03-10', '-20'), self.row('in', '2026-03-10', '20', feed='feed-2'),
                    self.row('wrong-account', '2026-03-10', '20'), self.row('wrong-way', '2026-03-10', '-20', feed='feed-2'))
        self.assertEqual((self.state('out'), self.state('in')), ('held', 'held'))
        self.assertIn(self.state('wrong-account'), {'posted', 'pending'})
        self.assertIn(self.state('wrong-way'), {'posted', 'pending'})
        self.assertEqual(self.held('out')['candidate_legs'][0]['account'], 'Assets:Bank:Checking')
        self.assertEqual(self.held('in')['candidate_legs'][0]['account'], 'Assets:Bank:Savings')

    def test_the_bank_row_arrives_before_the_import_and_the_import_refuses(self):
        self.build(first='2026-01-01', last='2026-02-28', opening=self.BASE, events=self.DEPOSIT)
        self.entity.entity_config['bank_account_mappings'] = {'feed-1': 'Assets:Bank:Checking'}
        self.ingest(self.row('early', '2026-03-04', '30'))
        plan = sync_transactions(self.entity, self.path)
        self.assertFalse(plan['ready'])
        self.assertTrue({b['code'] for b in plan['blockers']} & {'existing_accounting_requires_review', 'pending_accounting_requires_review'})

    def test_a_second_run_changes_nothing(self):
        self.history(self.DEPOSIT)
        row = self.row('again', '2026-03-10', '30')
        self.ingest(row);self.ingest(row)
        self.assertEqual(len(list_buffer_decisions(self.entity)), 1)
        store = LedgerStore(default_store_path(self.entity.path));before = store.content_digest()
        self.ingest(row)
        self.assertEqual(store.content_digest(), before)
        leg = self.held('again')['candidate_legs'][0]['source_id']
        resolve_buffer_decision(self.entity, 'plaid:again', 'history-match', leg)
        resolve_buffer_decision(self.entity, 'plaid:again', 'history-match', leg)
        self.ingest(row)
        self.assertEqual(self.state('again'), 'none')
        self.assertEqual(store.content_digest(), before)
        again = sync_transactions(self.entity, self.path)
        self.assertTrue(again['already_applied'])

    def test_history_match_needs_the_same_amount_direction_and_an_unmatched_leg(self):
        self.history([('2026-02-27', {'1': '30', '4': '-30'}), ('2026-02-26', {'1': '-30', '5': '30'})])
        self.ingest(self.row('credit', '2026-03-05', '30'))
        legs = {c['amount']: c['source_id'] for c in self.held('credit')['candidate_legs']}
        self.assertEqual(set(legs), {'30'})
        wrong_direction = next(l['source_id'] for l in qh.HistoryIndex(self.entity).legs() if l['amount'] == '-30')
        with self.assertRaisesRegex(ValueError, 'same account, amount and direction'):
            resolve_buffer_decision(self.entity, 'plaid:credit', 'history-match', wrong_direction)
        self.ingest(self.row('amount', '2026-03-05', '30.01'))
        self.assertIn(self.state('amount'), {'posted', 'pending'})

    def test_released_row_is_named_truthfully_and_posts_once(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('free', '2026-03-05', '30'))
        result = resolve_buffer_decision(self.entity, 'plaid:free', 'release')
        self.assertEqual(result['status'], 'released-to-intake')
        candidate = next(c for c in json.loads((self.entity.staging_dir / 'duplicate-candidates.json').read_text()) if c['source_id'] == 'plaid:free')
        self.assertEqual(candidate['status'], 'released-to-intake')
        self.ingest(self.row('free', '2026-03-05', '30'))
        entries = LedgerStore(default_store_path(self.entity.path)).load_entries()
        self.assertLessEqual(sum(e.source_id == 'plaid:free' for e in entries), 1)

    def test_release_of_1000_rows_loads_decisions_and_entries_a_fixed_number_of_times(self):
        self.history(self.DEPOSIT)
        rows = [self.row('bulk%d' % i, '2026-03-03', '%d.01' % (i + 1000)) for i in range(1000)]
        self.ingest(*rows)
        self.assertEqual(len(list_buffer_decisions(self.entity)), 1000)
        counts = {'entries': 0, 'decisions': 0, 'intake': 0}
        real_entries, real_decisions = LedgerStore.load_entries, qh._buffer_decisions
        from bookkeeping.ledger import importer
        real_intake = importer.import_transactions

        def entries(store, *args, **kwargs):
            if 'conn' not in kwargs:  # the store calls itself once with a connection
                counts['entries'] += 1
            return real_entries(store, *args, **kwargs)

        def decisions(entity):
            counts['decisions'] += 1
            return real_decisions(entity)

        def intake(*args, **kwargs):
            counts['intake'] += 1
            return real_intake(*args, **kwargs)
        with mock.patch.object(LedgerStore, 'load_entries', entries), mock.patch.object(qh, '_buffer_decisions', decisions), \
                mock.patch.object(importer, 'import_transactions', intake):
            out = release_due_buffer(self.entity, now='2027-01-01T00:00:00Z')
        self.assertEqual((len(out['released']), out['needs_review'], out['errors']), (1000, [], []))
        # The work does not grow with the rows: a fixed number of loads and one intake call.
        self.assertLessEqual(counts['entries'], 4)
        self.assertLessEqual(counts['decisions'], 4)
        self.assertEqual(counts['intake'], 1)

    def test_one_failing_row_does_not_stop_the_rows_after_it(self):
        self.history(self.DEPOSIT)
        self.ingest(*[self.row(n, '2026-03-03', a) for n, a in (('a', '5.01'), ('bad', '6.01'), ('c', '7.01'), ('d', '8.01'))])
        from bookkeeping.ledger import importer
        real = importer.import_transactions

        def flaky(entity, txns, *args, **kwargs):
            if any(t['id'] == 'plaid:bad' for t in txns):
                raise OSError('synthetic failure')
            return real(entity, txns, *args, **kwargs)
        with mock.patch.object(importer, 'import_transactions', flaky):
            out = release_due_buffer(self.entity, now='2027-01-01T00:00:00Z')
        self.assertEqual(sorted(out['released']), ['plaid:a', 'plaid:c', 'plaid:d'])
        self.assertEqual([e['source_id'] for e in out['errors']], ['plaid:bad'])
        self.assertIn('synthetic failure', out['errors'][0]['error'])
        self.assertEqual([r['source_id'] for r in list_buffer_decisions(self.entity)], ['plaid:bad'])
        # The next call retries the failed row only.
        out = release_due_buffer(self.entity, now='2027-01-02T00:00:00Z')
        self.assertEqual((out['released'], out['errors']), (['plaid:bad'], []))


class AccrualLagTests(LagCases, unittest.TestCase):
    basis = 'Accrual'


class CashLagTests(LagCases, unittest.TestCase):
    basis = 'Cash'


if __name__ == '__main__':
    unittest.main()
