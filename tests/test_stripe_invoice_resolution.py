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
        self.raw['paymentIntents'] = [{'id': 'pi_missing'}]  # Round 4: an ID alone is not the document.
        self.assertHeld(self.plan(), 'missing_payment_intent_evidence')
        self.raw['paymentIntents'] = [{'id': 'pi_missing', 'status': 'succeeded'}]
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
        self.raw['charges'][0]['payment_intent'] = dict(id='pi_actual', status='succeeded')
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


class HeldEntryTests(StripeFixture):
    """Round 4: a held sale or refund stays in the holding account until a person corrects it. The engine never
    moves it, whatever later evidence says (these tests used to expect a reclassification entry)."""

    RECLASS = 'stripe-reclassify:acct_test:txn_sale'

    def test_held_sale_stays_held_when_new_evidence_would_prove_it(self):
        from bookkeeping.ledger.importer import check_integrity
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        plan = self.plan()
        self.assertEqual(([], [dict(source_id=SALE, reason='already_posted')]), (plan['actions'], plan['skipped']))
        self.assertFalse({'reclassified', 'reclassification_blocked'} & set(plan))
        before = self.files()
        self.assertEqual(0, self.apply()['posted'])
        self.assertEqual(before, self.files())
        self.stage(); self.assertEqual(0, self.apply()['posted'])
        self.assertEqual([self.original], self.store.load_entries())
        self.assertEqual(Decimal('-100'), self.balances()[HOLDING])
        self.assertEqual('ok', check_integrity(self.entity).status)

    def test_nothing_moves_while_the_evidence_is_still_uncertain(self):
        self.hold_sale()
        self.raw['charges'] = [dict(self.charge, payment_intent='pi_missing')]
        self.assertEqual([], self.plan()['actions'])

    def test_a_person_corrects_a_held_sale_through_the_ledger(self):
        from bookkeeping.ledger.importer import reverse_and_correct
        from bookkeeping.ledger.model import Entry, Posting
        self.hold_sale()
        corrected = Entry(date=self.original.date, narration='Reviewed sale', meta=(('source-id', 'reviewed-sale'),),
                          postings=(Posting(base.CLEARING, Decimal('97'), 'USD'), Posting(base.FEES, Decimal('3'), 'USD'),
                                    Posting(base.INCOME, Decimal('-100'), 'USD')))
        reverse_and_correct(self.entity, SALE, corrected, 'review')
        self.assertEqual({base.CLEARING: Decimal('97'), HOLDING: Decimal('0'), base.FEES: Decimal('3'),
                          base.INCOME: Decimal('-100')}, self.balances())
        # Round 5 (rule 1): a corrected sale never proves a refund; its later refund holds for the same person
        # (it used to post to contra-revenue behind the live correction).
        self.raw['charges'] = [self.charge]; self.full_flow()
        self.raw['balanceTransactions'].pop(0)
        self.assertHeld(self.plan(), 'sale_corrected', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_held_refund_stays_held_after_its_sale_evidence_arrives(self):
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        self.full_flow()
        sale_bt = self.raw['balanceTransactions'].pop(0)
        self.stage(); self.apply()
        self.assertEqual('sale_in_holding', dict(self.entry(REFUND).meta)['stripe-review'])
        self.raw['balanceTransactions'].insert(0, sale_bt)
        self.assertEqual([], self.plan()['actions'])
        self.assertEqual(0, self.apply()['posted'])
        self.assertEqual({base.CLEARING: Decimal('0'), HOLDING: Decimal('-80'), base.FEES: Decimal('3'),
                          base.TRANSIT: Decimal('77')}, self.balances())

    def test_held_refund_does_not_move_while_its_sale_stays_held(self):
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        self.full_flow()
        self.raw['balanceTransactions'].pop(0)
        self.stage(); self.apply()
        self.assertEqual([], self.plan()['actions'])

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
                self.assertEqual([], plan['uncategorized'])


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

    def pre_release_move(self):
        """A held sale and the move a pre-release build wrote for it. The engine no longer writes moves, but the
        ledger correction guard still protects any that exist."""
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry, Posting
        self.hold_sale()
        move = Entry(date=self.original.date, narration='Reclassify', meta=(('source-id', self.RECLASS), ('stripe-reclassifies', SALE)),
                     postings=(Posting(HOLDING, Decimal('100'), 'USD'), Posting(base.INCOME, Decimal('-100'), 'USD')))
        _atomic_ledger_write(self.entity, [], [move], 'pre-release', None, 'pre-release move')
        return self.entry(self.RECLASS)

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

    # Round 4: a held sale never moves by itself, whatever the authority (it used to move within the limits).

    def test_held_sale_never_moves_whatever_the_ask_first_limit(self):
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        for limit in ('50.00', '500.00'):
            self.entity.trust_policy['ask_first_above'] = limit
            result = self.apply()
            self.assertEqual((0, []), (result['posted'], result['uncategorized']))
            self.assertNotIn('reclassified', result)
        self.assertEqual(Decimal('-100'), self.balances()[HOLDING])

    def test_held_sale_never_moves_with_or_without_accrual_treatment(self):
        from unittest.mock import patch
        from bookkeeping import stripe
        self.hold_sale()
        self.raw['charges'] = [self.charge]
        self.assertEqual([], self.plan()['actions'])
        with patch('bookkeeping.accrual.treatment.treatment_checker', return_value=lambda row: True):
            self.assertEqual([], stripe.stripe_plan(self.entity, self.raw, self.policy)['actions'])

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
        self.pre_release_move()
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
        # Round 4: the refund and payout post once; the held sale stays held (it used to move: 3 entries).
        self.assertEqual((2, 0), (self.apply()['posted'], self.apply()['posted']))
        self.assertEqual(Decimal('-80'), self.balances()[HOLDING])


class ReviewProbeTests(StripeFixture):
    """The round-one review probe not already covered by ReviewFindingTests (synthetic data)."""

    def test_later_cohort_refund_of_a_held_sale_stays_held_with_it(self):
        # Round 4: both used to leave the holding account together; now both wait for a person.
        self.hold_sale()
        self.raw['charges'].append(self.charge)
        self.full_flow(); self.stage()
        self.assertEqual(2, self.apply()['posted'])
        self.assertEqual('sale_in_holding', dict(self.entry(REFUND).meta)['stripe-review'])
        self.assertEqual({base.CLEARING: Decimal('0'), HOLDING: Decimal('-80'), base.FEES: Decimal('3'),
                          base.TRANSIT: Decimal('77')}, self.balances())
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
        self.assertEqual(0, result['posted'])
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
        self.pre_release_move()
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
        move = self.pre_release_move()
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

    # Finding 10 (E), round 4: there are no moves, so nothing is blocked and neither key exists.
    def test_held_sale_has_no_move_to_block(self):
        self.hold_sale(); self.raw['charges'] = [self.charge]
        self.entity.trust_policy['ask_first_above'] = '99.00'
        plan = self.plan()
        self.assertEqual(([], [], [], [dict(source_id=SALE, reason='already_posted')]),
                         (plan['actions'], plan['uncategorized'], plan['unresolved'], plan['skipped']))
        self.assertFalse({'reclassified', 'reclassification_blocked'} & set(plan))
        result = self.apply()
        self.assertEqual((0, 0), (result['posted'], result['held']))
        self.assertFalse({'reclassified', 'reclassification_blocked'} & set(result))

    def test_held_sale_stays_held_with_an_open_receivable(self):
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
        self.assertEqual([], stripe.stripe_plan(entity, raw, policy)['actions'])  # Round 4: it used to move here.
        request = dict(operation_id='inv', id='inv', kind='receivable', account=scope.AR, original_amount='100.00',
                       as_of='2026-01-01', counterparty='Customer', origin=dict(mode='new', source_id='inv-src',
                       counter_account=scope.INCOME), evidence_ids=[books.evidence('inv-ev')])
        scope.apply_item(entity, scope.preview_item(entity, request))
        plan = stripe.stripe_plan(entity, raw, policy)
        self.assertEqual([], plan['actions'])
        result = stripe.stripe_apply(entity, raw, policy, expected_plan_digest=plan['plan_digest'])
        self.assertEqual(0, result['posted'])


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

    def test_reversed_claim_keeps_the_invoice_reserved(self):
        # Round 5 (rule 3): a claim is a reservation; a true reversal no longer frees the invoice (it used to).
        from bookkeeping.ledger.importer import reverse_entry
        self.invoice(); self.stage(); self.apply()
        reverse_entry(self.entity, SALE, 'test')
        self.raw['charges'][0].update(id='ch_second', balance_transaction='txn_second')
        self.raw['balanceTransactions'][0].update(id='txn_second', source='ch_second')
        self.assertHeld(self.stage(), 'invoice_claimed_by_another_payment', sid='stripe:acct_test:txn_second')

    def test_claim_survives_an_interrupted_apply_and_never_blocks_its_own_sale(self):
        self.invoice(); self.stage()
        self.interrupted_apply()
        result = self.apply()
        self.assertEqual(([], []), (result['needs_review'], result['uncategorized']))
        self.full_flow(); self.raw['invoices'][0]['lines']['data'] = [dict(self.raw['invoices'][0]['lines']['data'][0], amount=10000)]
        self.raw['invoices'][0]['lines']['data'][0].pop('pricing'); self.raw['invoices'][0]['lines']['data'][0]['price'] = 'price_one'
        # Round 5: the capture no longer reproduces the evidence that decided the booked two-line sale (its closure
        # digest), so the refund holds (round 4 left it unresolved; it used to post).
        actions, unresolved = self.actions()
        self.assertEqual(([], [(REFUND, 'uncategorized-stripe-activity', 'sale_evidence_changed')]),
                         (unresolved, [row for row in actions if row[0] == REFUND]))

    def test_held_sale_owns_the_invoice_it_names(self):
        # Round 4 (replaces "a moved sale claims its invoice"): a held sale owns what it names meanwhile.
        self.invoice()
        invoices = self.raw.pop('invoices')
        self.stage(); self.apply()
        self.assertEqual(('missing_invoice_evidence', 'in_one'),
                         tuple(dict(self.entry(SALE).meta)[key] for key in ('stripe-review', 'stripe-invoice')))
        self.raw['invoices'] = invoices
        self.raw['charges'][0].update(id='ch_second', balance_transaction='txn_second')
        self.raw['balanceTransactions'][0].update(id='txn_second', source='ch_second')
        self.assertHeld(self.stage(), 'invoice_claimed_by_another_payment', sid='stripe:acct_test:txn_second')

    # C: refund release needs ledger proof.

    def test_held_refund_with_its_link_removed_stays(self):
        self.held_refund_with_restored_charge()
        del self.raw['charges'][0]['balance_transaction']
        self.assertEqual([], self.plan()['actions'])

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
        self.assertEqual([], self.plan()['actions'])

    def test_refund_waits_while_its_sales_move_is_blocked(self):
        self.held_refund_with_restored_charge()
        self.raw['balanceTransactions'].insert(0, dict(self.raw['balanceTransactions'][0], id='txn_sale', type='charge',
            source='ch_sale', amount=10000, fee=300, net=9700, created=1768435200))
        self.entity.trust_policy['ask_first_above'] = '99.00'
        plan = self.plan()
        self.assertEqual([], plan['actions'])
        self.assertNotIn('reclassification_blocked', plan)

    # D: lineage and canonical IDs.

    def test_correcting_the_original_while_its_move_lives_is_refused(self):
        from bookkeeping.ledger.importer import reverse_and_correct
        from bookkeeping.ledger.model import Entry, Posting
        self.pre_release_move()
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


class RecheckThreeFindingTests(RecheckFixture):
    """One test for each finding of PR 177 recheck 3 (its round3.py reproducers), under the round-4 scope:
    nothing moves out of holding automatically, refunds need positive ledger evidence of their sale, every new sale
    is unique by charge, payment intent and invoice across cohorts, and older entries never count as unowned."""

    SECOND = 'stripe:acct_test:txn_second'

    def new_movement(self, charge='ch_second'):
        """The next cohort: the same capture, but the sale movement (and charge) has a new ID."""
        self.raw['charges'][0].update(id=charge, balance_transaction='txn_second')
        self.raw['balanceTransactions'][0].update(id='txn_second', source=charge)

    def legacy_post(self):
        """Post the current sale as an earlier release did: the same postings and saved source, no ownership metadata."""
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry, Posting
        from datetime import date
        action = self.action(self.plan())
        self.assertIsNone(action.get('review_reason'))
        entry = Entry(date=date.fromisoformat(action['normalized_source']['date']), narration=action['normalized_source']['description'],
                      meta=(('source-id', SALE), ('import-session', 'main'), ('stripe-authority', action['authority_id']),
                            ('stripe-source-digest', action['source_digest'])),
                      postings=tuple(Posting(p['account'], Decimal(p['amount']), p['currency']) for p in action['postings']))
        _atomic_ledger_write(self.entity, [], [entry], 'main', None, 'legacy Stripe sale', [action['normalized_source']])
        self.assertNotIn('stripe-charge', dict(self.entry(SALE).meta))

    def refund_only(self):
        """Keep only the refund movement of the full flow (a later cohort)."""
        self.raw['balanceTransactions'] = [row for row in self.raw['balanceTransactions'] if row['type'] == 'refund']

    # P1 legacy_duplicate: an older sale's invoice is never booked again.
    def test_older_sale_without_ownership_holds_a_new_claimant(self):
        self.invoice(); self.legacy_post()
        self.assertEqual(0, self.apply()['posted'])  # Exact replay of the older sale posts nothing.
        self.new_movement(); self.stage()
        self.assertHeld(self.plan(), 'ownership_unproven', sid=self.SECOND)
        self.apply()
        self.assertEqual((Decimal('-60'), Decimal('-40'), Decimal('-100')),
                         tuple(self.balances()[name] for name in (base.INCOME, 'Income:Subscriptions', HOLDING)))

    def test_older_sale_proves_its_charge_only_from_its_captured_movement(self):
        self.legacy_post()
        older = copy.deepcopy(self.raw['balanceTransactions'][0])
        second = dict(copy.deepcopy(self.raw['charges'][0]), id='ch_second', balance_transaction='txn_second')
        self.raw['charges'].append(second)
        self.raw['balanceTransactions'].append(dict(older, id='txn_second', source='ch_second'))
        plan = self.plan()  # The older movement is captured and matches its saved digest: its charge is ch_sale.
        self.assertEqual({base.CLEARING: '97.00', base.INCOME: '-100.00', base.FEES: '3.00'}, self.postings(plan, self.SECOND))
        self.raw['balanceTransactions'].pop(0)  # Not captured: the older sale could own anything.
        self.assertHeld(self.plan(), 'ownership_unproven', sid=self.SECOND)
        self.raw['balanceTransactions'].insert(0, dict(older, amount=10001, net=9701))  # Changed: no proof either.
        self.assertHeld(self.plan(), 'ownership_unproven', sid=self.SECOND)
        self.raw['balanceTransactions'][0] = older
        second['payment_intent'] = 'pi_two'; self.raw['paymentIntents'] = [dict(id='pi_two', status='succeeded')]
        self.assertHeld(self.plan(), 'ownership_unproven', sid=self.SECOND)  # Its intents and invoices are never known.

    def test_older_sale_owns_its_charge(self):
        self.legacy_post()
        older = copy.deepcopy(self.raw['balanceTransactions'][0])
        self.new_movement('ch_sale'); self.raw['balanceTransactions'].insert(0, older)
        self.raw['charges'][0]['balance_transaction'] = 'txn_second'
        self.assertEqual('charge_reference_conflict', self.action(self.plan(), self.SECOND)['review_reason'])
        self.raw['balanceTransactions'].pop(0)
        self.assertHeld(self.plan(), 'ownership_unproven', sid=self.SECOND)

    # P1 corrected_claim: a correction that keeps the entry keeps its claim; a true reversal releases it.
    def test_corrected_sale_keeps_its_invoice_claim(self):
        from bookkeeping.ledger.importer import reverse_and_correct
        from bookkeeping.ledger.model import Entry
        self.invoice(); self.stage(); self.apply(); entry = self.entry(SALE)
        reverse_and_correct(self.entity, SALE, Entry(date=entry.date, narration='Correct payee',
                            meta=(('source-id', 'corrected-sale'),), postings=entry.postings), 'test')
        self.new_movement(); self.stage()
        self.assertHeld(self.plan(), 'invoice_claimed_by_another_payment', sid=self.SECOND)
        self.apply()
        self.assertEqual((Decimal('-60'), Decimal('-40')), (self.balances()[base.INCOME], self.balances()['Income:Subscriptions']))

    # P1 repeated_charge / shared_intent_across_cohorts: one charge and one intent earn revenue once.
    def test_same_charge_in_a_later_cohort_is_held(self):
        self.stage(); self.apply()
        self.assertEqual('ch_sale', dict(self.entry(SALE).meta)['stripe-charge'])
        self.new_movement('ch_sale'); self.stage()
        self.assertHeld(self.plan(), 'charge_claimed_by_another_movement', sid=self.SECOND)
        self.apply()
        self.assertEqual(Decimal('-100'), self.balances()[base.INCOME])

    def test_same_payment_intent_in_a_later_cohort_is_held(self):
        self.raw['paymentIntents'] = [dict(id='pi_one', status='succeeded')]; self.raw['charges'][0]['payment_intent'] = 'pi_one'
        self.stage(); self.apply()
        self.assertEqual('pi_one', dict(self.entry(SALE).meta)['stripe-payment-intent'])
        self.new_movement(); self.stage()
        self.assertHeld(self.plan(), 'payment_intent_claimed_by_another_payment', sid=self.SECOND)
        self.apply()
        self.assertEqual(Decimal('-100'), self.balances()[base.INCOME])

    def test_true_reversal_keeps_the_charge_and_intent_reserved(self):
        # Round 5 (rule 3): a reversed sale's claims stay reserved; the second movement holds (it used to post).
        from bookkeeping.ledger.importer import reverse_entry
        self.raw['paymentIntents'] = [dict(id='pi_one', status='succeeded')]; self.raw['charges'][0]['payment_intent'] = 'pi_one'
        self.stage(); self.apply(); reverse_entry(self.entity, SALE, 'test')
        self.new_movement('ch_sale')
        self.assertHeld(self.plan(), 'charge_claimed_by_another_movement', sid=self.SECOND)

    # P1 refund_released_by_payout / refund_changed_charge / unrelated_legacy_sale_releases.
    def test_held_refund_is_never_released_later(self):
        self.full_flow(); original = self.raw['balanceTransactions'].pop(0); charge = self.raw['charges'].pop()
        self.stage(); self.apply()
        self.assertEqual(('ch_sale', 'missing_refund_charge_evidence'),
                         tuple(dict(self.entry(REFUND).meta)[key] for key in ('stripe-refund-of', 'stripe-review')))
        for change in ('payout', 'other_charge'):
            with self.subTest(change):
                if change == 'payout':
                    self.raw['charges'] = [dict(charge, balance_transaction='txn_payout')]
                else:
                    self.raw['balanceTransactions'].append(dict(original, id='txn_other', source='ch_other'))
                    self.raw['charges'] = [dict(charge, id='ch_other', balance_transaction='txn_other')]
                    self.raw['refunds'][0]['charge'] = 'ch_other'
                plan = self.plan()
                self.assertNotIn(REFUND, {row['source_id'] for row in plan['actions']})
                self.stage(); self.apply()
                self.assertEqual(Decimal('20'), self.balances()[HOLDING])
                self.assertNotIn(base.REFUNDS, self.balances())

    def test_refund_whose_charge_names_a_payout_is_held(self):
        self.full_flow(); self.raw['balanceTransactions'].pop(0); self.raw['charges'][0]['balance_transaction'] = 'txn_payout'
        self.raw['balanceTransactions'] = [row for row in self.raw['balanceTransactions'] if row['type'] == 'payout']
        self.stage(); self.apply()
        self.full_flow(); self.raw['balanceTransactions'] = [row for row in self.raw['balanceTransactions'] if row['type'] == 'refund']
        self.raw['charges'][0]['balance_transaction'] = 'txn_payout'
        self.assertHeld(self.plan(), 'sale_movement_unverified', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_refund_linked_to_an_older_sale_of_another_charge_is_held(self):
        original = copy.deepcopy(self.raw); self.new_movement('ch_other'); self.legacy_post_other()
        self.raw = original; self.full_flow(); self.refund_only()
        self.raw['charges'][0]['balance_transaction'] = 'txn_second'
        self.assertNotIn(base.REFUNDS, self.postings(self.plan(), REFUND))

    def legacy_post_other(self):
        """legacy_post for the movement txn_second."""
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry, Posting
        from datetime import date
        action = self.action(self.plan(), self.SECOND)
        entry = Entry(date=date.fromisoformat(action['normalized_source']['date']), narration='older sale',
                      meta=(('source-id', self.SECOND), ('stripe-authority', action['authority_id'])),
                      postings=tuple(Posting(p['account'], Decimal(p['amount']), p['currency']) for p in action['postings']))
        _atomic_ledger_write(self.entity, [], [entry], 'main', None, 'legacy Stripe sale', [action['normalized_source']])

    def test_refund_of_an_older_sale_is_held(self):
        # An earlier release recorded neither the sale's ownership nor its refunds, captured movement or not.
        self.legacy_post(); self.full_flow(); sale = self.raw['balanceTransactions'][0]
        self.refund_only()
        self.assertHeld(self.plan(), 'ownership_unproven', sid=REFUND, amount='20.00', fee=None, net='-20.00')
        self.raw['balanceTransactions'].insert(0, sale)
        self.assertHeld(self.plan(), 'ownership_unproven', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_refunds_of_one_charge_never_exceed_its_booked_line(self):
        self.stage(); self.apply()
        self.full_flow(); self.refund_only()
        refund = copy.deepcopy(self.raw['refunds'][0]); movement = copy.deepcopy(self.raw['balanceTransactions'][0])
        self.raw['refunds'][0]['amount'] = 9000; self.raw['balanceTransactions'][0].update(amount=-9000, net=-9000)
        self.stage(); self.assertEqual(1, self.apply()['posted'])
        self.raw['refunds'] = [dict(refund, id='re_two', balance_transaction='txn_refund_two')]
        self.raw['balanceTransactions'] = [dict(movement, id='txn_refund_two', source='re_two')]
        self.assertIn(dict(source_id='stripe:acct_test:txn_refund_two', reason='refund_exceeds_booked_sale'), self.plan()['unresolved'])
        # Two in one batch count together too.
        self.setUp(); self.stage(); self.apply(); self.full_flow(); self.refund_only()
        self.raw['refunds'][0]['amount'] = 6000; self.raw['balanceTransactions'][0].update(amount=-6000, net=-6000)
        self.raw['refunds'].append(dict(self.raw['refunds'][0], id='re_two', balance_transaction='txn_refund_two'))
        self.raw['balanceTransactions'].append(dict(self.raw['balanceTransactions'][0], id='txn_refund_two', source='re_two'))
        self.assertEqual({REFUND, 'stripe:acct_test:txn_refund_two'},
                         {row['source_id'] for row in self.plan()['unresolved'] if row['reason'] == 'refund_exceeds_booked_sale'})

    def test_refund_in_a_later_cohort_follows_its_posted_sale(self):
        self.stage(); self.apply()
        self.full_flow(); self.refund_only()
        self.assertEqual({base.CLEARING: '-20.00', base.REFUNDS: '20.00'}, self.postings(self.plan(), REFUND))
        self.raw['refunds'][0]['amount'] = 12000; self.raw['balanceTransactions'][0].update(amount=-12000, net=-12000)
        self.assertIn(dict(source_id=REFUND, reason='refund_exceeds_booked_sale'), self.plan()['unresolved'])

    def test_refund_after_its_sale_was_reversed_is_held(self):
        from bookkeeping.ledger.importer import reverse_entry
        self.stage(); self.apply(); reverse_entry(self.entity, SALE, 'test')
        self.full_flow(); self.refund_only()
        self.assertHeld(self.plan(), 'sale_corrected', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_refund_without_its_sale_is_held(self):
        self.full_flow(); self.refund_only()  # Absent from the ledger and the batch (an earlier cutover).
        self.assertHeld(self.plan(), 'sale_movement_unverified', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    # P1 refund_drops_booked_invoice: the booked sale decides, not the refund's capture.
    def test_refund_that_drops_the_booked_invoice_never_posts(self):
        plain_rules = copy.deepcopy(self.policy['revenue_rules']); self.invoice(); self.policy['revenue_rules'] = plain_rules
        self.stage(); self.apply()
        self.assertEqual('in_one', dict(self.entry(SALE).meta)['stripe-invoice'])
        self.full_flow(); self.refund_only()
        self.assertIn(dict(source_id=REFUND, reason='mixed_line_refund_requires_allocation'), self.plan()['unresolved'])
        self.raw['invoices'] = []; del self.raw['charges'][0]['invoice']
        self.assertHeld(self.plan(), 'sale_links_changed', sid=REFUND, amount='20.00', fee=None, net='-20.00')
        self.stage(); self.apply()
        self.assertNotIn(base.REFUNDS, self.balances())

    def test_refund_of_a_two_line_booked_sale_needs_allocation_even_if_the_capture_shows_one_line(self):
        self.invoice(); self.stage(); self.apply()
        self.full_flow(); self.refund_only()
        self.raw['invoices'][0]['lines']['data'] = [dict(self.raw['invoices'][0]['lines']['data'][0], amount=10000)]
        # Round 5: the one-line capture does not reproduce the booked sale's closure digest, so the refund holds.
        self.assertHeld(self.plan(), 'sale_evidence_changed', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    # P1 correction_chain_alias: an alias-equivalent replacement is refused; ambiguous old lineage stays live.
    def test_alias_equivalent_replacement_is_refused(self):
        from bookkeeping.ledger.importer import _atomic_ledger_write, reverse_and_correct
        from bookkeeping.ledger.model import Entry, Posting
        from datetime import date
        postings = (Posting(base.BANK, Decimal('5'), 'USD'), Posting(base.INCOME, Decimal('-5'), 'USD'))
        _atomic_ledger_write(self.entity, [], [Entry(date=date(2026, 1, 15), narration='bank', meta=(('source-id', 'reviewed'),),
                                                     postings=postings)], 's', None, 'test')
        before = self.files()
        for target in ('banksync:reviewed', 'reviewed'):
            with self.subTest(target), self.assertRaisesRegex(ValueError, 'another spelling'):
                reverse_and_correct(self.entity, 'reviewed', Entry(date=date(2026, 1, 15), narration='fixed',
                                    meta=(('source-id', target),), postings=postings), 'test')
        self.assertEqual(before, self.files())
        reverse_and_correct(self.entity, 'reviewed', Entry(date=date(2026, 1, 15), narration='fixed',
                            meta=(('source-id', 'fixed-1'),), postings=postings), 'test')
        with self.assertRaisesRegex(ValueError, 'another spelling'):
            reverse_and_correct(self.entity, 'fixed-1', Entry(date=date(2026, 1, 15), narration='again',
                                meta=(('source-id', 'banksync:reviewed'),), postings=postings), 'test')

    def test_move_corrected_under_an_alias_keeps_protecting_its_original(self):
        from unittest.mock import patch
        from bookkeeping.ledger import corrections
        from bookkeeping.ledger.importer import _atomic_ledger_write, reverse_and_correct, reverse_entry
        from bookkeeping.ledger.model import Entry
        move = self.pre_release_move()
        with patch.object(corrections, '_refuse_entry_with_live_stripe_move'):  # As an older engine allowed.
            reverse_and_correct(self.entity, self.RECLASS, Entry(date=move.date, narration='Reviewed',
                                meta=(('source-id', 'reviewed-move'),), postings=move.postings), 'review')
        replacement = Entry(date=move.date, narration='Alias', meta=(('source-id', 'banksync:reviewed-move'),), postings=move.postings)
        with self.assertRaisesRegex(ValueError, 'another spelling'):
            reverse_and_correct(self.entity, 'reviewed-move', replacement, 'review')
        # Ambiguous lineage written by an older engine (reversal plus alias replacement) never counts as unwound.
        reversal = Entry(date=move.date, narration='Reversal', meta=(('reverses', 'reviewed-move'),),
                         postings=tuple(type(p)(p.account, -p.amount, p.currency) for p in move.postings))
        alias = Entry(date=move.date, narration='Alias', meta=(('source-id', 'banksync:reviewed-move'), ('correction-of', 'reviewed-move')),
                      postings=move.postings)
        _atomic_ledger_write(self.entity, [], [reversal, alias], 'old', None, 'older alias correction')
        with self.assertRaisesRegex(ValueError, 'Reverse banksync:reviewed-move first'):
            reverse_entry(self.entity, SALE, 'review')
        self.assertEqual({base.CLEARING: Decimal('97'), HOLDING: Decimal('0'), base.FEES: Decimal('3'), base.INCOME: Decimal('-100')},
                         self.balances())

    def test_alias_corrected_sale_still_owns_its_charge(self):
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry
        self.stage(); self.apply(); entry = self.entry(SALE)
        reversal = Entry(date=entry.date, narration='Reversal', meta=(('reverses', SALE),),
                         postings=tuple(type(p)(p.account, -p.amount, p.currency) for p in entry.postings))
        alias = Entry(date=entry.date, narration='Alias', meta=(('source-id', 'banksync:' + SALE), ('correction-of', SALE)),
                      postings=entry.postings)
        _atomic_ledger_write(self.entity, [], [reversal, alias], 'old', None, 'older alias correction')
        self.new_movement('ch_sale')
        self.assertHeld(self.plan(), 'charge_claimed_by_another_movement', sid=self.SECOND)

    # P1 placeholder_digest: the fingerprint reads the resolved price; an unavailable one is never evidence.
    def test_fingerprint_binds_an_expanded_price_in_pricing_details(self):
        self.invoice(); line = self.raw['invoices'][0]['lines']['data'][0]
        line['pricing']['price_details']['price'] = {'id': 'price_one', 'currency': 'usd'}
        plan = self.stage(); self.assertEqual(([], []), (plan['uncategorized'], plan['unresolved']))
        before = plan['source_capture_digests'][SALE]
        self.interrupted_apply()
        unchanged = copy.deepcopy(self.raw)
        self.raw['invoices'][0]['total_taxes'] = [{'amount': 100}]
        self.assertNotEqual(before, self.plan()['source_capture_digests'][SALE])
        self.assertEqual(['cleanup_needs_review'], [row['reason'] for row in self.apply()['needs_review']])
        self.raw = unchanged  # The same evidence is fingerprinted (never the unavailable marker) and recovers.
        self.assertEqual([], self.apply()['needs_review'])

    def test_unavailable_fingerprint_never_cleans_up_an_older_posting(self):
        from unittest.mock import patch
        from bookkeeping import stripe
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry, Posting
        from datetime import date
        plan = self.stage(); action = self.action(plan)
        # Posted by an older engine without a receipt: cleanup checks the saved source and policy digests.
        entry = Entry(date=date.fromisoformat(action['normalized_source']['date']), narration='older', meta=(
            ('source-id', SALE), ('stripe-authority', action['authority_id']), ('stripe-policy-digest', plan['policy_digest']),
            ('stripe-source-digest', stripe._digest(action['normalized_source']))),
            postings=tuple(Posting(p['account'], Decimal(p['amount']), p['currency']) for p in action['postings']))
        _atomic_ledger_write(self.entity, [], [entry], 'older', None, 'older Stripe sale', [action['normalized_source']])
        with patch.object(stripe, '_source_capture_digest', side_effect=TypeError):
            self.assertEqual(['cleanup_needs_review'], [row['reason'] for row in self.apply()['needs_review']])
        self.assertEqual([], self.apply()['needs_review'])

    def test_refund_linked_to_another_charges_sale_is_held(self):
        self.new_movement('ch_other'); self.stage(); self.apply()
        self.assertEqual('ch_other', dict(self.entry(self.SECOND).meta)['stripe-charge'])
        self.raw = base.evidence(); self.full_flow(); self.refund_only()
        self.raw['charges'][0]['balance_transaction'] = 'txn_second'  # The other charge's sale movement.
        self.assertHeld(self.plan(), 'sale_links_changed', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_unavailable_fingerprint_is_never_evidence(self):
        from unittest.mock import patch
        from bookkeeping import stripe
        with patch.object(stripe, '_source_capture_digest', side_effect=TypeError):
            self.assertEqual(([], [dict(source_id=SALE, reason='capture_fingerprint_unavailable')]), self.actions())
        self.stage(); self.interrupted_apply()
        with patch.object(stripe, '_source_capture_digest', side_effect=TypeError):
            review = self.apply()['needs_review']
        self.assertEqual([(SALE, 'cleanup_needs_review')], [(row['source_id'], row['reason']) for row in review])

    # P2 rejected_same_batch_sale: a sale this plan refuses is not an absent historical sale.
    def test_refund_of_a_sale_this_plan_refuses_is_held(self):
        self.full_flow(); self.entity.trust_policy['ask_first_above'] = '99.00'
        plan = self.stage()
        self.assertIn(dict(source_id=SALE, reason='above_ask_first_amount'), plan['unresolved'])
        self.assertHeld(plan, 'sale_not_posted', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    # P2 invalid_balance_status_move: a held sale never moves, whatever the movement's status.
    def test_held_sale_with_an_invalid_movement_status_stays_held(self):
        self.hold_sale(); self.raw['charges'] = [self.charge]; self.raw['balanceTransactions'][0]['status'] = 'invalid'
        self.assertEqual(([], []), self.actions())
        self.assertEqual(0, self.apply()['posted'])
        self.assertEqual(Decimal('-100'), self.balances()[HOLDING])

    # P2 changed_sale_receipt: the refund's receipt binds the sale movement's financial identity.
    def test_refund_receipt_binds_a_changed_sale_movement(self):
        from bookkeeping.stripe import _load_pending_categorization
        self.full_flow(); self.stage(); before = self.plan()['source_capture_digests'][REFUND]
        self.interrupted_apply()
        self.raw['balanceTransactions'][0]['currency'] = 'eur'
        plan = self.plan()
        self.assertNotEqual(before, plan['source_capture_digests'][REFUND])
        self.assertIn(REFUND, [row['source_id'] for row in self.apply()['needs_review']])
        self.assertIn(REFUND, [row['id'] for row in _load_pending_categorization(self.entity)])

    # P2 missing_intent: an ID-only expansion is not the document, whoever names the intent.
    def test_id_only_payment_intent_is_not_the_document(self):
        self.raw['charges'][0]['payment_intent'] = {'id': 'pi_missing'}
        self.assertHeld(self.plan(), 'missing_payment_intent_evidence')
        del self.raw['charges'][0]['payment_intent']; self.full_flow(); self.raw['refunds'][0]['payment_intent'] = 'pi_missing'
        plan = self.plan()
        self.assertHeld(plan, 'missing_payment_intent_evidence')
        self.assertHeld(plan, 'missing_payment_intent_evidence', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_id_only_charge_or_invoice_is_not_the_document(self):
        self.raw['balanceTransactions'][0]['source'] = {'id': 'ch_sale'}; self.raw['charges'] = []
        self.assertHeld(self.plan(), 'missing_charge_evidence')
        self.setUp(); self.invoice(); self.raw['invoices'] = []; self.raw['charges'][0]['invoice'] = {'id': 'in_one'}
        self.assertHeld(self.plan(), 'missing_invoice_evidence')

    # P2 unpaid_checkout_only_edge: an expired or unpaid session never links a payment.
    def test_unpaid_or_expired_checkout_session_never_links_the_invoice(self):
        for status, payment_status in (('expired', 'unpaid'), ('complete', 'unpaid'), ('open', 'paid')):
            with self.subTest(status=status, payment_status=payment_status):
                self.setUp(); self.invoice(); charge = self.raw['charges'][0]; del charge['invoice']; charge['payment_intent'] = 'pi_one'
                self.raw['checkoutSessions'] = [dict(id='cs_one', payment_intent='pi_one', invoice='in_one', currency='usd',
                                                     payment_status=payment_status, status=status)]
                self.assertHeld(self.plan(), 'checkout_session_not_paid')

    # P2 cross_line_product: one product's copies are merged across every line before rules are chosen.
    def test_product_copies_on_different_lines_are_merged(self):
        self.invoice(); lines = self.raw['invoices'][0]['lines']['data']
        lines[0]['pricing']['price_details']['product'] = dict(id='prod_two', metadata={'plan': 'a'})
        lines[1]['price']['product'] = dict(id='prod_two', metadata={'plan': 'b'})
        self.policy['revenue_rules'] = [dict(id=key, match={'metadata': dict(object='product', key='plan', value=key)}, category=category)
                                        for key, category in [('a', base.INCOME), ('b', 'Income:Subscriptions')]]
        self.assertHeld(self.plan(), 'product_reference_conflict')
        lines[1]['price']['product'] = dict(id='prod_two', metadata={'plan': 'a'})  # Agreeing copies still allocate.
        action = self.action(self.plan())
        self.assertEqual((None, [(base.INCOME, '-60.00'), (base.INCOME, '-40.00')]),
                         (action.get('review_reason'), [(row['account'], row['amount']) for row in action['postings']
                                                        if row['account'].startswith('Income:')]))

    def test_price_copies_on_different_lines_are_merged(self):
        self.invoice(); lines = self.raw['invoices'][0]['lines']['data']
        lines[1]['price'] = {'id': 'price_one', 'product': 'prod_one', 'metadata': {'plan': 'a'}}
        lines[0]['pricing']['price_details']['price'] = {'id': 'price_one', 'metadata': {'plan': 'b'}}
        self.assertHeld(self.plan(), 'price_reference_conflict')

    # P2 wrong_mode_catalog: every catalog copy is checked for mode (and currency).
    def test_catalog_copies_from_the_other_mode_or_currency_hold(self):
        for name, change in [('product', lambda raw: raw.update(products=[dict(id='prod_two', livemode=True, metadata={'kind': 'sub'})])),
                             ('price', lambda raw: raw.update(prices=[dict(id='price_two', product='prod_two', livemode=True)])),
                             ('expanded', lambda raw: raw['invoices'][0]['lines']['data'][1]['price'].update(livemode=True))]:
            with self.subTest(name):
                self.setUp(); self.invoice()
                self.policy['revenue_rules'][1:] = [dict(id='catalog', match={'product_id': 'prod_two'}, category='Income:Subscriptions')]
                change(self.raw)
                self.assertHeld(self.plan(), 'livemode_conflict')
        self.setUp(); self.invoice(); self.raw['products'] = [dict(id='prod_two', currency='eur')]
        self.assertHeld(self.plan(), 'product_currency_conflict')

    # Ownership metadata on every new sale entry, held or not.
    def test_new_sale_entries_record_what_they_own(self):
        self.invoice_without_charge_reference(); self.raw['paymentIntents'] = [dict(id='pi_one', invoice='in_one', status='succeeded')]
        self.stage(); self.apply()
        meta = dict(self.entry(SALE).meta)
        self.assertEqual(('ch_sale', 'pi_one', 'in_one'), (meta['stripe-charge'], meta['stripe-payment-intent'], meta['stripe-invoice']))


class RecheckFourFindingTests(RecheckFixture):
    """One test for each finding of PR 177 recheck 4 (its variants4.py reproducers), under the round-5 rules:
    refunds post only behind narrow proof of their sale, once, within its gross; claims are permanent reservations
    read from the whole lineage; kinds are proved from this engine's metadata, never names; one fully checked
    authority path; and fingerprints bind the whole closure. Every case must end held, unresolved or refused."""

    SECOND = RecheckThreeFindingTests.SECOND
    TWO = 'stripe:acct_test:txn_two'
    new_movement = RecheckThreeFindingTests.new_movement
    refund_only = RecheckThreeFindingTests.refund_only

    def legacy_sale(self, authority=None):
        """The current sale as an earlier release posted it: its postings and saved source, no engine metadata."""
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry, Posting
        from datetime import date
        action = self.action(self.plan())
        entry = Entry(date=date.fromisoformat(action['normalized_source']['date']), narration='older sale',
                      meta=(('source-id', SALE), ('stripe-authority', authority or action['authority_id'])),
                      postings=tuple(Posting(p['account'], Decimal(p['amount']), p['currency']) for p in action['postings']))
        _atomic_ledger_write(self.entity, [], [entry], 'main', None, 'legacy Stripe sale', [action['normalized_source']])

    def other_charge(self, charge='ch_two', movement='txn_two'):
        self.raw['charges'][0].update(id=charge, balance_transaction=movement)
        self.raw['balanceTransactions'][0].update(id=movement, source=charge)

    def plant_anonymous_correction(self, source_id=SALE):
        """The reversal and source-less replacement an older writer could leave (the writer now refuses one)."""
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry, Posting
        entry = self.entry(source_id)
        reversal = Entry(date=entry.date, narration='Reversal', flag='*', postings=tuple(
            Posting(p.account, -p.amount, p.currency) for p in entry.postings), meta=tuple(
            (k, v) for k, v in entry.meta if k not in ('source-id', 'import-session')) + (('reverses', source_id),))
        replacement = Entry(date=entry.date, narration='Reviewed', meta=(('correction-of', source_id),), postings=entry.postings)
        _atomic_ledger_write(self.entity, [], [reversal, replacement], 'older-writer', None, 'anonymous correction')

    # P1 legacy_authority (rule 4): a rule ID that looks like a transfer never decides what an older entry was.
    def test_older_sale_whose_rule_ids_look_like_transfers_still_holds_a_new_claimant(self):
        self.invoice()
        self.policy['revenue_rules'][0]['id'] = 'transfer-rule:services'
        self.policy['revenue_rules'][1]['id'] = 'transfer-rule:subscriptions'
        self.legacy_sale()
        self.assertTrue(dict(self.entry(SALE).meta)['stripe-authority'].startswith('transfer-rule:'))
        self.other_charge(); self.stage()
        self.assertHeld(self.plan(), 'ownership_unproven', sid=self.TWO)
        self.apply()
        self.assertEqual((Decimal('-60'), Decimal('-40'), Decimal('-100')),
                         tuple(self.balances()[name] for name in (base.INCOME, 'Income:Subscriptions', HOLDING)))

    def test_older_entry_of_unproven_kind_holds_every_new_sale(self):
        # No structured metadata says what the older entry was and its movement is not captured: nothing is backfilled.
        self.legacy_sale(authority='payout-transit-policy')
        self.other_charge()
        self.assertHeld(self.plan(), 'ownership_unproven', sid=self.TWO)

    def test_older_sale_proven_by_its_captured_movement_reserves_only_its_charge(self):
        self.legacy_sale()
        self.raw['balanceTransactions'].append(dict(self.raw['balanceTransactions'][0], id='txn_two', source='ch_two'))
        self.raw['charges'].append(dict(self.raw['charges'][0], id='ch_two', balance_transaction='txn_two'))
        plan = self.plan()
        self.assertEqual({base.CLEARING: '97.00', base.INCOME: '-100.00', base.FEES: '3.00'}, self.postings(plan, self.TWO))
        self.assertIn(dict(source_id=SALE, reason='already_posted'), plan['skipped'])

    def test_older_fee_proven_by_its_captured_movement_owns_nothing(self):
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry, Posting
        from datetime import date
        self.raw['balanceTransactions'].append({'id': 'txn_fee', 'created': 1768435200, 'amount': -500, 'fee': 0,
                                                'net': -500, 'currency': 'usd', 'status': 'available', 'type': 'stripe_fee'})
        fee = self.action(self.plan(), 'stripe:acct_test:txn_fee')
        _atomic_ledger_write(self.entity, [], [Entry(date=date(2026, 1, 15), narration='older fee', meta=(
            ('source-id', fee['source_id']), ('stripe-authority', 'fee-account-policy')), postings=tuple(
            Posting(p['account'], Decimal(p['amount']), p['currency']) for p in fee['postings']))],
            'main', None, 'legacy Stripe fee', [fee['normalized_source']])
        self.full_flow()
        plan = self.plan()
        self.assertEqual({base.CLEARING: '97.00', base.INCOME: '-100.00', base.FEES: '3.00'}, self.postings(plan))
        self.assertEqual({base.CLEARING: '-20.00', base.REFUNDS: '20.00'}, self.postings(plan, REFUND))

    def test_every_entry_this_engine_writes_records_its_kind(self):
        self.full_flow(); self.stage(); self.apply()
        kinds = {entry.source_id: dict(entry.meta).get('stripe-kind') for entry in self.store.load_entries()
                 if entry.source_id.startswith('stripe:')}
        self.assertEqual({SALE: 'sale', REFUND: 'refund', 'stripe:acct_test:txn_payout': 'other'}, kinds)

    # P1 anonymous_correction (rule 3): the writer refuses a source-less replacement; an existing one is unproven.
    def test_writer_refuses_a_replacement_without_a_source_id(self):
        from bookkeeping.ledger.importer import reverse_and_correct
        from bookkeeping.ledger.model import Entry
        self.invoice(); self.stage(); self.apply(); entry = self.entry(SALE)
        with self.assertRaisesRegex(ValueError, 'its own replacement source ID'):
            reverse_and_correct(self.entity, SALE, Entry(date=entry.date, narration='Reviewed', postings=entry.postings), 'review')
        self.assertEqual([entry], self.store.load_entries())

    def test_existing_anonymous_successor_holds_everything_it_touches(self):
        self.invoice(); self.stage(); self.apply()
        self.plant_anonymous_correction()
        self.other_charge(); self.stage()
        self.assertHeld(self.plan(), 'ownership_unproven', sid=self.TWO)
        self.apply()
        self.assertEqual(Decimal('-100'), self.balances()[HOLDING])

    def test_refund_of_an_anonymously_corrected_sale_holds(self):
        self.stage(); self.apply(); self.plant_anonymous_correction()
        self.full_flow(); self.refund_only()
        self.assertHeld(self.plan(), 'sale_corrected', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    # P1 manual_resolution_adds_invoice (rule 3): a live correction adds its own claims; the root's stay.
    def test_claims_a_correction_adds_are_reserved(self):
        from bookkeeping.ledger.importer import reverse_and_correct
        from bookkeeping.ledger.model import Entry, Posting
        self.hold_sale(); original = self.entry(SALE)
        self.raw['charges'] = [self.charge]; self.invoice()
        reverse_and_correct(self.entity, SALE, Entry(date=original.date, narration='Reviewed invoice in_one', meta=(
            ('source-id', 'reviewed-sale'), ('stripe-invoice', 'in_one')), postings=tuple(
            Posting('Income:Services' if p.account == HOLDING else p.account, p.amount, p.currency)
            for p in original.postings)), 'review')
        self.other_charge(); self.stage()
        self.assertHeld(self.plan(), 'invoice_claimed_by_another_payment', sid=self.TWO)
        self.apply()
        self.assertEqual((Decimal('-100'), Decimal('-100')), (self.balances()[base.INCOME], self.balances()[HOLDING]))

    def test_claims_are_read_from_every_correction_in_the_lineage(self):
        from bookkeeping.ledger.importer import reverse_and_correct
        from bookkeeping.ledger.model import Entry
        self.stage(); self.apply(); original = self.entry(SALE)
        reverse_and_correct(self.entity, SALE, Entry(date=original.date, narration='first', meta=(
            ('source-id', 'fix-1'), ('stripe-invoice', 'in_first')), postings=original.postings), 'review')
        reverse_and_correct(self.entity, 'fix-1', Entry(date=original.date, narration='second', meta=(
            ('source-id', 'fix-2'),), postings=original.postings), 'review')
        self.invoice(); self.raw['charges'][0]['invoice'] = 'in_first'; self.raw['invoices'][0]['id'] = 'in_first'
        self.other_charge()
        self.assertHeld(self.plan(), 'invoice_claimed_by_another_payment', sid=self.TWO)

    # P1 refund_without_cash_sale (rule 1): a corrected sale is never proof, whatever its live postings say.
    def test_refund_of_a_sale_corrected_into_a_noncash_adjustment_holds(self):
        from bookkeeping.ledger.importer import reverse_and_correct
        from bookkeeping.ledger.model import Entry, Posting
        self.stage(); self.apply(); entry = self.entry(SALE)
        reverse_and_correct(self.entity, SALE, Entry(date=entry.date, narration='Noncash adjustment', meta=(
            ('source-id', 'correction'),), postings=(Posting(base.FEES, Decimal('100'), 'USD'),
                                                     Posting(base.REFUNDS, Decimal('-100'), 'USD'))), 'review')
        self.full_flow(); self.refund_only(); self.stage()
        self.assertHeld(self.plan(), 'sale_corrected', sid=REFUND, amount='20.00', fee=None, net='-20.00')
        self.apply()
        self.assertEqual(Decimal('-100'), self.balances()[base.REFUNDS])

    # P1 dispute_offsets_refunds (rule 2): a dispute credit never raises what may be refunded.
    def add_refund(self, refund_id, movement, amount):
        refund = dict(id=refund_id, status='succeeded', amount=amount, currency='usd', balance_transaction=movement, charge='ch_sale')
        bt = dict(id=movement, created=1768521600, amount=-amount, fee=0, net=-amount, currency='usd', status='available',
                  type='refund', source=refund_id)
        self.raw.setdefault('refunds', [])
        self.raw['refunds'].append(refund); self.raw['balanceTransactions'].append(bt)

    def test_dispute_credit_in_the_same_batch_never_offsets_refunds(self):
        self.full_flow(); self.raw['refunds'] = []; self.raw['balanceTransactions'].pop(1)
        self.add_refund('re_one', 'txn_refund', 8000); self.add_refund('re_two', 'txn_two', 8000)
        self.dispute(); self.raw['balanceTransactions'][-1].update(amount=10000, net=10000, fee=0)
        _, unresolved = self.actions()
        self.assertEqual([dict(source_id=REFUND, reason='refund_exceeds_booked_sale'),
                          dict(source_id=self.TWO, reason='refund_exceeds_booked_sale')], unresolved)
        self.stage(); self.apply()
        self.assertNotIn(base.REFUNDS, self.balances())

    def test_dispute_credit_in_an_earlier_batch_never_offsets_a_later_refund(self):
        self.full_flow(); self.raw['refunds'] = []; self.raw['balanceTransactions'].pop(1)
        self.add_refund('re_one', 'txn_refund', 8000); self.stage(); self.apply()
        self.assertEqual(Decimal('80'), self.balances()[base.REFUNDS])
        self.raw['balanceTransactions'] = []; self.dispute()
        self.raw['balanceTransactions'][-1].update(amount=10000, net=10000, fee=0)
        self.stage(); self.assertEqual(1, self.apply()['posted'])
        self.raw['balanceTransactions'] = []; self.add_refund('re_two', 'txn_two', 8000)
        self.assertIn(dict(source_id=self.TWO, reason='refund_exceeds_booked_sale'), self.plan()['unresolved'])

    def test_held_refunds_count_against_the_cap(self):
        # A refund held in an earlier batch consumed part of the sale too.
        self.stage(); self.apply()
        self.full_flow(); self.refund_only(); self.raw['refunds'][0]['amount'] = 8000
        self.raw['balanceTransactions'][0].update(amount=-8000, net=-8000)
        self.raw['charges'][0]['metadata'] = {'stream': 'service', 'note': 'edited'}  # Holds it: the sale's evidence changed.
        self.stage(); self.apply()
        self.assertEqual('sale_evidence_changed', dict(self.entry(REFUND).meta)['stripe-review'])
        self.raw['charges'][0]['metadata'] = {'stream': 'service'}
        self.raw['balanceTransactions'] = []; self.add_refund('re_two', 'txn_two', 8000)
        self.assertIn(dict(source_id=self.TWO, reason='refund_exceeds_booked_sale'), self.plan()['unresolved'])

    # P1 duplicate_refund (rule 2): one refund ID posts once, whatever movement carries it.
    def test_refund_id_already_posted_never_posts_again(self):
        self.full_flow(); self.stage(); self.apply()
        self.assertEqual('re_one', dict(self.entry(REFUND).meta)['stripe-refund'])
        self.raw['balanceTransactions'] = [dict(self.raw['balanceTransactions'][1], id='txn_replacement')]
        self.raw['refunds'][0]['balance_transaction'] = 'txn_replacement'
        plan = self.stage()
        self.assertEqual(([], [dict(source_id='stripe:acct_test:txn_replacement', reason='refund_already_recorded')]),
                         self.actions(plan))
        self.assertEqual(0, self.apply()['posted'])
        self.assertEqual(Decimal('20'), self.balances()[base.REFUNDS])

    def test_two_movements_of_one_refund_in_a_batch_never_post(self):
        self.full_flow()
        self.raw['balanceTransactions'].append(dict(self.raw['balanceTransactions'][1], id='txn_copy'))
        _, unresolved = self.actions()
        self.assertEqual({REFUND, 'stripe:acct_test:txn_copy'},
                         {row['source_id'] for row in unresolved if row['reason'] == 'duplicate_refund_movement'})

    # P1 plain_price_receipt and P2 plain_charge_price (rule 5): plain-charge price selectors hold for launch.
    def test_plain_charge_price_selector_holds_its_sale_and_refund(self):
        self.full_flow(); self.raw['charges'][0]['price'] = dict(id='price_test', currency='eur', livemode=True)
        self.policy['revenue_rules'] = [dict(id='price', match={'price_id': 'price_test'}, category='Income:Subscriptions')]
        plan = self.plan()
        self.assertHeld(plan, 'plain_charge_price_unsupported')
        self.assertHeld(plan, 'plain_charge_price_unsupported', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_changed_plain_charge_selector_sends_cleanup_to_review(self):
        from bookkeeping import stripe
        self.raw['charges'][0]['price_id'] = 'price_one'
        self.policy['revenue_rules'] = [dict(id=key, match={'price_id': key}, category=account) for key, account in
                                        [('price_one', base.INCOME), ('price_two', 'Income:Subscriptions')]]
        self.assertHeld(self.plan(), 'plain_charge_price_unsupported')
        before = self.stage()['source_capture_digests'][SALE]
        self.interrupted_apply()
        self.raw['charges'][0]['price_id'] = 'price_two'
        self.assertNotEqual(before, self.plan()['source_capture_digests'][SALE])
        self.assertEqual([SALE], [row['source_id'] for row in self.apply()['needs_review']])
        # The fingerprint binds a document field the projection leaves out (the selector) by full content.
        bt = self.raw['balanceTransactions'][0]
        objects = dict(charges={c['id']: c for c in self.raw['charges']}, refunds={}, invoices={}, payouts={}, prices={},
                       products={}, paymentIntents={}, invoicePayments={}, checkoutSessions={}, disputes={},
                       balanceTransactions={bt['id']: bt}, connectEvidence={})
        digest = stripe._source_capture_digest(bt, objects, self.policy)
        self.raw['charges'][0]['price_id'] = 'price_three'
        self.assertNotEqual(digest, stripe._source_capture_digest(bt, objects, self.policy))

    # P2 changed_booked_sale_in_batch (rule 1): a capture that contradicts the booked sale rejects it as proof.
    def test_refund_holds_when_the_captured_sale_movement_changed(self):
        self.stage(); self.apply(); self.full_flow()
        self.raw['balanceTransactions'][0].update(amount=5000, net=4700)
        plan = self.plan()
        self.assertIn(dict(source_id=SALE, reason='changed_source'), plan['unresolved'])
        self.assertHeld(plan, 'sale_not_posted', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_refund_holds_when_the_captured_booked_sale_is_no_longer_available(self):
        self.stage(); self.apply(); self.full_flow()
        self.raw['balanceTransactions'][0]['status'] = 'invalid'
        plan = self.plan()
        self.assertIn(dict(source_id=SALE, reason='already_posted'), plan['skipped'])
        self.assertHeld(plan, 'sale_movement_changed', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    # P2 booked_intent_replaced (rule 1): recorded links must equal the refund's current closure exactly.
    def test_refund_holds_when_its_capture_names_another_payment_intent(self):
        self.raw['charges'][0]['payment_intent'] = dict(id='pi_booked', status='succeeded')
        self.stage(); self.apply()
        self.assertEqual('pi_booked', dict(self.entry(SALE).meta)['stripe-payment-intent'])
        self.full_flow(); self.refund_only()
        self.raw['charges'][0]['payment_intent'] = dict(id='pi_other', status='succeeded')
        self.assertHeld(self.plan(), 'sale_links_changed', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_cross_cohort_refund_of_an_unchanged_engine_sale_posts(self):
        # Rule 1(b) control: a live, never-corrected sale this engine wrote, with matching links and closure digest.
        self.stage(); self.apply()
        self.assertIn('stripe-closure', dict(self.entry(SALE).meta))
        self.full_flow(); self.refund_only()
        self.assertEqual({base.CLEARING: '-20.00', base.REFUNDS: '20.00'}, self.postings(self.plan(), REFUND))

    def test_refund_holds_when_the_sale_evidence_changed_since_it_was_booked(self):
        self.stage(); self.apply(); self.full_flow(); self.refund_only()
        self.raw['charges'][0]['metadata'] = {'stream': 'service', 'note': 'edited'}
        self.assertHeld(self.plan(), 'sale_evidence_changed', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    # P2 foreign_follower (rule 5): a referring document in another mode or currency is not imported.
    def test_foreign_refund_never_links_a_sale_to_an_invoice(self):
        self.invoice(); del self.raw['charges'][0]['invoice']
        self.raw['refunds'] = [dict(id='re_other', charge='ch_sale', payment_intent='pi_one', currency='eur', livemode=True)]
        self.raw['paymentIntents'] = [dict(id='pi_one', invoice='in_one', livemode=False, currency='usd')]
        plan = self.plan()
        self.assertIsNone(self.action(plan))
        self.assertEqual([dict(source_id=SALE, reason='missing_revenue_mapping')], plan['unresolved'])
        # The same follower in the policy's mode and currency still links the invoice.
        self.raw['refunds'][0].update(currency='usd', livemode=False)
        self.assertRevenue(self.plan())

    # P2 conflicting_intent_copies (rule 5): copies that disagree on status invalidate the closure.
    def test_payment_intent_copies_that_disagree_on_status_hold(self):
        self.full_flow(); self.raw['charges'][0]['payment_intent'] = dict(id='pi_one', status='canceled')
        self.raw['paymentIntents'] = [dict(id='pi_one', status='succeeded', metadata={'plan': 'renewal'})]
        self.policy['revenue_rules'] = [dict(id='renewal', match={'metadata': dict(object='payment_intent', key='plan',
                                                                                    value='renewal')}, category='Income:Subscriptions')]
        plan = self.plan()
        self.assertHeld(plan, 'payment_intent_reference_conflict')
        self.assertHeld(plan, 'payment_intent_reference_conflict', sid=REFUND, amount='20.00', fee=None, net='-20.00')
        self.raw['charges'][0]['payment_intent'] = 'pi_one'; self.raw['paymentIntents'][0]['status'] = 'canceled'
        self.assertHeld(self.plan(), 'payment_intent_not_succeeded')

    # P2 sale_status_retry (rule 6): the refund's fingerprint binds its sale's status.
    def test_changed_sale_status_sends_refund_cleanup_to_review(self):
        self.full_flow(); first = self.stage()['source_capture_digests'][REFUND]
        self.interrupted_apply(); self.raw['balanceTransactions'][0]['status'] = 'invalid'
        plan = self.plan()
        self.assertNotEqual(first, plan['source_capture_digests'][REFUND])
        self.assertEqual({SALE, REFUND}, {row['source_id'] for row in self.apply()['needs_review']})

    def test_incomplete_fingerprint_is_unavailable(self):
        from bookkeeping import stripe
        self.invoice(); self.raw['invoices'][0]['total_taxes'] = [{'amount': 100}]
        plan = self.plan()
        self.assertEqual([dict(source_id=SALE, reason='tax_discount_or_credit_requires_review')], plan['unresolved'])
        self.assertEqual(stripe._unavailable_fingerprint(SALE), plan['source_capture_digests'][SALE])

    # Same-batch answer (rule 1a): only an approved sale action with the same charge, intent and invoice proves.
    def test_same_batch_sale_proves_its_refund_only_when_approved_and_identical(self):
        self.full_flow()
        self.assertEqual({base.CLEARING: '-20.00', base.REFUNDS: '20.00'}, self.postings(self.plan(), REFUND))
        rows = self.raw['balanceTransactions']; charge = self.raw['charges'][0]
        charge['payment_intent'] = dict(id='pi_one', status='succeeded')
        rows.append(dict(rows[0], id='txn_two', source='ch_two'))
        self.raw['charges'].append(dict(charge, id='ch_two', balance_transaction='txn_two'))
        self.assertHeld(self.plan(), 'payment_intent_reference_conflict', sid=REFUND, amount='20.00', fee=None, net='-20.00')
        rows.pop(); self.raw['charges'].pop(); rows[0]['status'] = 'invalid'
        self.assertHeld(self.plan(), 'sale_not_posted', sid=REFUND, amount='20.00', fee=None, net='-20.00')
        rows[0]['status'] = 'available'; charge['invoice'] = 'missing'
        self.assertHeld(self.plan(), 'missing_invoice_evidence', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_same_batch_duplicate_of_a_booked_charge_proves_nothing(self):
        self.stage(); self.apply(); self.full_flow()
        self.raw['balanceTransactions'][0]['id'] = 'txn_second'; self.raw['charges'][0]['balance_transaction'] = 'txn_second'
        plan = self.plan()
        self.assertHeld(plan, 'charge_claimed_by_another_movement', sid=self.SECOND)
        self.assertHeld(plan, 'sale_in_holding', sid=REFUND, amount='20.00', fee=None, net='-20.00')

    def test_held_refund_of_an_unrecorded_charge_makes_later_refunds_unproven(self):
        # It consumed some charge's revenue, but the ledger does not say which: no cap can be proved.
        self.stage(); self.apply()
        self.full_flow(); self.refund_only(); self.raw['refunds'] = []
        self.stage(); self.apply()
        self.assertEqual('missing_refund_evidence', dict(self.entry(REFUND).meta)['stripe-review'])
        self.raw['balanceTransactions'] = []; self.add_refund('re_two', 'txn_two', 1000)
        self.assertHeld(self.plan(), 'ownership_unproven', sid=self.TWO, amount='10.00', fee=None, net='-10.00')

    def test_refund_fingerprint_binds_its_sale_leaving_the_capture(self):
        self.full_flow(); first = self.plan()['source_capture_digests'][REFUND]
        self.raw['balanceTransactions'].pop(0)
        self.assertNotEqual(first, self.plan()['source_capture_digests'][REFUND])

    # Clozy Worker contract: no new top-level keys in stage, plan, action or apply.
    def test_worker_keys_are_unchanged(self):
        from bookkeeping import stripe
        del self.raw['charges'][:]
        staged = stripe.stripe_stage(self.entity, self.raw, self.policy)
        plan = staged['plan']; applied = self.apply()
        self.assertEqual(['duplicates', 'plan', 'staged'], sorted(staged))
        self.assertEqual(['actions', 'entry_links', 'evidence_digest', 'ledger_digest', 'next_cursor', 'no_effect',
                          'normalized_sources', 'plan_digest', 'policy_digest', 'skipped', 'source_capture_digests',
                          'uncategorized', 'unresolved'], sorted(plan))
        self.assertEqual(['authority_id', 'kind', 'normalized_source', 'postings', 'review_reason', 'source_digest',
                          'source_id'], sorted(plan['actions'][0]))
        self.assertEqual(['held', 'needs_review', 'next_cursor', 'no_financial_effect', 'plan_digest', 'posted', 'skipped',
                          'uncategorized', 'unresolved'], sorted(applied))


if __name__ == '__main__':
    unittest.main()
