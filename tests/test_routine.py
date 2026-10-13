"""Synthetic authority, source-role, replay and read-only routine regressions."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bookkeeping.entity import Entity
from bookkeeping.ledger.importer import import_transactions, reverse_and_correct
from bookkeeping.ledger.model import Entry, Open, Posting
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.queue import confirm, load_learned_context, make_categorizer, propose
from bookkeeping.routine import routine_context, routine_plan, routine_source_digest
from decimal import Decimal


BANK = 'Assets:Bank:Checking'
CARD = 'Liabilities:CreditCard:Company'
CATEGORY = 'Expenses:Software'


def txn(sid='one', **changes):
    return dict(id=sid, accountId='checking', accountName='Checking', accountType='checking',
                description='Service 1234', amount='-12.00', date='2026-01-15', **changes)


class RoutineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name)
        self.entity = Entity(path, dict(bank_account_mappings={'checking': BANK, 'card': CARD}), {})
        store = LedgerStore(path / 'ledger.sqlite')
        store.initialize()
        with store.transaction() as conn:
            store.insert_opens([Open(date(2000, 1, 1), name, currencies=('USD',)) for name in
                                (BANK, CARD, CATEGORY, 'Income:Services', 'Expenses:Fees')], conn)
        self.policy = dict(version=1, enabled=True, rules=[dict(id='rule-1', source_account_id='checking',
            counterparty_key='SERVICE', category=CATEGORY, approved_ledger_account=BANK,
            max_abs_amount='20.00')], review_categories=[CATEGORY, 'Expenses:Fees'], answer_roles=['client'])

    def stage(self, *rows):
        return import_transactions(self.entity, list(rows), session_id='intake', intake_only=True)

    def files(self):
        return {str(path.relative_to(self.entity.path)): path.read_bytes()
                for path in self.entity.path.rglob('*') if path.is_file()}

    def pending(self, rows):
        path = self.entity.staging_dir / 'pending-categorization.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(rows))

    def decision(self, rows, **changes):
        return dict(id='answer-1', source_ids=[row['id'] for row in rows],
                    source_digest=routine_source_digest(rows), category='Expenses:Fees',
                    source_digests={row['id']: routine_source_digest([row]) for row in rows},
                    approved_ledger_account=BANK, **changes)

    def test_intake_suppresses_categorizer_and_learned_trust_in_all_paths(self):
        for forced in (False, True):
            with self.subTest(forced=forced), patch.dict('os.environ', {'SLASHBOOKS_INTAKE_ONLY': '1' if forced else ''}):
                categorizer = Mock(return_value=(CATEGORY, 'auto'))
                result = import_transactions(self.entity, [txn(str(forced))], 'intake',
                                             categorizer, intake_only=not forced)
                self.assertEqual((0, 1), (result.new_entries, result.pending_categorization))
                categorizer.assert_not_called()
        self.assertEqual(2, len(routine_plan(self.entity, self.policy)['actions']))

    def test_rule_actions_are_pure_and_apply_without_implicit_learning(self):
        self.stage(txn())
        before = self.files()
        plan = routine_plan(self.entity, self.policy)
        self.assertEqual([dict(source_id='one', category=CATEGORY, authority_id='rule-1')], plan['actions'])
        self.assertEqual(before, self.files())
        propose(self.entity, 'one', CATEGORY, 'Approved rule rule-1')
        confirm(self.entity, 'one', 'routine', learn=False)
        self.assertEqual({}, load_learned_context(self.entity))
        entries = LedgerStore(self.entity.path / 'ledger.sqlite').load_entries()
        self.assertEqual([(BANK, Decimal('-12.00')), (CATEGORY, Decimal('12.00'))],
                         [(posting.account, posting.amount) for posting in entries[0].postings])
        before = self.files()
        self.assertEqual([{'source_id': 'one', 'reason': 'already_posted'}],
                         routine_plan(self.entity, self.policy, ['one'])['skipped'])
        self.assertEqual(before, self.files())
        result = self.stage(txn())
        self.assertEqual((0, 0, 1), (result.new_entries, result.pending_categorization, result.skipped_duplicate))

    def test_default_confirm_still_learns(self):
        self.stage(txn())
        propose(self.entity, 'one', CATEGORY, 'Human review')
        confirm(self.entity, 'one', 'human')
        self.assertEqual(1, load_learned_context(self.entity)['SERVICE']['confirmed_count'])

    def test_learning_is_not_policy_authority(self):
        for sid in ('one', 'two', 'three'):
            self.stage(txn(sid))
            propose(self.entity, sid, CATEGORY, 'Human review')
            confirm(self.entity, sid, 'human')
        self.assertEqual((CATEGORY, 'auto'), make_categorizer(self.entity)(txn('four')))
        self.stage(txn('four'))
        self.policy['rules'] = []
        self.assertEqual('needs_category', routine_plan(self.entity, self.policy)['unresolved'][0]['reason'])

    def test_frozen_decision_applies_only_exact_group_and_captured_values(self):
        rows = [txn('one'), txn('two')]
        self.stage(*rows)
        decision = self.decision(rows)
        self.assertEqual(['answer-1'] * 2, [row['authority_id'] for row in
                         routine_plan(self.entity, self.policy, decisions=[decision])['actions']])
        self.pending([dict(rows[0], amount='-13.00'), rows[1]])
        self.assertEqual({'stale_decision'}, {row['reason'] for row in
                         routine_plan(self.entity, self.policy, decisions=[decision])['unresolved']})
        self.pending(rows)
        decision['category'] = 'Income:Services'
        self.assertEqual({'stale_decision'}, {row['reason'] for row in
                         routine_plan(self.entity, self.policy, decisions=[decision])['unresolved']})

    def test_digest_is_order_stable_and_validates_money_identity(self):
        first, second = txn('one'), txn('two')
        digest = routine_source_digest([first, second])
        self.assertEqual(digest, routine_source_digest([dict(second, amount='-12'), first]))
        for field, value in [('amount', '-12.01'), ('accountId', 'other'), ('date', '2026-01-16'),
                             ('currency', 'EUR'), ('description', 'Other')]:
            self.assertNotEqual(digest, routine_source_digest([dict(first, **{field: value}), second]))
        for rows in ([], [first, first], [dict(first, amount='NaN')], [dict(first, amount='1.001')],
                     [dict(first, id='')], [dict(first, date='2026-1-2')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                routine_source_digest(rows)

    def test_decisions_cannot_mix_account_currency_counterparty_or_sign(self):
        for change in ({'accountId': 'card', 'accountType': 'credit_card'}, {'amount': '12.00'},
                       {'description': 'Other'}, {'currency': 'EUR'}):
            rows = [txn('one'), dict(txn('two'), **change)]
            self.pending(rows)
            plan = routine_plan(self.entity, self.policy, decisions=[self.decision(rows)])
            self.assertFalse(plan['actions'])

    def test_mapping_snapshot_and_source_role_are_required(self):
        self.stage(txn())
        decision = self.decision([txn()])
        self.entity.entity_config['bank_account_mappings']['checking'] = 'Expenses:Fees'
        self.assertEqual('source_account_role_conflict', routine_plan(self.entity, self.policy)['unresolved'][0]['reason'])
        self.entity.entity_config['bank_account_mappings']['checking'] = 'Assets:Bank:Other'
        store = LedgerStore(self.entity.path / 'ledger.sqlite')
        with store.transaction() as conn:
            store.insert_opens([Open(date(2000, 1, 1), 'Assets:Bank:Other')], conn)
        for answers in ([], [decision]):
            self.assertEqual('mapping_changed', routine_plan(self.entity, self.policy, decisions=answers)['unresolved'][0]['reason'])
        self.pending([dict(txn(), accountId='card', accountType='credit_card')])
        self.policy['rules'][0].update(source_account_id='card', approved_ledger_account=CARD)
        self.assertEqual(1, len(routine_plan(self.entity, self.policy)['actions']))

    def test_browser_bridge_worklist_needs_no_queue_proposal_and_preserves_identity(self):
        # merge_documents uses this normalized shape and hashed hosted account ID.
        account_id = 'hosted-account:abc123'
        self.entity.entity_config['bank_account_mappings'][account_id] = BANK
        row = dict(txn(), accountId=account_id, accountName=BANK, status='posted')
        self.pending([row])
        self.assertFalse((self.entity.path / 'review-queue').exists())
        self.policy['rules'][0]['source_account_id'] = account_id
        before = self.files()
        self.assertEqual('one', routine_plan(self.entity, self.policy)['actions'][0]['source_id'])
        options = routine_context(self.entity)
        self.assertEqual(BANK, options['source_accounts'][0]['ledger_account'])
        self.assertEqual(before, self.files())

    def test_category_options_exclude_source_accounts_and_invalid_decisions(self):
        self.stage(txn())
        names = {row['name'] for row in routine_context(self.entity)['categories']}
        self.assertNotIn(BANK, names)
        self.assertNotIn(CARD, names)
        for bad in (BANK, CARD, 'Expenses:Unknown'):
            self.policy['review_categories'] = [bad]
            with self.assertRaises(ValueError):
                routine_plan(self.entity, self.policy)

    def test_reserved_lineage_payload_is_never_eligible_even_if_already_staged(self):
        for changes in ({'metadata': {'correction-of': 'original'}}, {'reverses': 'original'},
                        {'accountId': None, 'account_id': 'checking'}):
            self.pending([dict(txn(), **changes)])
            before = self.files()
            self.assertEqual('invalid_source', routine_plan(self.entity, self.policy)['unresolved'][0]['reason'])
            self.assertEqual(before, self.files())

    def test_pending_closed_currency_zero_invalid_mapping_and_caps_hold(self):
        cases = [({'pending': True}, 'pending'), ({'currency': 'EUR'}, 'unsupported_currency_or_zero_amount'),
                 ({'amount': '0'}, 'unsupported_currency_or_zero_amount'), ({'amount': 'Inf'}, 'invalid_source'),
                 ({'accountId': 'missing'}, 'mapping_required'), ({'accountType': 'unknown'}, 'source_account_type_unknown'),
                 ({'amount': '-21'}, 'needs_category')]
        for change, reason in cases:
            self.pending([dict(txn(), **change)])
            before = self.files()
            self.assertEqual(reason, routine_plan(self.entity, self.policy)['unresolved'][0]['reason'])
            self.assertEqual(before, self.files())
        self.pending([txn()])
        self.entity.entity_config['closed_through'] = None
        self.assertEqual('closed_period', routine_plan(self.entity, self.policy, closed_through='2026-01-31')['unresolved'][0]['reason'])
        self.policy['enabled'] = False
        self.assertEqual('policy_disabled', routine_plan(self.entity, self.policy)['unresolved'][0]['reason'])

    def test_ambiguous_rules_and_specialized_proposal_hold(self):
        self.stage(txn())
        self.policy['rules'].append(dict(self.policy['rules'][0], id='another'))
        self.assertEqual('ambiguous_authority', routine_plan(self.entity, self.policy)['unresolved'][0]['reason'])
        path = self.entity.path / 'review-queue'
        path.mkdir()
        (path / 'pair.json').write_text(json.dumps(dict(source_id='pair', source_ids=['one', 'two'],
                                                     status='open', proposal_type='transfer-pair')))
        self.assertEqual('existing_review', routine_plan(self.entity, self.policy)['unresolved'][0]['reason'])

    def test_generic_human_proposal_is_not_overwritten_by_routine(self):
        self.stage(txn())
        propose(self.entity, 'one', 'Expenses:Fees', 'Human pending decision')
        before = self.files()
        self.assertEqual('existing_review', routine_plan(self.entity, self.policy)['unresolved'][0]['reason'])
        self.assertEqual(before, self.files())

    def test_group_answer_survives_partial_posting_and_cursor_restart(self):
        rows = [txn(sid) for sid in ('a', 'b', 'c')]
        self.stage(*rows)
        decision = self.decision(rows)
        first = routine_plan(self.entity, self.policy, decisions=[decision], limit=2)
        for action in first['actions']:
            propose(self.entity, action['source_id'], action['category'], action['authority_id'])
            confirm(self.entity, action['source_id'], 'routine', learn=False)
        for cursor in ('', first['next_cursor']):
            last = routine_plan(self.entity, self.policy, decisions=[decision], after_source_id=cursor)
            self.assertEqual([dict(source_id='c', category='Expenses:Fees', authority_id='answer-1')], last['actions'])
            self.assertFalse(last['unresolved'])
        self.pending([dict(rows[-1], amount='-12.01')])
        self.assertEqual('stale_decision', routine_plan(self.entity, self.policy, decisions=[decision])['unresolved'][0]['reason'])

    def test_lexical_cursor_remains_correct_after_previous_batch_removed(self):
        self.stage(*(txn(sid) for sid in ('c', 'a', 'b')))
        first = routine_plan(self.entity, self.policy, limit=2)
        self.assertEqual(['a', 'b'], [row['source_id'] for row in first['actions']])
        self.assertEqual('b', first['next_cursor'])
        for row in first['actions']:
            propose(self.entity, row['source_id'], row['category'], row['authority_id'])
            confirm(self.entity, row['source_id'], 'routine', learn=False)
        last = routine_plan(self.entity, self.policy, limit=2, after_source_id=first['next_cursor'])
        self.assertEqual(['c'], [row['source_id'] for row in last['actions']])
        self.assertIsNone(last['next_cursor'])
        self.assertEqual(dict(posted=2, held=1), routine_context(self.entity)['counts'])

    def test_explicit_human_hold_overrides_rule_without_stopping_other_sources(self):
        rows = [txn('a'), txn('b')]
        self.stage(*rows)
        decision = self.decision([rows[0]])
        decision['category'] = None
        before = self.files()
        plan = routine_plan(self.entity, self.policy, decisions=[decision])
        self.assertEqual(['b'], [row['source_id'] for row in plan['actions']])
        self.assertEqual([('a', 'human_hold')], [(row['source_id'], row['reason']) for row in plan['unresolved']])
        self.assertEqual(before, self.files())
        self.pending([dict(rows[0], amount='-13.00'), rows[1]])
        self.assertEqual('stale_decision', routine_plan(self.entity, self.policy, decisions=[decision])['unresolved'][0]['reason'])

    def test_decision_missing_nonposted_member_cannot_partially_approve(self):
        rows = [txn('a'), txn('b')]
        decision = self.decision(rows)
        self.pending([rows[0]])
        self.assertEqual('stale_decision', routine_plan(self.entity, self.policy, decisions=[decision])['unresolved'][0]['reason'])

    def test_malformed_existing_review_ownership_fails_closed(self):
        self.stage(txn())
        path = self.entity.path / 'review-queue'
        path.mkdir()
        (path / 'unknown.json').write_text(json.dumps({'status': 'open'}))
        before = self.files()
        with self.assertRaises(ValueError):
            routine_plan(self.entity, self.policy)
        self.assertEqual(before, self.files())

    def test_malformed_state_and_policy_fail_closed_without_writes(self):
        self.stage(txn())
        for change in ({'rules': [dict(self.policy['rules'][0], category='Expenses:Missing')]},
                       {'rules': [dict(self.policy['rules'][0], approved_ledger_account='')]},
                       {'answer_roles': ['stranger']}, {'enabled': 'true'}):
            before = self.files()
            with self.assertRaises(ValueError):
                routine_plan(self.entity, dict(self.policy, **change))
            self.assertEqual(before, self.files())
        path = self.entity.staging_dir / 'pending-categorization.json'
        path.write_text('{broken')
        before = self.files()
        with self.assertRaises(ValueError):
            routine_plan(self.entity, self.policy)
        self.assertEqual(before, self.files())

    def test_reversed_originals_cannot_be_reactivated_from_staging(self):
        self.stage(txn())
        propose(self.entity, 'one', CATEGORY, 'Human review')
        confirm(self.entity, 'one', 'human', learn=False)
        replacement = Entry(date=date(2026, 1, 15), narration='Correction', meta=(('source-id', 'replacement'),),
                            postings=(Posting(BANK, Decimal('-12'), 'USD'), Posting('Expenses:Fees', Decimal('12'), 'USD')))
        reverse_and_correct(self.entity, 'one', replacement, 'correction')
        self.pending([txn()])
        before = self.files()
        self.assertEqual([dict(source_id='one', reason='reversed')], routine_plan(self.entity, self.policy)['skipped'])
        self.assertEqual(before, self.files())


if __name__ == '__main__':
    unittest.main()
