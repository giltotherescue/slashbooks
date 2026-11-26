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

    def hold_sale(self):
        self.charge = self.raw['charges'].pop()
        self.stage(); self.assertEqual(1, self.apply()['posted'])
        self.original = self.store.load_entries()[0]

    def entry(self, source_id):
        return next(entry for entry in self.store.load_entries() if entry.source_id == source_id)

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


if __name__ == '__main__':
    unittest.main()
