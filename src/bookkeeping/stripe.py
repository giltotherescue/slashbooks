"""Deterministic Stripe clearing proposals under explicit trusted policy.

Balance transaction IDs own financial effects. Charge/invoice/payout objects
provide evidence only. The hosted adapter must authenticate policy and captured
bank matches; source text never supplies authority. No provider writes occur.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from . import queue
from .entity import flat_bank_account_mappings
from .ledger.importer import (_atomic_ledger_write_unlocked, _entity_write_lock, _find_live_entry,
                             _load_pending_categorization, import_transactions)
from .ledger.model import Entry, Posting
from .ledger.staging import StagingStore
from .ledger.store import LedgerStore, default_store_path
from .routine import _state, routine_source_digest
from .source_ids import source_id_variants


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _id(value, label):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError(f'Invalid {label}.')
    return value


def _minor(value):
    if type(value) is not int or abs(value) > 10**15:
        raise ValueError('Stripe amounts must be bounded integer minor units.')
    return value


def _amount(value):
    return f'{Decimal(_minor(value)) / 100:.2f}'


def _object_id(value):
    return value.get('id') if isinstance(value, dict) else value


def _objects(evidence, name):
    rows = evidence.get(name, [])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f'{name} must be an array of objects.')
    result = {}
    for row in rows:
        sid = _id(row.get('id'), name + ' ID')
        if sid in result:
            raise ValueError(f'Duplicate {name} ID.')
        result[sid] = row
    return result


def _policy(policy, opens, book='USD', account_book=None):
    if not isinstance(policy, dict) or type(policy.get('version')) is not int or policy['version'] < 1 or type(policy.get('enabled')) is not bool:
        raise ValueError('Stripe policy requires a positive version and enabled boolean.')
    _id(policy.get('stripe_account_id'), 'Stripe account')
    if type(policy.get('livemode')) is not bool or policy.get('currency') != book:
        raise ValueError(f'Stripe policy requires explicit mode and {book} currency.')
    roles = {'clearing_account': 'Assets:Processors:', 'payout_transit_account': 'Assets:Transfers:',
             'fee_account': 'Expenses:', 'refund_account': 'Income:'}
    if policy.get('reserve_account') is not None:
        roles['reserve_account'] = 'Assets:Processors:'
    for field, prefix in roles.items():
        account = policy.get(field)
        if not isinstance(account, str) or account not in opens or not account.startswith(prefix):
            raise ValueError(f'{field} must be a catalog {prefix[:-1]} account.')
        if opens[account].currencies and (account_book or book) not in opens[account].currencies:
            raise ValueError(f'{field} must support {book}.')
    if len({policy[field] for field in roles}) != len(roles):
        raise ValueError('Stripe account roles require distinct accounts.')
    if STRIPE_HOLDING_ACCOUNT in {policy[field] for field in roles}:
        raise ValueError(f'{STRIPE_HOLDING_ACCOUNT} holds Stripe activity for review; it cannot be a policy account.')
    transfer_rules = policy.get('transfer_rules', [])
    if not isinstance(transfer_rules, list) or len(transfer_rules) > 100:
        raise ValueError('Stripe transfer_rules must be a bounded array.')
    transfer_ids = set()
    for rule in transfer_rules:
        if not isinstance(rule, dict) or _id(rule.get('id'), 'transfer rule ID') in transfer_ids:
            raise ValueError('Transfer rules require unique IDs.')
        transfer_ids.add(rule['id'])
        match = rule.get('match')
        if not isinstance(match, dict) or len(match) != 1 or next(iter(match)) not in {'transfer_id', 'destination'}:
            raise ValueError('Transfer rules require one exact transfer or destination.')
        field, value = next(iter(match.items()))
        if not _id(value, field).startswith('tr_' if field == 'transfer_id' else 'acct_'):
            raise ValueError('Invalid transfer selector.')
        category = rule.get('category')
        if (category not in opens or not category.startswith(('Expenses:', 'Income:', 'Liabilities:'))
                or category in {policy[k] for k in roles} or category == STRIPE_HOLDING_ACCOUNT):
            raise ValueError('Transfer rules require a separate expense, income or liability account.')
        note = rule.get('accounting_note')
        if not isinstance(note, str) or not note.strip() or len(note) > 1000:
            raise ValueError('Transfer rules require the confirmed accounting treatment.')
    if policy['clearing_account'].startswith('Assets:Bank:') or policy['payout_transit_account'].startswith('Assets:Bank:'):
        raise ValueError('Processor and payout transit accounts cannot be bank cash accounts.')
    rules = policy.get('revenue_rules')
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError('Stripe revenue_rules must be a bounded array.')
    ids = set()
    for rule in rules:
        if not isinstance(rule, dict) or _id(rule.get('id'), 'rule ID') in ids:
            raise ValueError('Stripe rules require unique IDs.')
        ids.add(rule['id'])
        category, match = rule.get('category'), rule.get('match')
        if (category not in opens or not category.startswith('Income:') or category == policy['refund_account']
                or category == STRIPE_HOLDING_ACCOUNT):
            raise ValueError('Revenue rules require a distinct catalog income account.')
        if not isinstance(match, dict) or len(match) != 1:
            raise ValueError('Each revenue rule requires one exact selector.')
        field, value = next(iter(match.items()))
        if field in {'price_id', 'product_id'}:
            _id(value, field)
        elif field == 'metadata':
            if not isinstance(value, dict) or set(value) != {'object', 'key', 'value'} or value['object'] not in {'charge', 'payment_intent', 'invoice', 'invoice_line', 'price', 'product'}:
                raise ValueError('Metadata selectors require an explicit object, key and value.')
            _id(value['key'], 'metadata key'); _id(value['value'], 'metadata value')
        else:
            raise ValueError('Only price, product, or exact metadata selectors are supported.')


def _revenue(item, policy, context=None):
    context = context or {}
    price = item.get('price') or (item.get('pricing') or {}).get('price_details') or {}
    if isinstance(price, str):
        price = {'id': price}
    price_id = price.get('id') or price.get('price') or item.get('price_id')
    product_id = (_object_id(price.get('product')) or item.get('product_id')
                  or _object_id((context.get('price') or {}).get('product')))
    matches = []
    for rule in policy['revenue_rules']:
        field, value = next(iter(rule['match'].items()))
        if ((field == 'price_id' and price_id == value) or
                (field == 'product_id' and product_id == value) or
                (field == 'metadata' and (context.get(value['object'], {}).get('metadata') or {}).get(value['key']) == value['value'])):
            matches.append(rule)
    if len(matches) != 1:
        raise ValueError('ambiguous_revenue_mapping' if matches else 'missing_revenue_mapping')
    return matches[0]


def _transfer_treatment(bt, connect, policy, cur):
    """Validate provider relationships; only trusted rules choose the account."""
    transfers = _objects(connect, 'transfers')
    reversals = _objects(connect, 'transferReversals')
    source = _object_id(bt.get('source'))
    reversal = None
    if bt['type'] == 'transfer_refund':
        matches = [row for row in reversals.values()
                   if _object_id(row.get('balance_transaction')) == bt['id']]
        if len(matches) != 1:
            raise ValueError('missing_transfer_reversal_evidence')
        reversal = matches[0]
        parent = _object_id(reversal.get('transfer'))
        if (source not in {parent, reversal['id']} or reversal.get('object') != 'transfer_reversal'
                or reversal.get('currency') != cur or type(reversal.get('amount')) is not int
                or reversal['amount'] != bt['amount'] or bt['amount'] <= 0):
            raise ValueError('transfer_reversal_identity_conflict')
    else:
        parent = source
    transfer = transfers.get(parent)
    if (not transfer or transfer.get('object') != 'transfer' or transfer.get('livemode') is not policy['livemode']
            or transfer.get('currency') != cur or type(transfer.get('amount')) is not int or transfer['amount'] <= 0
            or not isinstance(_object_id(transfer.get('destination')), str)
            or not _object_id(transfer['destination']).startswith('acct_') or bt['fee'] != 0):
        raise ValueError('missing_or_invalid_transfer_evidence')
    if reversal:
        if reversal['amount'] > transfer['amount']:
            raise ValueError('transfer_reversal_exceeds_original')
    elif _object_id(transfer.get('balance_transaction')) != bt['id'] or transfer['amount'] != -bt['amount']:
        raise ValueError('transfer_identity_conflict')
    matches = [rule for rule in policy.get('transfer_rules', [])
               if rule['match'].get('transfer_id') == parent
               or rule['match'].get('destination') == _object_id(transfer['destination'])]
    if len(matches) != 1:
        raise ValueError('ambiguous_transfer_mapping' if matches else 'missing_transfer_mapping')
    return transfer, reversal, matches[0]


# "Uncategorized Stripe activity": a sale, refund or dispute the engine cannot tie to one charge and one invoice
# with certainty. Its processor side still posts (the Stripe balance did move); the other side waits here, and
# the entry carries a ``stripe-review`` reason for a person. The engine opens it the first time it is needed.
# When later evidence proves the movement, a separate reclassification entry moves the amount out; the original
# entry is never edited or deleted.
STRIPE_HOLDING_ACCOUNT = 'Income:Uncategorized:Stripe'
STRIPE_HOLDING_AUTHORITY = 'uncategorized-stripe-activity'
STRIPE_RECLASSIFY_PREFIX = 'stripe-reclassify:'


class _Hold(ValueError):
    """The engine cannot place this activity with certainty; post it to the holding account for review."""


class _Conflict(Exception):
    """Two copies of one provider object disagree."""


_REFERENCE_FIELDS = frozenset({'invoice', 'payment_intent', 'balance_transaction', 'charge', 'product', 'price'})
_ATOMIC_FIELDS = frozenset({'metadata'})
_CHARGE_FIELDS = ('id', 'livemode', 'status', 'paid', 'captured', 'amount', 'currency', 'balance_transaction',
                  'invoice', 'payment_intent', 'metadata')
_INTENT_FIELDS = ('id', 'invoice', 'metadata', 'amount', 'currency', 'livemode')
_REFUND_FIELDS = ('id', 'status', 'amount', 'currency', 'balance_transaction', 'charge', 'payment_intent', 'livemode')
_DISPUTE_FIELDS = ('id', 'charge', 'payment_intent', 'amount', 'currency', 'livemode')
_INVOICE_TOTAL_FIELDS = ('total_tax_amounts', 'total_taxes', 'total_discount_amounts', 'shipping_cost',
                         'pre_payment_credit_notes_amount', 'post_payment_credit_notes_amount', 'starting_balance',
                         'ending_balance')
_INVOICE_FIELDS = ('id', 'status', 'amount_paid', 'amount_due', 'total', 'currency', 'livemode', 'metadata',
                   'lines') + _INVOICE_TOTAL_FIELDS
_SALE_KINDS = frozenset({'charge', 'payment'})
_REFUND_KINDS = frozenset({'refund', 'payment_refund'})


def _comparable(field, value):
    return _object_id(value) if field in _REFERENCE_FIELDS else value


def _row_ids(rows):
    ids = [row.get('id') if isinstance(row, dict) else None for row in rows]
    return ids if all(isinstance(sid, str) and sid for sid in ids) and len(set(ids)) == len(ids) else None


def _merge_rows(first, second, first_complete, second_complete):
    """Merge two lists of the same rows; a complete list must hold every row of the other copy."""
    ids_a, ids_b = _row_ids(first), _row_ids(second)
    if ids_a is None or ids_b is None:
        if first_complete != second_complete or len(first) != len(second):
            raise _Conflict
        return [_deep(a, b) for a, b in zip(first, second)], first_complete
    set_a, set_b = set(ids_a), set(ids_b)
    if ((first_complete and second_complete and set_a != set_b)
            or (first_complete and not set_b <= set_a) or (second_complete and not set_a <= set_b)):
        raise _Conflict
    rows_a = dict(zip(ids_a, first)); rows_b = dict(zip(ids_b, second))
    order = ids_a if first_complete or not second_complete else ids_b
    order = order + [sid for sid in (ids_b if order is ids_a else ids_a) if sid not in set(order)]
    merged = [_deep(rows_a[sid], rows_b[sid]) if sid in rows_a and sid in rows_b else rows_a.get(sid, rows_b.get(sid))
              for sid in order]
    return merged, first_complete or second_complete


def _deep(first, second, key=None):
    """One value from two copies that must agree; a copy may add what the other leaves out."""
    if key in _ATOMIC_FIELDS:
        if first != second:
            raise _Conflict
        return first
    if key in _REFERENCE_FIELDS and (isinstance(first, str) or isinstance(second, str)):
        if _object_id(first) != _object_id(second):
            raise _Conflict
        return first if isinstance(first, dict) else second
    if isinstance(first, dict) and isinstance(second, dict):
        if (isinstance(first.get('data'), list) and isinstance(second.get('data'), list)
                and 'has_more' in first and 'has_more' in second):
            rows, complete = _merge_rows(first['data'], second['data'], first['has_more'] is False,
                                         second['has_more'] is False)
            merged = _deep({k: v for k, v in first.items() if k not in {'data', 'has_more'}},
                           {k: v for k, v in second.items() if k not in {'data', 'has_more'}})
            return {**merged, 'data': rows, 'has_more': not complete}
        merged = dict(first)
        for name, value in second.items():
            merged[name] = _deep(first[name], value, name) if name in first else value
        return merged
    if isinstance(first, list) and isinstance(second, list):
        return _merge_rows(first, second, True, True)[0]
    if type(first) is not type(second) or first != second:
        raise _Conflict
    return first


def _docs(refs, captured, label):
    """The ID and every copy (expanded copies, then the captured one) of the one object that *refs* name."""
    refs = [ref for ref in refs if ref not in (None, '')]
    for ref in refs:
        if not isinstance(ref, (dict, str)) or (isinstance(ref, dict) and not (isinstance(ref.get('id'), str) and ref['id'])):
            raise _Hold(f'{label}_identity_unverified')
    ids = {_object_id(ref) for ref in refs}
    if len(ids) > 1:
        raise _Hold(f'{label}_reference_conflict')
    if not ids:
        return None, []
    identity = ids.pop()
    docs = [ref for ref in refs if isinstance(ref, dict)]
    if captured.get(identity) is not None:
        docs.append(captured[identity])
    return identity, docs


def _combine(docs, label, fields):
    """One view of every copy. Copies must agree on each field the engine reads, nested rows included;
    a field that only one copy has is kept. Other fields come from the captured copy first."""
    view = {}
    for doc in reversed(docs):
        for name, value in doc.items():
            view.setdefault(name, value)
    for field in fields:
        values = [doc[field] for doc in docs if field in doc]
        if len(values) < 2:
            continue
        try:
            if field in _REFERENCE_FIELDS:
                # The referenced object is resolved on its own, from every copy; here only its identity counts.
                if len({_object_id(value) for value in values}) != 1:
                    raise _Conflict
                view[field] = next((value for value in values if isinstance(value, dict)), values[0])
            else:
                merged = values[0]
                for value in values[1:]:
                    merged = _deep(merged, value, field)
                view[field] = merged
        except _Conflict:
            raise _Hold(f'{label}_reference_conflict') from None
    return view


class _Resolver:
    """Which charge, payment intent, invoice or Checkout session a movement belongs to, from every copy.

    ``_Hold`` means the identity is uncertain (the movement posts to the holding account); any other
    ``ValueError`` means a known sale whose allocation or settlement is not complete (it stays unposted).
    """

    def __init__(self, policy, bts, objects, sale_held=None):
        self.policy, self.mode, self.cur = policy, policy['livemode'], policy['currency'].lower()
        self.bts = bts
        self.charges, self.refunds, self.invoices = objects['charges'], objects['refunds'], objects['invoices']
        self.prices, self.products, self.intents = objects['prices'], objects['products'], objects['paymentIntents']
        self.payments, self.sessions, self.disputes = objects['invoicePayments'], objects['checkoutSessions'], objects['disputes']
        self.sale_held = sale_held or (lambda sale_bt: None)

    def check_mode(self, doc):
        if doc.get('livemode') is not None and doc.get('livemode') is not self.mode:
            raise _Hold('livemode_conflict')

    def charge(self, refs):
        """The combined charge, its copies, and the one sale movement of it in the cohort (or None)."""
        identity, docs = _docs(refs, self.charges, 'charge')
        if identity is None or not docs:
            return None, docs, None
        sales = sorted(sid for sid, row in self.bts.items()
                       if row.get('type') in _SALE_KINDS and _object_id(row.get('source')) == identity)
        identity, docs = _docs(list(refs) + [self.bts[sid]['source'] for sid in sales], self.charges, 'charge')
        charge = _combine(docs, 'charge', _CHARGE_FIELDS)
        self.check_mode(charge)
        if charge.get('livemode') is not self.mode:
            raise _Hold('charge_identity_unverified')
        linked = _object_id(charge.get('balance_transaction'))
        if len(sales) > 1 or (sales and linked and linked != sales[0]) or (
                linked in self.bts and _object_id(self.bts[linked].get('source')) != identity):
            raise _Hold('charge_reference_conflict')
        return charge, docs, linked or (sales[0] if sales else None)

    def document(self, charge, charge_docs, gross):
        """``(invoice or Checkout view, has_document)`` for one combined charge."""
        intent_id = _object_id(charge.get('payment_intent'))
        sessions, payments, intent_docs = [], [], []
        if intent_id:
            sessions = [row for row in self.sessions.values() if _object_id(row.get('payment_intent')) == intent_id]
            payments = [row for row in self.payments.values()
                        if (row.get('payment') or {}).get('type') == 'payment_intent'
                        and _object_id((row.get('payment') or {}).get('payment_intent')) == intent_id]
            _, intent_docs = _docs([doc.get('payment_intent') for doc in charge_docs]
                                   + [row.get('payment_intent') for row in sessions]
                                   + [(row.get('payment') or {}).get('payment_intent') for row in payments],
                                   self.intents, 'payment_intent')
            if intent_docs:
                self.check_mode(_combine(intent_docs, 'payment_intent', _INTENT_FIELDS))
        if len(sessions) > 1:
            raise _Hold('multiple_checkout_sessions')
        for row in sessions + payments:
            self.check_mode(row)
        if len({_object_id(row.get('invoice')) for row in payments if row.get('invoice')}) > 1:
            raise _Hold('invoice_reference_conflict')
        if payments:
            row = payments[0]
            if (len(payments) != 1 or row.get('status') != 'paid' or row.get('amount_paid') != gross
                    or row.get('amount_requested') != gross or row.get('currency') != self.cur
                    or row.get('livemode') is not self.mode):
                raise ValueError('partial_or_multiple_invoice_payments')
            if not row.get('invoice'):
                raise _Hold('invoice_identity_unverified')
        identity, docs = _docs([doc.get('invoice') for doc in charge_docs + intent_docs]
                               + [row.get('invoice') for row in payments + sessions], self.invoices, 'invoice')
        if identity is not None:
            if not docs:
                raise _Hold('missing_invoice_evidence')
            invoice = _combine(docs, 'invoice', _INVOICE_FIELDS)
            self.check_mode(invoice)
            return invoice, True
        if intent_id and not intent_docs and not payments and not sessions:
            # A named payment intent that the evidence does not hold is no proof of a sale without an invoice.
            raise _Hold('missing_payment_intent_evidence')
        if not sessions:
            return None, False
        session = sessions[0]
        if (session.get('payment_status') != 'paid' or session.get('status') != 'complete'
                or session.get('amount_total') != gross or session.get('amount_subtotal') != gross
                or session.get('currency') != self.cur):
            raise ValueError('partial_or_adjusted_checkout')
        if any((session.get('total_details') or {}).get(key) for key in ('amount_tax', 'amount_discount', 'amount_shipping')):
            raise ValueError('tax_discount_or_credit_requires_review')
        lines = session.get('line_items')
        if not isinstance(lines, dict) or lines.get('has_more') is not False or not isinstance(lines.get('data'), list):
            raise ValueError('incomplete_checkout_lines')
        converted = []
        for row in lines['data']:
            if (not isinstance(row, dict) or row.get('amount_total') != row.get('amount_subtotal')
                    or row.get('amount_tax') or row.get('amount_discount')):
                raise ValueError('tax_discount_or_credit_requires_review')
            converted.append({**row, 'amount': row.get('amount_subtotal')})
        # Reuse exact line allocation checks; this is an internal view, never a
        # fabricated invoice saved as provider evidence.
        return dict(status='paid', amount_paid=gross, amount_due=gross, total=gross,
                    lines=dict(has_more=False, data=converted)), True

    def allocations(self, charge, charge_docs, gross):
        policy, cur = self.policy, self.cur
        if charge.get('status') != 'succeeded' or charge.get('paid') is not True or charge.get('captured') is not True:
            raise ValueError('unsettled_charge')
        if _minor(charge.get('amount')) != gross or charge.get('currency') != cur:
            raise ValueError('charge_amount_conflict')
        invoice, has_invoice = self.document(charge, charge_docs, gross)
        _, intent_docs = _docs([doc.get('payment_intent') for doc in charge_docs], self.intents, 'payment_intent')
        context = {'charge': charge, 'payment_intent': _combine(intent_docs, 'payment_intent', _INTENT_FIELDS)}
        if not has_invoice:
            rule = _revenue(charge, policy, context)
            return [(rule['category'], gross, rule['id'])]
        lines = invoice.get('lines')
        if (not isinstance(lines, dict) or lines.get('has_more') is not False
                or not isinstance(lines.get('data'), list) or not 1 <= len(lines['data']) <= 100):
            raise ValueError('incomplete_invoice_lines')
        if invoice.get('status') != 'paid' or invoice.get('amount_paid') != gross or invoice.get('amount_due') != gross or invoice.get('total') != gross:
            raise ValueError('partial_or_adjusted_invoice')
        if any(invoice.get(field) for field in _INVOICE_TOTAL_FIELDS):
            raise ValueError('tax_discount_or_credit_requires_review')
        allocations = []
        for line in lines['data']:
            if not isinstance(line, dict) or line.get('currency') != cur or _minor(line.get('amount')) <= 0:
                raise ValueError('unsupported_invoice_line')
            if any(line.get(field) for field in ('discount_amounts', 'taxes', 'tax_amounts', 'pretax_credit_amounts')):
                raise ValueError('tax_discount_or_credit_requires_review')
            parent = line.get('parent') or {}
            if line.get('proration') or any((parent.get(key) or {}).get('proration') for key in ('invoice_item_details', 'subscription_item_details')):
                raise ValueError('proration_requires_review')
            raw_price = line.get('price') or (line.get('pricing') or {}).get('price_details') or {}
            price_id = raw_price if isinstance(raw_price, str) else raw_price.get('id') or raw_price.get('price')
            price = self.prices.get(price_id) or (raw_price if isinstance(raw_price, dict) else {})
            product = self.products.get(_object_id(price.get('product')), {})
            rule = _revenue(line, policy, {**context, 'invoice': invoice, 'invoice_line': line, 'price': price, 'product': product})
            allocations.append((rule['category'], _minor(line['amount']), rule['id']))
        if sum(amount for _, amount, _ in allocations) != gross:
            raise ValueError('invoice_allocation_mismatch')
        return allocations

    def sale(self, bt):
        charge, docs, _ = self.charge([bt.get('source')])
        if charge is None:
            raise _Hold('missing_charge_evidence')
        if _object_id(charge.get('balance_transaction')) != bt['id']:
            raise _Hold('charge_identity_conflict')
        return self.allocations(charge, docs, bt['amount'])

    def follow(self, docs, label, missing):
        """The charge a refund or dispute belongs to, with the sale's complete evidence: ``(charge, docs, gross, held)``.

        ``held`` is the reason the sale itself sits in the holding account, if it does.
        """
        charge, charge_docs, sale_bt = self.charge([doc.get('charge') for doc in docs])
        if charge is None:
            raise _Hold(missing)
        own_intents = {_object_id(doc.get('payment_intent')) for doc in docs if doc.get('payment_intent')}
        charge_intent = _object_id(charge.get('payment_intent'))
        if len(own_intents) > 1 or (own_intents and charge_intent and own_intents != {charge_intent}):
            raise _Hold(f'{label}_reference_conflict')
        return charge, charge_docs, _minor(charge.get('amount')), (self.sale_held(sale_bt) if sale_bt else None)

    def refund(self, bt):
        _, docs = _docs([bt.get('source')], self.refunds, 'refund')
        if not docs:
            raise _Hold('missing_refund_evidence')
        refund = _combine(docs, 'refund', _REFUND_FIELDS)
        self.check_mode(refund)
        linked = _object_id(refund.get('balance_transaction'))
        if linked is not None and linked != bt['id']:
            raise _Hold('refund_reference_conflict')
        if (refund.get('status') != 'succeeded' or refund.get('currency') != self.cur
                or refund.get('amount') != -bt['amount'] or linked is None):
            raise ValueError('missing_or_unsettled_refund')
        charge, charge_docs, gross, held = self.follow(docs, 'refund', 'missing_refund_charge_evidence')
        # A refund follows its sale: the same identity checks first, then the sale's posted treatment.
        invoice, has_invoice = self.document(charge, charge_docs, gross)
        if held:
            raise _Hold(held)
        if has_invoice and (not invoice or not isinstance(invoice.get('lines'), dict)
                            or invoice['lines'].get('has_more') is not False
                            or len(invoice['lines'].get('data') or []) != 1):
            raise ValueError('mixed_line_refund_requires_allocation')
        # A single line still can contain tax, discounts, credits or
        # partial payment. Do not turn a refused original sale into an
        # apparently safe full contra-revenue refund.
        self.allocations(charge, charge_docs, gross)

    def dispute(self, bt):
        """A dispute always waits in the holding account: no trusted policy names a dispute account.

        It follows its charge first, so a dispute on a charge the engine cannot place carries that charge's reason.
        """
        _, docs = _docs([bt.get('source')], self.disputes, 'dispute')
        if not docs:
            raise _Hold('missing_dispute_evidence')
        dispute = _combine(docs, 'dispute', _DISPUTE_FIELDS)
        self.check_mode(dispute)
        if dispute.get('currency') not in (None, self.cur) or any(
                isinstance(doc.get('balance_transactions'), list)
                and bt['id'] not in {_object_id(row) for row in doc['balance_transactions']} for doc in docs):
            raise _Hold('dispute_reference_conflict')
        charge, charge_docs, gross, held = self.follow(docs, 'dispute', 'missing_dispute_charge_evidence')
        try:
            self.document(charge, charge_docs, gross)
        except _Hold:
            raise
        except (ValueError, KeyError, TypeError, ArithmeticError):
            pass  # The charge's sale has its own hold; the dispute still waits for review.
        raise _Hold(held or 'dispute_requires_review')

    def holding(self, bt):
        postings = [(self.policy['clearing_account'], bt['net']), (STRIPE_HOLDING_ACCOUNT, -bt['amount'])]
        if bt['fee']:
            postings.append((self.policy['fee_account'], bt['fee']))
        return postings

    def place(self, bt):
        """``(postings, authority, review_reason)`` for a sale, refund or dispute movement.

        A ``_Hold`` becomes holding-account postings with its reason; other errors propagate (unposted).
        """
        policy = self.policy
        postings = [(policy['clearing_account'], bt['net'])]
        try:
            if bt.get('type') in _SALE_KINDS:
                allocations = self.sale(bt)
                postings.extend((category, -amount) for category, amount, _ in allocations)
                authority = sorted({rule for _, _, rule in allocations})
            elif bt.get('type') in _REFUND_KINDS:
                self.refund(bt)
                postings.append((policy['refund_account'], -bt['amount']))
                authority = ['refund-account-policy']
            else:
                self.dispute(bt)
        except _Hold as hold:
            return self.holding(bt), [STRIPE_HOLDING_AUTHORITY], str(hold)
        if bt['fee']:
            postings.append((policy['fee_account'], bt['fee']))
        return postings, authority, None


def _is_dispute(source):
    identity = _object_id(source)
    return ((isinstance(source, dict) and source.get('object') == 'dispute')
            or (isinstance(identity, str) and identity.startswith(('du_', 'dp_'))))


def _identity_movement(bt):
    kind = bt.get('type')
    return ((kind in _SALE_KINDS and bt['amount'] > 0) or (kind in _REFUND_KINDS and bt['amount'] < 0)
            or (kind == 'adjustment' and _is_dispute(bt.get('source'))))


def _financial(bt, account, mode):
    return dict(account=account, livemode=mode, id=bt['id'], type=bt.get('type'),
                amount=bt['amount'], fee=bt['fee'], net=bt['net'], currency=bt['currency'],
                created=bt['created'], source=_object_id(bt.get('source')))


def stripe_preview(evidence):
    """Pure discovery totals in minor units; no company/policy is required.

    These are captured processor movements, not bank cash or a revenue forecast.
    Other-currency values retain explicit minor units without assuming 2 decimals.
    """
    if not isinstance(evidence, dict) or evidence.get('source') != 'stripe':
        raise ValueError('Expected Stripe evidence.')
    bts = _objects(evidence, 'balanceTransactions')
    totals, unsupported = {}, []
    for bt in bts.values():
        amount, fee, net = (_minor(bt.get(field)) for field in ('amount', 'fee', 'net'))
        if amount - fee != net:
            raise ValueError('Stripe amount minus fee must equal net.')
        currency = _id(bt.get('currency'), 'currency').upper()
        row = totals.setdefault(currency, dict(currency=currency, units='minor', gross_sales=0, refunds=0,
                                               fees=0, payouts=0, net_balance_change=0, count=0))
        row['count'] += 1; row['net_balance_change'] += net; row['fees'] += fee
        kind = bt.get('type')
        if kind in {'charge', 'payment'} and amount > 0:
            row['gross_sales'] += amount
        elif kind in {'refund', 'payment_refund'} and amount < 0:
            row['refunds'] -= amount
        elif kind in {'stripe_fee', 'stripe_fx_fee'} and net != 0:
            row['fees'] -= amount
        elif (kind == 'reserve_transaction' and amount != 0 and fee == 0
              or kind == 'payout_minimum_balance_hold' and amount < 0 and fee == 0
              or kind == 'payout_minimum_balance_release' and amount > 0 and fee == 0):
            pass  # Asset movements do not change sales, refunds, fees or payouts.
        elif kind == 'payout' and amount <= 0 and fee >= 0:
            row['payouts'] -= amount
        elif kind in {'transfer', 'transfer_refund'} and fee == 0 and (
                kind == 'transfer' and amount < 0 or kind == 'transfer_refund' and amount > 0):
            pass  # Accounting treatment comes from an explicit transfer rule.
        else:
            unsupported.append(dict(balance_transaction_id=bt['id'], type=kind, currency=currency))
    metadata = []
    names = {'charges': 'charge', 'paymentIntents': 'payment_intent', 'invoices': 'invoice',
             'prices': 'price', 'products': 'product', 'invoiceLines': 'invoice_line'}
    for name, kind in names.items():
        for item in _objects(evidence, name).values():
            for key, value in (item.get('metadata') or {}).items():
                if isinstance(key, str) and isinstance(value, str) and key and value and len(key) <= 200 and len(value) <= 200:
                    metadata.append(dict(object=kind, key=key, value=value, source_id=item['id']))
    connect = evidence.get('connectEvidence') or {}
    transfer_options = [{key: row.get(key) for key in ('id', 'destination', 'source_transaction',
                        'balance_transaction', 'amount', 'currency')}
                        for row in _objects(connect, 'transfers').values()]
    return dict(stripe_account_id=(evidence.get('account') or {}).get('id'),
                livemode=evidence.get('livemode', (evidence.get('account') or {}).get('livemode')),
                coverage=evidence.get('coverage'), transaction_count=len(bts), balance_transaction_ids=sorted(bts),
                by_currency=[totals[key] for key in sorted(totals)], unsupported=unsupported,
                metadata_options=metadata[:500], prices=list(_objects(evidence, 'prices').values())[:500],
                products=list(_objects(evidence, 'products').values())[:500],
                transfer_options=transfer_options[:500])


def stripe_merge_previews(previous, current):
    """Combine disjoint captured cohorts; never count a provider root twice."""
    fields = ('gross_sales', 'refunds', 'fees', 'payouts', 'net_balance_change', 'count')
    def validate(summary):
        if not isinstance(summary, dict): raise ValueError('Expected processor preview.')
        _id(summary.get('stripe_account_id'), 'Stripe account')
        if type(summary.get('livemode')) is not bool: raise ValueError('Preview requires explicit mode.')
        ids = summary.get('balance_transaction_ids')
        if not isinstance(ids, list) or len(ids) > 100000 or any(not isinstance(sid, str) for sid in ids) or len(set(ids)) != len(ids):
            raise ValueError('Preview requires distinct balance transaction IDs.')
        for sid in ids: _id(sid, 'balance transaction ID')
        if type(summary.get('transaction_count')) is not int or summary['transaction_count'] != len(ids):
            raise ValueError('Preview count must match captured IDs.')
        rows = summary.get('by_currency')
        if not isinstance(rows, list): raise ValueError('Preview currencies must be an array.')
        currencies, count = set(), 0
        for row in rows:
            if (not isinstance(row, dict) or row.get('units') != 'minor' or not isinstance(row.get('currency'), str)
                    or len(row['currency']) != 3 or not row['currency'].isascii() or not row['currency'].isalpha()
                    or row['currency'] != row['currency'].upper() or row['currency'] in currencies):
                raise ValueError('Preview currencies require distinct explicit minor units.')
            currencies.add(row['currency'])
            for field in fields: _minor(row.get(field))
            if row['count'] < 0 or any(row[field] < 0 for field in ('gross_sales', 'refunds', 'payouts')):
                raise ValueError('Invalid processor preview totals.')
            count += row['count']
        if count != len(ids): raise ValueError('Currency counts must match captured IDs.')
        return set(ids)
    old_ids, new_ids = validate(previous), validate(current)
    if (previous['stripe_account_id'], previous['livemode']) != (current['stripe_account_id'], current['livemode']):
        raise ValueError('Cannot merge different Stripe accounts or modes.')
    if old_ids & new_ids: raise ValueError('Preview cohorts overlap; do not count a balance transaction twice.')
    totals = {}
    for summary in (previous, current):
        for row in summary['by_currency']:
            merged = totals.setdefault(row['currency'], dict(currency=row['currency'], units='minor', **{field: 0 for field in fields}))
            for field in fields: merged[field] = _minor(merged[field] + row[field])
    result = dict(current, transaction_count=len(old_ids | new_ids), balance_transaction_ids=sorted(old_ids | new_ids),
                  by_currency=[totals[key] for key in sorted(totals)])
    for field, identity in (('prices', 'id'), ('products', 'id'), ('transfer_options', 'id'), ('unsupported', 'balance_transaction_id')):
        rows = {}
        for summary in (previous, current):
            discovered = summary.get(field, [] if field == 'transfer_options' else None)
            if not isinstance(discovered, list): raise ValueError('Invalid preview discovery array.')
            for row in discovered:
                if not isinstance(row, dict): raise ValueError('Invalid preview discovery row.')
                key = _id(row.get(identity), identity)
                if key in rows and rows[key] != row: raise ValueError('Conflicting preview discovery evidence.')
                rows[key] = row
        result[field] = list(rows.values())
    metadata = {_digest(row): row for summary in (previous, current) for row in summary.get('metadata_options', [])}
    result['metadata_options'] = list(metadata.values())[:500]
    return result


# The accounts that stripe_prepare opens. engine_accounts.engine_owned_accounts() lists them too.
STRIPE_ACCOUNTS = dict(clearing_account='Assets:Processors:Stripe',
                       payout_transit_account='Assets:Transfers:Stripe-Payouts',
                       reserve_account='Assets:Processors:Stripe-Reserved',
                       fee_account='Expenses:Payment-Processing:Stripe', refund_account='Income:Refunds:Stripe')


def stripe_prepare(entity, *, opening_date):
    """Human-authorized catalog setup using normal audited account creation.

    Creates no journals, source mappings, revenue rules, or opening balances.
    The host obtains the explicit cutover/opening date and publishes via CAS.
    """
    from .entity import add_account
    when = date.fromisoformat(opening_date)
    if when.isoformat() != opening_date:
        raise ValueError('Stripe opening_date must use YYYY-MM-DD.')
    accounts = dict(STRIPE_ACCOUNTS)
    store = LedgerStore(default_store_path(entity.path))
    opened = {item.account: item for item in store.load_opens()}
    book = entity.currency
    for name in accounts.values():
        if name in opened and ((opened[name].currencies and book not in opened[name].currencies) or opened[name].date > when):
            raise ValueError('Existing Stripe account currency/opening date conflicts; review the catalog explicitly.')
    with store.connection() as conn:
        typed_catalog = store.has_financial_kinds(conn)
    created, existing = [], []
    for role, name in accounts.items():
        if name in opened:
            existing.append(name)
        else:
            cash = role in {"clearing_account", "payout_transit_account", "reserve_account"}
            add_account(entity.path, name, open_date=opening_date, currency=book, open_date_source="import",
                        financial_kind="cash" if typed_catalog and cash else None)
            created.append(name)
    return dict(accounts=accounts, created=created, existing=existing, opening_date=opening_date,
                opening_balance_supplied=False, policy_approved=False)


def stripe_configure(entity, policy):
    """Validate and map only under the host's explicit human policy approval.

    The host publishes this candidate and its D1 policy in one transaction.
    Processor/transit roles cannot reuse another feed's mapped asset account.
    """
    from .entity import map_bank_account
    _, opens, _, _, _ = _state(entity)
    _policy(policy, opens, entity.currency)
    mappings = flat_bank_account_mappings(entity.entity_config)
    targets = {policy['clearing_account'], policy['payout_transit_account']}
    if policy.get('reserve_account'):
        targets.add(policy['reserve_account'])
    if any(key != policy['stripe_account_id'] and value in targets for key, value in mappings.items()):
        raise ValueError('Stripe processor/transit account is already mapped to another source account.')
    result = map_bank_account(entity.path, policy['stripe_account_id'], policy['clearing_account'])
    entity.entity_config.setdefault('bank_account_mappings', {})[policy['stripe_account_id']] = policy['clearing_account']
    return dict(mapping=result, policy_digest=_digest(policy), version=policy['version'])


def _same_source(first, second):
    try:
        return (first['id'], first['date'], Decimal(str(first['amount'])), first['accountId'], first.get('currency', 'USD')) == (
            second['id'], second['date'], Decimal(str(second['amount'])), second['accountId'], second.get('currency', 'USD'))
    except (KeyError, ArithmeticError, TypeError):
        return False


def _bank_action(match, payout, net, pending, posted, owned, opens, entity, cutoff, transit, saved, entries, reversed_ids):
    sid = match['bank_source_id']
    book = entity.currency
    if sid in posted:
        original = saved.get(sid)
        entry = entries.get(sid)
        expected = {(match['approved_ledger_account'], Decimal(_amount(-net)), book),
                    (transit, Decimal(_amount(net)), book)}
        if (sid not in reversed_ids and original and entry
                and routine_source_digest([original]) == match['bank_source_digest']
                and dict(entry.meta).get('stripe-authority') == payout
                and {(p.account, p.amount, p.currency) for p in entry.postings} == expected):
            return None
        raise ValueError('bank_source_already_posted_requires_review')
    bank = pending.get(sid)
    if not bank or routine_source_digest([bank]) != match['bank_source_digest']:
        raise ValueError('bank_source_changed_or_missing')
    from .accrual.treatment import needs_treatment
    if needs_treatment(entity, bank):
        raise ValueError('accrual_treatment_required')
    account = match['approved_ledger_account']
    if (flat_bank_account_mappings(entity.entity_config).get(bank.get('accountId')) != account
            or account not in opens or not account.startswith('Assets:Bank:')
            or bank.get('accountType') not in {'checking', 'savings'} or bank.get('pending')
            or str(bank.get('currency') or book).upper() != book or Decimal(str(bank['amount'])) != Decimal(_amount(-net))):
        raise ValueError('bank_payout_match_conflict')
    when = date.fromisoformat(bank['date'])
    if cutoff and when <= cutoff:
        raise ValueError('closed_bank_period')
    if opens[account].date > when or (opens[account].currencies and book not in opens[account].currencies):
        raise ValueError('bank_account_catalog_conflict')
    if sid in owned:
        raise ValueError('existing_bank_review')
    return dict(source_id=sid, source_digest=routine_source_digest([bank]), kind='bank_payout',
                authority_id=payout, postings=[dict(account=account, amount=f"{Decimal(str(bank['amount'])):.2f}", currency=book),
                dict(account=transit, amount=_amount(net), currency=book)], normalized_source=dict(bank))


def _source_capture_digest(bt, objects):
    """Bind this movement's authority graph without unrelated captured objects.

    Expanded and separately captured references resolve through the same rules
    as allocation. Reverse invoice-payment/session relations matter too: adding
    another relation to this payment can make its original authority ambiguous.
    """
    captured = {'movement': {**_financial(bt, '', False), 'status': bt.get('status')}, 'objects': {}}

    def project(doc, fields):
        value = dict(doc) if isinstance(doc, dict) else doc
        if isinstance(doc, dict):
            if fields is not None:
                value = {key: doc.get(key) for key in fields}
            for key in ('invoice', 'payment_intent', 'balance_transaction', 'charge', 'product'):
                if key in value:
                    value[key] = _object_id(value[key])
        return value

    def remember(kind, ref, fields=None):
        identity = _object_id(ref)
        doc = ref if isinstance(ref, dict) else objects[kind].get(identity)
        captured['objects'][kind + ':' + str(identity)] = project(doc, fields)
        return doc or {}

    def lines(document, field):
        for line in (document.get(field) or {}).get('data', []):
            raw = line.get('price') or (line.get('pricing') or {}).get('price_details') or {}
            identity = raw if isinstance(raw, str) else raw.get('id') or raw.get('price')
            price = remember('prices', objects['prices'].get(identity) or raw)
            product_id = _object_id(price.get('product'))
            if product_id:
                remember('products', product_id)

    def redundant(copy, primary, fields):
        # A copy adds nothing when every field it carries (that resolution reads) equals the primary's.
        for key in (fields if fields is not None else list(copy)):
            if key not in copy:
                continue
            if key not in primary:
                if copy[key] is not None:
                    return False
            elif _comparable(key, copy[key]) != _comparable(key, primary[key]):
                return False
        return True

    def copies(kind, primary, docs, fields=None):
        # Every other copy that resolution combines with the primary binds the graph too. A copy that adds
        # nothing leaves the digest as it was before copies were read, so unchanged evidence keeps its digest.
        primary = primary if isinstance(primary, dict) else {}
        for doc in docs:
            if isinstance(doc, dict) and doc is not primary and not redundant(doc, primary, fields):
                value = project(doc, fields)
                captured['objects'][f'copy:{kind}:{_object_id(doc)}:{_digest(value)}'] = value

    def charge(ref, extra=()):
        fields = _CHARGE_FIELDS
        doc = remember('charges', ref, fields)
        identity = _object_id(ref)
        sales = [row['source'] for row in objects['balanceTransactions'].values()
                 if row.get('type') in _SALE_KINDS and isinstance(row.get('source'), dict)
                 and _object_id(row['source']) == identity]
        docs = [item for item in (ref, *extra, *sales, objects['charges'].get(identity)) if isinstance(item, dict)]
        copies('charges', doc, docs, fields)
        intent_ref = doc.get('payment_intent')
        intent_id = _object_id(intent_ref)
        sessions = [row for row in objects['checkoutSessions'].values()
                    if intent_id and _object_id(row.get('payment_intent')) == intent_id]
        related = [row for row in objects['invoicePayments'].values() if intent_id
                   and (row.get('payment') or {}).get('type') == 'payment_intent'
                   and _object_id((row.get('payment') or {}).get('payment_intent')) == intent_id]
        intent_docs = []
        if intent_id:
            intent = remember('paymentIntents', intent_ref, ('id', 'metadata'))
            intent_docs = [item for item in ([d.get('payment_intent') for d in docs]
                           + [row.get('payment_intent') for row in sessions]
                           + [(row.get('payment') or {}).get('payment_intent') for row in related]
                           + [objects['paymentIntents'].get(intent_id)]) if isinstance(item, dict)]
            copies('paymentIntents', intent, intent_docs, _INTENT_FIELDS)
            extra_fields = {key: _comparable(key, intent.get(key)) for key in _INTENT_FIELDS
                            if key not in ('id', 'metadata') and intent.get(key) is not None}
            if extra_fields:
                captured['objects']['payment_intent_fields:' + str(intent_id)] = extra_fields
        refs = [item.get('invoice') for item in docs + intent_docs if item.get('invoice')]
        for payment in sorted(related, key=lambda row: row['id']):
            remember('invoicePayments', payment)
            if payment.get('invoice'):
                refs.append(payment['invoice'])
        refs.extend(row['invoice'] for row in sessions if row.get('invoice'))
        for session in sorted(sessions, key=lambda row: row['id']):
            remember('checkoutSessions', session)
            if not refs:
                lines(session, 'line_items')
        for invoice_id in sorted({str(_object_id(item)) for item in refs}):
            group = sorted((item for item in refs if str(_object_id(item)) == invoice_id), key=lambda item: isinstance(item, dict))
            invoice = remember('invoices', group[-1])
            group_docs = [item for item in group if isinstance(item, dict)] + [objects['invoices'].get(_object_id(group[-1]))]
            copies('invoices', invoice, group_docs)
            for item in [invoice] + group_docs:
                if isinstance(item, dict):
                    lines(item, 'lines')

    def charges_of(kind, ref):
        doc = remember(kind, ref)
        docs = [item for item in (ref, objects[kind].get(_object_id(ref))) if isinstance(item, dict)]
        copies(kind, doc, docs)
        charge_refs = [item.get('charge') for item in docs if item.get('charge')]
        if charge_refs:
            charge(charge_refs[0], charge_refs[1:])

    kind, source = bt.get('type'), bt.get('source')
    if kind in {'charge', 'payment'}:
        charge(source)
    elif kind in {'refund', 'payment_refund'}:
        charges_of('refunds', source)
    elif kind == 'adjustment' and _is_dispute(source):
        charges_of('disputes', source)
    elif kind == 'payout':
        remember('payouts', source)
    elif kind in {'transfer', 'transfer_refund'}:
        # Include the complete captured relationship, including duplicates that
        # would make a reversal ambiguous. Future reversal totals are mutable.
        connect = objects['connectEvidence']
        source_id = _object_id(source)
        reversals = [row for row in connect.get('transferReversals', [])
                     if _object_id(row.get('balance_transaction')) == bt['id']]
        parents = {source_id, *(_object_id(row.get('transfer')) for row in reversals)}
        captured['connect'] = dict(reversals=reversals, transfers=[
            {key: row.get(key) for key in ('id', 'object', 'amount', 'currency', 'livemode',
             'balance_transaction', 'destination', 'source_transaction')}
            for row in connect.get('transfers', []) if row.get('id') in parents])
    return _digest(captured)


def _holding_opens(entity, policy, plan):
    """Open the holding account in the same write as its first entry; nothing when it exists or is unused."""
    from .ledger.model import Open
    if not any(posting['account'] == STRIPE_HOLDING_ACCOUNT for action in plan['actions']
               for posting in action.get('postings', [])):
        return []
    opens = {item.account: item for item in LedgerStore(default_store_path(entity.path)).load_opens()}
    if STRIPE_HOLDING_ACCOUNT in opens:
        return []
    return [Open(opens[policy['clearing_account']].date, STRIPE_HOLDING_ACCOUNT, (entity.currency,))]


def stripe_plan(entity, evidence, policy, *, bank_matches=None, closed_through=None,
                limit=50, after_source_id='', _book=None):
    """Read-only bounded plan. All rules and bank bindings come from trusted host.

    Evidence is the connector envelope with optional charges/refunds/invoices
    arrays. Expanded BT source objects are also accepted. Invoice lines must be
    complete; partial payments, taxes, discounts and credits are held, not guessed.
    """
    from .accrual.scope import DISCIPLINE_ALL, discipline
    from .accrual.treatment import treatment_checker
    if discipline(entity.entity_config) == DISCIPLINE_ALL:
        raise ValueError('Cash-only Stripe posting is unavailable for accrual companies. Capture provider evidence with books accrual intake, then preview a journal/schedule/settlement with the explicit accounting treatment.')
    needs_treatment = treatment_checker(entity)
    pending, opens, posted, reversed_ids, owned = _state(entity)
    # _book is the currency a company with no entries is about to adopt (stripe_stage); the catalog still
    # names the current currency until the importer adopts it.
    book = _book or entity.currency
    cur = book.lower()
    _policy(policy, opens, book, entity.currency)
    if type(limit) is not int or not 1 <= limit <= 500 or not isinstance(after_source_id, str):
        raise ValueError('Invalid Stripe batch cursor.')
    if not isinstance(evidence, dict) or evidence.get('source') != 'stripe' or not isinstance(evidence.get('account'), dict):
        raise ValueError('Expected a Stripe evidence envelope.')
    account = evidence['account'].get('id')
    mode = evidence.get('livemode', evidence['account'].get('livemode'))
    if account != policy['stripe_account_id'] or mode is not policy['livemode']:
        raise ValueError('Stripe account or live/test mode does not match approved policy.')
    if not isinstance(evidence.get('coverage'), dict) or evidence['coverage'].get('complete') is not True:
        raise ValueError('Captured Stripe cohort coverage must be explicitly complete.')
    mapping = flat_bank_account_mappings(entity.entity_config).get(account)
    if mapping != policy['clearing_account']:
        raise ValueError('Stripe account mapping changed; approve the current processor mapping.')
    cutoff = date.fromisoformat(closed_through) if closed_through else None
    bts = _objects(evidence, 'balanceTransactions')
    charges, refunds, invoices, payouts, disputes = (_objects(evidence, name) for name in ('charges', 'refunds', 'invoices', 'payouts', 'disputes'))
    prices, products, intents, invoice_payments, sessions = (_objects(evidence, name) for name in ('prices', 'products', 'paymentIntents', 'invoicePayments', 'checkoutSessions'))
    store = LedgerStore(default_store_path(entity.path))
    with store.connection() as conn:
        saved = {row['id']: json.loads(row['payload_json']) for row in conn.execute('SELECT id,payload_json FROM source_transactions')}
    entries = {entry.source_id: entry for entry in store.load_entries() if entry.source_id}
    validated = {}
    for bt in bts.values():
        for field in ('amount', 'fee', 'net', 'created'):
            _minor(bt.get(field))
        if bt['amount'] - bt['fee'] != bt['net'] or not isinstance(bt.get('currency'), str):
            raise ValueError('Stripe amount minus fee must equal net, with explicit currency.')
        when = datetime.fromtimestamp(bt['created'], timezone.utc).date()
        sid = f"stripe:{account}:{bt['id']}"
        normalized = dict(id=sid, date=when.isoformat(), amount=_amount(bt['net']), currency=bt['currency'].upper(),
                          description=str(bt.get('description') or f"Stripe {bt.get('type')} {bt['id']}"),
                          accountId=account, accountName='Stripe processor balance', accountType='processor',
                          pending=False, stripe_financial_digest=_digest(_financial(bt, account, policy['livemode'])))
        validated[sid] = (bt, normalized)
    matches = bank_matches or []
    if not isinstance(matches, list):
        raise ValueError('Bank matches must be captured binding objects.')
    claimed = set()
    by_payout = {}
    for match in matches:
        if not isinstance(match, dict) or set(match) != {'payout_id', 'bank_source_id', 'bank_source_digest', 'approved_ledger_account'}:
            raise ValueError('Bank match requires payout, captured bank source digest and approved bank account.')
        po, sid = _id(match['payout_id'], 'payout ID'), _id(match['bank_source_id'], 'bank source ID')
        if po in by_payout or sid in claimed:
            raise ValueError('A payout and bank source can each be matched only once.')
        by_payout[po] = match; claimed.add(sid)
    result = dict(actions=[], no_effect=[], normalized_sources=[], unresolved=[], skipped=[], uncategorized=[],
                  reclassified=[], next_cursor=None,
                  source_capture_digests={},
                  policy_digest=_digest(policy), evidence_digest=_digest(evidence), ledger_digest=store.content_digest())
    objects = dict(charges=charges, refunds=refunds, invoices=invoices, payouts=payouts, prices=prices,
                   products=products, paymentIntents=intents, invoicePayments=invoice_payments,
                   checkoutSessions=sessions, disputes=disputes, balanceTransactions=bts,
                   connectEvidence=evidence.get('connectEvidence') or {})
    all_ids = sorted(sid for sid in validated if sid > after_source_id)
    selected = all_ids[:limit]
    result['next_cursor'] = selected[-1] if len(all_ids) > limit else None
    in_batch = set(selected)
    reclassifications = {}

    def held_entry(source):
        entry = entries.get(source)
        return (entry is not None and source in posted and source not in reversed_ids
                and dict(entry.meta).get('stripe-authority') == STRIPE_HOLDING_AUTHORITY)

    def reclassify_id(source):
        return STRIPE_RECLASSIFY_PREFIX + source[len('stripe:'):]

    def sale_held(sale_bt):
        """Why a refund or dispute must wait: its sale sits in the holding account and does not move in this batch."""
        source = f"stripe:{account}:{sale_bt}"
        if not held_entry(source):
            return None
        moved = reclassify_id(source)
        if moved in posted and moved not in reversed_ids:
            return None
        if source in in_batch and reclassification(source):
            return None
        return 'sale_in_holding'

    resolver = _Resolver(policy, bts, objects, sale_held)

    def reclassification(source):
        """One entry that moves a held sale or refund out of the holding account once the evidence proves it.

        The original entry is never changed. The new entry has its own source ID, derived from the balance
        transaction, so it can post only once; ``None`` when nothing can move yet.
        """
        if source in reclassifications:
            return reclassifications[source]
        reclassifications[source] = None
        bt = validated[source][0]
        moved = reclassify_id(source)
        if (not held_entry(source) or source in pending or moved in posted or not policy['enabled']
                or bt.get('type') not in _SALE_KINDS | _REFUND_KINDS or bt['currency'] != cur):
            return None
        entry = entries[source]
        expected = {(account_name, Decimal(_amount(amount)), book) for account_name, amount in resolver.holding(bt) if amount}
        if {(p.account, p.amount, p.currency) for p in entry.postings} != expected:
            return None  # The held entry is not the engine's holding entry for this movement; a person decides.
        try:
            postings, authority, review = resolver.place(bt)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            return None
        if review:
            return None
        lines = [(STRIPE_HOLDING_ACCOUNT, bt['amount'])] + [
            (name, amount) for name, amount in postings if name not in {policy['clearing_account'], policy['fee_account']}]
        when = entry.date if not cutoff or entry.date > cutoff else cutoff + timedelta(days=1)
        docs = [dict(account=name, amount=_amount(amount), currency=book) for name, amount in lines if amount]
        if sum(amount for _, amount in lines) != 0 or any(
                name not in opens or opens[name].date > when or (opens[name].currencies and book not in opens[name].currencies)
                for name in {doc['account'] for doc in docs}):
            return None
        action = dict(source_id=moved, kind='reclassification', reclassifies=source, authority_id=','.join(authority),
                      cleared_review_reason=dict(entry.meta).get('stripe-review'), date=when.isoformat(),
                      description=f'Reclassify {entry.narration} from uncategorized Stripe activity', postings=docs)
        action['source_digest'] = _digest({key: action[key] for key in ('source_id', 'reclassifies', 'date', 'postings')})
        reclassifications[source] = action
        return action
    for sid in selected:
        bt, normalized = validated[sid]
        # Invoice imports own exact processor cash events in both book views.
        # Never turn an imported AR settlement back into another cash sale.
        from .accrual.store import load_record
        from .accrual.items import _find_live_entry
        claim = load_record(store, 'intents', 'stripe-balance:' + _digest({'account': account, 'livemode': mode, 'id': bt['id']}))
        if claim:
            try:
                if claim.get('financial_digest') != normalized['stripe_financial_digest']:
                    raise ValueError('changed_invoice_payment_source')
                _find_live_entry(store, sid)
            except ValueError:
                result['unresolved'].append(dict(source_id=sid, reason='changed_invoice_payment_source'))
            else:
                result['skipped'].append(dict(source_id=sid, reason='invoice_settlement_recorded'))
            continue
        try:
            result['source_capture_digests'][sid] = _source_capture_digest(bt, objects)
        except (ValueError, KeyError, TypeError, AttributeError):
            # Ordinary validation below holds malformed new movements. A changed
            # posted movement must not match its previously valid authority graph.
            result['source_capture_digests'][sid] = _digest({'invalid_source': sid})
        if sid in reversed_ids:
            result['skipped'].append(dict(source_id=sid, reason='reversed')); continue
        previous = pending.get(sid) or saved.get(sid)
        if previous and (previous.get('stripe_financial_digest') != normalized['stripe_financial_digest'] or not _same_source(previous, normalized)):
            result['unresolved'].append(dict(source_id=sid, reason='changed_source')); continue
        recorded_no_effect = (saved.get(sid) or {}).get('stripe_no_effect_capture_digest')
        if recorded_no_effect:
            if recorded_no_effect != result['source_capture_digests'][sid]:
                result['unresolved'].append(dict(source_id=sid, reason='changed_no_effect_evidence'))
            else:
                result['no_effect'].append(dict(source_id=sid, already_recorded=True,
                    normalized_source={**normalized, 'stripe_no_effect_capture_digest': recorded_no_effect}))
            continue
        if sid in posted:
            result['skipped'].append(dict(source_id=sid, reason='already_posted'))
            action = reclassification(sid)
            if action:
                result['actions'].append(action)
                result['reclassified'].append(dict(source_id=sid, reclassification_id=action['source_id']))
            continue
        result['normalized_sources'].append(normalized)
        try:
            if not policy['enabled']:
                raise ValueError('policy_disabled')
            if cutoff and date.fromisoformat(normalized['date']) <= cutoff:
                raise ValueError('closed_period')
            if bt['currency'] != cur:
                raise ValueError('unsupported_currency')
            if bt['status'] not in {'available', 'pending'}:
                raise ValueError('invalid_balance_status')
            if sid in owned:
                raise ValueError('existing_review')
            # Per-account accrual: hold processor activity that could settle an open invoice or fund a schedule.
            if needs_treatment(normalized) or (bt.get('amount') and needs_treatment({**normalized, 'amount': _amount(bt['amount'])})):
                raise ValueError('accrual_treatment_required')
            # A row above the ask-first amount waits for a person; apply never records it.
            if queue.txn_is_above_ask_first(entity, normalized) or (
                    bt.get('amount') and queue.txn_is_above_ask_first(entity, {**normalized, 'amount': _amount(bt['amount'])})):
                raise ValueError('above_ask_first_amount')
            kind = bt.get('type')
            source_id = _object_id(bt.get('source'))
            source = bt.get('source') if isinstance(bt.get('source'), dict) else None
            postings = [(policy['clearing_account'], bt['net'])]
            authority = []
            review = None
            try:
                if _identity_movement(bt):
                    postings, authority, review = resolver.place(bt)
                elif kind in {'stripe_fee', 'stripe_fx_fee'} and bt['net'] != 0:
                    postings.append((policy['fee_account'], -bt['net'])); authority = ['fee-account-policy']
                elif kind in {'payout_minimum_balance_hold', 'payout_minimum_balance_release', 'reserve_transaction'} and bt['fee'] == 0 and bt['amount'] != 0:
                    if not policy.get('reserve_account'):
                        raise ValueError('missing_reserve_account')
                    if ((kind == 'payout_minimum_balance_hold' and bt['amount'] > 0)
                            or (kind == 'payout_minimum_balance_release' and bt['amount'] < 0)):
                        raise ValueError('invalid_reserve_direction')
                    postings.append((policy['reserve_account'], -bt['amount']))
                    authority = ['reserve-account-policy']
                elif kind in {'transfer', 'transfer_refund'}:
                    transfer, reversal, rule = _transfer_treatment(bt, objects['connectEvidence'], policy, cur)
                    if reversal:
                        original_id = f"stripe:{account}:{_object_id(transfer.get('balance_transaction'))}"
                        original = entries.get(original_id)
                        if original and (original_id in reversed_ids or
                                not any(p.account == rule['category'] and p.amount == Decimal(_amount(transfer['amount']))
                                        for p in original.postings)):
                            raise ValueError('transfer_reversal_treatment_changed')
                    postings.append((rule['category'], -bt['amount']))
                    authority = ['transfer-rule:' + rule['id']]
                elif kind == 'payout' and bt['amount'] <= 0 and bt['fee'] >= 0:
                    payout = source or payouts.get(source_id)
                    if (not payout or _object_id(payout.get('balance_transaction')) != bt['id'] or payout.get('amount') != -bt['amount']
                            or payout.get('currency') != cur or payout.get('status') not in {'paid', 'in_transit', 'pending'}):
                        raise ValueError('missing_or_failed_payout')
                    if bt['amount'] == 0:
                        if bt['fee'] != 0 or payout.get('status') != 'paid' or payout.get('failure_balance_transaction'):
                            raise ValueError('invalid_zero_payout')
                        capture = result['source_capture_digests'][sid]
                        previous_effect = (saved.get(sid) or {}).get('stripe_no_effect_capture_digest')
                        if sid in saved and previous_effect != capture:
                            raise ValueError('changed_no_effect_evidence')
                        result['no_effect'].append(dict(source_id=sid, already_recorded=bool(previous_effect),
                            normalized_source={**normalized, 'stripe_no_effect_capture_digest': capture}))
                        continue
                    postings.append((policy['payout_transit_account'], -bt['amount']))
                    if bt['fee']:
                        postings.append((policy['fee_account'], bt['fee']))
                    authority = ['payout-transit-policy']
                else:
                    raise ValueError('unsupported_balance_type')
            except _Hold as hold:
                # The processor side is certain; the other side waits in the holding account for a person.
                review = str(hold)
                postings = [(policy['clearing_account'], bt['net']), (STRIPE_HOLDING_ACCOUNT, -bt['amount'])]
                if bt['fee']:
                    postings.append((policy['fee_account'], bt['fee']))
                authority = [STRIPE_HOLDING_AUTHORITY]
            if sum(amount for _, amount in postings) != 0:
                raise ValueError('unbalanced_stripe_evidence')
            posting_docs = [dict(account=acc, amount=_amount(amount), currency=book) for acc, amount in postings if amount]
            for posting in posting_docs:
                # The holding account opens with the clearing account the first time it is needed.
                opened = opens.get(posting['account']) or opens[policy['clearing_account']]
                if opened.date > date.fromisoformat(normalized['date']) or (opened.currencies and book not in opened.currencies):
                    raise ValueError('account_catalog_conflict')
            action = dict(source_id=sid, source_digest=_digest(normalized), kind=kind,
                          authority_id=','.join(authority), postings=posting_docs, normalized_source=normalized)
            if review:
                action['review_reason'] = review
                result['uncategorized'].append(dict(source_id=sid, reason=review))
            result['actions'].append(action)
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            # A payout match conflict holds the payout cohort too: never quietly
            # add processor detail over an old bank deposit booked as income.
            result['actions'] = [row for row in result['actions'] if row['source_id'] != sid]
            result['uncategorized'] = [row for row in result['uncategorized'] if row['source_id'] != sid]
            result['unresolved'].append(dict(source_id=sid, reason=str(exc)))
    # Match bank arrivals separately: a payout may have posted to transit in a
    # previous cohort/month before its real bank deposit arrived.
    for sid in selected:
        bt, normalized = validated[sid]
        payout = _object_id(bt.get('source'))
        if bt.get('type') != 'payout' or payout not in by_payout:
            continue
        result['source_capture_digests'][by_payout[payout]['bank_source_id']] = _digest({
            'payout_capture': result['source_capture_digests'][sid], 'binding': by_payout[payout]})
        original = saved.get(sid)
        already_applied = (sid in posted and sid not in reversed_ids and original
                           and original.get('stripe_financial_digest') == normalized['stripe_financial_digest']
                           and _same_source(original, normalized))
        if not any(row['source_id'] == sid for row in result['actions']) and not already_applied:
            continue
        try:
            if not policy['enabled']:
                raise ValueError('policy_disabled')
            action = _bank_action(by_payout[payout], payout, bt['amount'], pending, posted, owned, opens, entity, cutoff,
                                  policy['payout_transit_account'], saved, entries, reversed_ids)
            if action:
                result['actions'].append(action)
            else:
                result['skipped'].append(dict(source_id=by_payout[payout]['bank_source_id'], reason='already_posted'))
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            result['actions'] = []  # Prior bank income must be corrected first.
            result['uncategorized'] = []
            result['reclassified'] = []
            result['unresolved'].append(dict(source_id=by_payout[payout]['bank_source_id'], reason=str(exc)))
            break
    missing = set(by_payout) - {_object_id(bt.get('source')) for bt in bts.values() if bt.get('type') == 'payout'}
    if missing:
        raise ValueError('Bank binding refers to a payout absent from captured evidence.')
    result['plan_digest'] = _digest(result)
    return result


def stripe_stage(entity, evidence, policy, **kwargs):
    """Stage validated sources in the company currency only; returned proposals need trusted approval.

    A company with no entries adopts the one Stripe balance currency (balance transactions in more than
    one currency refuse). For a company with entries, a row in another currency stays unresolved with
    ``unsupported_currency`` and is not staged; the other rows are staged. Nothing converts.
    """
    from .currency import can_adopt, resolve_import_currency
    raw = evidence.get('balanceTransactions') if isinstance(evidence, dict) else None
    found = {row['currency'].upper() for row in raw or [] if isinstance(row, dict) and isinstance(row.get('currency'), str)}
    target = entity.currency
    if found and found != {target} and can_adopt(entity):
        # A company with no entries takes the one Stripe balance currency (more than one refuses). Check only:
        # the policy and the plan are validated first, and the importer adopts inside its write lock.
        target = resolve_import_currency(entity, found, 'Stripe account (balance currency)', adopt=False)
    # A company with entries keeps its currency: a row in another currency stays unresolved (unsupported_currency).
    plan = stripe_plan(entity, evidence, policy, _book=target, **kwargs)
    rows = [row for row in plan['normalized_sources'] if row['currency'] == target]
    imported = import_transactions(entity, rows, 'stripe-intake', intake_only=True)
    return dict(staged=imported.pending_categorization, duplicates=imported.skipped_duplicate,
                plan=stripe_plan(entity, evidence, policy, **kwargs))


def _entry_fingerprint(entry):
    return _digest(dict(date=entry.date.isoformat(), narration=entry.narration, meta=list(entry.meta),
                       postings=[(p.account, str(p.amount), p.currency) for p in entry.postings]))


def _without_hold(row):
    """A staged row as it was before a review hold marker was added."""
    return {key: value for key, value in row.items() if key != 'review_hold_reason'}


def _recover_stripe_cleanup(entity, plan):
    """Clean only exact sources bound to a canonical, intact Stripe receipt."""
    eligible = {row['source_id'] for row in plan['skipped'] if row['reason'] == 'already_posted'}
    pending = {row['id']: _without_hold(row) for row in _load_pending_categorization(entity)}
    eligible &= set(pending)
    if not eligible:
        return [], []
    store = LedgerStore(default_store_path(entity.path))
    if store.verify_audit_chain():
        raise ValueError('Cannot recover Stripe cleanup from an invalid audit chain.')
    entries = {entry.source_id: entry for entry in store.load_entries() if entry.source_id}
    with store.connection() as conn:
        saved = {sid: json.loads(raw) for sid, raw in conn.execute('SELECT id,payload_json FROM source_transactions')}
    recovered = set()
    covered = set()  # Sources named by a receipt for this policy, exact or not.
    review = {}
    for event in store.load_audit_events():
        receipt = event['payload']
        if (event['type'] != 'queue-finalization-intent' or not isinstance(receipt, dict)
                or receipt.get('kind') != 'stripe-finalization'
                or receipt.get('policy_digest') != plan['policy_digest']):
            continue
        body = {key: value for key, value in receipt.items() if key != 'operation_hash'}
        if receipt.get('operation_hash') != _digest(body):
            raise ValueError('Stripe finalization receipt changed.')
        for source in receipt['sources']:
            sid = source['id']
            if sid not in eligible:
                continue
            covered.add(sid)
            capture_matches = (source.get('capture_digest') == plan['source_capture_digests'].get(sid)
                               if source.get('capture_digest') is not None else
                               receipt.get('evidence_digest') == plan['evidence_digest'])
            if (not capture_matches or _digest(pending[sid]) != source['pending_digest']
                    or sid not in entries or _entry_fingerprint(entries[sid]) != source['entry_digest']
                    or sid not in saved or _digest(saved[sid]) != source['source_digest']):
                review[sid] = 'receipt does not match the current evidence'
                continue
            recovered.add(sid)
    for sid in sorted(eligible - covered):
        # No receipt: an older engine posted this source. Clean up only when the
        # posted entry, the saved source and the staged row all match the plan.
        entry, meta = entries.get(sid), dict(entries[sid].meta) if sid in entries else {}
        if (entry is not None and sid in saved
                and meta.get('stripe-policy-digest') == plan['policy_digest']
                and meta.get('stripe-source-digest') == _digest(saved[sid])
                and pending[sid].get('stripe_financial_digest') == saved[sid].get('stripe_financial_digest')
                and _same_source(pending[sid], saved[sid])):
            recovered.add(sid)
        else:
            review[sid] = 'no receipt and the posted entry does not match the current plan'
    recovered -= set(review)
    return sorted(recovered), [dict(source_id=sid, reason='cleanup_needs_review', detail=review[sid])
                               for sid in sorted(review)]


def stripe_apply(entity, evidence, policy, *, expected_plan_digest, bank_matches=None,
                 closed_through=None, limit=50, after_source_id='', session_id='stripe-approved', ts=None):
    """Apply an approved batch and recover exact post-commit cleanup under one lock."""
    # Refuse an unstaged/changed proposal before creating even a lock file.
    preview = stripe_plan(entity, evidence, policy, bank_matches=bank_matches, closed_through=closed_through,
                          limit=limit, after_source_id=after_source_id)
    if preview['plan_digest'] != expected_plan_digest:
        raise ValueError('Stripe plan changed; refresh the captured plan before applying.')
    staged = {row['id'] for row in _load_pending_categorization(entity)}
    if any(action['source_id'] not in staged for action in preview['actions'] if action['kind'] != 'reclassification'):
        raise ValueError('Stage every selected source before applying the approved Stripe plan.')
    with _entity_write_lock(entity.path):
        plan = stripe_plan(entity, evidence, policy, bank_matches=bank_matches, closed_through=closed_through,
                           limit=limit, after_source_id=after_source_id)
        if plan['plan_digest'] != expected_plan_digest:
            raise ValueError('Stripe plan changed; refresh the captured plan before applying.')
        recovered, needs_review = _recover_stripe_cleanup(entity, plan)
        plan['unresolved'] = [*plan['unresolved'], *needs_review]  # After the digest; not part of it.
        if needs_review:
            queue._set_pending_hold_reason(entity, [row['source_id'] for row in needs_review], queue.STRIPE_CLEANUP_HOLD_REASON)
        pending = {row['id']: row for row in _load_pending_categorization(entity)}
        sourced = [action for action in plan['actions'] if action['kind'] != 'reclassification']
        if any(action['source_id'] not in pending for action in sourced):
            raise ValueError('Stage every selected source before applying the approved Stripe plan.')
        entries = [Entry(date=date.fromisoformat(action['normalized_source']['date']),
            narration=action['normalized_source']['description'],
            meta=(('source-id', action['source_id']), ('import-session', session_id),
                  ('stripe-policy-digest', plan['policy_digest']), ('stripe-authority', action['authority_id']),
                  ('stripe-source-digest', action['source_digest']),
                  *((('stripe-review', action['review_reason']),) if action.get('review_reason') else ())),
            postings=tuple(Posting(p['account'], Decimal(p['amount']), p['currency']) for p in action['postings']))
            for action in sourced]
        # A reclassification has no bank row of its own: its source ID is derived from the held movement, so the
        # ledger's unique source ID refuses a second copy, and it is written in the same transaction as the batch.
        moves = [Entry(date=date.fromisoformat(action['date']), narration=action['description'],
            meta=(('source-id', action['source_id']), ('import-session', session_id),
                  ('stripe-policy-digest', plan['policy_digest']), ('stripe-authority', action['authority_id']),
                  ('stripe-source-digest', action['source_digest']), ('stripe-reclassifies', action['reclassifies'])),
            postings=tuple(Posting(p['account'], Decimal(p['amount']), p['currency']) for p in action['postings']))
            for action in plan['actions'] if action['kind'] == 'reclassification']
        if entries or moves:
            actions = {action['source_id']: action for action in sourced}
            body = dict(schema_version=1, kind='stripe-finalization', policy_digest=plan['policy_digest'],
                        evidence_digest=plan['evidence_digest'], sources=[
                            dict(id=entry.source_id, pending_digest=_digest(pending[entry.source_id]),
                                 entry_digest=_entry_fingerprint(entry),
                                 source_digest=_digest(actions[entry.source_id]['normalized_source']),
                                 capture_digest=plan['source_capture_digests'][entry.source_id]) for entry in entries])
            receipt = {**body, 'operation_hash': _digest(body)}
            _atomic_ledger_write_unlocked(entity, _holding_opens(entity, policy, plan), entries + moves, session_id, ts,
                                         'Apply approved Stripe clearing batch',
                                         [action['normalized_source'] for action in sourced],
                                         finalization_receipt=receipt)
        no_effect = [row['normalized_source'] for row in plan['no_effect'] if not row['already_recorded']]
        if no_effect:
            _atomic_ledger_write_unlocked(entity, [], [], session_id, ts, 'Verify zero-value Stripe payouts',
                                         no_effect, source_status='no_financial_effect')
        ids = sorted({*recovered, *(action['source_id'] for action in sourced),
                      *(row['source_id'] for row in plan['no_effect'])})
        if ids:
            StagingStore(entity.staging_dir).bulk_mark_seen(ids)
            queue._remove_many_from_pending_categorization(entity, ids)
        return dict(posted=len(entries) + len(moves), no_financial_effect=len(plan['no_effect']),
                    skipped=len(plan['skipped']), held=len(plan['unresolved']),
                    next_cursor=plan['next_cursor'], plan_digest=plan['plan_digest'],
                    unresolved=[dict(row) for row in plan['unresolved']],
                    needs_review=[dict(row) for row in needs_review],
                    uncategorized=[dict(row) for row in plan['uncategorized']],
                    reclassified=[dict(row) for row in plan['reclassified']])


def stripe_dismiss_held(entity, source_id, *, note, session_id='stripe-dismiss', ts=None):
    """Remove one held, already posted Stripe row from Review after a person checks the posted entry.

    Only a row marked ``cleanup_needs_review`` whose source is already in the ledger can be dismissed.
    The ledger entry is not changed. The dismissal is written to the audit log with the note.
    """
    note = ' '.join(str(note or '').split())[:500]
    if not note:
        raise ValueError('A note that says what the person checked is required to dismiss a held Stripe row.')
    with _entity_write_lock(entity.path):
        row = next((r for r in _load_pending_categorization(entity) if r.get('id') == source_id), None)
        if row is None or row.get('review_hold_reason') != queue.STRIPE_CLEANUP_HOLD_REASON:
            raise ValueError('Only a Stripe row held with cleanup_needs_review can be dismissed.')
        store = LedgerStore(default_store_path(entity.path))
        if not store.source_exists(source_id):
            raise ValueError('The row is not posted; confirm it or leave it in Review instead.')
        try:
            _find_live_entry(store, source_id)
        except ValueError as exc:
            raise ValueError(f'The posted entry for this source is not live ({exc}); '
                             'a person must reconcile it before the row is dismissed.') from exc
        if store.verify_audit_chain():
            raise ValueError('The audit chain is not intact, so the dismissal is not written. Restore the ledger first.')
        spellings = source_id_variants(source_id)
        for item in queue.list_queue_items(entity):
            if item.get('status') not in queue.ACTIONABLE_QUEUE_STATUSES:
                continue
            named = {str(value) for value in (item.get('source_ids') or []) + [item.get('source_id')] if value}
            if any(source_id_variants(value) & spellings for value in named):
                raise ValueError(f"Queue item {item.get('source_id')} is {item.get('status')} for this source. "
                                 'Withdraw the item before the row is dismissed.')
        with store.transaction() as conn:
            store.append_audit_event('stripe-cleanup-dismissed', dict(
                source_id=source_id, note=note, session_id=session_id, pending_digest=_digest(_without_hold(row))),
                conn, ts=ts)
        StagingStore(entity.staging_dir).bulk_mark_seen([source_id])
        queue._remove_many_from_pending_categorization(entity, [source_id])
        return dict(dismissed=source_id, note=note)
