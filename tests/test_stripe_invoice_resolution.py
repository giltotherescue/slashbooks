"""Which invoice a Stripe charge belongs to, and the holding account for activity the engine cannot place.

Every shape Stripe uses to name a charge's invoice counts: charge.invoice, payment_intent.invoice, a modern
invoice payment, a Checkout session's invoice, and inline or expanded objects. References that disagree, an
invoice the engine does not have, or an expanded object without its own ID never post as revenue: the sale,
its refunds and its disputes post to "Uncategorized Stripe activity" with a review reason. All data is synthetic.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_stripe as base  # noqa: E402  (shared synthetic fixtures; its TestCase is not re-exported here)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bookkeeping.engine_accounts import engine_owned_accounts  # noqa: E402
from bookkeeping.stripe import STRIPE_HOLDING_ACCOUNT, stripe_apply, stripe_plan  # noqa: E402

HOLDING = STRIPE_HOLDING_ACCOUNT
SALE = 'stripe:acct_test:txn_sale'
REFUND = 'stripe:acct_test:txn_refund'
DISPUTE = 'stripe:acct_test:txn_dispute'


class StripeFixture(unittest.TestCase):
    """Shared synthetic fixture and helpers; it has no tests of its own."""

    setUp = base.StripeTests.setUp
    files = base.StripeTests.files
    stage = base.StripeTests.stage
    apply = base.StripeTests.apply
    balances = base.StripeTests.balances
    full_flow = base.StripeTests.full_flow
    invoice = base.StripeTests.invoice

    def plan(self, raw=None):
        return stripe_plan(self.entity, raw or self.raw, self.policy)

    def action(self, plan, sid=SALE):
        return next((row for row in plan['actions'] if row['source_id'] == sid), None)

    def postings(self, plan, sid=SALE):
        action = self.action(plan, sid)
        return action and {row['account']: row['amount'] for row in action['postings']}

    def assertRevenue(self, plan, sid=SALE):
        self.assertEqual({base.CLEARING: '97.00', base.INCOME: '-60.00', 'Income:Subscriptions': '-40.00',
                          base.FEES: '3.00'}, self.postings(plan, sid))
        self.assertNotIn(sid, {row['source_id'] for row in plan['uncategorized']})

    def assertHeld(self, plan, reason, sid=SALE, amount='-100.00', fee='3.00', net='97.00'):
        action = self.action(plan, sid)
        self.assertIsNotNone(action, plan['unresolved'])
        expected = {base.CLEARING: net, HOLDING: amount}
        if fee:
            expected[base.FEES] = fee
        self.assertEqual(expected, self.postings(plan, sid))
        self.assertEqual(reason, action['review_reason'])
        self.assertEqual('uncategorized-stripe-activity', action['authority_id'])
        self.assertIn(dict(source_id=sid, reason=reason), plan['uncategorized'])

    def hold_sale(self):
        self.charge = self.raw['charges'].pop()
        self.stage(); self.assertEqual(1, self.apply()['posted'])
        self.original = self.store.load_entries()[0]

    def entry(self, source_id):
        return next(entry for entry in self.store.load_entries() if entry.source_id == source_id)

    def invoice_without_charge_reference(self):
        """The two-line invoice fixture, reachable only through the payment intent pi_one."""
        self.invoice()
        self.raw['charges'][0].pop('invoice')
        self.raw['charges'][0]['payment_intent'] = 'pi_one'

    def dispute(self, **changes):
        self.raw['balanceTransactions'].append({'id': 'txn_dispute', 'created': 1768521600, 'amount': -10000,
            'fee': 1500, 'net': -11500, 'currency': 'usd', 'status': 'available', 'type': 'adjustment', 'source': 'du_one'})
        self.raw['disputes'] = [dict({'id': 'du_one', 'object': 'dispute', 'charge': 'ch_sale', 'amount': 10000,
                                      'currency': 'usd', 'balance_transactions': [{'id': 'txn_dispute'}]}, **changes)]


class InvoiceResolutionTests(StripeFixture):
    # Every shape that names the invoice resolves the same exact two-line sale.

    def test_payment_intent_invoice_resolves_the_sale(self):
        self.invoice_without_charge_reference()
        self.raw['paymentIntents'] = [{'id': 'pi_one', 'invoice': 'in_one', 'metadata': {}}]
        self.assertRevenue(self.plan())

    def test_expanded_payment_intent_invoice_resolves_the_sale(self):
        self.invoice_without_charge_reference()
        self.raw['charges'][0]['payment_intent'] = {'id': 'pi_one', 'invoice': 'in_one', 'metadata': {}}
        self.assertRevenue(self.plan())

    def test_checkout_session_invoice_resolves_the_invoice_not_the_session_lines(self):
        self.invoice_without_charge_reference()
        self.raw['checkoutSessions'] = [dict(id='cs_one', payment_intent='pi_one', invoice='in_one', status='complete',
            payment_status='paid', currency='usd', amount_total=10000, amount_subtotal=10000, total_details={},
            line_items=dict(has_more=False, data=[dict(id='li', amount_total=10000, amount_subtotal=10000,
                                                       currency='usd', price='price_unmapped')]))]
        self.assertRevenue(self.plan())

    def test_inline_invoice_on_the_charge_resolves_without_a_captured_copy(self):
        self.invoice()
        self.raw['charges'][0]['invoice'] = self.raw.pop('invoices')[0]
        self.assertRevenue(self.plan())

    def test_expanded_charge_with_inline_invoice_agrees_with_captured_copies(self):
        self.invoice()
        expanded = dict(self.raw['charges'][0], invoice=copy.deepcopy(self.raw['invoices'][0]))
        self.raw['balanceTransactions'][0]['source'] = expanded
        self.assertRevenue(self.plan())

    def test_every_reference_agreeing_still_posts_revenue(self):
        self.invoice()
        self.raw['charges'][0]['payment_intent'] = 'pi_one'
        self.raw['paymentIntents'] = [{'id': 'pi_one', 'invoice': 'in_one'}]
        self.raw['invoicePayments'] = [dict(id='inpay_one', payment={'type': 'payment_intent', 'payment_intent': 'pi_one'},
            invoice='in_one', amount_paid=10000, amount_requested=10000, currency='usd', livemode=False, status='paid')]
        self.raw['checkoutSessions'] = [dict(id='cs_one', payment_intent='pi_one', invoice='in_one')]
        self.assertRevenue(self.plan())

    # References that disagree or name something the engine does not have go to the holding account.

    def test_disagreeing_references_hold_the_sale(self):
        cases = {
            'charge_vs_payment_intent': lambda raw: raw.update(paymentIntents=[{'id': 'pi_one', 'invoice': 'in_two'}]),
            'charge_vs_invoice_payment': lambda raw: raw.update(invoicePayments=[dict(
                id='inpay_one', payment={'type': 'payment_intent', 'payment_intent': 'pi_one'}, invoice='in_two',
                amount_paid=10000, amount_requested=10000, currency='usd', livemode=False, status='paid')]),
            'charge_vs_checkout_session': lambda raw: raw.update(checkoutSessions=[dict(
                id='cs_one', payment_intent='pi_one', invoice='in_two')]),
            'expanded_vs_captured_payment_intent': lambda raw: raw.update(paymentIntents=[{'id': 'pi_one', 'invoice': 'in_one'}]) or
                raw['charges'][0].update(payment_intent={'id': 'pi_one', 'invoice': 'in_two'}),
        }
        reasons = {'expanded_vs_captured_payment_intent': 'payment_intent_reference_conflict'}
        self.invoice(); self.raw['charges'][0]['payment_intent'] = 'pi_one'
        self.raw['invoices'].append(dict(self.raw['invoices'][0], id='in_two'))
        original = copy.deepcopy(self.raw)
        for name, change in cases.items():
            with self.subTest(name):
                self.raw = copy.deepcopy(original); change(self.raw)
                self.assertHeld(self.plan(), reasons.get(name, 'invoice_reference_conflict'))

    def test_inline_invoice_that_disagrees_with_the_captured_invoice_is_held(self):
        self.invoice()
        self.raw['charges'][0]['invoice'] = dict(self.raw['invoices'][0], amount_paid=9000)
        self.assertHeld(self.plan(), 'invoice_reference_conflict')

    def test_expanded_charge_that_disagrees_with_the_captured_charge_is_held(self):
        self.invoice()
        self.raw['balanceTransactions'][0]['source'] = dict(self.raw['charges'][0], invoice='in_other')
        self.assertHeld(self.plan(), 'charge_reference_conflict')

    def test_invoice_the_engine_does_not_have_is_held(self):
        self.invoice(); self.raw['invoices'] = []
        self.assertHeld(self.plan(), 'missing_invoice_evidence')

    def test_charge_the_engine_does_not_have_is_held(self):
        self.raw['charges'] = []
        self.assertHeld(self.plan(), 'missing_charge_evidence')

    def test_two_checkout_sessions_for_one_payment_are_held(self):
        self.raw['charges'][0]['payment_intent'] = 'pi_one'
        self.raw['checkoutSessions'] = [dict(id='cs_one', payment_intent='pi_one'), dict(id='cs_two', payment_intent='pi_one')]
        self.assertHeld(self.plan(), 'multiple_checkout_sessions')

    # The three cases from the Clozy recheck.

    def test_recheck_checkout_session_naming_an_unfetched_invoice(self):
        self.invoice_without_charge_reference()
        self.raw['invoices'] = []
        self.raw['checkoutSessions'] = [dict(id='cs_x', payment_intent='pi_one', invoice='in_missing', status='complete',
            payment_status='paid', amount_total=10000, amount_subtotal=10000, currency='usd',
            line_items=dict(object='list', has_more=False, data=[dict(id='li', amount_total=10000, amount_subtotal=10000,
                                                                      currency='usd', price='price_one')]))]
        self.assertHeld(self.plan(), 'missing_invoice_evidence')

    def test_recheck_idless_expanded_charge_carrying_an_inline_invoice(self):
        self.invoice()
        expanded = dict(self.raw['charges'][0], invoice=copy.deepcopy(self.raw['invoices'][0]))
        del expanded['id']
        self.raw['balanceTransactions'][0]['source'] = expanded
        self.assertHeld(self.plan(), 'charge_identity_unverified')

    def test_recheck_conflicting_charge_references_on_copies_of_one_refund(self):
        self.full_flow()
        alternate = dict(self.raw['charges'][0], id='ch_other', invoice=None, payment_intent=None)
        refund = self.raw['refunds'][0]
        next(bt for bt in self.raw['balanceTransactions'] if bt['id'] == 'txn_refund')['source'] = dict(refund, charge=alternate)
        plan = self.plan()
        self.assertHeld(plan, 'refund_reference_conflict', sid=REFUND, amount='20.00', fee=None, net='-20.00')
        self.assertEqual({base.CLEARING: '97.00', base.INCOME: '-100.00', base.FEES: '3.00'}, self.postings(plan))

    # Refunds and disputes follow their charge.

    def test_refund_follows_its_charge_into_the_holding_account(self):
        self.full_flow(); self.invoice()
        self.raw['invoices'][0]['lines']['data'] = [dict(self.raw['invoices'][0]['lines']['data'][0], amount=10000)]
        self.assertIsNotNone(self.action(self.plan(), REFUND))
        self.raw['charges'][0]['payment_intent'] = 'pi_one'
        self.raw['paymentIntents'] = [{'id': 'pi_one', 'invoice': 'in_two'}]
        plan = self.plan()
        self.assertHeld(plan, 'invoice_reference_conflict')
        self.assertHeld(plan, 'invoice_reference_conflict', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_refund_expanded_with_its_charge_still_posts_when_every_copy_agrees(self):
        self.full_flow()
        next(bt for bt in self.raw['balanceTransactions'] if bt['id'] == 'txn_refund')['source'] = dict(
            self.raw['refunds'][0], charge=copy.deepcopy(self.raw['charges'][0]))
        self.assertEqual({base.CLEARING: '-20.00', base.REFUNDS: '20.00'}, self.postings(self.plan(), REFUND))

    def test_dispute_posts_to_the_holding_account_for_review(self):
        self.dispute()
        plan = self.plan()
        self.assertHeld(plan, 'dispute_requires_review', sid=DISPUTE, amount='100.00', fee='15.00', net='-115.00')
        self.assertEqual({base.CLEARING: '97.00', base.INCOME: '-100.00', base.FEES: '3.00'}, self.postings(plan))

    def test_dispute_follows_its_charge_reason(self):
        self.invoice(); self.dispute()
        self.raw['invoices'] = []
        self.assertHeld(self.plan(), 'missing_invoice_evidence', sid=DISPUTE, amount='100.00', fee='15.00', net='-115.00')

    def test_dispute_with_missing_or_conflicting_evidence_is_held(self):
        for changes, reason in [(dict(id='du_other'), 'missing_dispute_evidence'),
                                (dict(balance_transactions=[{'id': 'txn_elsewhere'}]), 'dispute_reference_conflict'),
                                (dict(charge='ch_missing'), 'missing_dispute_charge_evidence')]:
            with self.subTest(reason):
                self.setUp(); self.dispute(**changes)
                self.assertHeld(self.plan(), reason, sid=DISPUTE, amount='100.00', fee='15.00', net='-115.00')

    def test_adjustment_without_a_dispute_stays_unsupported(self):
        self.raw['balanceTransactions'][0]['type'] = 'adjustment'
        plan = self.plan()
        self.assertEqual([dict(source_id=SALE, reason='unsupported_balance_type')], plan['unresolved'])
        self.assertEqual([], plan['uncategorized'])

    # Writing: the account opens once, in the same write as its first entry, and the entry carries the reason.

    def test_apply_opens_the_holding_account_once_and_flags_the_entry(self):
        self.assertNotIn(HOLDING, {item.account for item in self.store.load_opens()})
        self.raw['charges'] = []
        self.stage(); result = self.apply()
        self.assertEqual(1, result['posted'])
        self.assertEqual([dict(source_id=SALE, reason='missing_charge_evidence')], result['uncategorized'])
        opens = {item.account: item for item in self.store.load_opens()}
        self.assertEqual(opens[base.CLEARING].date, opens[HOLDING].date)
        self.assertEqual({base.CLEARING: Decimal('97'), HOLDING: Decimal('-100'), base.FEES: Decimal('3')}, self.balances())
        (entry,) = self.store.load_entries()
        self.assertEqual('missing_charge_evidence', dict(entry.meta)['stripe-review'])
        # A second run changes nothing; a later held sale reuses the account.
        before = self.files()
        self.assertEqual(0, self.apply()['posted'])
        self.assertEqual(before, self.files())
        self.raw['balanceTransactions'].append(dict(self.raw['balanceTransactions'][0], id='txn_second', source='ch_second'))
        self.stage(); self.assertEqual(1, self.apply()['posted'])
        self.assertEqual(Decimal('-200'), self.balances()[HOLDING])
        self.assertEqual(1, sum(item.account == HOLDING for item in self.store.load_opens()))

    def test_a_plan_that_does_not_need_the_holding_account_does_not_open_it(self):
        self.stage(); self.apply()
        self.assertNotIn(HOLDING, {item.account for item in self.store.load_opens()})
        self.assertNotIn('stripe-review', dict(self.store.load_entries()[0].meta))

    def test_holding_account_cannot_be_a_policy_account_and_is_engine_owned(self):
        from bookkeeping.ledger.model import Open
        from datetime import date
        with self.store.transaction() as conn:
            self.store.insert_opens([Open(date(2000, 1, 1), HOLDING, ('USD',))], conn)
        for policy in (dict(self.policy, revenue_rules=[dict(self.policy['revenue_rules'][0], category=HOLDING)]),
                       dict(self.policy, refund_account=HOLDING)):
            with self.assertRaises(ValueError):
                stripe_plan(self.entity, self.raw, policy)
        self.assertIn(HOLDING, engine_owned_accounts())

    def test_changed_resolution_refuses_an_old_approved_plan(self):
        self.invoice(); self.stage()
        approved = self.plan()['plan_digest']
        self.raw['paymentIntents'] = [{'id': 'pi_one', 'invoice': 'in_two'}]
        self.raw['charges'][0]['payment_intent'] = 'pi_one'
        before = self.files()
        with self.assertRaisesRegex(ValueError, 'plan changed'):
            stripe_apply(self.entity, self.raw, self.policy, expected_plan_digest=approved)
        self.assertEqual(before, self.files())


class ReviewFindingTests(StripeFixture):
    """One test for each finding of the PR 177 review (synthetic data)."""

    def payment(self, identity, invoice, **changes):
        return dict(dict(id=identity, payment={'type': 'payment_intent', 'payment_intent': 'pi_one'}, invoice=invoice,
                         amount_paid=10000, amount_requested=10000, currency='usd', livemode=False, status='paid'), **changes)

    def test_two_checkout_sessions_naming_one_invoice_hold_the_sale_and_its_refund(self):
        self.full_flow(); self.invoice_without_charge_reference()
        self.raw['checkoutSessions'] = [dict(id=name, payment_intent='pi_one', invoice='in_one') for name in ('cs_a', 'cs_b')]
        plan = self.plan()
        self.assertHeld(plan, 'multiple_checkout_sessions')
        self.assertHeld(plan, 'multiple_checkout_sessions', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_refund_uses_the_sales_expanded_evidence_in_the_same_plan(self):
        self.full_flow()
        self.raw['balanceTransactions'][0]['source'] = dict(self.raw['charges'][0], invoice='in_missing')
        plan = self.plan()
        self.assertHeld(plan, 'missing_invoice_evidence')
        self.assertHeld(plan, 'missing_invoice_evidence', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_later_refund_waits_while_its_sale_stays_in_the_holding_account(self):
        charge = self.raw['charges'].pop()
        self.stage(); self.apply()
        self.raw['charges'] = [charge]
        self.full_flow()
        self.raw['balanceTransactions'] = [row for row in self.raw['balanceTransactions'] if row['id'] != 'txn_sale']
        self.assertHeld(self.plan(), 'sale_in_holding', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_dispute_on_a_held_sale_carries_the_sales_reason(self):
        charge = self.raw['charges'].pop()
        self.stage(); self.apply()
        self.raw['charges'] = [charge]
        self.raw['balanceTransactions'] = []
        self.dispute()
        self.assertHeld(self.plan(), 'sale_in_holding', sid=DISPUTE, amount='100.00', fee='15.00', net='-115.00')

    def test_tax_on_one_invoice_copy_is_not_lost_when_the_other_copy_omits_it(self):
        self.invoice()
        self.raw['charges'][0]['invoice'] = dict(copy.deepcopy(self.raw['invoices'][0]), total_taxes=[{'amount': 100}])
        plan = self.plan()
        self.assertEqual([], plan['actions'])
        self.assertEqual('tax_discount_or_credit_requires_review', plan['unresolved'][0]['reason'])

    def test_complete_inline_invoice_combines_with_an_id_only_captured_invoice(self):
        self.invoice()
        self.raw['charges'][0]['invoice'] = self.raw['invoices'][0]
        self.raw['invoices'] = [{'id': 'in_one'}]
        self.assertRevenue(self.plan())

    def test_first_page_of_lines_combines_with_the_complete_invoice(self):
        self.invoice()
        inline = copy.deepcopy(self.raw['invoices'][0])
        inline['lines'] = dict(has_more=True, data=inline['lines']['data'][:1])
        self.raw['charges'][0]['invoice'] = inline
        self.assertRevenue(self.plan())

    def test_invoice_copies_with_different_line_amounts_are_held(self):
        self.invoice()
        inline = copy.deepcopy(self.raw['invoices'][0])
        inline['lines']['data'][0]['amount'] = 5000
        self.raw['charges'][0]['invoice'] = inline
        self.assertHeld(self.plan(), 'invoice_reference_conflict')

    def test_named_payment_intent_without_evidence_is_held_unless_an_invoice_names_the_sale(self):
        self.raw['charges'][0]['payment_intent'] = 'pi_missing'
        self.assertHeld(self.plan(), 'missing_payment_intent_evidence')
        self.raw['paymentIntents'] = [{'id': 'pi_missing'}]
        self.assertEqual({base.CLEARING: '97.00', base.INCOME: '-100.00', base.FEES: '3.00'}, self.postings(self.plan()))
        self.setUp(); self.invoice(); self.raw['charges'][0]['payment_intent'] = 'pi_missing'
        self.assertRevenue(self.plan())

    def test_expanded_intent_on_a_matched_session_counts(self):
        self.invoice_without_charge_reference()
        self.raw['checkoutSessions'] = [dict(id='cs_a', invoice='in_one', payment_intent=dict(id='pi_one', invoice='in_other'))]
        self.assertHeld(self.plan(), 'invoice_reference_conflict')

    def test_expanded_intent_on_an_invoice_payment_counts(self):
        self.invoice_without_charge_reference()
        self.raw['invoicePayments'] = [self.payment('inpay', 'in_one', payment={
            'type': 'payment_intent', 'payment_intent': dict(id='pi_one', invoice='in_other')})]
        self.assertHeld(self.plan(), 'invoice_reference_conflict')

    def test_documents_from_the_other_mode_are_held(self):
        for name, change in [
                ('intent', lambda raw: raw.update(paymentIntents=[dict(id='pi_one', invoice='in_one', livemode=True)])),
                ('invoice', lambda raw: raw.update(paymentIntents=[dict(id='pi_one', invoice='in_one')]) or raw['invoices'][0].update(livemode=True)),
                ('session', lambda raw: raw.update(checkoutSessions=[dict(id='cs', payment_intent='pi_one', invoice='in_one', livemode=True)])),
                ('charge', lambda raw: raw.update(paymentIntents=[dict(id='pi_one', invoice='in_one')]) or raw['charges'][0].update(livemode=True))]:
            with self.subTest(name):
                self.setUp(); self.invoice_without_charge_reference(); change(self.raw)
                self.assertHeld(self.plan(), 'livemode_conflict')

    def test_refund_naming_another_payment_intent_than_its_charge_is_held(self):
        self.full_flow()
        self.raw['charges'][0]['payment_intent'] = dict(id='pi_actual')
        self.raw['refunds'][0]['payment_intent'] = 'pi_different'
        plan = self.plan()
        self.assertHeld(plan, 'refund_reference_conflict', sid=REFUND, amount='20.00', fee=None, net='-20.00')
        self.raw['refunds'][0]['payment_intent'] = 'pi_actual'
        self.assertEqual({base.CLEARING: '-20.00', base.REFUNDS: '20.00'}, self.postings(self.plan(), REFUND))

    def test_capture_digest_covers_the_captured_intent_and_changed_evidence_needs_review(self):
        from unittest.mock import patch
        from bookkeeping import stripe
        intent = dict(id='pi', metadata={'key': 'value'})
        self.raw['charges'][0]['payment_intent'] = intent
        self.raw['paymentIntents'] = [copy.deepcopy(intent)]
        before = self.plan()['source_capture_digests'][SALE]
        self.raw['paymentIntents'][0]['metadata']['key'] = 'other'
        self.assertNotEqual(before, self.plan()['source_capture_digests'][SALE])
        self.raw['paymentIntents'][0]['metadata']['key'] = 'value'
        self.stage()
        with patch.object(stripe.StagingStore, 'bulk_mark_seen', side_effect=OSError), self.assertRaises(OSError):
            self.apply()
        self.raw['paymentIntents'][0]['metadata']['key'] = 'other'
        self.assertEqual(['cleanup_needs_review'], [row['reason'] for row in self.apply()['needs_review']])

    def test_expanded_charge_with_a_captured_stub_posts_as_on_main(self):
        self.raw['balanceTransactions'][0]['source'] = copy.deepcopy(self.raw['charges'][0])
        self.raw['charges'] = [dict(id='ch_sale', invoice=None)]
        self.assertEqual({base.CLEARING: '97.00', base.INCOME: '-100.00', base.FEES: '3.00'}, self.postings(self.plan()))

    def test_charge_naming_another_balance_transaction_is_held(self):
        self.raw['charges'][0]['balance_transaction'] = 'txn_other'
        self.assertHeld(self.plan(), 'charge_reference_conflict')

    def test_unfetched_refund_is_held_and_a_failed_refund_stays_unposted(self):
        self.full_flow(); self.raw['refunds'] = []
        self.assertHeld(self.plan(), 'missing_refund_evidence', sid=REFUND, amount='20.00', fee=None, net='-20.00')
        self.setUp(); self.full_flow(); self.raw['refunds'][0]['status'] = 'failed'
        plan = self.plan()
        self.assertIsNone(self.action(plan, REFUND))
        self.assertIn(dict(source_id=REFUND, reason='missing_or_unsettled_refund'), plan['unresolved'])

    def test_invoice_payments_naming_different_invoices_are_held(self):
        self.invoice_without_charge_reference()
        self.raw['invoicePayments'] = [self.payment('inpay_a', 'in_one'), self.payment('inpay_b', 'in_two')]
        self.assertHeld(self.plan(), 'invoice_reference_conflict')
        self.raw['invoicePayments'] = [self.payment('inpay_a', 'in_one'), self.payment('inpay_b', 'in_one')]
        plan = self.plan()
        self.assertEqual([], plan['actions'])
        self.assertEqual('partial_or_multiple_invoice_payments', plan['unresolved'][0]['reason'])


class ReclassificationTests(StripeFixture):
    """A held sale or refund moves out of the holding account once, by its own entry, when evidence proves it."""

    RECLASS = 'stripe-reclassify:acct_test:txn_sale'
    REFUND_RECLASS = 'stripe-reclassify:acct_test:txn_refund'

    def test_sale_moves_out_of_the_holding_account_once_new_evidence_proves_it(self):
        from bookkeeping.ledger.importer import check_integrity
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        plan = self.plan()
        (action,) = plan['actions']
        self.assertEqual(dict(source_id=self.RECLASS, kind='reclassification', reclassifies=SALE),
                         {key: action[key] for key in ('source_id', 'kind', 'reclassifies')})
        self.assertEqual('missing_charge_evidence', action['cleared_review_reason'])
        self.assertEqual([dict(source_id=SALE, reclassification_id=self.RECLASS)], plan['reclassified'])
        result = self.apply()
        self.assertEqual((1, [dict(source_id=SALE, reclassification_id=self.RECLASS)]), (result['posted'], result['reclassified']))
        self.assertEqual({base.CLEARING: Decimal('97'), HOLDING: Decimal('0'), base.FEES: Decimal('3'),
                          base.INCOME: Decimal('-100')}, self.balances())
        moved = self.entry(self.RECLASS)
        self.assertEqual(SALE, dict(moved.meta)['stripe-reclassifies'])
        self.assertEqual(self.original.date, moved.date)
        self.assertEqual(self.original, self.entry(SALE))  # The original posting is never changed.
        self.assertEqual('ok', check_integrity(self.entity).status)
        # Apply again and re-import: nothing posts twice.
        before = self.files()
        self.assertEqual(0, self.apply()['posted'])
        self.assertEqual(before, self.files())
        self.stage(); self.assertEqual(0, self.apply()['posted'])
        self.assertEqual(2, len(self.store.load_entries()))

    def test_nothing_moves_while_the_evidence_is_still_uncertain(self):
        self.hold_sale()
        self.raw['charges'] = [dict(self.charge, payment_intent='pi_missing')]
        plan = self.plan()
        self.assertEqual(([], []), (plan['actions'], plan['reclassified']))

    def test_interrupted_apply_never_posts_the_reclassification_twice(self):
        from unittest.mock import patch
        from bookkeeping import stripe
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        before = self.files()
        with patch.object(stripe, '_atomic_ledger_write_unlocked', side_effect=OSError), self.assertRaises(OSError):
            self.apply()
        self.assertEqual(before, self.files())
        self.full_flow(); self.stage()
        with patch.object(stripe.StagingStore, 'bulk_mark_seen', side_effect=OSError), self.assertRaises(OSError):
            self.apply()
        self.assertEqual(1, sum(entry.source_id == self.RECLASS for entry in self.store.load_entries()))
        result = self.apply()
        self.assertEqual((0, []), (result['posted'], result['needs_review']))
        self.assertEqual(1, sum(entry.source_id == self.RECLASS for entry in self.store.load_entries()))
        self.assertEqual(Decimal('0'), self.balances()[HOLDING])

    def test_refund_held_for_its_sale_moves_once_the_sale_has_moved(self):
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        self.full_flow()
        sale_bt = self.raw['balanceTransactions'].pop(0)
        self.stage(); self.apply()
        self.assertEqual('sale_in_holding', dict(self.entry(REFUND).meta)['stripe-review'])
        self.raw['balanceTransactions'].insert(0, sale_bt)
        plan = self.plan()
        self.assertEqual({self.RECLASS, self.REFUND_RECLASS}, {row['source_id'] for row in plan['actions']})
        self.apply()
        self.assertEqual({base.CLEARING: Decimal('0'), HOLDING: Decimal('0'), base.FEES: Decimal('3'),
                          base.INCOME: Decimal('-100'), base.REFUNDS: Decimal('20'), base.TRANSIT: Decimal('77')},
                         self.balances())
        self.assertEqual(0, self.apply()['posted'])

    def test_held_refund_does_not_move_while_its_sale_stays_held(self):
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        self.full_flow()
        self.raw['balanceTransactions'].pop(0)
        self.stage(); self.apply()
        self.assertEqual([], self.plan()['actions'])

    def test_reclassification_of_a_closed_period_posts_on_the_first_open_day(self):
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        self.stripe_apply_closed('2026-01-31')
        self.assertEqual('2026-02-01', self.entry(self.RECLASS).date.isoformat())

    def stripe_apply_closed(self, closed):
        plan = stripe_plan(self.entity, self.raw, self.policy, closed_through=closed)
        stripe_apply(self.entity, self.raw, self.policy, expected_plan_digest=plan['plan_digest'], closed_through=closed)

    def test_disputes_never_move_out_of_the_holding_account(self):
        self.raw['balanceTransactions'] = []; self.dispute()
        self.stage(); self.apply()
        self.assertEqual([], self.plan()['actions'])


class MainCompatibilityTests(StripeFixture):
    """Sales that posted on main post the same way: every plan field but plan_digest matches main's golden plan
    (tests/fixtures/stripe/main_baselines.json, captured from main with the same synthetic shapes)."""

    GOLDEN = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'stripe' / 'main_baselines.json').read_text())

    def shape(self, name):
        if name == 'full':
            self.full_flow()
        if name in ('invoice', 'invoice_payment'):
            self.invoice()
        if name == 'invoice_payment':
            self.invoice_without_charge_reference()
            self.raw['invoicePayments'] = [dict(id='inpay', payment=dict(type='payment_intent', payment_intent='pi_one'),
                invoice='in_one', amount_paid=10000, amount_requested=10000, currency='usd', livemode=False, status='paid')]
        if name == 'expanded':
            self.raw['balanceTransactions'][0]['source'] = copy.deepcopy(self.raw['charges'][0])
        if name == 'checkout':
            self.raw['charges'][0]['payment_intent'] = 'pi_one'
            self.raw['checkoutSessions'] = [dict(id='cs', payment_intent='pi_one', status='complete', payment_status='paid',
                currency='usd', amount_total=10000, amount_subtotal=10000, line_items=dict(has_more=False,
                data=[dict(id='li', amount_total=10000, amount_subtotal=10000, currency='usd', price='price_one')]))]
            self.policy['revenue_rules'] = [dict(id='one', match={'price_id': 'price_one'}, category=base.INCOME)]

    def test_main_shapes_keep_every_plan_field_and_capture_digest(self):
        for name, golden in self.GOLDEN.items():
            with self.subTest(name):
                self.setUp(); self.shape(name)
                plan = self.plan()
                self.assertTrue(golden['actions'])
                self.assertEqual(golden, {key: plan[key] for key in golden})
                self.assertEqual(([], []), (plan['uncategorized'], plan['reclassified']))


class RecheckFixture(StripeFixture):
    """Helpers shared by the recheck tests; it has no tests of its own."""

    RECLASS = 'stripe-reclassify:acct_test:txn_sale'

    def actions(self, plan=None):
        plan = plan or self.plan()
        return [(row['source_id'], row['authority_id'], row.get('review_reason')) for row in plan['actions']], plan['unresolved']

    def held_refund_with_restored_charge(self):
        """The sale sits in holding; its refund was posted later, held with sale_in_holding. Returns the sale movement."""
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        self.full_flow()
        movement = self.raw['balanceTransactions'].pop(0)
        self.stage(); self.apply()
        self.assertEqual('sale_in_holding', dict(self.entry(REFUND).meta)['stripe-review'])
        return movement

    def interrupted_apply(self):
        from unittest.mock import patch
        from bookkeeping import stripe
        with patch.object(stripe.StagingStore, 'bulk_mark_seen', side_effect=OSError), self.assertRaises(OSError):
            self.apply()


class RecheckOneFindingTests(RecheckFixture):
    """One test for each finding of the PR 177 recheck (reproducers from its review; synthetic data)."""

    # Refunds leave holding only behind a sale move the batch accepts, linked by a verified movement.

    def test_rejected_sale_move_does_not_release_its_held_refund(self):
        movement = self.held_refund_with_restored_charge()
        movement['created'] += 86400
        self.raw['balanceTransactions'].append(movement)
        plan = self.plan()
        self.assertEqual(([], [dict(source_id=SALE, reason='changed_source')]), self.actions(plan))
        self.assertEqual([], plan['reclassified'])
        self.apply()
        self.assertEqual(Decimal('-80'), self.balances()[HOLDING])
        self.assertNotIn(base.REFUNDS, self.balances())

    def test_refund_without_a_verified_sale_movement_stays_in_holding(self):
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        self.full_flow()
        self.raw['balanceTransactions'].pop(0)
        del self.charge['balance_transaction']
        plan = self.stage()
        self.assertHeld(plan, 'sale_movement_unverified', sid=REFUND, amount='20.00', fee=None, net='-20.00')
        self.apply()
        self.assertEqual(Decimal('-80'), self.balances()[HOLDING])
        self.assertNotIn(base.REFUNDS, self.balances())

    def test_refund_merges_its_own_expanded_payment_intent(self):
        self.full_flow()
        self.raw['charges'][0]['payment_intent'] = dict(id='pi_one')
        self.raw['refunds'][0]['payment_intent'] = dict(id='pi_one', invoice='in_missing')
        plan = self.plan()
        self.assertHeld(plan, 'missing_invoice_evidence', sid=REFUND, amount='20.00', fee=None, net='-20.00')
        # Round 3: the refund's copy of pi_one is a known copy of the sale's intent too, so the sale holds as well.
        self.assertHeld(plan, 'missing_invoice_evidence')

    def test_refund_finds_a_charge_expanded_only_on_the_sale_movement(self):
        self.full_flow()
        self.raw['balanceTransactions'][0]['source'] = self.raw['charges'].pop()
        self.stage(); self.apply()
        self.assertEqual({base.CLEARING: Decimal('0'), base.INCOME: Decimal('-100'), base.FEES: Decimal('3'),
                          base.REFUNDS: Decimal('20'), base.TRANSIT: Decimal('77')}, self.balances())
        self.assertEqual(([], []), self.actions())

    def test_charge_expanded_only_on_a_refund_resolves_the_sale_and_the_refund(self):
        # Every known copy of a charge counts: the refund's expanded charge resolves its sale too.
        self.full_flow()
        charge = self.raw['charges'].pop()
        refund_bt = next(bt for bt in self.raw['balanceTransactions'] if bt['id'] == 'txn_refund')
        refund_bt['source'] = dict(self.raw['refunds'][0], charge=charge)
        plan = self.plan()
        self.assertEqual({base.CLEARING: '97.00', base.INCOME: '-100.00', base.FEES: '3.00'}, self.postings(plan))
        self.assertEqual({base.CLEARING: '-20.00', base.REFUNDS: '20.00'}, self.postings(plan, REFUND))

    def test_sale_and_refund_of_a_missing_invoice_both_hold(self):
        self.full_flow()
        self.raw['charges'][0]['invoice'] = 'in_missing'
        plan = self.plan()
        self.assertHeld(plan, 'missing_invoice_evidence')
        self.assertHeld(plan, 'missing_invoice_evidence', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    # A move needs the same authority as a fresh posting.

    def test_move_above_the_ask_first_limit_waits_for_a_person(self):
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        self.entity.trust_policy['ask_first_above'] = '50.00'
        result = self.apply()
        self.assertEqual((0, []), (result['posted'], result['reclassified']))
        self.assertEqual(Decimal('-100'), self.balances()[HOLDING])
        self.entity.trust_policy['ask_first_above'] = '500.00'
        self.assertEqual([dict(source_id=SALE, reclassification_id=self.RECLASS)], self.apply()['reclassified'])

    def test_move_waits_while_the_account_needs_accrual_treatment(self):
        from unittest.mock import patch
        from bookkeeping import stripe
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        with patch('bookkeeping.accrual.treatment.treatment_checker', return_value=lambda row: True):
            self.assertEqual([], stripe.stripe_plan(self.entity, self.raw, self.policy)['reclassified'])

    # The cleanup receipt binds exactly what the resolver read.

    def test_receipt_binds_the_payment_intent_of_an_id_only_expanded_charge(self):
        self.raw['balanceTransactions'][0]['source'] = {'id': 'ch_sale'}
        self.raw['charges'][0]['payment_intent'] = 'pi_one'
        self.raw['paymentIntents'] = [dict(id='pi_one', metadata={})]
        self.stage()
        before = self.plan()['source_capture_digests'][SALE]
        self.interrupted_apply()
        self.raw['paymentIntents'][0]['invoice'] = 'in_missing'
        self.assertNotEqual(before, self.plan()['source_capture_digests'][SALE])
        self.assertEqual(['cleanup_needs_review'], [row['reason'] for row in self.apply()['needs_review']])

    def test_receipt_binds_competing_sale_movements(self):
        first = self.raw['balanceTransactions'][0]
        before = self.plan()['source_capture_digests'][SALE]
        self.stage()
        self.interrupted_apply()
        self.raw['balanceTransactions'].append(dict(first, id='txn_duplicate'))
        self.assertNotEqual(before, self.plan()['source_capture_digests'][SALE])
        self.stage()
        self.assertEqual(['cleanup_needs_review'], [row['reason'] for row in self.apply()['needs_review']])

    def test_unchanged_evidence_recovers_after_an_interrupted_apply(self):
        self.raw['balanceTransactions'][0]['source'] = {'id': 'ch_sale'}
        self.raw['charges'][0]['payment_intent'] = 'pi_one'
        self.raw['paymentIntents'] = [dict(id='pi_one', metadata={})]
        self.stage()
        self.interrupted_apply()
        self.assertEqual([], self.apply()['needs_review'])

    # Every invoice payment shape counts, and an invoice belongs to one charge.

    def test_charge_type_invoice_payment_names_the_invoice(self):
        self.raw['invoicePayments'] = [dict(id='inpay_one', payment=dict(type='charge', charge='ch_sale'),
            invoice='in_missing', amount_paid=10000, amount_requested=10000, currency='usd', livemode=False, status='paid')]
        self.assertHeld(self.plan(), 'missing_invoice_evidence')

    def test_charge_type_invoice_payment_resolves_the_exact_invoice(self):
        self.invoice(); self.raw['charges'][0].pop('invoice')
        self.raw['invoicePayments'] = [dict(id='inpay_one', payment=dict(type='charge', charge='ch_sale'),
            invoice='in_one', amount_paid=10000, amount_requested=10000, currency='usd', livemode=False, status='paid')]
        self.assertRevenue(self.plan())
        self.raw['invoicePayments'].append(dict(self.raw['invoicePayments'][0], id='inpay_two',
            payment=dict(type='payment_intent', payment_intent='pi_one')))
        self.raw['charges'][0]['payment_intent'] = 'pi_one'
        self.assertEqual('partial_or_multiple_invoice_payments', self.plan()['unresolved'][0]['reason'])

    def test_payment_intent_whose_latest_charge_is_another_charge_is_held(self):
        self.raw['charges'][0]['payment_intent'] = dict(id='pi_one', latest_charge='ch_other')
        self.assertHeld(self.plan(), 'payment_intent_reference_conflict')
        self.raw['charges'][0]['payment_intent'] = dict(id='pi_one', latest_charge='ch_sale')
        self.assertEqual({base.CLEARING: '97.00', base.INCOME: '-100.00', base.FEES: '3.00'}, self.postings(self.plan()))

    def test_invoice_naming_another_charge_or_payment_intent_is_held(self):
        for field, value in (('charge', 'ch_different'), ('payment_intent', 'pi_different')):
            with self.subTest(field):
                self.setUp(); self.invoice()
                self.raw['invoices'][0][field] = value
                self.assertHeld(self.plan(), 'invoice_reference_conflict')
        self.setUp(); self.invoice(); self.raw['invoices'][0]['charge'] = 'ch_sale'
        self.assertRevenue(self.plan())

    def test_invoice_in_another_currency_is_held(self):
        self.invoice()
        self.raw['invoices'][0]['currency'] = 'eur'
        self.assertHeld(self.plan(), 'invoice_currency_conflict')

    def test_two_charges_claiming_one_invoice_never_post_it_twice(self):
        self.invoice()
        self.raw['balanceTransactions'].append(dict(self.raw['balanceTransactions'][0], id='txn_extra', source='ch_extra'))
        self.raw['charges'].append(dict(self.raw['charges'][0], id='ch_extra', balance_transaction='txn_extra'))
        self.stage(); self.apply()
        self.assertEqual({base.CLEARING: Decimal('194'), HOLDING: Decimal('-200'), base.FEES: Decimal('6')}, self.balances())
        (entry,) = [entry for entry in self.store.load_entries() if entry.source_id == SALE]
        self.assertEqual('invoice_claimed_by_another_payment', dict(entry.meta)['stripe-review'])

    def test_failed_charge_naming_the_invoice_does_not_compete(self):
        self.invoice()
        self.raw['charges'].append(dict(self.raw['charges'][0], id='ch_failed', status='failed', paid=False,
                                        balance_transaction=None))
        self.assertRevenue(self.plan())

    def test_subscription_checkout_session_without_a_payment_intent_does_not_compete(self):
        self.invoice()
        self.raw['checkoutSessions'] = [dict(id='cs_sub', mode='subscription', invoice='in_one', payment_intent=None,
                                             status='complete', payment_status='paid')]
        self.assertRevenue(self.plan())

    def test_paid_invoice_payment_for_another_intent_competes(self):
        self.invoice()
        self.raw['invoicePayments'] = [dict(id='inpay_x', payment=dict(type='payment_intent', payment_intent='pi_other'),
            invoice='in_one', amount_paid=10000, amount_requested=10000, currency='usd', livemode=False, status='paid')]
        self.assertHeld(self.plan(), 'invoice_claimed_by_another_payment')

    # Allocation reads the same merged objects that resolution accepted.

    def test_allocation_uses_the_payment_intent_expanded_on_checkout(self):
        self.invoice()
        self.raw['charges'][0]['payment_intent'] = 'pi_one'
        self.raw['checkoutSessions'] = [dict(id='cs', invoice='in_one', payment_intent=dict(id='pi_one', metadata={'kind': 'plan'}))]
        self.policy['revenue_rules'].append(dict(id='intent_rule', match={'metadata': {
            'object': 'payment_intent', 'key': 'kind', 'value': 'plan'}}, category='Income:Subscriptions'))
        expected = ([], [dict(source_id=SALE, reason='ambiguous_revenue_mapping')])
        self.assertEqual(expected, self.actions())
        self.raw['paymentIntents'] = [self.raw['checkoutSessions'][0]['payment_intent']]
        self.assertEqual(expected, self.actions())

    def test_line_price_and_captured_price_naming_different_products_hold(self):
        self.invoice()
        self.raw['prices'] = [dict(id='price_two', product='prod_other')]
        self.assertHeld(self.plan(), 'price_reference_conflict')
        self.raw['prices'] = [dict(id='price_two', product='prod_two')]
        self.assertRevenue(self.plan())

    def test_line_price_references_that_disagree_hold(self):
        self.invoice()
        self.raw['invoices'][0]['lines']['data'][1]['price_id'] = 'price_elsewhere'
        self.assertHeld(self.plan(), 'price_reference_conflict')
        self.setUp(); self.invoice()
        self.raw['invoices'][0]['lines']['data'][1]['product_id'] = 'prod_elsewhere'
        self.assertHeld(self.plan(), 'product_reference_conflict')

    def test_captured_product_metadata_conflict_holds(self):
        self.invoice()
        line = self.raw['invoices'][0]['lines']['data'][1]
        line['price']['product'] = dict(id='prod_two', metadata={'tier': 'a'})
        self.raw['products'] = [dict(id='prod_two', metadata={'tier': 'b'})]
        self.assertHeld(self.plan(), 'product_reference_conflict')

    # Copies of an invoice may word a line differently; amounts, tax and selectors stay strict.

    def test_line_description_difference_keeps_mains_allocation(self):
        self.invoice()
        captured = self.raw['invoices'][0]
        captured['lines']['data'][0]['description'] = 'revised'
        inline = copy.deepcopy(captured)
        inline['lines']['data'][0]['description'] = 'initial'
        self.raw['charges'][0]['invoice'] = inline
        self.assertRevenue(self.plan())
        for field, value in (('amount', 5000), ('price', {'id': 'price_other'})):
            with self.subTest(field):
                changed = copy.deepcopy(inline)
                changed['lines']['data'][1][field] = value
                self.raw['charges'][0]['invoice'] = changed
                self.assertHeld(self.plan(), 'invoice_reference_conflict')
        changed = copy.deepcopy(inline)  # Tax on one copy is kept, so the sale waits for tax review.
        changed['lines']['data'][1]['tax_amounts'] = [{'amount': 1}]
        self.raw['charges'][0]['invoice'] = changed
        plan = self.plan()
        self.assertEqual([], plan['actions'])
        self.assertEqual('tax_discount_or_credit_requires_review', plan['unresolved'][0]['reason'])

    # An original with a live move cannot be reversed or corrected on its own.

    def test_original_with_a_live_move_refuses_reversal_and_correction(self):
        from bookkeeping.ledger.importer import reverse_and_correct, reverse_entry
        from bookkeeping.ledger.model import Entry, Posting
        self.hold_sale(); self.raw['charges'] = [self.charge]; self.apply()
        before = self.files()
        with self.assertRaisesRegex(ValueError, 'Reverse stripe-reclassify:acct_test:txn_sale first'):
            reverse_entry(self.entity, SALE, 'test')
        corrected = Entry(date=self.original.date, narration='x', meta=(('source-id', 'fix-1'),),
                          postings=(Posting(base.CLEARING, Decimal('97'), 'USD'), Posting(base.INCOME, Decimal('-97'), 'USD')))
        with self.assertRaisesRegex(ValueError, 'Reverse stripe-reclassify'):
            reverse_and_correct(self.entity, SALE, corrected, 'test')
        self.assertEqual(before, self.files())
        # Reversing the move first, then the original, leaves nothing behind.
        reverse_entry(self.entity, self.RECLASS, 'test')
        reverse_entry(self.entity, SALE, 'test')
        self.assertEqual(set(), {account for account, amount in self.balances().items() if amount})
        self.assertEqual(([], []), self.actions())

    def test_rollback_and_retry_post_every_entry_once(self):
        from unittest.mock import patch
        self.hold_sale(); self.raw['charges'] = [self.charge]; self.full_flow(); self.stage()
        before = self.store.content_digest()
        with patch.object(type(self.store), 'insert_source_transactions', side_effect=OSError), self.assertRaises(OSError):
            self.apply()
        self.assertEqual(before, self.store.content_digest())
        self.assertEqual([self.original], self.store.load_entries())
        self.assertEqual((3, 0), (self.apply()['posted'], self.apply()['posted']))


class ReviewProbeTests(StripeFixture):
    """The round-one review probe not already covered by ReviewFindingTests (synthetic data)."""

    def test_later_cohort_refund_and_its_held_sale_leave_holding_together(self):
        self.hold_sale()
        self.raw['charges'].append(self.charge)
        self.full_flow(); self.stage()
        self.assertEqual(3, self.apply()['posted'])
        self.assertEqual({base.CLEARING: Decimal('0'), HOLDING: Decimal('0'), base.FEES: Decimal('3'),
                          base.INCOME: Decimal('-100'), base.REFUNDS: Decimal('20'), base.TRANSIT: Decimal('77')},
                         self.balances())
        self.assertEqual(0, self.apply()['posted'])
        self.assertEqual(1, sum(item.account == HOLDING for item in self.store.load_opens()))


class RecheckTwoFindingTests(RecheckFixture):
    """One test for each finding of PR 177 recheck 2 (its reproducers; synthetic data)."""

    def second_sale(self, charge_changes=None):
        charge = dict(self.raw['charges'][0], id='ch_second', balance_transaction='txn_second', **(charge_changes or {}))
        self.raw['charges'].append(charge)
        self.raw['balanceTransactions'].append(dict(self.raw['balanceTransactions'][0], id='txn_second', source='ch_second'))
        return charge

    # Finding 1 (C): an unfamiliar sale movement ID never releases a held refund.
    def test_rerouted_sale_link_does_not_release_a_held_refund(self):
        self.held_refund_with_restored_charge()
        self.raw['charges'][0]['balance_transaction'] = 'txn_unrelated'
        result = self.apply()
        self.assertEqual([], result['reclassified'])
        self.assertEqual(Decimal('-80'), self.balances()[HOLDING])
        self.assertNotIn(base.REFUNDS, self.balances())

    # Finding 2 (B): one payment intent, two charges; and separately applied cohorts.
    def test_two_charges_for_one_payment_intent_never_post_its_invoice_twice(self):
        self.invoice(); charge = self.raw['charges'][0]; charge.pop('invoice'); charge['payment_intent'] = 'pi_shared'
        self.raw['paymentIntents'] = [dict(id='pi_shared', invoice='in_one', status='succeeded')]
        self.second_sale()
        self.stage(); self.apply()
        self.assertEqual({base.CLEARING: Decimal('194'), HOLDING: Decimal('-200'), base.FEES: Decimal('6')}, self.balances())

    def test_invoice_claimed_in_an_earlier_cohort_holds_a_later_claimant(self):
        self.invoice(); self.stage(); self.apply()
        self.assertEqual('in_one', dict(self.entry(SALE).meta)['stripe-invoice'])
        charge = self.raw['charges'][0]; charge.update(id='ch_second', balance_transaction='txn_second')
        self.raw['balanceTransactions'][0].update(id='txn_second', source='ch_second')
        plan = self.stage()
        self.assertHeld(plan, 'invoice_claimed_by_another_payment', sid='stripe:acct_test:txn_second')
        self.apply()
        self.assertEqual(Decimal('-100'), self.balances()[HOLDING])
        self.assertEqual(Decimal('-60'), self.balances()[base.INCOME])

    # Finding 3 (A): an invoice that only names the charge back.
    def test_invoice_found_only_by_its_backlink_decides_the_sale(self):
        rules = copy.deepcopy(self.policy['revenue_rules'])
        self.invoice(); self.policy['revenue_rules'] = rules
        self.raw['charges'][0].pop('invoice')
        self.raw['invoices'][0].update(charge='ch_sale', total_taxes=[{'amount': 200}])
        self.assertEqual(([], [dict(source_id=SALE, reason='tax_discount_or_credit_requires_review')]), self.actions())

    # Finding 4 (A): the refund receipt binds the sale movement it looked up, or its absence.
    def test_refund_receipt_binds_the_looked_up_sale_movement(self):
        self.full_flow()
        sale = self.raw['balanceTransactions'].pop(0)
        self.raw['balanceTransactions'] = [row for row in self.raw['balanceTransactions'] if row['type'] == 'refund']
        self.stage()
        before = self.plan()['source_capture_digests'][REFUND]
        self.interrupted_apply()
        self.raw['balanceTransactions'].append(dict(sale, source='ch_different'))
        self.assertNotEqual(before, self.plan()['source_capture_digests'][REFUND])
        self.stage()
        self.assertEqual(['cleanup_needs_review'], [row['reason'] for row in self.apply()['needs_review']])

    # Finding 5 (D): an alias of a provider-qualified ID is refused.
    def test_alias_of_a_moved_original_is_refused(self):
        from bookkeeping.ledger.importer import reverse_entry
        self.hold_sale(); self.raw['charges'] = [self.charge]; self.apply()
        before = self.files()
        with self.assertRaisesRegex(ValueError, 'another spelling'):
            reverse_entry(self.entity, 'banksync:' + SALE, 'test')
        self.assertEqual(before, self.files())

    # Finding 6 (D): a move cannot be replaced, and a replaced move (older data) still protects its original.
    def test_move_cannot_be_corrected_and_its_lineage_protects_the_original(self):
        from unittest.mock import patch
        from bookkeeping.ledger import corrections
        from bookkeeping.ledger.importer import reverse_and_correct, reverse_entry
        from bookkeeping.ledger.model import Entry
        self.hold_sale(); self.raw['charges'] = [self.charge]; self.apply()
        move = self.entry(self.RECLASS)
        replacement = Entry(date=move.date, narration='Reviewed move', meta=(('source-id', 'reviewed-move'),), postings=move.postings)
        with self.assertRaisesRegex(ValueError, 'cannot be replaced'):
            reverse_and_correct(self.entity, self.RECLASS, replacement, 'test')
        with patch.object(corrections, '_refuse_entry_with_live_stripe_move'):  # As an older engine allowed.
            reverse_and_correct(self.entity, self.RECLASS, replacement, 'test')
        with self.assertRaisesRegex(ValueError, 'Reverse reviewed-move first'):
            reverse_entry(self.entity, SALE, 'test')
        reverse_entry(self.entity, 'reviewed-move', 'test'); reverse_entry(self.entity, SALE, 'test')
        self.assertEqual(set(), {account for account, amount in self.balances().items() if amount})

    # Finding 7 (A): explicit currency on every relationship used.
    def test_checkout_session_in_another_currency_holds_the_sale(self):
        self.invoice(); charge = self.raw['charges'][0]; charge.pop('invoice'); charge['payment_intent'] = 'pi_one'
        self.raw['checkoutSessions'] = [dict(id='cs_one', payment_intent='pi_one', invoice='in_one', currency='eur',
                                             status='complete', payment_status='paid', amount_total=10000, amount_subtotal=10000)]
        self.assertHeld(self.plan(), 'checkout_session_currency_conflict')

    def test_invoice_payment_in_another_currency_holds_the_sale(self):
        self.invoice()
        self.raw['invoicePayments'] = [dict(id='inpay', payment=dict(type='charge', charge='ch_sale'), invoice='in_one',
            amount_paid=10000, amount_requested=10000, currency='eur', livemode=False, status='paid')]
        self.assertHeld(self.plan(), 'invoice_payment_currency_conflict')

    # Finding 8 (A): relations are collected after every copy of the charge and intent is known.
    def test_intent_named_only_by_the_refund_reaches_its_checkout_invoice(self):
        self.full_flow()
        self.raw['refunds'][0]['payment_intent'] = 'pi_one'
        self.raw['checkoutSessions'] = [dict(id='cs_one', payment_intent='pi_one', invoice='in_missing')]
        plan = self.plan()
        self.assertHeld(plan, 'missing_invoice_evidence', sid=REFUND, amount='20.00', fee=None, net='-20.00')
        self.assertHeld(plan, 'missing_invoice_evidence')

    def test_charge_expanded_on_an_invoice_payment_is_a_copy_of_the_charge(self):
        self.raw['invoicePayments'] = [dict(id='inpay', invoice='in_one', status='paid', currency='usd', livemode=False,
            amount_paid=10000, amount_requested=10000, payment=dict(type='charge', charge=dict(id='ch_sale', invoice='in_other')))]
        self.invoice()
        self.assertHeld(self.plan(), 'charge_reference_conflict')

    # Finding 9 (A): every expanded product copy.
    def test_price_copies_expanding_one_product_with_different_metadata_hold(self):
        self.invoice()
        self.raw['invoices'][0]['lines']['data'][1]['price']['product'] = dict(id='prod_two', metadata={'plan': 'a'})
        self.raw['prices'] = [dict(id='price_two', product=dict(id='prod_two', metadata={'plan': 'b'}))]
        self.policy['revenue_rules'][1:] = [dict(id=key, match={'metadata': dict(object='product', key='plan', value=key)},
                                                 category=category) for key, category in [('a', base.INCOME), ('b', 'Income:Subscriptions')]]
        self.assertHeld(self.plan(), 'product_reference_conflict')

    # Finding 10 (E): a blocked move says why, in plan and apply, apart from new holds.
    def test_blocked_move_reports_its_reason(self):
        self.hold_sale(); self.raw['charges'] = [self.charge]
        self.entity.trust_policy['ask_first_above'] = '99.00'
        plan = self.plan()
        expected = [dict(source_id=SALE, reason='above_ask_first_amount')]
        self.assertEqual((expected, [], [], []), (plan['reclassification_blocked'], plan['reclassified'],
                                                  plan['uncategorized'], plan['unresolved']))
        result = self.apply()
        self.assertEqual((0, expected, 0), (result['posted'], result['reclassification_blocked'], result['held']))

    def test_open_receivable_blocks_a_move_with_accrual_treatment_required(self):
        if str(Path(__file__).resolve().parents[1]) not in sys.path:  # test_treatment_scope imports tests.*
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import test_treatment_scope as scope
        books = scope.AccrualFixtureBase(); books.setUp(); self.addCleanup(books.doCleanups)
        policy = self.policy
        for name in (policy['clearing_account'], policy['fee_account'], policy['refund_account'],
                     policy['payout_transit_account'], policy['revenue_rules'][0]['category']):
            scope.add_account(books.root, name, open_date='2000-01-01')
        entity = scope.set_config(books.root, accrual_tracking_start='2026-01-01',
                                  bank_account_mappings={'acct_test': policy['clearing_account'], 'checking': scope.BANK})
        raw = self.raw; charge = raw['charges'].pop()
        from bookkeeping import stripe
        plan = stripe.stripe_stage(entity, raw, policy)['plan']
        self.assertEqual(1, stripe.stripe_apply(entity, raw, policy, expected_plan_digest=plan['plan_digest'])['posted'])
        raw['charges'] = [charge]
        self.assertEqual(1, len(stripe.stripe_plan(entity, raw, policy)['reclassified']))
        request = dict(operation_id='inv', id='inv', kind='receivable', account=scope.AR, original_amount='100.00',
                       as_of='2026-01-01', counterparty='Customer', origin=dict(mode='new', source_id='inv-src',
                       counter_account=scope.INCOME), evidence_ids=[books.evidence('inv-ev')])
        scope.apply_item(entity, scope.preview_item(entity, request))
        plan = stripe.stripe_plan(entity, raw, policy)
        self.assertEqual([dict(source_id=SALE, reason='accrual_treatment_required')], plan['reclassification_blocked'])
        result = stripe.stripe_apply(entity, raw, policy, expected_plan_digest=plan['plan_digest'])
        self.assertEqual((0, plan['reclassification_blocked']), (result['posted'], result['reclassification_blocked']))


class AdversarialInvariantTests(RecheckFixture):
    """Variants of invariants A-D beyond the review's reproducers (synthetic data)."""

    def permutations(self):
        import itertools
        names = [name for name in ('charges', 'invoices', 'paymentIntents', 'checkoutSessions', 'invoicePayments',
                                   'refunds', 'balanceTransactions', 'prices') if len(self.raw.get(name) or []) > 1]
        original = copy.deepcopy(self.raw)
        for order in itertools.product((False, True), repeat=len(names)):
            raw = copy.deepcopy(original)
            for name, flip in zip(names, order):
                if flip:
                    raw[name].reverse()
            yield raw

    def assertOrderFree(self):
        keys = ('actions', 'unresolved', 'uncategorized', 'source_capture_digests', 'entry_links')
        results = {json.dumps({key: stripe_plan(self.entity, raw, self.policy)[key] for key in keys}, sort_keys=True)
                   for raw in self.permutations()}
        self.assertEqual(1, len(results))

    # A: order and direction of references never change the decision.

    def test_permuted_evidence_gives_one_plan(self):
        for build in (self.shared_intent, self.claimed_by_payment, self.backlink_invoice):
            with self.subTest(build.__name__):
                self.setUp(); build(); self.assertOrderFree()

    def shared_intent(self):
        self.invoice(); charge = self.raw['charges'][0]; charge.pop('invoice'); charge['payment_intent'] = 'pi_shared'
        self.raw['paymentIntents'] = [dict(id='pi_shared', invoice='in_one'), dict(id='pi_other')]
        self.raw['charges'].append(dict(charge, id='ch_second', balance_transaction='txn_second'))
        self.raw['balanceTransactions'].append(dict(self.raw['balanceTransactions'][0], id='txn_second', source='ch_second'))

    def claimed_by_payment(self):
        self.invoice(); self.full_flow()
        self.raw['invoicePayments'] = [dict(id='inpay_b', payment=dict(type='charge', charge='ch_other'), invoice='in_one',
            amount_paid=10000, amount_requested=10000, currency='usd', livemode=False, status='paid'),
            dict(id='inpay_a', payment=dict(type='charge', charge='ch_sale'), invoice='in_one',
            amount_paid=10000, amount_requested=10000, currency='usd', livemode=False, status='paid')]

    def backlink_invoice(self):
        self.invoice(); self.raw['charges'][0].pop('invoice')
        self.raw['invoices'][0]['charge'] = 'ch_sale'
        self.raw['invoices'].append(dict(copy.deepcopy(self.raw['invoices'][0]), id='in_unrelated', charge='ch_elsewhere'))

    def test_backlink_only_invoice_allocates_by_its_lines(self):
        self.backlink_invoice()
        self.assertRevenue(self.plan())

    def test_payment_intent_backlink_only_invoice_is_found(self):
        self.invoice(); self.raw['charges'][0].pop('invoice')
        self.raw['charges'][0]['payment_intent'] = 'pi_one'
        self.raw['paymentIntents'] = [dict(id='pi_one')]
        self.raw['invoices'][0]['payment_intent'] = 'pi_one'
        self.assertRevenue(self.plan())

    def test_intent_known_only_by_its_latest_charge_is_followed(self):
        self.invoice(); self.raw['charges'][0].pop('invoice')
        self.raw['paymentIntents'] = [dict(id='pi_one', latest_charge='ch_sale', invoice='in_missing')]
        self.assertHeld(self.plan(), 'missing_invoice_evidence')

    def test_two_backlink_invoices_for_one_charge_hold(self):
        self.backlink_invoice()
        self.raw['invoices'].append(dict(copy.deepcopy(self.raw['invoices'][0]), id='in_two', charge='ch_sale'))
        self.assertHeld(self.plan(), 'invoice_reference_conflict')

    def test_live_charge_on_the_same_intent_outside_the_cohort_holds_the_sale(self):
        self.invoice(); charge = self.raw['charges'][0]; charge.pop('invoice'); charge['payment_intent'] = 'pi_shared'
        self.raw['paymentIntents'] = [dict(id='pi_shared', invoice='in_one')]
        self.raw['charges'].append(dict(charge, id='ch_second', balance_transaction='txn_elsewhere'))
        self.assertHeld(self.plan(), 'invoice_claimed_by_another_payment')

    def test_failed_charge_on_the_same_intent_does_not_compete(self):
        self.invoice(); charge = self.raw['charges'][0]; charge['payment_intent'] = 'pi_one'
        self.raw['charges'].append(dict(charge, id='ch_failed', status='failed', paid=False, balance_transaction=None))
        self.assertRevenue(self.plan())

    def test_receipt_binds_a_sale_movement_that_disappears(self):
        self.full_flow(); self.stage()
        before = self.plan()['source_capture_digests'][REFUND]
        self.interrupted_apply()
        self.raw['balanceTransactions'] = [row for row in self.raw['balanceTransactions'] if row['id'] != 'txn_sale']
        self.assertNotEqual(before, self.plan()['source_capture_digests'][REFUND])

    def test_receipt_binds_a_new_backlink_invoice(self):
        self.stage()
        before = self.plan()['source_capture_digests'][SALE]
        self.interrupted_apply()
        self.raw['invoices'] = [dict(id='in_late', charge='ch_sale', status='paid')]
        self.assertNotEqual(before, self.plan()['source_capture_digests'][SALE])
        self.assertEqual(['cleanup_needs_review'], [row['reason'] for row in self.apply()['needs_review']])

    # B: durable claims across cohorts, order, reversal and retry.

    def test_durable_claim_holds_whichever_cohort_comes_second(self):
        self.invoice()
        first = copy.deepcopy(self.raw)
        self.raw['charges'][0].update(id='ch_second', balance_transaction='txn_second')
        self.raw['balanceTransactions'][0].update(id='txn_second', source='ch_second')
        self.stage(); self.apply()
        self.raw = first
        self.assertHeld(self.stage(), 'invoice_claimed_by_another_payment')

    def test_reversed_claim_frees_the_invoice(self):
        from bookkeeping.ledger.importer import reverse_entry
        self.invoice(); self.stage(); self.apply()
        reverse_entry(self.entity, SALE, 'test')
        self.raw['charges'][0].update(id='ch_second', balance_transaction='txn_second')
        self.raw['balanceTransactions'][0].update(id='txn_second', source='ch_second')
        self.assertRevenue(self.stage(), sid='stripe:acct_test:txn_second')

    def test_claim_survives_an_interrupted_apply_and_never_blocks_its_own_sale(self):
        self.invoice(); self.stage()
        self.interrupted_apply()
        result = self.apply()
        self.assertEqual(([], []), (result['needs_review'], result['uncategorized']))
        self.full_flow(); self.raw['invoices'][0]['lines']['data'] = [dict(self.raw['invoices'][0]['lines']['data'][0], amount=10000)]
        self.raw['invoices'][0]['lines']['data'][0].pop('pricing'); self.raw['invoices'][0]['lines']['data'][0]['price'] = 'price_one'
        self.assertEqual({base.CLEARING: '-20.00', base.REFUNDS: '20.00'}, self.postings(self.plan(), REFUND))

    def test_moved_sale_claims_its_invoice(self):
        self.invoice()
        self.hold_sale(); self.raw['charges'] = [self.charge]
        self.apply()
        self.assertEqual('in_one', dict(self.entry(self.RECLASS).meta)['stripe-invoice'])
        self.raw['charges'][0] = dict(self.charge, id='ch_second', balance_transaction='txn_second')
        self.raw['balanceTransactions'] = [dict(self.raw['balanceTransactions'][0], id='txn_second', source='ch_second')]
        self.assertHeld(self.stage(), 'invoice_claimed_by_another_payment', sid='stripe:acct_test:txn_second')

    # C: refund release needs ledger proof.

    def test_held_refund_with_its_link_removed_stays(self):
        self.held_refund_with_restored_charge()
        del self.raw['charges'][0]['balance_transaction']
        self.assertEqual([], self.plan()['reclassified'])

    def test_refund_linked_to_another_charges_posted_sale_stays(self):
        self.held_refund_with_restored_charge()
        self.raw['balanceTransactions'] = [row for row in self.raw['balanceTransactions'] if row['id'] != 'txn_sale']
        other = dict(self.raw['balanceTransactions'][0], id='txn_other', type='charge', amount=5000, fee=0, net=5000,
                     source='ch_other')
        self.raw['balanceTransactions'].append(other)
        self.raw['charges'].append(dict(self.charge, id='ch_other', amount=5000, balance_transaction='txn_other'))
        self.stage(); self.apply()
        self.raw['charges'][0]['balance_transaction'] = 'txn_other'
        self.raw['balanceTransactions'].remove(other)
        self.assertEqual([], self.plan()['reclassified'])

    def test_refund_waits_while_its_sales_move_is_blocked(self):
        self.held_refund_with_restored_charge()
        self.raw['balanceTransactions'].insert(0, dict(self.raw['balanceTransactions'][0], id='txn_sale', type='charge',
            source='ch_sale', amount=10000, fee=300, net=9700, created=1768435200))
        self.entity.trust_policy['ask_first_above'] = '99.00'
        plan = self.plan()
        self.assertEqual([], plan['reclassified'])
        self.assertIn(dict(source_id=SALE, reason='above_ask_first_amount'), plan['reclassification_blocked'])

    # D: lineage and canonical IDs.

    def test_correcting_the_original_while_its_move_lives_is_refused(self):
        from bookkeeping.ledger.importer import reverse_and_correct
        from bookkeeping.ledger.model import Entry, Posting
        self.hold_sale(); self.raw['charges'] = [self.charge]; self.apply()
        corrected = Entry(date=self.original.date, narration='x', meta=(('source-id', 'fix-1'),),
                          postings=(Posting(base.CLEARING, Decimal('97'), 'USD'), Posting(base.INCOME, Decimal('-97'), 'USD')))
        with self.assertRaisesRegex(ValueError, 'Reverse stripe-reclassify'):
            reverse_and_correct(self.entity, SALE, corrected, 'test')
        with self.assertRaisesRegex(ValueError, 'another spelling'):
            reverse_and_correct(self.entity, 'banksync:' + SALE, corrected, 'test')

    def test_unprefixed_bank_ids_keep_their_alias(self):
        from bookkeeping.ledger.corrections import _require_unmanaged_entry
        from bookkeeping.ledger.model import Entry, Posting
        from bookkeeping.ledger.importer import _atomic_ledger_write
        _atomic_ledger_write(self.entity, [], [Entry(date=self.original_date(), narration='bank', meta=(('source-id', 'abc'),),
            postings=(Posting(base.BANK, Decimal('1'), 'USD'), Posting(base.INCOME, Decimal('-1'), 'USD')))], 's', None, 'test')
        _require_unmanaged_entry(self.entity, 'banksync:abc')  # A BankSync row keeps both spellings.

    def original_date(self):
        from datetime import date
        return date(2026, 1, 15)


if __name__ == '__main__':
    unittest.main()
