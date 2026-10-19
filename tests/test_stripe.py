"""Synthetic processor clearing, explicit mapping and immutable-source proofs."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bookkeeping.entity import Entity
from bookkeeping.ledger.importer import import_transactions
from bookkeeping.ledger.model import Open
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.routine import routine_source_digest
from bookkeeping.stripe import stripe_apply, stripe_plan, stripe_stage, stripe_preview, stripe_prepare, stripe_configure, stripe_merge_previews

BANK = 'Assets:Bank:Checking'
CLEARING = 'Assets:Processors:Stripe'
TRANSIT = 'Assets:Transfers:Stripe'
INCOME = 'Income:Services'
REFUNDS = 'Income:Refunds'
FEES = 'Expenses:Payment-Fees'


def evidence():
    return {'source': 'stripe', 'account': {'id': 'acct_test', 'livemode': False},
            'coverage': {'complete': True},
            'balanceTransactions': [{'id': 'txn_sale', 'created': 1768435200, 'amount': 10000,
                'fee': 300, 'net': 9700, 'currency': 'usd', 'status': 'available', 'type': 'charge', 'source': 'ch_sale'}],
            'charges': [{'id': 'ch_sale', 'livemode': False, 'amount': 10000, 'currency': 'usd',
                'status': 'succeeded', 'paid': True, 'captured': True, 'balance_transaction': 'txn_sale',
                'metadata': {'stream': 'service'}}]}


class StripeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.entity = Entity(root, {'bank_account_mappings': {'acct_test': CLEARING, 'bank': BANK}}, {})
        (root / 'entity.json').write_text(json.dumps(self.entity.entity_config))
        self.store = LedgerStore(root / 'ledger.sqlite'); self.store.initialize()
        with self.store.transaction() as conn:
            self.store.insert_opens([Open(date(2000, 1, 1), account, ('USD',)) for account in
                (BANK, CLEARING, TRANSIT, INCOME, 'Income:Subscriptions', REFUNDS, FEES)], conn)
        self.policy = dict(version=1, enabled=True, stripe_account_id='acct_test', livemode=False, currency='USD',
            clearing_account=CLEARING, payout_transit_account=TRANSIT, fee_account=FEES, refund_account=REFUNDS,
            revenue_rules=[dict(id='services', match={'metadata': {'object': 'charge', 'key': 'stream', 'value': 'service'}}, category=INCOME)])
        self.raw = evidence()

    def files(self):
        return {str(path.relative_to(self.entity.path)): path.read_bytes() for path in self.entity.path.rglob('*') if path.is_file()}

    def stage(self, **kwargs):
        return stripe_stage(self.entity, self.raw, self.policy, **kwargs)['plan']

    def apply(self, **kwargs):
        plan = stripe_plan(self.entity, self.raw, self.policy, **kwargs)
        return stripe_apply(self.entity, self.raw, self.policy, expected_plan_digest=plan['plan_digest'], **kwargs)

    def balances(self):
        result = {}
        for entry in self.store.load_entries():
            for posting in entry.postings:
                result[posting.account] = result.get(posting.account, Decimal(0)) + posting.amount
        return result

    def full_flow(self):
        self.raw['balanceTransactions'].extend([
            {'id': 'txn_refund', 'created': 1768521600, 'amount': -2000, 'fee': 0, 'net': -2000,
             'currency': 'usd', 'status': 'available', 'type': 'refund', 'source': 're_one'},
            {'id': 'txn_payout', 'created': 1768608000, 'amount': -7700, 'fee': 0, 'net': -7700,
             'currency': 'usd', 'status': 'available', 'type': 'payout', 'source': 'po_one'}])
        self.raw['refunds'] = [{'id': 're_one', 'status': 'succeeded', 'amount': 2000, 'currency': 'usd',
                               'balance_transaction': 'txn_refund', 'charge': 'ch_sale'}]
        self.raw['payouts'] = [{'id': 'po_one', 'amount': 7700, 'currency': 'usd', 'status': 'paid', 'balance_transaction': 'txn_payout'}]
        bank = dict(id='bank-original', date='2026-01-18', amount='77.00', accountId='bank', accountType='checking', description='Processor payout')
        import_transactions(self.entity, [bank], 'bank-intake', intake_only=True)
        return [dict(payout_id='po_one', bank_source_id=bank['id'], bank_source_digest=routine_source_digest([bank]), approved_ledger_account=BANK)]

    def test_sale_fee_refund_payout_bank_transfer_post_once_with_exact_roles(self):
        matches = self.full_flow()
        plan = self.stage(bank_matches=matches)
        self.assertEqual(4, len(plan['actions']))
        self.assertEqual(4, self.apply(bank_matches=matches)['posted'])
        self.assertEqual({CLEARING: Decimal('0'), TRANSIT: Decimal('0'), BANK: Decimal('77'),
                          INCOME: Decimal('-100'), REFUNDS: Decimal('20'), FEES: Decimal('3')}, self.balances())
        self.assertEqual({'stripe:acct_test:txn_sale', 'stripe:acct_test:txn_refund', 'stripe:acct_test:txn_payout', 'bank-original'},
                         {entry.source_id for entry in self.store.load_entries()})
        before = self.files()
        result = self.apply(bank_matches=matches)
        self.assertEqual((0, 4, 0), (result['posted'], result['skipped'], result['held']))
        self.assertEqual(before, self.files())
        self.assertFalse((self.entity.path / 'learned-context').exists())

    def test_row_above_the_ask_first_amount_is_staged_held_and_never_applied(self):
        self.entity.trust_policy['ask_first_above'] = '50.00'
        plan = self.stage()
        self.assertEqual([], plan['actions'])
        self.assertEqual([('stripe:acct_test:txn_sale', 'above_ask_first_amount')],
                         [(row['source_id'], row['reason']) for row in plan['unresolved']])
        result = self.apply()
        self.assertEqual((0, 1), (result['posted'], result['held']))
        self.assertEqual([], list(self.store.load_entries()))
        self.entity.trust_policy['ask_first_above'] = '100.00'
        self.assertEqual(1, len(stripe_plan(self.entity, self.raw, self.policy)['actions']))

    def test_plan_is_read_only_and_apply_requires_staged_sources(self):
        before = self.files()
        plan = stripe_plan(self.entity, self.raw, self.policy)
        self.assertEqual(before, self.files())
        with self.assertRaisesRegex(ValueError, 'Stage every'):
            stripe_apply(self.entity, self.raw, self.policy, expected_plan_digest=plan['plan_digest'])
        self.assertEqual(before, self.files())

    def test_preview_is_pure_and_keeps_currencies_separate_in_minor_units(self):
        self.full_flow()
        self.raw['balanceTransactions'].append(dict(self.raw['balanceTransactions'][0], id='foreign', currency='jpy', amount=1000, fee=0, net=1000))
        before = self.files(); result = stripe_preview(self.raw)
        self.assertEqual(before, self.files())
        usd = next(row for row in result['by_currency'] if row['currency'] == 'USD')
        self.assertEqual((10000, 2000, 300, 7700, 0), (usd['gross_sales'], usd['refunds'], usd['fees'], usd['payouts'], usd['net_balance_change']))
        self.assertEqual(1000, next(row for row in result['by_currency'] if row['currency'] == 'JPY')['gross_sales'])

    def test_prepare_creates_audited_catalog_only_and_repeat_is_noop(self):
        first = stripe_prepare(self.entity, opening_date='2026-01-01')
        self.assertEqual(3, len(first['created']))  # clearing already exists
        self.assertEqual([], self.store.load_entries())
        self.assertFalse(first['opening_balance_supplied']); self.assertFalse(first['policy_approved'])
        before = self.files()
        self.assertEqual([], stripe_prepare(self.entity, opening_date='2026-01-01')['created'])
        self.assertEqual(before, self.files())

    def test_preview_merge_counts_disjoint_roots_and_rejects_overlap_or_invalid_totals(self):
        first = stripe_preview(self.raw)
        raw = copy.deepcopy(self.raw); raw['balanceTransactions'][0]['id'] = 'txn_other'
        second = stripe_preview(raw)
        merged = stripe_merge_previews(first, second)
        self.assertEqual((2, 20000, 600), (merged['transaction_count'], merged['by_currency'][0]['gross_sales'], merged['by_currency'][0]['fees']))
        self.assertEqual(['txn_other', 'txn_sale'], merged['balance_transaction_ids'])
        with self.assertRaisesRegex(ValueError, 'overlap'): stripe_merge_previews(first, first)
        for field, value in [('fees', 1.2), ('gross_sales', True), ('net_balance_change', 10**16), ('count', 9)]:
            bad = copy.deepcopy(second); bad['by_currency'][0][field] = value
            with self.assertRaises(ValueError): stripe_merge_previews(first, bad)

    def test_configure_rejects_other_feed_mapped_processor_transit_and_wrong_asset_roles(self):
        self.entity.entity_config['bank_account_mappings']['other-feed'] = TRANSIT
        before = self.files()
        with self.assertRaisesRegex(ValueError, 'another source'):
            stripe_configure(self.entity, self.policy)
        self.assertEqual(before, self.files())
        del self.entity.entity_config['bank_account_mappings']['other-feed']
        for field in ('clearing_account', 'payout_transit_account'):
            policy = dict(self.policy, **{field: BANK})
            with self.assertRaises(ValueError): stripe_configure(self.entity, policy)
        self.assertEqual(before, self.files())

    def test_configure_uses_normal_mapping_without_posting_or_learning(self):
        self.entity.entity_config['bank_account_mappings'].pop('acct_test')
        (self.entity.path / 'entity.json').write_text(json.dumps(self.entity.entity_config))
        result = stripe_configure(self.entity, self.policy)
        self.assertEqual(1, result['version']); self.assertEqual([], self.store.load_entries())
        self.assertEqual(CLEARING, self.entity.entity_config['bank_account_mappings']['acct_test'])
        self.assertEqual(CLEARING, json.loads((self.entity.path / 'entity.json').read_text())['bank_account_mappings']['acct_test'])

    def test_changed_evidence_policy_and_closed_period_refuse_old_preview(self):
        for change in ('amount', 'policy', 'close'):
            with self.subTest(change=change):
                plan = self.stage()
                before = self.files()
                raw, policy = copy.deepcopy(self.raw), copy.deepcopy(self.policy)
                kwargs = {}
                if change == 'amount':
                    raw['balanceTransactions'][0]['amount'] += 1; raw['balanceTransactions'][0]['net'] += 1
                elif change == 'policy':
                    policy['version'] += 1
                else:
                    kwargs['closed_through'] = '2026-01-31'
                with self.assertRaisesRegex(ValueError, 'plan changed'):
                    stripe_apply(self.entity, raw, policy, expected_plan_digest=plan['plan_digest'], **kwargs)
                self.assertEqual(before, self.files())

    def test_wrong_account_mode_mapping_and_incomplete_capture_reject_before_stage(self):
        for field, value in [('account', {'id': 'acct_other', 'livemode': False}), ('account', {'id': 'acct_test', 'livemode': True}),
                             ('coverage', {'complete': False})]:
            raw = copy.deepcopy(self.raw); raw[field] = value
            before = self.files()
            with self.assertRaises(ValueError): stripe_stage(self.entity, raw, self.policy)
            self.assertEqual(before, self.files())
        self.entity.entity_config['bank_account_mappings']['acct_test'] = BANK
        with self.assertRaisesRegex(ValueError, 'mapping changed'):
            self.stage()

    def test_no_metadata_fallback_or_ambiguous_rule_precedence(self):
        self.raw['charges'][0]['metadata'] = {}
        self.assertEqual('missing_revenue_mapping', stripe_plan(self.entity, self.raw, self.policy)['unresolved'][0]['reason'])
        self.raw['charges'][0]['metadata'] = {'stream': 'service'}
        self.policy['revenue_rules'].append(dict(self.policy['revenue_rules'][0], id='other'))
        self.assertEqual('ambiguous_revenue_mapping', stripe_plan(self.entity, self.raw, self.policy)['unresolved'][0]['reason'])

    def invoice(self):
        self.raw['charges'][0]['invoice'] = 'in_one'
        self.raw['invoices'] = [{'id': 'in_one', 'status': 'paid', 'amount_paid': 10000, 'amount_due': 10000, 'total': 10000,
            'lines': {'has_more': False, 'data': [
                {'id': 'il_one', 'amount': 6000, 'currency': 'usd', 'pricing': {'price_details': {'price': 'price_one', 'product': 'prod_one'}}},
                {'id': 'il_two', 'amount': 4000, 'currency': 'usd', 'price': {'id': 'price_two', 'product': 'prod_two'}}]}}]
        self.policy['revenue_rules'] = [dict(id='one', match={'price_id': 'price_one'}, category=INCOME),
                                        dict(id='two', match={'product_id': 'prod_two'}, category='Income:Subscriptions')]

    def test_exact_complete_multiline_invoice_maps_both_revenue_streams(self):
        self.invoice(); self.stage(); self.apply()
        self.assertEqual(Decimal('-60'), self.balances()[INCOME])
        self.assertEqual(Decimal('-40'), self.balances()['Income:Subscriptions'])

    def test_product_rule_uses_complete_resolved_price_when_line_has_only_price_id(self):
        self.invoice()
        self.raw['invoices'][0]['lines']['data'] = [{'id': 'il_one', 'amount': 10000, 'currency': 'usd', 'price': 'price_one'}]
        self.raw['prices'] = [{'id': 'price_one', 'product': 'prod_one'}]
        self.policy['revenue_rules'] = [dict(id='product', match={'product_id': 'prod_one'}, category=INCOME)]
        self.stage(); self.assertEqual(1, self.apply()['posted'])
        self.assertEqual(Decimal('-100'), self.balances()[INCOME])

    def test_modern_invoice_payment_resolves_exact_invoice_and_holds_partial_or_multiple(self):
        self.invoice(); self.raw['charges'][0].pop('invoice')
        self.raw['charges'][0]['payment_intent'] = 'pi_one'
        payment = dict(id='inpay_one', payment={'type': 'payment_intent', 'payment_intent': 'pi_one'},
                       invoice='in_one', amount_paid=10000, amount_requested=10000,
                       currency='usd', livemode=False, status='paid')
        self.raw['invoicePayments'] = [payment]
        self.assertEqual(1, len(stripe_plan(self.entity, self.raw, self.policy)['actions']))
        for rows in ([dict(payment, amount_paid=9000)], [payment, dict(payment, id='inpay_two')]):
            self.raw['invoicePayments'] = rows
            self.assertEqual('partial_or_multiple_invoice_payments', stripe_plan(self.entity, self.raw, self.policy)['unresolved'][0]['reason'])

    def test_checkout_exact_price_mapping_and_refund_share_adjustment_refusal(self):
        self.full_flow(); self.raw['charges'][0]['payment_intent'] = 'pi_one'
        self.policy['revenue_rules'] = [dict(id='checkout', match={'product_id': 'prod_one'}, category=INCOME)]
        self.raw['prices'] = [dict(id='price_one', product='prod_one')]
        self.raw['checkoutSessions'] = [dict(id='cs_one', payment_intent='pi_one', status='complete', payment_status='paid',
            currency='usd', amount_total=10000, amount_subtotal=10000, total_details={},
            line_items=dict(has_more=False, data=[dict(id='li_one', amount_total=10000, amount_subtotal=10000,
                                                    currency='usd', price='price_one')]))]
        self.assertEqual(3, len(stripe_plan(self.entity, self.raw, self.policy)['actions']))
        original = copy.deepcopy(self.raw)
        for change in ('tax', 'partial', 'truncated', 'multiline_refund'):
            self.raw = copy.deepcopy(original); session = self.raw['checkoutSessions'][0]
            if change == 'tax': session['total_details']['amount_tax'] = 10
            elif change == 'partial': session['amount_total'] = 9000
            elif change == 'truncated': session['line_items']['has_more'] = True
            else:
                session['line_items']['data'][0].update(amount_total=5000, amount_subtotal=5000)
                session['line_items']['data'].append(dict(session['line_items']['data'][0], id='li_two'))
            plan = stripe_plan(self.entity, self.raw, self.policy)
            actions = {row['source_id'] for row in plan['actions']}
            self.assertNotIn('stripe:acct_test:txn_refund', actions, change)
            if change != 'multiline_refund': self.assertNotIn('stripe:acct_test:txn_sale', actions, change)

    def test_tax_shipping_discounts_proration_partial_and_truncated_lines_are_held(self):
        self.invoice()
        original = copy.deepcopy(self.raw)
        for change in ('tax', 'shipping', 'discount', 'proration', 'partial', 'truncated', 'missing_line_map'):
            self.raw = copy.deepcopy(original)
            invoice = self.raw['invoices'][0]
            if change == 'tax': invoice['total_taxes'] = [{'amount': 100}]
            elif change == 'shipping': invoice['shipping_cost'] = {'amount_total': 100}
            elif change == 'discount': invoice['lines']['data'][0]['discount_amounts'] = [{'amount': 100}]
            elif change == 'proration': invoice['lines']['data'][0]['proration'] = True
            elif change == 'partial': invoice['amount_due'] = 20000
            elif change == 'truncated': invoice['lines']['has_more'] = True
            else: invoice['lines']['data'][1]['price']['product'] = 'prod_unknown'
            before = self.files(); plan = stripe_plan(self.entity, self.raw, self.policy)
            self.assertFalse(plan['actions'], change); self.assertEqual(1, len(plan['unresolved']), change)
            self.assertEqual(before, self.files())

    def test_mixed_line_refund_without_allocation_is_held(self):
        self.full_flow(); self.invoice()
        plan = stripe_plan(self.entity, self.raw, self.policy)
        self.assertIn('mixed_line_refund_requires_allocation', {row['reason'] for row in plan['unresolved']})

    def test_single_line_refund_cannot_bypass_original_tax_discount_shipping_or_partial_refusal(self):
        self.full_flow(); self.invoice()
        invoice = self.raw['invoices'][0]
        invoice['lines']['data'] = [dict(invoice['lines']['data'][0], amount=10000)]
        original = copy.deepcopy(self.raw)
        for field, value in [('total_taxes', [{'amount': 100}]), ('shipping_cost', {'amount_total': 100}),
                             ('total_discount_amounts', [{'amount': 100}]), ('amount_due', 20000)]:
            self.raw = copy.deepcopy(original); self.raw['invoices'][0][field] = value
            before = self.files(); plan = stripe_plan(self.entity, self.raw, self.policy)
            self.assertNotIn('stripe:acct_test:txn_sale', {row['source_id'] for row in plan['actions']})
            self.assertNotIn('stripe:acct_test:txn_refund', {row['source_id'] for row in plan['actions']})
            self.assertEqual(before, self.files())

    def test_bank_income_already_posted_blocks_entire_batch_without_new_roots(self):
        matches = self.full_flow()
        from bookkeeping.queue import propose, confirm
        propose(self.entity, 'bank-original', INCOME, 'Old net deposit review')
        confirm(self.entity, 'bank-original', 'old-review')
        before = self.files(); plan = stripe_plan(self.entity, self.raw, self.policy, bank_matches=matches)
        self.assertFalse(plan['actions'])
        self.assertIn('bank_source_already_posted_requires_review', {row['reason'] for row in plan['unresolved']})
        self.assertEqual(before, self.files())

    def test_bank_arrival_after_processor_payout_does_not_repeat_payout(self):
        matches = self.full_flow()
        self.stage(); self.apply()
        count = len(self.store.load_entries())
        plan = stripe_plan(self.entity, self.raw, self.policy, bank_matches=matches)
        self.assertEqual(['bank-original'], [row['source_id'] for row in plan['actions']])
        self.apply(bank_matches=matches)
        self.assertEqual(count + 1, len(self.store.load_entries()))
        self.assertEqual(Decimal('0'), self.balances()[TRANSIT])

    def test_currency_dispute_failed_payout_and_unsettled_refund_are_explicit_holds(self):
        self.full_flow()
        for sid, changes in [('txn_sale', {'currency': 'eur'}), ('txn_sale', {'type': 'adjustment'})]:
            raw = copy.deepcopy(self.raw)
            next(row for row in raw['balanceTransactions'] if row['id'] == sid).update(changes)
            plan = stripe_plan(self.entity, raw, self.policy)
            self.assertIn('stripe:acct_test:' + sid, {row['source_id'] for row in plan['unresolved']})
        self.raw['payouts'][0]['status'] = 'failed'; self.raw['refunds'][0]['status'] = 'pending'
        self.assertEqual(2, len(stripe_plan(self.entity, self.raw, self.policy)['unresolved']))

    def test_malformed_later_row_causes_zero_intake_mutation(self):
        self.raw['balanceTransactions'].append(dict(self.raw['balanceTransactions'][0], id='bad', net=123))
        before = self.files()
        with self.assertRaisesRegex(ValueError, 'amount minus fee'):
            self.stage()
        self.assertEqual(before, self.files())


if __name__ == '__main__': unittest.main()
