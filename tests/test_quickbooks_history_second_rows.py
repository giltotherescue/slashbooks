"""A real second row that looks like a bank row already matched to QuickBooks history.

Cardinality, from how the ordinary duplicate rule pairs rows: a matched bank row has at most ONE
recognized copy from each OTHER source (a feed row has one statement copy, a statement row has one
feed copy). A row from the SAME source as the matched row (a re-sent row under a new ID, or a second
real row) and a second copy from a source that already has its copy are never silently skipped:
they are held for a person (hold_rule possible_second_row). The same source ID is always the same row.
Synthetic data only."""
from datetime import date
from decimal import Decimal
import json
import unittest

from bookkeeping.ledger.store import LedgerStore, default_store_path
from bookkeeping.quickbooks_history import (HistoryIndex, list_buffer_decisions, release_due_buffer,
                                            resolve_buffer_decision, reverse_buffer_decision)

from test_quickbooks_history_duplicates import DuplicateCases, CHECKING


class SecondRowCases(DuplicateCases):
    START = Decimal('1530')  # 1500 opening plus the 30 deposit that QuickBooks recorded

    def matched(self, day='2026-03-04', deposit=None):
        self.history(deposit or self.DEPOSIT)
        self.ingest(self.row('1', day, '30'))
        self.match('1')
        self.assertEqual(self.bank(), self.START)

    def settle(self, sid):
        pending = self.entity.staging_dir / 'pending-categorization.json'
        if pending.exists() and any(t.get('id') == sid for t in json.loads(pending.read_text())):
            self.confirm_pending(sid)

    def second_rows(self):
        return [r for r in list_buffer_decisions(self.entity) if r['hold_rule'] in {'possible_second_row', 'possible_copy'}]

    def digest(self):
        return LedgerStore(default_store_path(self.entity.path)).content_digest()

    # two genuine identical rows

    def test_second_identical_row_in_the_same_pass_is_held_and_posts_once_when_released(self):
        self.matched()
        counts = self.statement(('2026-03-04', 'DEPOSIT', '30'), ('2026-03-04', 'DEPOSIT B', '30'))['statements'][0]
        self.assertEqual((counts['counts']['matched_to_history'], counts['counts']['held_for_review'], counts['counts']['to_import']), (1, 1, 0))
        self.assertEqual(self.bank(), self.START)
        held, = self.second_rows()
        self.assertEqual(held['matched_original']['source_id'], 'plaid:1')
        resolve_buffer_decision(self.entity, held['source_id'], 'release')
        self.settle(held['source_id'])
        self.assertEqual(self.bank(), self.START + 30)

    def test_second_identical_row_in_a_later_pass_is_held_not_skipped(self):
        self.matched()
        result = self.ingest(self.row('2', '2026-03-04', '30'))
        self.assertEqual((result.matched_to_history, result.new_entries, result.duplicate_candidates), ([], 0, 1))
        self.assertEqual([r['source_id'] for r in result.possible_second_rows], ['plaid:2'])
        self.assertEqual(result.possible_second_rows[0]['matched_original'], 'plaid:1')
        self.assertEqual(self.bank(), self.START)
        held, = self.second_rows()
        self.assertEqual(held['source_id'], 'plaid:2')
        resolve_buffer_decision(self.entity, 'plaid:2', 'release')
        self.settle('plaid:2')
        self.assertEqual(self.bank(), self.START + 30)
        self.assertEqual(self.second_rows(), [])

    def test_held_second_row_output_for_a_host(self):
        self.matched()
        self.ingest(self.row('2', '2026-03-04', '30'))
        row, = list_buffer_decisions(self.entity)
        self.assertEqual(row['hold_rule'], 'possible_second_row')
        original = row['matched_original']
        self.assertEqual((original['source_id'], original['account'], original['date'], original['amount']),
                         ('plaid:1', CHECKING, '2026-03-04', '30.00'))
        self.assertEqual((original['leg']['date'], original['leg']['amount'], original['leg']['type']), ('2026-02-27', '30', 'JournalEntry'))
        for key in ('doc_number', 'name'):
            self.assertIn(key, original['leg'])
        self.assertEqual(row['held']['amount'], '30.00')
        self.assertEqual(self.bank(), self.START)

    def test_it_never_releases_by_time(self):
        self.matched()
        self.ingest(self.row('2', '2026-03-04', '30'))
        out = release_due_buffer(self.entity, now='2030-01-01T00:00:00Z')
        self.assertEqual(out, {'released': [], 'needs_review': ['plaid:2'], 'errors': []})
        self.assertEqual(self.bank(), self.START)

    # feed and statement copy of one row, then a genuine second row

    def test_feed_row_and_statement_copy_then_a_genuine_second_row_by_feed(self):
        self.matched()
        self.assertEqual(self.statement(('2026-03-04', 'DEPOSIT', '30'))['statements'][0]['counts']['matched_to_history'], 1)
        self.assertEqual(self.bank(), self.START)
        self.ingest(self.row('2', '2026-03-04', '30'))
        held, = self.second_rows()
        resolve_buffer_decision(self.entity, held['source_id'], 'release')
        self.settle(held['source_id'])
        self.assertEqual(self.bank(), self.START + 30)

    def test_feed_row_and_statement_copy_then_a_genuine_second_row_by_statement(self):
        self.matched()
        self.statement(('2026-03-04', 'DEPOSIT', '30'))
        later = self.statement(('2026-03-04', 'DEPOSIT TWO', '30'))['statements'][0]
        self.assertEqual((later['counts'].get('matched_to_history', 0), later['counts']['held_for_review']), (0, 1))
        self.assertEqual(self.bank(), self.START)
        held, = self.second_rows()
        resolve_buffer_decision(self.entity, held['source_id'], 'release')
        self.settle(held['source_id'])
        self.assertEqual(self.bank(), self.START + 30)

    def test_a_statement_original_has_one_feed_copy_and_a_second_feed_row_is_held(self):
        self.history(self.DEPOSIT)
        self.statement(('2026-03-04', 'DEPOSIT', '30'))
        sid = list_buffer_decisions(self.entity)[0]['source_id']
        resolve_buffer_decision(self.entity, sid, 'history-match', list_buffer_decisions(self.entity)[0]['candidate_legs'][0]['source_id'])
        first = self.ingest(self.row('copy', '2026-03-04', '30'))
        self.assertEqual([m['duplicate_of'] for m in first.matched_to_history], [sid])
        second = self.ingest(self.row('another', '2026-03-04', '30'))
        self.assertEqual((second.matched_to_history, len(second.possible_second_rows)), ([], 1))
        self.assertEqual(self.bank(), self.START)

    # resends

    def test_resend_with_the_same_source_id_is_the_same_row(self):
        self.matched()
        self.ingest(self.row('2', '2026-03-04', '30'))
        before = self.digest()
        again = self.ingest(self.row('2', '2026-03-04', '30'))
        self.assertEqual((again.duplicate_candidates, again.new_entries), (0, 0))
        self.assertEqual(len(list_buffer_decisions(self.entity)), 1)
        self.assertEqual(self.digest(), before)
        # A recognized statement copy is recognized again when its file is uploaded again.
        self.statement(('2026-03-04', 'DEPOSIT', '30'))
        before = self.digest()
        again = self.statement(('2026-03-04', 'DEPOSIT', '30'))['statements'][0]
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.bank(), self.START)

    def test_resend_with_a_new_source_id_is_held_for_a_person(self):
        self.matched()
        self.ingest(self.row('2', '2026-03-04', '30'))
        self.ingest(self.row('3', '2026-03-04', '30'))
        self.assertEqual(sorted(r['source_id'] for r in self.second_rows()), ['plaid:2', 'plaid:3'])
        self.assertEqual(self.bank(), self.START)

    def test_every_pass_is_idempotent(self):
        self.matched()
        passes = [lambda: self.statement(('2026-03-04', 'DEPOSIT', '30'), ('2026-03-04', 'DEPOSIT B', '30')),
                  lambda: self.ingest(self.row('2', '2026-03-04', '30'))]
        for run in passes:
            run()
            before, held = self.digest(), len(list_buffer_decisions(self.entity))
            copies = len(HistoryIndex(self.entity).copies)
            run()
            self.assertEqual((self.digest(), len(list_buffer_decisions(self.entity)), len(HistoryIndex(self.entity).copies)),
                             (before, held, copies))
        self.assertEqual(self.bank(), self.START)

    # the person decides

    def test_release_of_a_held_second_row_inside_the_period_posts_once(self):
        self.history([('2026-02-27', {'1': '30', '4': '-30'})])
        self.ingest(self.row('1', '2026-02-27', '30'))
        self.match('1')
        self.assertEqual(self.bank(), self.START)
        self.ingest(self.row('2', '2026-02-27', '30'))
        held, = list_buffer_decisions(self.entity)
        self.assertEqual(held['hold_rule'], 'possible_second_row')
        resolve_buffer_decision(self.entity, 'plaid:2', 'release')
        self.settle('plaid:2')
        self.assertEqual(self.bank(), self.START + 30)
        self.assertEqual(len(self.entries('plaid:2')), 1)
        self.ingest(self.row('2', '2026-02-27', '30'))
        resolve_buffer_decision(self.entity, 'plaid:2', 'release')
        self.assertEqual(self.bank(), self.START + 30)
        self.assertEqual(len(self.entries('plaid:2')), 1)

    def test_a_person_can_call_a_held_row_a_copy_and_it_is_kept(self):
        self.matched()
        self.ingest(self.row('2', '2026-03-04', '30'))
        result = resolve_buffer_decision(self.entity, 'plaid:2', 'copy')
        self.assertEqual(result['status'], 'confirmed-duplicate')
        self.assertEqual(HistoryIndex(self.entity).copy_record('plaid:2')['original'], 'plaid:1')
        self.assertEqual(list_buffer_decisions(self.entity), [])
        again = self.ingest(self.row('2', '2026-03-04', '30'))
        self.assertEqual((again.new_entries, again.duplicate_candidates), (0, 0))
        self.assertEqual(self.bank(), self.START)
        self.ingest(self.row('3', '2026-03-04', '30'))
        with self.assertRaisesRegex(ValueError, 'matched to QuickBooks history'):
            resolve_buffer_decision(self.entity, 'plaid:3', 'copy', original_source_id='plaid:none')

    # reversing the match after copies were recorded

    def test_reversing_the_match_holds_its_recorded_copies_again(self):
        self.matched()
        self.statement(('2026-03-04', 'DEPOSIT', '30'))
        self.assertEqual(len(HistoryIndex(self.entity).copies), 1)
        self.assertEqual(list_buffer_decisions(self.entity), [])
        result = reverse_buffer_decision(self.entity, 'plaid:1', 'wrong leg')
        self.assertEqual(len(result['reopened_rows']), 1)
        self.assertEqual(HistoryIndex(self.entity).copies, {})
        held = {r['source_id'] for r in list_buffer_decisions(self.entity)}
        self.assertEqual(held, {'plaid:1', result['reopened_rows'][0]})
        self.assertEqual(self.bank(), self.START)
        # The statement file sent again is held, not recognized as a copy of a match that no longer stands.
        self.statement(('2026-03-04', 'DEPOSIT', '30'))
        self.assertEqual(self.bank(), self.START)
        copy_id = result['reopened_rows'][0]
        resolve_buffer_decision(self.entity, copy_id, 'release')
        self.settle(copy_id)
        self.assertEqual(self.bank(), self.START + 30)

    def test_reversing_the_match_also_reopens_a_copy_that_a_person_called_a_copy(self):
        self.matched()
        self.ingest(self.row('2', '2026-03-04', '30'))
        resolve_buffer_decision(self.entity, 'plaid:2', 'copy')
        result = reverse_buffer_decision(self.entity, 'plaid:1')
        self.assertEqual(result['reopened_rows'], ['plaid:2'])
        self.assertIsNone(HistoryIndex(self.entity).decision('plaid:2'))
        self.assertEqual({r['source_id'] for r in list_buffer_decisions(self.entity)}, {'plaid:1', 'plaid:2'})
        self.assertEqual(self.bank(), self.START)

    # starting balance

    def test_statement_that_spans_the_history_start_gets_the_true_message(self):
        self.history(self.DEPOSIT)
        result = self.statement(('2025-12-20', 'OLD', '9.99'), ('2026-01-15', 'NEW', '5.55'))
        text = json.dumps(result)
        self.assertIn('before_opening_balance', text)
        self.assertIn('comes from the QuickBooks history', text)
        self.assertIn('on or after 2026-01-01', text)
        self.assertNotIn('--replace', text)
        self.assertEqual(self.bank(), self.START)

    def test_record_opening_replace_is_refused_on_a_history_owned_account(self):
        from bookkeeping.statement_opening import record_opening
        self.history(self.DEPOSIT)
        before = self.digest()
        result = record_opening(self.entity.path, account=CHECKING, on='2025-12-01', amount='900', replace=True)
        self.assertEqual((result['status'], result['reason']), ('not_eligible', 'opening_exists_other_source'))
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.bank(), self.START)


for _name in [n for n in dir(DuplicateCases) if n.startswith('test_') and n not in SecondRowCases.__dict__]:
    setattr(SecondRowCases, _name, None)


class AccrualSecondRowTests(SecondRowCases, unittest.TestCase):
    basis = 'Accrual'


class CashSecondRowTests(SecondRowCases, unittest.TestCase):
    basis = 'Cash'


if __name__ == '__main__':
    unittest.main()
