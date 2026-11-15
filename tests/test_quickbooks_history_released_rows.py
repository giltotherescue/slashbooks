"""A bank row released as new is an ordinary posted bank row: its copies are recognized or held; it can be reversed
by a person; a row already in the starting balance can be marked so. Synthetic data only."""
from datetime import date
from decimal import Decimal
import unittest

from bookkeeping.ledger.store import LedgerStore, default_store_path
from bookkeeping.quickbooks_history import (HistoryIndex, list_buffer_decisions, resolve_buffer_decision)
from bookkeeping.statement_import import plan_duplicates

from test_quickbooks_history_second_rows import SecondRowCases
from test_quickbooks_history_duplicates import CHECKING

OPENING = Decimal('1500')


class ReleasedRowCases(SecondRowCases):
    START = OPENING  # no QuickBooks bank activity in these histories

    def released_statement_row(self, day='2026-01-10', desc='COFFEE', amount='-5'):
        self.history([])
        self.statement((day, desc, amount))
        sid = list_buffer_decisions(self.entity)[0]['source_id']
        resolve_buffer_decision(self.entity, sid, 'release')
        self.settle(sid)
        self.assertEqual(self.bank(), OPENING + Decimal(amount))
        return sid

    # P1-2: the other channel's copy of a released row

    def test_the_exact_copy_of_a_released_row_through_the_other_channel_is_recognized(self):
        sid = self.released_statement_row()
        result = self.ingest({**self.row('coffee', '2026-01-10', '-5'), 'description': 'COFFEE'})
        self.assertEqual([m['duplicate_of'] for m in result.matched_to_history], [sid])
        self.assertEqual((result.new_entries, result.duplicate_candidates), (0, 0))
        self.assertEqual(self.bank(), OPENING - 5)

    def test_a_near_copy_of_a_released_row_is_held_naming_it_and_can_be_called_a_copy(self):
        sid = self.released_statement_row()
        self.ingest(self.row('feedcopy', '2026-01-10', '-5'))  # another description: the ordinary rule only pairs by amount
        row, = list_buffer_decisions(self.entity)
        self.assertEqual(row['hold_rule'], 'possible_copy')
        self.assertEqual((row['matched_original']['source_id'], row['matched_original']['kind']), (sid, 'release'))
        self.assertEqual(row['released_rows'][0]['source_id'], sid)
        self.assertEqual(row['released_rows'][0]['posted_entry'], sid)
        self.assertEqual(self.bank(), OPENING - 5)
        resolve_buffer_decision(self.entity, 'plaid:feedcopy', 'copy')
        self.assertEqual(list_buffer_decisions(self.entity), [])
        self.assertEqual(self.bank(), OPENING - 5)

    def test_a_statement_sent_again_after_the_release_posts_nothing(self):
        self.released_statement_row()
        before = self.bank()
        self.statement(('2026-01-10', 'COFFEE', '-5'))
        self.assertEqual(self.bank(), before)

    # P1-3: a wrong release can be undone

    def test_a_wrong_release_is_undone_with_reverse_buffer_and_the_row_is_held_again(self):
        from bookkeeping.quickbooks_history import _audit, reverse_buffer_decision
        self.history([])
        self.ingest(self.row('r', '2026-01-03', '30'))
        resolve_buffer_decision(self.entity, 'plaid:r', 'release')
        self.settle('plaid:r')
        self.assertEqual(self.bank(), OPENING + 30)
        result = reverse_buffer_decision(self.entity, 'plaid:r', 'released by mistake')
        self.assertTrue(result['entry_reversed'])
        self.assertEqual(self.bank(), OPENING)
        self.assertEqual([r['source_id'] for r in list_buffer_decisions(self.entity)], ['plaid:r'])
        events = [e for _, e in _audit(self.entity, ('qbo-buffer-decision-reversed',))]
        self.assertEqual((events[-1]['reversed_decision'], events[-1]['entry_reversed'], events[-1]['reason']), ('release', True, 'released by mistake'))
        # The same source ID cannot be posted again: the answer must not silently post nothing.
        with self.assertRaisesRegex(ValueError, 'posted and then reversed'):
            resolve_buffer_decision(self.entity, 'plaid:r', 'release')
        self.assertEqual(self.bank(), OPENING)

    def test_a_person_can_reverse_the_posted_entry_directly_and_then_reverse_the_decision(self):
        from bookkeeping.ledger.importer import reverse_entry
        from bookkeeping.quickbooks_history import reverse_buffer_decision
        self.history([])
        self.ingest(self.row('r', '2026-01-03', '30'))
        resolve_buffer_decision(self.entity, 'plaid:r', 'release')
        self.settle('plaid:r')
        reverse_entry(self.entity, 'plaid:r', 'undo')
        self.assertEqual(self.bank(), OPENING)
        self.assertFalse(reverse_buffer_decision(self.entity, 'plaid:r')['entry_reversed'])
        self.assertEqual(self.bank(), OPENING)

    def test_a_release_that_waits_in_the_review_queue_is_refused(self):
        from bookkeeping.quickbooks_history import reverse_buffer_decision
        self.history([])
        self.ingest(self.row('q', '2026-01-03', '30'))
        resolve_buffer_decision(self.entity, 'plaid:q', 'release')
        if self.state('q') != 'pending':
            self.skipTest('this basis posts the released row at once')
        with self.assertRaisesRegex(ValueError, 'review queue'):
            reverse_buffer_decision(self.entity, 'plaid:q')

    # P2-1: a row that is already in the starting balance

    def test_a_deposit_in_transit_at_the_opening_can_be_marked_as_already_in_the_starting_balance(self):
        from bookkeeping.quickbooks_history import _audit, reverse_buffer_decision
        self.history([])
        self.ingest(self.row('transit', '2026-01-03', '30'))
        self.assertEqual(self.state('transit'), 'held')
        result = resolve_buffer_decision(self.entity, 'plaid:transit', 'in-opening')
        self.assertEqual(result['status'], 'already-in-opening')
        self.assertEqual(list_buffer_decisions(self.entity), [])
        self.assertEqual(self.bank(), OPENING)
        self.assertEqual([e['decision'] for _, e in _audit(self.entity, ('qbo-buffer-decision',))][-1], 'in-opening')
        # It is never posted when the feed sends it again, and it stands in the pairing like a matched row.
        again = self.ingest(self.row('transit', '2026-01-03', '30'))
        self.assertEqual((again.new_entries, again.duplicate_candidates), (0, 0))
        copy = self.statement(('2026-01-03', 'DEPOSIT', '30'))['statements'][0]
        self.assertEqual((copy['counts'].get('matched_to_history', 0), copy['counts']['held_for_review']), (1, 0))
        self.assertEqual(self.bank(), OPENING)
        # A person can change their mind: the row is held again and the copy is held with it.
        reversed_ = reverse_buffer_decision(self.entity, 'plaid:transit', 'not in the opening')
        self.assertEqual(len(reversed_['reopened_rows']), 1)
        self.assertEqual(len(list_buffer_decisions(self.entity)), 2)
        self.assertEqual(self.bank(), OPENING)

    def test_the_command_line_offers_every_decision(self):
        from bookkeeping.remote_contract import command_spec
        for decision in ('release', 'history-match', 'copy', 'in-opening'):
            argv = ['qb', 'resolve-buffer', '--entity', '/tmp/books', '--source-id', 'plaid:1', '--decision', decision, '--original-id', 'plaid:0']
            self.assertTrue(command_spec(argv)['mutates'])

    # P3 of the previous review: named released rows in a held row

    def test_three_rows_two_legs_the_held_row_names_the_released_row(self):
        self.history([('2026-02-27', {'1': '30', '4': '-30'}), ('2026-02-20', {'1': '30', '4': '-30'})])
        base = self.bank()
        self.ingest(self.row('1', '2026-03-04', '30'), self.row('2', '2026-03-04', '30'), self.row('3', '2026-03-04', '30'))
        for name in ('1', '2'):
            held = {r['source_id']: r for r in list_buffer_decisions(self.entity)}
            resolve_buffer_decision(self.entity, 'plaid:' + name, 'history-match', held['plaid:' + name]['candidate_legs'][0]['source_id'])
        resolve_buffer_decision(self.entity, 'plaid:3', 'release')
        self.settle('plaid:3')
        self.assertEqual(self.bank(), base + 30)
        # A statement arrives with a row that is really the copy of the released row.
        self.statement(('2026-03-04', 'STATEMENT LINE', '30'))
        row, = list_buffer_decisions(self.entity)
        self.assertEqual(row['hold_rule'], 'possible_copy')
        self.assertEqual([(r['source_id'], r['date'], r['amount'], r['posted_entry']) for r in row['released_rows']],
                         [('plaid:3', '2026-03-04', '30.00', 'plaid:3')])
        self.assertEqual(self.bank(), base + 30)


for _name in [n for n in dir(SecondRowCases) if n.startswith('test_') and n not in ReleasedRowCases.__dict__]:
    setattr(ReleasedRowCases, _name, None)


class AccrualReleasedTests(ReleasedRowCases, unittest.TestCase):
    basis = 'Accrual'


class CashReleasedTests(ReleasedRowCases, unittest.TestCase):
    basis = 'Cash'


class OrdinaryPoolTests(unittest.TestCase):
    def test_the_ordinary_pool_holds_a_near_copy_of_a_posted_row_with_no_history_at_all(self):
        # What the ordinary path does for a pair like the released-row case: hold it as a possible duplicate
        # that names the posted row. (With history, the history hold also names the released row.)
        import tempfile
        from pathlib import Path
        from bookkeeping.entity import init_entity, load_entity
        from bookkeeping.ledger.importer import import_transactions
        with tempfile.TemporaryDirectory() as temp:
            init_entity(Path(temp) / 'books', name='Synthetic ordinary pool', basis='cash')
            entity = load_entity(Path(temp) / 'books')
            entity.entity_config['bank_account_mappings'] = {'feed-1': CHECKING}
            feed = {'id': 'plaid:posted', 'date': '2026-01-10', 'amount': '-5', 'description': 'COFFEE', 'accountId': 'feed-1',
                    'accountName': 'x', 'pending': False}
            import_transactions(entity, [feed], 's', categorizer=lambda t: ('Expenses:Supplies', 'auto'))
            row = {'id': 'stmt:near', 'date': '2026-01-11', 'description': 'OTHER TEXT', 'amount': '-5'}
            plan = plan_duplicates(entity, [row], CHECKING, 'statement:other')
            self.assertEqual([(h['source_id'], h['existing_source_id'], h['match_rule']) for h in plan['held']],
                             [('stmt:near', 'plaid:posted', 'same_amount_within_3_days_other_source')])
            self.assertEqual(plan['import'], [])


if __name__ == '__main__':
    unittest.main()
