"""Which invoice a Stripe charge belongs to, and the holding account for activity the engine cannot place.

Every shape Stripe uses to name a charge's invoice counts: charge.invoice, payment_intent.invoice, a modern
invoice payment, a Checkout session's invoice, and inline or expanded objects. References that disagree, an
invoice the engine does not have, or an expanded object without its own ID never post as revenue: the sale,
its refunds and its disputes post to "Uncategorized Stripe activity" with a review reason. All data is synthetic.
"""
from __future__ import annotations

import copy
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


class InvoiceResolutionTests(unittest.TestCase):
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

    def invoice_without_charge_reference(self):
        """The two-line invoice fixture, reachable only through the payment intent pi_one."""
        self.invoice()
        self.raw['charges'][0].pop('invoice')
        self.raw['charges'][0]['payment_intent'] = 'pi_one'

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

    def dispute(self, **changes):
        self.raw['balanceTransactions'].append({'id': 'txn_dispute', 'created': 1768521600, 'amount': -10000,
            'fee': 1500, 'net': -11500, 'currency': 'usd', 'status': 'available', 'type': 'adjustment', 'source': 'du_one'})
        self.raw['disputes'] = [dict({'id': 'du_one', 'object': 'dispute', 'charge': 'ch_sale', 'amount': 10000,
                                      'currency': 'usd', 'balance_transactions': [{'id': 'txn_dispute'}]}, **changes)]

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


if __name__ == '__main__':
    unittest.main()
