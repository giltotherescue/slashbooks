"""A near-date pair is never auto-recognized as a copy of a bank row matched to QuickBooks history.

Invariant: the history path is never MORE permissive than the ordinary duplicate path. A row is
recognized (skipped) as a copy only where the ordinary rule calls the pair an exact duplicate (the same
date, amount and description from another source) AND the copy's source can hold the original (a feed
always; a statement only when its period covers the original's date). Every other pair that the ordinary
rule would hold, the history path holds too (hold_rule possible_copy). Synthetic data only."""
from datetime import date, timedelta
from decimal import Decimal
import itertools
import unittest

from bookkeeping.quickbooks_history import HistoryIndex, list_buffer_decisions, resolve_buffer_decision
from bookkeeping.statement_import import _match_pool, normalize_description

from test_quickbooks_history_second_rows import SecondRowCases


class NearCopyCases(SecondRowCases):
    def kinds(self):
        return {r['source_id']: r['hold_rule'] for r in list_buffer_decisions(self.entity)}

    def test_L1_a_statement_that_starts_after_the_original_cannot_be_its_copy(self):
        self.matched('2026-03-04')
        counts = self.statement(('2026-03-06', 'DEPOSIT NEW', '30'))['statements'][0]['counts']
        self.assertEqual((counts.get('matched_to_history', 0), counts['held_for_review'], counts['to_import']), (0, 1, 0))
        self.assertEqual(self.bank(), self.START)
        row, = list_buffer_decisions(self.entity)
        self.assertEqual(row['hold_rule'], 'possible_copy')
        self.assertEqual(row['matched_original']['source_id'], 'plaid:1')
        self.assertEqual(row['matched_original']['leg']['date'], '2026-02-27')
        out = self.release_all()
        self.assertEqual(self.bank(), self.START + 30)
        self.assertEqual(out, 1)

    def release_all(self):
        rows = list_buffer_decisions(self.entity)
        for r in rows:
            resolve_buffer_decision(self.entity, r['source_id'], 'release')
            self.settle(r['source_id'])
        return len(rows)

    def test_a_statement_that_covers_the_original_with_the_exact_row_is_recognized(self):
        self.matched('2026-03-04')
        counts = self.statement(('2026-03-04', 'DEPOSIT', '30'), ('2026-03-10', 'OTHER', '7.77'))['statements'][0]['counts']
        self.assertEqual((counts['matched_to_history'], counts['held_for_review']), (1, 0))
        self.assertEqual(list_buffer_decisions(self.entity), [])

    def test_a_statement_that_covers_the_original_with_a_near_date_row_holds_it(self):
        self.matched('2026-03-04')
        counts = self.statement(('2026-03-04', 'EARLIER', '1.11'), ('2026-03-05', 'DEPOSIT LATER', '30'))['statements'][0]['counts']
        self.assertEqual((counts.get('matched_to_history', 0), counts['held_for_review']), (0, 1))
        self.assertEqual(list(self.kinds().values()), ['possible_copy'])
        self.assertEqual(self.bank(), self.START)

    def test_a_feed_row_near_the_date_of_a_matched_statement_row_is_held(self):
        self.history(self.DEPOSIT)
        self.statement(('2026-03-04', 'DEPOSIT', '30'))
        sid = list_buffer_decisions(self.entity)[0]['source_id']
        resolve_buffer_decision(self.entity, sid, 'history-match', list_buffer_decisions(self.entity)[0]['candidate_legs'][0]['source_id'])
        result = self.ingest(self.row('near', '2026-03-05', '30'))
        self.assertEqual((result.matched_to_history, len(result.possible_copies), result.new_entries), ([], 1, 0))
        self.assertEqual(result.possible_copies[0]['matched_original'], sid)
        self.assertEqual(self.kinds(), {'plaid:near': 'possible_copy'})
        self.assertEqual(self.bank(), self.START)

    def test_possible_copy_never_releases_by_time_and_can_be_a_copy(self):
        from bookkeeping.quickbooks_history import release_due_buffer
        self.matched('2026-03-04')
        self.statement(('2026-03-06', 'DEPOSIT NEW', '30'))
        sid, = self.kinds()
        self.assertEqual(release_due_buffer(self.entity, now='2030-01-01T00:00:00Z')['needs_review'], [sid])
        resolve_buffer_decision(self.entity, sid, 'copy')
        self.assertEqual(self.bank(), self.START)

    def test_every_pass_is_idempotent(self):
        self.matched('2026-03-04')
        run = lambda: self.statement(('2026-03-06', 'DEPOSIT NEW', '30'))
        run()
        before, held = self.digest(), len(list_buffer_decisions(self.entity))
        run()
        self.assertEqual((self.digest(), len(list_buffer_decisions(self.entity))), (before, held))
        self.assertEqual(self.bank(), self.START)

    # The invariant, over generated cases

    def test_history_recognizes_a_copy_only_where_the_ordinary_rule_calls_an_exact_duplicate(self):
        original_day = date(2026, 3, 10)
        feed, statement = 'plaid', 'statement:profile'
        cases = itertools.product(range(-4, 5), (True, False), (feed, statement), (feed, statement), (True, False),
                                  (Decimal('30'), Decimal('30.01')))
        seen = set()
        for offset, same_desc, original_family, copy_family, covers, amount in cases:
            item = {'source_id': original_family + ':orig', 'date': original_day, 'amount': Decimal('30'),
                    'desc': normalize_description('DEPOSIT'), 'family': original_family, 'history_source_id': 'leg'}
            index = object.__new__(HistoryIndex)
            index.copies, index.copy_families = {}, {}
            index.handled_items = lambda account, item=item: [item]
            row = {'date': (original_day + timedelta(days=offset)).isoformat(), 'amount': str(amount),
                   'description': 'DEPOSIT' if same_desc else 'SOMETHING ELSE'}
            period = None if copy_family == feed else (
                (original_day - timedelta(days=1), original_day + timedelta(days=1)) if covers
                else (original_day + timedelta(days=1), original_day + timedelta(days=9)))
            pair = index.pair('Assets:Bank:Checking', copy_family, row, {}, period)
            pool = [{'source_id': 'orig', 'date': original_day, 'amount': Decimal('30'),
                     'desc': normalize_description('DEPOSIT'), 'family': original_family}]
            _, ordinary = _match_pool(pool, set(), date.fromisoformat(row['date']), amount,
                                      normalize_description(row['description']), copy_family)
            if pair and pair[0] == 'copy':
                seen.add('copy')
                self.assertEqual(ordinary, 'same_date_amount_description', (offset, same_desc, original_family, copy_family, covers, amount))
                self.assertNotEqual(original_family, copy_family)
                if copy_family == statement:
                    self.assertTrue(covers)
            if ordinary is not None:
                # The ordinary path holds this pair, so the history path never lets it through unseen.
                self.assertIsNotNone(pair, (offset, same_desc, original_family, copy_family, covers, amount))
            else:
                self.assertIsNone(pair)
        self.assertIn('copy', seen)


for _name in [n for n in dir(SecondRowCases) if n.startswith('test_') and n not in NearCopyCases.__dict__]:
    setattr(NearCopyCases, _name, None)


class AccrualNearCopyTests(NearCopyCases, unittest.TestCase):
    basis = 'Accrual'


class CashNearCopyTests(NearCopyCases, unittest.TestCase):
    basis = 'Cash'


if __name__ == '__main__':
    unittest.main()
