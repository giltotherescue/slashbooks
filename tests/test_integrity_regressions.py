"""Synthetic regression proofs for policy, recovery, and source bookkeeping."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bookkeeping import import_check, preparation_runtime, queue, statement_import, stripe, tieout
from bookkeeping.accrual import journals, opening, schedules
from bookkeeping.accrual.store import SupportingMutation
from bookkeeping.connectors import ofx
from bookkeeping.connectors.bankcsv import StatementFileError
from bookkeeping.ledger import importer
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.ledger.staging import StagingStore
from bookkeeping.ledger.store import LedgerStore
import test_accrual_journals as journal_fixtures
import test_accrual_opening_v2 as opening_fixtures
import test_accrual_schedules as schedule_fixtures
import test_preparation_runtime as policy_fixtures
import test_queue as queue_fixtures
import test_statement_import as statement_fixtures
import test_stripe as stripe_fixtures
import test_tieout as tieout_fixtures

BANK = 'Assets:Bank:Checking'


class IntegrityRegressions(unittest.TestCase):
    def fixture(self, cls):
        fixture = cls()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if hasattr(fixture, 'tearDown'):
            self.addCleanup(fixture.tearDown)
        return fixture

    def test_future_opening_prepaid_waits_until_service_and_keeps_twelve_months(self):
        f = self.fixture(opening_fixtures.OpeningV2Tests)
        f.request['lines'][3]['amount'] = '-31400.00'
        f.request['lines'].append({'account': 'Assets:Other:Prepaid-Expenses', 'amount': '1200.00'})
        f.request['schedules'] = [dict(id='future', kind='prepaid', account='Assets:Other:Prepaid-Expenses',
            recognition_account='Expenses:Software', amount='1200.00', recognized_amount='0.00',
            service_start='2026-03-01', service_end='2027-02-28', method='equal_month')]
        plan = opening.preview_opening(f.entity, f.request)
        record = next(row['payload'] for row in plan['support'] if row['table'] == 'schedules')
        self.assertEqual(record['recognition_start'], '2026-03-01')
        self.assertEqual(len(schedules.installments(record)), 12)
        self.assertEqual({row['amount'] for row in schedules.installments(record)}, {'100.00'})
        opening.apply_opening(f.entity, plan)
        self.assertEqual(schedules.preview_due(f.entity, '2026-02-28', operation_id='before-service')['entries'], [])

    def test_future_deferred_opening_does_not_earn_before_service(self):
        f = self.fixture(opening_fixtures.OpeningV2Tests)
        f.request['lines'][3]['amount'] = '-29000.00'
        f.request['lines'].append({'account': 'Liabilities:Other:Deferred-Revenue', 'amount': '-1200.00'})
        f.request['schedules'] = [dict(id='future-deferred', kind='deferred', account='Liabilities:Other:Deferred-Revenue',
            recognition_account='Income:Consulting', amount='1200.00', recognized_amount='0.00',
            service_start='2026-03-01', service_end='2027-02-28', method='equal_month')]
        plan = opening.preview_opening(f.entity, f.request)
        opening.apply_opening(f.entity, plan)
        self.assertEqual(schedules.preview_due(f.entity, '2026-02-28', operation_id='early')['entries'], [])
        due = schedules.preview_due(f.entity, '2026-03-31', operation_id='march')
        self.assertEqual(len(due['entries']), 1)
        self.assertEqual({p['amount'] for p in due['entries'][0]['postings']}, {'100.00', '-100.00'})

    def test_opening_equal_month_partial_interval_is_rejected_at_preview(self):
        f = self.fixture(opening_fixtures.OpeningV2Tests)
        f.request['lines'][3]['amount'] = '-31400.00'
        f.request['lines'].append({'account': 'Assets:Other:Prepaid-Expenses', 'amount': '1200.00'})
        f.request['schedules'] = [dict(id='partial', kind='prepaid', account='Assets:Other:Prepaid-Expenses',
            recognition_account='Expenses:Software', amount='1200.00', recognized_amount='0.00',
            service_start='2026-03-15', service_end='2027-03-14', method='equal_month')]
        before = LedgerStore(f.root / 'ledger.sqlite').content_digest()
        with self.assertRaisesRegex(ValueError, 'whole calendar months'):
            opening.preview_opening(f.entity, f.request)
        self.assertEqual(LedgerStore(f.root / 'ledger.sqlite').content_digest(), before)

    def test_adjustment_ceiling_uses_whole_entry_debits_and_accepts_equality(self):
        for amount, blocked in [('500.00', False), ('500.01', True)]:
            with self.subTest(amount=amount):
                f = self.fixture(journal_fixtures.AccrualJournalTests)
                context = journal_fixtures.trusted_context()
                f.bind(replace(context, policy=replace(context.policy, ask_first_above=Decimal('500.00'))))
                f.request['postings'] = [dict(account='Expenses:Software', amount=str(Decimal(amount) / 2)),
                    dict(account='Expenses:Software', amount=str(Decimal(amount) - Decimal(amount) / 2)),
                    dict(account=BANK, amount='-' + amount)] if not blocked else [
                        dict(account='Expenses:Software', amount='250.00'),
                        dict(account='Expenses:Software', amount='250.01'), dict(account=BANK, amount='-500.01')]
                if blocked:
                    with self.assertRaisesRegex(queue.AskFirstError, 'ask-first'):
                        journals.preview_journal(f.entity, f.request)
                    self.assertEqual(f.store.counts().entries, 0)
                else:
                    journals.apply_journal(f.entity, journals.preview_journal(f.entity, f.request))
                    self.assertEqual(f.store.counts().entries, 1)

    def test_supporting_only_amount_cannot_bypass_ceiling(self):
        f = self.fixture(journal_fixtures.AccrualJournalTests)
        context = journal_fixtures.trusted_context()
        f.bind(replace(context, policy=replace(context.policy, ask_first_above=Decimal('500.00'))))
        for table in ('items', 'schedules'):
            with self.subTest(table=table), self.assertRaises(queue.AskFirstError):
                preparation_runtime.authorize_accrual_operation(f.entity, ['2026-01-31'], [], [],
                    [SupportingMutation(table, 'synthetic', {'amount': '1000.00'})])
        self.assertEqual(f.store.counts().entries, 0)

    def test_supporting_only_schedule_preview_enforces_ceiling_and_small_due_is_allowed(self):
        f = self.fixture(schedule_fixtures.AccrualFixture)
        f.post('opening', '2025-12-31', [(schedule_fixtures.PREPAID, '1200'), ('Equity:Opening-Balances', '-1200')])
        request = f.schedule(origin={'mode': 'opening', 'source_id': 'opening'})
        plan = schedules.preview_schedule(f.entity, request)
        self.assertEqual(plan['entries'], [])
        context = journal_fixtures.trusted_context()
        context = replace(context, policy=replace(context.policy, ask_first_above=Decimal('500')))
        from bookkeeping.accrual.store import load_record
        evidence = load_record(f.store, 'evidence', f.eid)
        def bind():
            token = preparation_runtime._active.set(preparation_runtime._Boundary(context, f.root.resolve(),
                f.store.content_digest(), ((f.eid, evidence['evidence_hash']),), '', '', 'adjust'))
            return token
        token = bind()
        try:
            with self.assertRaises(queue.AskFirstError):
                schedules.preview_schedule(f.entity, request)
        finally:
            preparation_runtime._active.reset(token)
        schedules.apply_schedule(f.entity, plan)
        token = bind()
        try:
            due = schedules.preview_due(f.entity, '2026-01-31', operation_id='january')
            schedules.apply_due(f.entity, due)
        finally:
            preparation_runtime._active.reset(token)
        self.assertEqual(f.balances()[schedule_fixtures.PREPAID], Decimal('1100'))

    def test_correction_replacement_increase_cannot_bypass_ceiling(self):
        f = self.fixture(policy_fixtures.PreparationRuntimeTests)
        f.posted()
        f.policy = replace(f.policy, ask_first_above=Decimal('150'))
        argv = ['ledger', 'correct', '--entity', str(f.entity.path), '--source-id', 'source-a',
                '--new-source-id', 'larger', '--date', '2026-02-15', '--description', 'Synthetic correction',
                '--posting', BANK + '=-200', '--posting', 'Expenses:Software=200']
        before = f.store.content_digest()
        from contextlib import redirect_stdout
        from io import StringIO
        with redirect_stdout(StringIO()):
            status = preparation_runtime.run_preparation_command(argv, f.authority(), entity_path=f.entity.path,
                expected_ledger_digest=f.store.content_digest(), source_hashes={}, entry_hashes=f.entry_hashes())
        self.assertNotEqual(status, 0)
        self.assertEqual(f.store.content_digest(), before)

    def test_correction_cannot_bypass_original_amount_ceiling(self):
        f = self.fixture(policy_fixtures.PreparationRuntimeTests)
        f.posted()
        f.policy = replace(f.policy, ask_first_above=Decimal('50.00'))
        before = f.store.content_digest()
        self.assertNotEqual(f.correction(), 0)
        self.assertEqual(f.store.content_digest(), before)

    def test_no_learning_is_bound_to_receipt_even_when_retry_asks_to_learn(self):
        with tempfile.TemporaryDirectory() as tmp:
            entity = queue_fixtures._make_entity(Path(tmp))
            queue_fixtures._add_pending_categorization(entity, 's1', 'SYNTHETIC VENDOR', amount='-100.00')
            queue.propose(entity, 's1', 'Expenses:Software', 'Reviewed')
            with patch.object(queue, '_remove_from_pending_categorization', side_effect=OSError('after commit')):
                with self.assertRaises(OSError):
                    queue.confirm(entity, 's1', 'routine', learn=False)
            queue.confirm(entity, 's1', 'routine', learn=True)
            self.assertEqual(queue.load_learned_context(entity), {})
            self.assertEqual(LedgerStore(entity.path / 'ledger.sqlite').counts().entries, 1)

    def test_import_checks_follow_reversal_and_multiple_corrections(self):
        with tempfile.TemporaryDirectory() as tmp:
            entity = queue_fixtures._make_entity(Path(tmp))
            queue_fixtures._add_pending_categorization(entity, 's1', 'SYNTHETIC', amount='-100.00')
            queue.propose(entity, 's1', 'Expenses:Software', 'Reviewed')
            queue.confirm(entity, 's1', 'review')
            self.assertEqual(import_check._ledger_rows(entity, BANK)[0]['s1'], Decimal('-100'))
            for parent, child, amount in [('s1', 'c1', '120'), ('c1', 'c2', '80')]:
                replacement = Entry(date(2026, 1, 10), 'Correction',
                    meta=(('source-id', child),), postings=(Posting(BANK, -Decimal(amount)),
                    Posting('Expenses:Software', Decimal(amount))))
                importer.reverse_and_correct(entity, parent, replacement, 'review')
                self.assertEqual(import_check._ledger_rows(entity, BANK)[0]['s1'], -Decimal(amount))
            importer.reverse_entry(entity, 'c2', 'review')
            self.assertEqual(import_check._ledger_rows(entity, BANK)[0]['s1'], 0)

    def test_paired_sources_keep_both_sides_and_reverse_to_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            entity = queue_fixtures._make_entity(Path(tmp))
            queue_fixtures._add_pending_categorization(entity, 'bank', 'CARD PAYMENT', amount='-125', txn_date='2026-02-01')
            queue_fixtures._add_pending_categorization(entity, 'card', 'PAYMENT', amount='125', txn_date='2026-02-01')
            path = entity.staging_dir / 'pending-categorization.json'
            rows = json.loads(path.read_text())
            next(row for row in rows if row['id'] == 'card').update(accountName='Amex', accountType='credit_card')
            path.write_text(json.dumps(rows))
            item = queue.propose_transfer(entity, ['bank', 'card'], 'Matched transfer')
            queue.confirm_transfer(entity, item['source_id'], 'review')
            self.assertEqual(import_check._ledger_rows(entity, BANK)[0]['bank'], Decimal('-125'))
            self.assertEqual(import_check._ledger_rows(entity, 'Liabilities:CreditCard:Amex')[0]['card'], Decimal('125'))
            importer.reverse_entry(entity, 'bank', 'review')
            self.assertEqual(import_check._ledger_rows(entity, BANK)[0]['bank'], 0)
            self.assertEqual(import_check._ledger_rows(entity, 'Liabilities:CreditCard:Amex')[0]['card'], 0)

    def test_opening_state_is_not_statement_movement_but_remains_cumulative(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = tieout_fixtures.make_company(Path(tmp))
            tieout_fixtures.post(company, BANK, '2026-01-01', '100', other='Equity:Opening-Balances')
            store = LedgerStore(company / 'ledger.sqlite')
            with store.transaction() as conn:
                conn.execute('UPDATE entries SET source_id=?, metadata_json=?', ('opening', json.dumps([
                    ['source-id', 'opening'], ['import-source', 'quickbooks-opening']])))
            tieout_fixtures.post(company, BANK, '2026-01-15', '-20', other='Expenses:Software')
            series = tieout.load_postings(company, [BANK])[BANK]
            check = tieout.period_tieout(account=BANK, statement_id='january', start=date(2026,1,1),
                end=date(2026,1,31), opening=Decimal('100'), closing=Decimal('80'), postings=series, waiting=tieout.Series())
            self.assertEqual((check['status'], check['books_balance_through']), ('matches', '80.00'))
            self.assertEqual(series.through(date(2026,1,31)), Decimal('80'))
            later = tieout.period_tieout(account=BANK, statement_id='february', start=date(2026,2,1),
                end=date(2026,2,28), opening=Decimal('80'), closing=Decimal('80'), postings=series, waiting=tieout.Series())
            self.assertEqual(later['status'], 'matches')

    def test_mixed_missing_and_duplicate_bank_snapshots_do_not_match(self):
        f = self.fixture(tieout_fixtures.BalanceCheckTests)
        for second in [dict(account_id='feed-chk-2', as_of='2026-02-28'),
                       dict(account_id='feed-chk-2', as_of=None, data_through=None),
                       dict(account_id='feed-chk', as_of='2026-01-31')]:
            with self.subTest(second=second):
                [row] = f.check(f.item('500.00'), f.item('300.00', **second))
                self.assertEqual(row['status'], 'not_aligned')
                self.assertIsNone(row['difference'])
                self.assertIsNone(row['bank_balance'])

    def test_stripe_replays_seen_and_pending_cleanup_failures_without_posting_twice(self):
        for boundary in ('seen', 'pending'):
            with self.subTest(boundary=boundary):
                f = self.fixture(stripe_fixtures.StripeTests)
                f.stage()
                target, name = (StagingStore, 'bulk_mark_seen') if boundary == 'seen' else (queue, '_remove_many_from_pending_categorization')
                with patch.object(target, name, side_effect=OSError('after commit')):
                    with self.assertRaises(OSError):
                        f.apply()
                self.assertEqual(len(f.store.load_entries()), 1)
                self.assertEqual(f.apply()['posted'], 0)
                self.assertEqual(json.loads((f.entity.staging_dir / 'pending-categorization.json').read_text()), [])
                self.assertEqual(len(f.store.load_entries()), 1)
                self.assertEqual(f.store.verify_audit_chain(), [])

    def test_stripe_recovery_refuses_changed_pending_evidence(self):
        f = self.fixture(stripe_fixtures.StripeTests)
        f.stage()
        with patch.object(StagingStore, 'bulk_mark_seen', side_effect=OSError('after commit')):
            with self.assertRaises(OSError):
                f.apply()
        path = f.entity.staging_dir / 'pending-categorization.json'
        pending = json.loads(path.read_text()); pending[0]['description'] = 'Changed description'
        path.write_text(json.dumps(pending))
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            f.apply()
        self.assertEqual(len(json.loads(path.read_text())), 1)
        self.assertEqual(len(f.store.load_entries()), 1)

    def test_ofx_rejects_all_nonfinite_and_subcent_money_before_import_writes(self):
        base = (Path(__file__).parent / 'fixtures/statements/checking.ofx').read_text()
        for field, original in [('TRNAMT', '-49.99'), ('BALAMT', '3142.26')]:
            for invalid in ('NaN', 'Infinity', '-Infinity', '49.995', '1e999999'):
                with self.subTest(field=field, invalid=invalid):
                    f = self.fixture(statement_fixtures.ImportTests)
                    f.map('checking.ofx', 'checking-ofx')
                    path = f.root / 'invalid.ofx'
                    path.write_text(base.replace(f'<{field}>{original}', f'<{field}>{invalid}'))
                    before = {str(p.relative_to(f.entity)): p.read_bytes() for p in f.entity.rglob('*') if p.is_file()}
                    result = statement_import.import_file(f.entity, path, profile='checking-ofx')
                    self.assertEqual(result['status'], 'error')
                    self.assertEqual(result['error']['code'], 'malformed_ofx')
                    after = {str(p.relative_to(f.entity)): p.read_bytes() for p in f.entity.rglob('*') if p.is_file()}
                    self.assertEqual(before, after)
