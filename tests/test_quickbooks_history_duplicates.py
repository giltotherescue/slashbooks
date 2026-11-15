"""A bank row that a person matched to QuickBooks history, and the rows that arrive after it.

Synthetic data only."""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import re
import unittest

from bookkeeping import statement_import as si
from bookkeeping.entity import load_entity
from bookkeeping.ledger.store import LedgerStore, default_store_path
from bookkeeping.preparation_runtime import PreparationDenied, supported_preparation_command
from bookkeeping.quickbooks_history import (HistoryIndex, _audit, list_buffer_decisions, release_due_buffer,
                                            resolve_buffer_decision, reverse_buffer_decision)
from bookkeeping.remote_contract import command_spec

from test_quickbooks_history_lag import LagCases

CHECKING = 'Assets:Bank:Checking'


class DuplicateCases(LagCases):
    DEPOSIT = [('2026-02-27', {'1': '30', '4': '-30'})]
    count = 0

    def row(self, name, day, amount, feed='feed-1'):
        # One description for every row, as a re-sent bank row has the same text under its new ID.
        return {**super().row(name, day, amount, feed), 'description': 'Synthetic bank deposit'}

    def reload(self):
        mappings = self.entity.entity_config.get('bank_account_mappings')
        self.entity = load_entity(self.entity.path)
        self.entity.entity_config['bank_account_mappings'] = mappings

    def statement(self, *rows):
        """Upload a statement; rows are (date, description, amount). Returns the import result."""
        DuplicateCases.count += 1
        path = Path(self.temp.name) / ('statement-%d.csv' % DuplicateCases.count)
        path.write_text('Date,Description,Amount\n' + ''.join('%s,%s,%s\n' % r for r in rows))
        si.save_profile(self.entity.path, path, name='synthetic-upload', ledger_account=CHECKING,
                        overrides={'date_format': '%Y-%m-%d', 'sign': 'standard'})
        result = si.import_file(self.entity.path, path, profile='synthetic-upload', ts='2026-03-10T00:00:00Z')
        self.reload()
        return result

    def entries(self, prefix=''):
        return [e for e in LedgerStore(default_store_path(self.entity.path)).load_entries() if (e.source_id or '').startswith(prefix)]

    def bank(self):
        return sum((p.amount for e in self.entries() for p in e.postings if p.account == CHECKING), Decimal(0))

    def match(self, name, held_source_id=None):
        sid = held_source_id or 'plaid:' + name
        row = next(r for r in list_buffer_decisions(self.entity) if r['source_id'] == sid)
        resolve_buffer_decision(self.entity, sid, 'history-match', row['candidate_legs'][0]['source_id'])

    def matched(self, result):
        return result['statements'][0].get('matched_to_history', []) if 'statements' in result else []

    # A. a matched bank row stands in the duplicate pool

    def test_feed_row_matched_to_history_then_statement_copy_is_not_posted_or_held(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('1', '2026-03-04', '30'))
        self.match('1')
        before = self.bank()
        result = self.statement(('2026-03-04', 'DEPOSIT', '30'))
        statement = result['statements'][0]
        self.assertEqual(statement['counts']['matched_to_history'], 1)
        self.assertEqual(statement['counts']['to_import'], 0)
        self.assertEqual(statement['counts']['held_for_review'], 0)
        self.assertEqual(statement['matched_to_history'][0]['duplicate_of'], 'plaid:1')
        self.assertEqual(self.bank(), before)
        self.assertEqual([r for r in list_buffer_decisions(self.entity)], [])

    def test_statement_row_matched_to_history_then_feed_copy_is_recognized(self):
        self.history(self.DEPOSIT)
        self.statement(('2026-03-04', 'DEPOSIT', '30'))
        held = list_buffer_decisions(self.entity)
        self.assertEqual(len(held), 1)
        sid = held[0]['source_id']
        resolve_buffer_decision(self.entity, sid, 'history-match', held[0]['candidate_legs'][0]['source_id'])
        before = self.bank()
        result = self.ingest(self.row('copy', '2026-03-04', '30'))
        self.assertEqual([m['duplicate_of'] for m in result.matched_to_history], [sid])
        self.assertEqual((result.new_entries, result.new_pending, result.duplicate_candidates), (0, 0, 0))
        self.assertEqual(self.state('copy'), 'none')
        self.assertEqual(self.bank(), before)

    def test_feed_row_with_a_new_id_after_the_match_is_recognized_every_time(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('1', '2026-03-04', '30'))
        self.match('1')
        before = self.bank()
        for _ in range(2):
            result = self.ingest(self.row('1-again', '2026-03-04', '30'))
            self.assertEqual([m['duplicate_of'] for m in result.matched_to_history], ['plaid:1'])
            self.assertEqual(self.state('1-again'), 'none')
        # The row that was matched keeps being skipped when the feed sends it again.
        self.assertEqual(self.ingest(self.row('1', '2026-03-04', '30')).skipped_duplicate, 1)
        self.assertEqual(self.bank(), before)

    def test_feed_copy_and_statement_copy_of_one_matched_row_are_both_recognized(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('1', '2026-03-04', '30'))
        self.match('1')
        self.assertEqual(len(self.ingest(self.row('new-id', '2026-03-04', '30')).matched_to_history), 1)
        self.assertEqual(self.statement(('2026-03-05', 'Deposit 0305', '30'))['statements'][0]['counts']['matched_to_history'], 1)

    def test_two_real_rows_of_one_amount_on_one_day_only_one_matched_to_history(self):
        self.history(self.DEPOSIT)
        # The bank paid 30 twice on 03-04: one is the QuickBooks deposit, one is new.
        self.ingest(self.row('a', '2026-03-04', '30'), self.row('b', '2026-03-04', '30'))
        self.assertEqual((self.state('a'), self.state('b')), ('held', 'held'))
        self.match('a')
        resolve_buffer_decision(self.entity, 'plaid:b', 'release')
        self.assertIn(self.state('b'), {'posted', 'pending'})
        # A statement shows both. One copy is recognized as the matched row. The other is not: the
        # ordinary rule holds it against the released row, as it holds any copy of a posted row.
        result = self.statement(('2026-03-04', 'DEPOSIT A', '30'), ('2026-03-04', 'DEPOSIT B', '30'))
        counts = result['statements'][0]['counts']
        self.assertEqual((counts['matched_to_history'], counts['to_import'] + counts['held_for_review']), (1, 1))

    def test_two_copies_in_one_pass_only_one_is_recognized_as_the_matched_row(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('1', '2026-03-04', '30'))
        self.match('1')
        counts = self.statement(('2026-03-04', 'DEPOSIT A', '30'), ('2026-03-04', 'DEPOSIT B', '30'))['statements'][0]['counts']
        self.assertEqual((counts['matched_to_history'], counts['to_import']), (1, 1))

    # B. a person can reverse a buffer decision

    def test_reversing_a_history_match_unmatches_the_leg_and_holds_the_row_again(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('1', '2026-03-04', '30'))
        self.match('1')
        self.assertEqual(list_buffer_decisions(self.entity), [])
        result = reverse_buffer_decision(self.entity, 'plaid:1', reason='Matched by mistake')
        self.assertEqual((result['reversed_decision'], result['status']), ('history-match', 'duplicate-candidate'))
        held = self.held('1')
        self.assertEqual(len(held['candidate_legs']), 1)
        # The match no longer stands in the duplicate pool.
        self.assertEqual(len(self.ingest(self.row('x', '2026-03-04', '30')).matched_to_history), 0)
        events = [r for kind, r in _audit(self.entity, ('qbo-buffer-decision', 'qbo-buffer-decision-reversed'))]
        self.assertEqual([e.get('decision') or 'reversed:' + e['reversed_decision'] for e in events], ['history-match', 'reversed:history-match'])
        self.assertEqual(events[-1]['reason'], 'Matched by mistake')
        # And the person can decide again.
        resolve_buffer_decision(self.entity, 'plaid:1', 'release')
        self.assertIn(self.state('1'), {'posted', 'pending'})

    def test_reversing_a_release_that_posted_is_refused_and_changes_nothing(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('1', '2026-03-04', '30'))
        resolve_buffer_decision(self.entity, 'plaid:1', 'release')
        if self.state('1') == 'pending':
            self.confirm_pending('plaid:1')
        self.assertEqual(self.state('1'), 'posted')
        digest = LedgerStore(default_store_path(self.entity.path)).content_digest()
        with self.assertRaisesRegex(ValueError, 'already posted'):
            reverse_buffer_decision(self.entity, 'plaid:1')
        self.assertEqual(LedgerStore(default_store_path(self.entity.path)).content_digest(), digest)
        self.assertEqual(HistoryIndex(self.entity).decision('plaid:1')['decision'], 'release')

    def test_reversing_a_release_that_waits_in_the_queue_is_refused(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('1', '2026-03-04', '30'))
        resolve_buffer_decision(self.entity, 'plaid:1', 'release')
        if self.state('1') != 'pending':
            self.skipTest('this basis posts the released row at once')
        with self.assertRaisesRegex(ValueError, 'review queue'):
            reverse_buffer_decision(self.entity, 'plaid:1')

    def test_reversing_without_a_decision_is_refused(self):
        self.history(self.DEPOSIT)
        with self.assertRaisesRegex(ValueError, 'no buffer decision'):
            reverse_buffer_decision(self.entity, 'plaid:none')

    def test_the_reverse_command_is_a_write_and_is_not_offered_to_an_agent(self):
        argv = ['qb', 'reverse-buffer', '--entity', '/tmp/books', '--source-id', 'plaid:1', '--reason', 'x']
        self.assertTrue(command_spec(argv)['mutates'])
        for command in (argv, ['qb', 'resolve-buffer', '--entity', '/tmp/books', '--source-id', 'plaid:1', '--decision', 'release']):
            with self.assertRaises(PreparationDenied):
                supported_preparation_command(command)

    # C. a released row is not a candidate

    def test_a_released_row_does_not_match_a_leg_and_is_not_offered_again(self):
        self.history(self.DEPOSIT)
        self.ingest(self.row('first', '2026-03-04', '30'))
        resolve_buffer_decision(self.entity, 'plaid:first', 'release')
        self.assertEqual([r['source_id'] for r in list_buffer_decisions(self.entity)], [])
        # The feed sends it again: nothing is posted twice and nothing is held again.
        count = len(self.entries('plaid:first'))
        again = self.ingest(self.row('first', '2026-03-04', '30'))
        self.assertEqual((again.duplicate_candidates, again.matched_to_history), (0, []))
        self.assertEqual(len(self.entries('plaid:first')), count)
        # The leg is still unmatched, so the next same-amount row is held, and its only candidate is the leg.
        self.ingest(self.row('next', '2026-03-10', '30'))
        legs = self.held('next')['candidate_legs']
        self.assertEqual([c['date'] for c in legs], ['2026-02-27'])
        self.assertNotIn('plaid:first', [c['source_id'] for c in legs])

    # D. direct ledger writes

    def test_a_person_journal_with_the_amount_of_an_unmatched_leg_is_not_blocked(self):
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry, Posting
        self.history(self.DEPOSIT)
        entry = Entry(date(2026, 3, 10), 'Synthetic person journal', meta=(('source-id', 'journal:person'),),
                      postings=(Posting(CHECKING, Decimal(30)), Posting('Income:Sales', Decimal(-30))))
        _atomic_ledger_write(self.entity, [], [entry], 'journal', None, 'journal')
        self.assertEqual(len(self.entries('journal:person')), 1)

    def test_statement_row_with_the_amount_of_an_unmatched_leg_after_the_period_is_held(self):
        self.history(self.DEPOSIT)
        result = self.statement(('2026-03-12', 'DEPOSIT', '30'))
        self.assertEqual(result['statements'][0]['counts']['held_for_review'], 1)
        self.assertEqual(self.held_by_rule('unmatched_history_leg'), 1)

    def held_by_rule(self, rule):
        return sum(r['hold_rule'] == rule for r in list_buffer_decisions(self.entity))

    def test_every_direct_ledger_writer_is_classified(self):
        """A bank row reaches the ledger only through intake or a queue that intake filled.

        - importer.py: ``import_transactions`` is intake (feeds, statements, ``ingest``, context release,
          the buffer release); ``reverse_and_correct`` and ``reverse_entry`` correct an existing entry.
        - queue.py: confirm, split, transfer and related-entity writes take an item that intake staged.
        - stripe.py: applies a plan only for rows already staged by intake.
        - reclassify.py and accrual/journals.py: a person's or approved journal, never a bank row.
        - quickbooks_history.py: the history import itself.
        A new writer must be added here with its reason, so it is reviewed against the history hold.
        """
        src = Path(__file__).resolve().parents[1] / 'src' / 'bookkeeping'
        writers = {str(p.relative_to(src)) for p in src.rglob('*.py')
                   if p.name != 'importer.py' and re.search(r'_atomic_ledger_write(_unlocked)?\(', p.read_text())}
        self.assertEqual(writers, {'queue.py', 'stripe.py', 'ledger/reclassify.py', 'accrual/journals.py', 'quickbooks_history.py'})

    def confirm_pending(self, sid):
        from bookkeeping import queue
        category = next(o.account for o in LedgerStore(default_store_path(self.entity.path)).load_opens() if o.account.startswith('Income:'))
        queue.propose(self.entity, sid, category, 'Synthetic review')
        queue.confirm(self.entity, sid, 'owner-review', accrual_treatment='new_cash_event' if self.basis == 'Accrual' else None)

    def test_a_row_staged_by_intake_and_confirmed_is_checked_at_intake_not_at_the_write(self):
        # A row with no candidate leg goes the normal path: staged by intake, then confirmed.
        self.history(self.DEPOSIT)
        self.ingest(self.row('free', '2026-03-20', '12.34'))
        if self.state('free') == 'pending':
            self.confirm_pending('plaid:free')
        self.assertEqual(self.state('free'), 'posted')

    # E. rows before the books start

    def test_feed_rows_before_the_history_start_are_before_the_books_not_held(self):
        self.history(self.DEPOSIT)
        result = self.ingest(self.row('old', '2025-12-20', '9.99'), self.row('opening-day', '2025-12-31', '8.88'),
                             self.row('first-day', '2026-01-01', '7.77'))
        self.assertEqual({b['source_id'] for b in result.before_opening}, {'plaid:old', 'plaid:opening-day'})
        self.assertEqual([r['source_id'] for r in list_buffer_decisions(self.entity)], ['plaid:first-day'])
        out = release_due_buffer(self.entity, now='2027-01-01T00:00:00Z')
        self.assertEqual((out['released'], out['errors']), ([], []))
        self.assertEqual((self.state('old'), self.state('opening-day')), ('none', 'none'))

    def test_a_statement_row_before_the_history_start_follows_the_opening_balance_rule(self):
        self.history(self.DEPOSIT)
        result = self.statement(('2025-12-20', 'OLD', '9.99'), ('2026-01-15', 'NEW', '5.55'))
        text = json.dumps(result)
        self.assertIn('before_opening_balance', text)
        self.assertEqual(self.entries('stmt:'), [])


class AccrualDuplicateTests(DuplicateCases, unittest.TestCase):
    basis = 'Accrual'


class CashDuplicateTests(DuplicateCases, unittest.TestCase):
    basis = 'Cash'


if __name__ == '__main__':
    unittest.main()
