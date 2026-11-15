"""Each held row names the exact answers the engine accepts, and the engine refuses any other.

The host renders its buttons from allowed_decisions and never guesses. in-opening rule: a row on a mapped bank
or card account, dated from the history start up to 45 days after it, with no unmatched leg that could explain it.
Synthetic data only."""
import unittest

from bookkeeping.quickbooks_history import list_buffer_decisions, resolve_buffer_decision, reverse_buffer_decision

from test_quickbooks_history_second_rows import SecondRowCases


class AllowedCases(SecondRowCases):
    def allowed(self, name):
        return next(r for r in list_buffer_decisions(self.entity) if r['source_id'] == name)['allowed_decisions']

    def names(self, name):
        return [a['decision'] for a in self.allowed(name)]

    def test_a_row_in_the_first_days_with_no_leg_can_be_released_or_marked_in_the_opening(self):
        self.history([])
        self.ingest(self.row('transit', '2026-01-03', '30'))
        self.assertEqual(self.allowed('plaid:transit'), [{'decision': 'release'}, {'decision': 'in-opening'}])

    def test_the_opening_window_is_45_days_from_the_history_start(self):
        self.history([])
        self.ingest(self.row('edge', '2026-02-15', '30'), self.row('late', '2026-02-16', '31'))
        self.assertEqual(self.names('plaid:edge'), ['release', 'in-opening'])
        self.assertEqual(self.names('plaid:late'), ['release'])
        with self.assertRaisesRegex(ValueError, 'in-opening is not allowed for this row now. Allowed: release'):
            resolve_buffer_decision(self.entity, 'plaid:late', 'in-opening')
        self.assertEqual(self.bank(), self.START_NO_DEPOSIT)

    START_NO_DEPOSIT = SecondRowCases.START - 30

    def test_a_row_that_an_unmatched_leg_could_explain_is_matched_not_marked_in_the_opening(self):
        self.history([('2026-01-04', {'1': '30', '4': '-30'})])
        self.ingest(self.row('clears', '2026-01-05', '30'))
        allowed = self.allowed('plaid:clears')
        self.assertEqual([a['decision'] for a in allowed], ['release', 'history-match'])
        leg = allowed[1]['history_source_ids']
        self.assertEqual(len(leg), 1)
        with self.assertRaisesRegex(ValueError, 'in-opening is not allowed'):
            resolve_buffer_decision(self.entity, 'plaid:clears', 'in-opening')
        with self.assertRaisesRegex(ValueError, 'Select an unmatched history journal entry'):
            resolve_buffer_decision(self.entity, 'plaid:clears', 'history-match', 'quickbooks:sandbox:456:JournalEntry:nope')
        resolve_buffer_decision(self.entity, 'plaid:clears', 'history-match', leg[0])

    def test_a_possible_second_row_offers_release_and_copy_of_the_named_originals(self):
        self.matched()
        self.ingest(self.row('2', '2026-03-04', '30'))
        self.assertEqual(self.allowed('plaid:2'), [{'decision': 'release'},
                                                   {'decision': 'copy', 'original_source_ids': ['plaid:1']}])
        for decision, kwargs in (('history-match', {'history_source_id': 'x'}), ('in-opening', {})):
            with self.assertRaisesRegex(ValueError, 'not allowed for this row now. Allowed: release, copy'):
                resolve_buffer_decision(self.entity, 'plaid:2', decision, **kwargs)
        with self.assertRaisesRegex(ValueError, 'within three days'):
            resolve_buffer_decision(self.entity, 'plaid:2', 'copy', original_source_id='plaid:nobody')
        resolve_buffer_decision(self.entity, 'plaid:2', 'copy', original_source_id='plaid:1')
        self.assertEqual(self.bank(), self.START)

    def test_a_posted_then_reversed_row_cannot_be_released_again(self):
        self.history([])
        self.ingest(self.row('r', '2026-01-03', '30'))
        resolve_buffer_decision(self.entity, 'plaid:r', 'release')
        self.settle('plaid:r')
        reverse_buffer_decision(self.entity, 'plaid:r')
        self.assertEqual(self.names('plaid:r'), ['in-opening'])
        with self.assertRaisesRegex(ValueError, 'posted and then reversed'):
            resolve_buffer_decision(self.entity, 'plaid:r', 'release')

    def test_a_retry_of_the_same_decision_is_still_accepted(self):
        self.history([])
        self.ingest(self.row('t', '2026-01-03', '30'))
        resolve_buffer_decision(self.entity, 'plaid:t', 'in-opening')
        self.assertEqual(resolve_buffer_decision(self.entity, 'plaid:t', 'in-opening')['decision'], 'in-opening')

    def test_statement_and_intake_output_carry_the_allowed_answers(self):
        self.matched()
        counts = self.statement(('2026-03-04', 'SECOND LOOK', '30'))['statements'][0]
        held, = counts['held']
        self.assertEqual(held['allowed_decisions'], [{'decision': 'release'},
                                                      {'decision': 'copy', 'original_source_ids': ['plaid:1']}])
        result = self.ingest(self.row('again', '2026-03-04', '30'))
        self.assertEqual(result.possible_second_rows[0]['allowed_decisions'][1],
                         {'decision': 'copy', 'original_source_ids': ['plaid:1']})


for _name in [n for n in dir(SecondRowCases) if n.startswith('test_') and n not in AllowedCases.__dict__]:
    setattr(AllowedCases, _name, None)


class AccrualAllowedTests(AllowedCases, unittest.TestCase):
    basis = 'Accrual'


class CashAllowedTests(AllowedCases, unittest.TestCase):
    basis = 'Cash'


if __name__ == '__main__':
    unittest.main()
