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
    return _rule(policy, price_id, product_id, context)


def _rule(policy, price_id, product_id, context):
    """The one revenue rule that the price, product and metadata select; none or several stay unposted."""
    matches = []
    for rule in policy['revenue_rules']:
        field, value = next(iter(rule['match'].items()))
        if ((field == 'price_id' and price_id == value) or
                (field == 'product_id' and product_id == value) or
                (field == 'metadata' and ((context.get(value['object']) or {}).get('metadata') or {}).get(value['key']) == value['value'])):
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


_REFERENCE_FIELDS = frozenset({'invoice', 'payment_intent', 'balance_transaction', 'charge', 'product', 'price',
                               'latest_charge'})
_ATOMIC_FIELDS = frozenset({'metadata'})
_CHARGE_FIELDS = ('id', 'livemode', 'status', 'paid', 'captured', 'amount', 'currency', 'balance_transaction',
                  'invoice', 'payment_intent', 'metadata')
_INTENT_FIELDS = ('id', 'invoice', 'metadata', 'amount', 'currency', 'livemode', 'latest_charge')
_REFUND_FIELDS = ('id', 'status', 'amount', 'currency', 'balance_transaction', 'charge', 'payment_intent', 'livemode')
_DISPUTE_FIELDS = ('id', 'charge', 'payment_intent', 'amount', 'currency', 'livemode')
_INVOICE_TOTAL_FIELDS = ('total_tax_amounts', 'total_taxes', 'total_discount_amounts', 'shipping_cost',
                         'pre_payment_credit_notes_amount', 'post_payment_credit_notes_amount', 'starting_balance',
                         'ending_balance')
_INVOICE_FIELDS = ('id', 'status', 'amount_paid', 'amount_due', 'total', 'currency', 'livemode', 'metadata',
                   'lines', 'charge', 'payment_intent') + _INVOICE_TOTAL_FIELDS
# Fields added to a projection after release: bound only when a document has them, so older digests still match.
_OPTIONAL_DIGEST_FIELDS = frozenset({'latest_charge'})
_PRICE_FIELDS = ('id', 'product', 'metadata', 'currency')
_PRODUCT_FIELDS = ('id', 'metadata')
# Text for people, never read for accounting: two copies may word it differently and still be one object.
_DESCRIPTIVE_FIELDS = frozenset({'description', 'statement_descriptor', 'calculated_statement_descriptor', 'footer',
                                 'receipt_url', 'hosted_invoice_url', 'invoice_pdf'})
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
            if name in first and name in _DESCRIPTIVE_FIELDS:
                continue  # Descriptive text keeps the first copy's wording; it never decides an amount or account.
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


def _ref_id(value):
    """The ID a reference names (a string, or an expanded object's ``id``); None for anything else."""
    identity = _object_id(value)
    return identity if isinstance(identity, str) and identity else None


def _live_charge(doc):
    return doc.get('status') != 'failed' and doc.get('paid') is not False


class _Index:
    """Every relationship in one evidence envelope, by the ID it names, built once per plan.

    Each relationship is indexed from both ends: a charge's ``invoice`` and an invoice whose ``charge`` names
    the charge both connect the two. Every known copy of an object (captured, or expanded anywhere in the
    envelope) is listed under its ID, so neither the order of the arrays nor the side that states a
    relationship changes what the resolver finds.
    """

    def __init__(self, bts, objects):
        self.copies = {'charge': {}, 'intent': {}, 'invoice': {}}
        self.names = {'charge': {}, 'intent': {}, 'invoice': {}}  # id -> [(kind, doc)] that name the object
        self.sales = {}  # charge id -> sorted sale movement ids
        seen = set()

        def copy(kind, doc):
            if isinstance(doc, dict) and _ref_id(doc.get('id')) and (kind, id(doc)) not in seen:
                seen.add((kind, id(doc)))
                self.copies[kind].setdefault(doc['id'], []).append(doc)
                return True
            return False

        def name(kind, ref, by, doc):
            identity = _ref_id(ref)
            if identity:
                rows = self.names[kind].setdefault(identity, [])
                if not any(row is doc for _, row in rows):
                    rows.append((by, doc))

        def charge(doc):
            if copy('charge', doc):
                name('intent', doc.get('payment_intent'), 'charge', doc)
                name('invoice', doc.get('invoice'), 'charge', doc)
                intent(doc.get('payment_intent')); invoice(doc.get('invoice'))

        def intent(doc):
            if copy('intent', doc):
                name('invoice', doc.get('invoice'), 'intent', doc)
                name('charge', doc.get('latest_charge'), 'intent', doc)
                invoice(doc.get('invoice')); charge(doc.get('latest_charge'))

        def invoice(doc):
            if copy('invoice', doc):
                name('charge', doc.get('charge'), 'invoice', doc)
                name('intent', doc.get('payment_intent'), 'invoice', doc)
                charge(doc.get('charge')); intent(doc.get('payment_intent'))

        def follower(kind, doc):  # A refund or dispute names its charge and maybe its intent.
            name('charge', doc.get('charge'), kind, doc)
            name('intent', doc.get('payment_intent'), kind, doc)
            charge(doc.get('charge')); intent(doc.get('payment_intent'))

        for doc in objects['charges'].values():
            charge(doc)
        for doc in objects['paymentIntents'].values():
            intent(doc)
        for doc in objects['invoices'].values():
            invoice(doc)
        for row in sorted(bts.values(), key=lambda row: row['id']):
            source = row.get('source')
            if row.get('type') in _SALE_KINDS:
                charge(source)
                if _ref_id(source):
                    self.sales.setdefault(_ref_id(source), []).append(row['id'])
            elif row.get('type') in _REFUND_KINDS and isinstance(source, dict):
                follower('refund', source)
            elif row.get('type') == 'adjustment' and isinstance(source, dict) and _is_dispute(source):
                follower('dispute', source)
        for kind, rows in (('refund', objects['refunds']), ('dispute', objects['disputes'])):
            for doc in rows.values():
                follower(kind, doc)
        for doc in objects['checkoutSessions'].values():
            name('intent', doc.get('payment_intent'), 'session', doc)
            name('invoice', doc.get('invoice'), 'session', doc)
            intent(doc.get('payment_intent')); invoice(doc.get('invoice'))
        for doc in objects['invoicePayments'].values():
            payment = doc.get('payment') if isinstance(doc.get('payment'), dict) else {}
            if payment.get('type') == 'charge':
                name('charge', payment.get('charge'), 'payment', doc)
            name('intent', payment.get('payment_intent'), 'payment', doc)
            name('invoice', doc.get('invoice'), 'payment', doc)
            charge(payment.get('charge')); intent(payment.get('payment_intent')); invoice(doc.get('invoice'))

    def named(self, kind, identity, by):
        return [doc for name, doc in self.names[kind].get(identity, []) if name == by]


class _Closure:
    """Everything the evidence says about one charge, gathered before anything is decided."""

    def __init__(self, charge_id):
        self.charge_id = charge_id
        self.charge_docs, self.intent_refs, self.intent_docs = [], [], []
        self.sessions, self.payments, self.invoice_refs, self.invoice_docs = [], [], [], []
        self.backlink_intents, self.named_charges, self.claimants, self.followers = set(), set(), [], []
        self.rivals = []  # Other live charges for the same payment intent.


class _Resolver:
    """Which charge, payment intent, invoice or Checkout session a movement belongs to.

    Resolve first, then decide: :meth:`closure` follows every reference to and from one charge, through its
    payment intent, Checkout sessions, invoice payments of both types and invoices (including an invoice that
    only names the charge back), and only then :meth:`resolve` validates the whole closure. ``_Hold`` means the
    identity is uncertain (the movement posts to the holding account); any other ``ValueError`` means a known
    sale whose allocation or settlement is not complete (it stays unposted).

    ``trace`` (a list) collects every provider document the closure used; ``lookups`` the movement IDs it looked
    up with what it found (or None), so a receipt binds exactly what decided a posting. ``links`` maps each
    movement to its charge, its sale movement and its invoice. ``ledger_claims`` maps an invoice ID to the sale
    movements whose live ledger entries already claim it.
    """

    def __init__(self, policy, bts, objects, trace=None, ledger_claims=None):
        self.policy, self.mode, self.cur = policy, policy['livemode'], policy['currency'].lower()
        self.bts = bts
        self.charges, self.refunds, self.invoices = objects['charges'], objects['refunds'], objects['invoices']
        self.prices, self.products, self.intents = objects['prices'], objects['products'], objects['paymentIntents']
        self.disputes = objects['disputes']
        if objects.get('_index') is None:
            objects['_index'] = _Index(bts, objects)  # One index per plan, shared by every resolver.
        self.index = objects['_index']
        self.trace, self.lookups, self.competing, self.links = trace, {}, set(), {}
        self.ledger_claims = ledger_claims or {}

    def use(self, *docs):
        if self.trace is not None:
            self.trace.extend(doc for doc in docs if isinstance(doc, dict))

    def docs(self, refs, captured, label):
        identity, docs = _docs(refs, captured, label)
        self.use(*docs)
        return identity, docs

    def check_mode(self, doc):
        if doc.get('livemode') is not None and doc.get('livemode') is not self.mode:
            raise _Hold('livemode_conflict')

    def check_currency(self, doc, label):
        currency = doc.get('currency')
        if currency is not None and (not isinstance(currency, str) or currency.lower() != self.cur):
            raise _Hold(f'{label}_currency_conflict')

    @staticmethod
    def unique(docs):
        result = []
        for doc in docs:
            if isinstance(doc, dict) and not any(doc is row for row in result):
                result.append(doc)
        return result

    # Resolve: gather every copy and relationship of one charge.

    def closure(self, refs, followers=()):
        """The closure of the one charge that *refs* name; ``followers`` are the movement's refund/dispute copies."""
        identity, _ = _docs(refs, {}, 'charge')
        if identity is None:
            return None
        index, found = self.index, _Closure(identity)
        found.charge_docs = self.unique([ref for ref in refs if isinstance(ref, dict)] + index.copies['charge'].get(identity, []))
        # A refund or dispute of this charge that names a payment intent or expands the charge adds evidence.
        found.followers = self.unique(list(followers) + [doc for kind in ('refund', 'dispute')
                                                         for doc in index.named('charge', identity, kind)
                                                         if doc.get('payment_intent') or isinstance(doc.get('charge'), dict)])
        own_payments = index.named('charge', identity, 'payment')
        found.intent_refs = ([doc.get('payment_intent') for doc in found.charge_docs + found.followers]
                             + [(doc.get('payment') or {}).get('payment_intent') for doc in own_payments]
                             + [{'id': doc['id']} for doc in index.named('charge', identity, 'intent')])
        intents = {_ref_id(ref) for ref in found.intent_refs} - {None}
        invoices, previous = set(), None
        while previous != (frozenset(intents), frozenset(invoices)):  # Follow references until nothing new is found.
            previous = (frozenset(intents), frozenset(invoices))
            sessions = [doc for intent in sorted(intents) for doc in index.named('intent', intent, 'session')]
            payments = own_payments + [doc for intent in sorted(intents) for doc in index.named('intent', intent, 'payment')]
            found.sessions, found.payments = self.unique(sessions), self.unique(payments)
            backlinks = index.named('charge', identity, 'invoice') + [
                doc for intent in sorted(intents) for doc in index.named('intent', intent, 'invoice')]
            found.invoice_refs = ([doc.get('invoice') for doc in found.charge_docs]
                                  + [doc.get('invoice') for intent in sorted(intents) for doc in index.copies['intent'].get(intent, [])]
                                  + [doc.get('invoice') for doc in found.sessions + found.payments]
                                  + [{'id': doc['id']} for doc in backlinks])
            invoices = {_ref_id(ref) for ref in found.invoice_refs} - {None}
            intents |= {_ref_id((doc.get('payment') or {}).get('payment_intent')) for doc in found.payments} - {None}
        found.intent_docs = self.unique([ref for ref in found.intent_refs if isinstance(ref, dict) and len(ref) > 1]
                                        + [doc for intent in sorted(intents) for doc in index.copies['intent'].get(intent, [])])
        found.invoice_docs = self.unique([doc for invoice in sorted(invoices) for doc in index.copies['invoice'].get(invoice, [])])
        for doc in found.invoice_docs:
            if _ref_id(doc.get('charge')):
                found.named_charges.add(_ref_id(doc['charge']))
            if _ref_id(doc.get('payment_intent')):
                found.backlink_intents.add(_ref_id(doc['payment_intent']))
        for doc in found.intent_docs:
            if doc.get('latest_charge') is not None:
                found.named_charges.add(_ref_id(doc['latest_charge']))
        # Anything else that claims the same payment intent or invoice competes with this charge.
        found.rivals = self.unique([doc for intent in sorted(intents) for doc in index.named('intent', intent, 'charge')
                                    if doc['id'] != identity and _live_charge(doc)])
        for invoice in sorted(invoices):
            found.claimants += [doc for doc in index.named('invoice', invoice, 'charge') if doc['id'] != identity and _live_charge(doc)]
            found.claimants += [doc for doc in index.named('invoice', invoice, 'intent')
                                if doc['id'] not in intents and doc.get('status') in (None, 'succeeded')]
            found.claimants += [doc for doc in index.named('invoice', invoice, 'session')
                                if _ref_id(doc.get('payment_intent')) not in intents | {None}
                                and doc.get('payment_status') != 'unpaid' and doc.get('status') != 'expired']
            found.claimants += [doc for doc in index.named('invoice', invoice, 'payment')
                                if doc.get('status') == 'paid' and not any(doc is row for row in found.payments)]
        found.claimants = self.unique(found.claimants)
        self.use(*found.charge_docs, *found.followers, *found.intent_docs, *found.sessions, *found.payments,
                 *found.invoice_docs, *found.rivals, *found.claimants)
        return found

    def charge(self, refs, followers=()):
        """``(charge view, closure, sale movement)`` for the one charge *refs* name, or ``(None, None, None)``."""
        found = self.closure(refs, followers)
        if found is None:
            return None, None, None
        identity = found.charge_id
        sales = sorted(self.index.sales.get(identity, []))
        if len(sales) > 1:
            self.competing.add(tuple(sales))
        if not found.charge_docs:
            self.lookups[('sales', identity)] = (tuple(sales), None, None)
            return None, found, None
        charge = _combine(found.charge_docs, 'charge', _CHARGE_FIELDS)
        self.check_mode(charge)
        if charge.get('livemode') is not self.mode:
            raise _Hold('charge_identity_unverified')
        linked = _ref_id(charge.get('balance_transaction'))
        source = _ref_id(self.bts[linked].get('source')) if linked in self.bts else None
        # The movement lookups decide the posting too: bind what they found, including nothing.
        self.lookups[('sales', identity)] = (tuple(sales), linked, source)
        if len(sales) > 1 or (sales and linked and linked != sales[0]) or (linked in self.bts and source != identity):
            raise _Hold('charge_reference_conflict')
        return charge, found, linked or (sales[0] if sales else None)

    # Decide: validate the complete closure, then allocate.

    def document(self, charge, found, gross, sale_bt):
        """``(invoice or Checkout view, has_document, payment intent view, invoice ID)`` for a resolved charge."""
        identity = found.charge_id
        intent_id, _ = _docs(found.intent_refs + [(row.get('payment') or {}).get('payment_intent') for row in found.payments],
                             {}, 'payment_intent')
        intent = _combine(found.intent_docs, 'payment_intent', _INTENT_FIELDS) if found.intent_docs else {}
        if found.intent_docs:
            self.check_mode(intent)
            if (intent.get('latest_charge') is not None and _ref_id(intent['latest_charge']) != identity) or (
                    intent.get('currency') not in (None, self.cur)):
                raise _Hold('payment_intent_reference_conflict')
        if len(found.sessions) > 1:
            raise _Hold('multiple_checkout_sessions')
        for row in found.sessions:
            self.check_mode(row)
            self.check_currency(row, 'checkout_session')
        for row in found.payments:
            self.check_mode(row)
            self.check_currency(row, 'invoice_payment')
        if len({_ref_id(row.get('invoice')) for row in found.payments if row.get('invoice')}) > 1:
            raise _Hold('invoice_reference_conflict')
        if found.payments:
            row = found.payments[0]
            if (len(found.payments) != 1 or row.get('status') != 'paid' or row.get('amount_paid') != gross
                    or row.get('amount_requested') != gross or row.get('livemode') is not self.mode):
                raise ValueError('partial_or_multiple_invoice_payments')
            if not row.get('invoice'):
                raise _Hold('invoice_identity_unverified')
        invoice_id, docs = self.docs(found.invoice_refs, {}, 'invoice')
        if invoice_id is not None:
            docs = found.invoice_docs
            if not docs:
                raise _Hold('missing_invoice_evidence')
            invoice = _combine(docs, 'invoice', _INVOICE_FIELDS)
            self.check_mode(invoice)
            self.check_currency(invoice, 'invoice')
            if found.named_charges - {identity} or found.backlink_intents - {intent_id}:
                raise _Hold('invoice_reference_conflict')
            if found.claimants or found.rivals or (self.ledger_claims.get(invoice_id, set()) - {sale_bt}):
                raise _Hold('invoice_claimed_by_another_payment')
            return invoice, True, intent, invoice_id
        if found.named_charges - {identity} or found.rivals:
            raise _Hold('payment_intent_reference_conflict')
        if _ref_id(charge.get('payment_intent')) and not found.intent_docs and not found.payments and not found.sessions:
            # A named payment intent that the evidence does not hold is no proof of a sale without an invoice.
            raise _Hold('missing_payment_intent_evidence')
        if not found.sessions:
            return None, False, intent, None
        session = found.sessions[0]
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
                    lines=dict(has_more=False, data=converted)), True, intent, None

    def line_price(self, line):
        """``(price_id, product_id, price view, product view)`` for one line, from every reference to them.

        The line's price, its pricing details, its ``price_id``/``product_id``, the captured price and every
        expanded or captured product copy must name the same objects; a contradiction holds the sale.
        """
        raw = line.get('price')
        details = (line.get('pricing') or {}).get('price_details')
        views = []
        if isinstance(raw, dict):
            views.append({**raw, 'id': raw.get('id') or raw.get('price')})
        elif isinstance(raw, str) and raw:
            views.append({'id': raw})
        if isinstance(details, dict):
            reference = details.get('price')
            if isinstance(reference, dict):
                views.append(reference)
            view = {'id': _object_id(reference)} if reference else {}
            if details.get('product') is not None:
                view['product'] = details['product']
            if view:
                views.append(view)
        if line.get('price_id'):
            views.append({'id': line['price_id']})
        ids = {view.get('id') for view in views if view.get('id') is not None}
        if len(ids) > 1 or any(not isinstance(value, str) or not value for value in ids):
            raise _Hold('price_reference_conflict')
        price_id = next(iter(ids), None)
        if price_id is not None and self.prices.get(price_id) is not None:
            self.use(self.prices[price_id])
            views.append(self.prices[price_id])
        try:
            price = _combine(views, 'price', _PRICE_FIELDS) if views else {}
        except _Hold:
            raise _Hold('price_reference_conflict') from None
        if price.get('currency') not in (None, self.cur):
            raise _Hold('price_currency_conflict')
        # Every product reference and copy: each price copy's product (expanded or not), the pricing details,
        # the line's product_id and the captured product.
        products = [view.get('product') for view in views if view.get('product')]
        if line.get('product_id'):
            products.append(line['product_id'])
        product_id, product_docs = self.docs(products, self.products, 'product')
        product = _combine(product_docs, 'product', _PRODUCT_FIELDS) if product_docs else {}
        return price_id, product_id, price, product

    def allocations(self, charge, found, gross, sale_bt, link):
        policy, cur = self.policy, self.cur
        if charge.get('status') != 'succeeded' or charge.get('paid') is not True or charge.get('captured') is not True:
            raise ValueError('unsettled_charge')
        if _minor(charge.get('amount')) != gross or charge.get('currency') != cur:
            raise ValueError('charge_amount_conflict')
        invoice, has_invoice, intent, invoice_id = self.document(charge, found, gross, sale_bt)
        link['invoice'] = invoice_id
        # The same payment intent view that resolved the document selects metadata rules.
        context = {'charge': charge, 'payment_intent': intent}
        if not has_invoice:
            rule = _revenue(charge, policy, context)
            return [(rule['category'], gross, rule['id'])], invoice
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
            price_id, product_id, price, product = self.line_price(line)
            rule = _rule(policy, price_id, product_id,
                         {**context, 'invoice': invoice, 'invoice_line': line, 'price': price, 'product': product})
            allocations.append((rule['category'], _minor(line['amount']), rule['id']))
        if sum(amount for _, amount, _ in allocations) != gross:
            raise ValueError('invoice_allocation_mismatch')
        return allocations, invoice

    def sale(self, bt):
        link = self.links.setdefault(bt['id'], {'charge': _ref_id(bt.get('source')), 'sale': bt['id']})
        charge, found, _ = self.charge([bt.get('source')])
        if charge is None:
            raise _Hold('missing_charge_evidence')
        if _ref_id(charge.get('balance_transaction')) != bt['id']:
            raise _Hold('charge_identity_conflict')
        return self.allocations(charge, found, bt['amount'], bt['id'], link)[0]

    def follow(self, bt, docs, label, missing):
        """The charge a refund or dispute belongs to: ``(charge, closure, gross)``.

        Records the charge and the sale movement it follows in ``links``; the plan decides from the ledger
        and the final batch whether that sale is out of the holding account.
        """
        link = self.links.setdefault(bt['id'], {'charge': None, 'sale': None})
        refs = [doc.get('charge') for doc in docs]
        link['charge'] = next((_ref_id(ref) for ref in refs if _ref_id(ref)), None)
        charge, found, sale_bt = self.charge(refs, docs)
        if charge is None:
            raise _Hold(missing)
        own_intents = {_ref_id(doc.get('payment_intent')) for doc in docs if doc.get('payment_intent')}
        charge_intent = _ref_id(charge.get('payment_intent'))
        if len(own_intents) > 1 or (own_intents and charge_intent and own_intents != {charge_intent}):
            raise _Hold(f'{label}_reference_conflict')
        link['sale'] = sale_bt
        return charge, found, _minor(charge.get('amount'))

    def refund(self, bt):
        _, docs = self.docs([bt.get('source')], self.refunds, 'refund')
        if not docs:
            raise _Hold('missing_refund_evidence')
        refund = _combine(docs, 'refund', _REFUND_FIELDS)
        self.check_mode(refund)
        linked = _ref_id(refund.get('balance_transaction'))
        if linked is not None and linked != bt['id']:
            raise _Hold('refund_reference_conflict')
        if (refund.get('status') != 'succeeded' or refund.get('currency') != self.cur
                or refund.get('amount') != -bt['amount'] or linked is None):
            raise ValueError('missing_or_unsettled_refund')
        charge, found, gross = self.follow(bt, docs, 'refund', 'missing_refund_charge_evidence')
        link = self.links[bt['id']]
        # A refund follows its sale: the same closure (with the refund's own copies), then the sale's treatment.
        invoice, has_invoice, _, _ = self.document(charge, found, gross, link['sale'])
        if link['sale'] is None:
            raise _Hold('sale_movement_unverified')
        if has_invoice and (not invoice or not isinstance(invoice.get('lines'), dict)
                            or invoice['lines'].get('has_more') is not False
                            or len(invoice['lines'].get('data') or []) != 1):
            raise ValueError('mixed_line_refund_requires_allocation')
        # A single line still can contain tax, discounts, credits or
        # partial payment. Do not turn a refused original sale into an
        # apparently safe full contra-revenue refund.
        self.allocations(charge, found, gross, link['sale'], dict(link))

    def dispute(self, bt):
        """A dispute always waits in the holding account: no trusted policy names a dispute account.

        It follows its charge first, so a dispute on a charge the engine cannot place carries that charge's reason.
        """
        _, docs = self.docs([bt.get('source')], self.disputes, 'dispute')
        if not docs:
            raise _Hold('missing_dispute_evidence')
        dispute = _combine(docs, 'dispute', _DISPUTE_FIELDS)
        self.check_mode(dispute)
        if dispute.get('currency') not in (None, self.cur) or any(
                isinstance(doc.get('balance_transactions'), list)
                and bt['id'] not in {_object_id(row) for row in doc['balance_transactions']} for doc in docs):
            raise _Hold('dispute_reference_conflict')
        charge, found, gross = self.follow(bt, docs, 'dispute', 'missing_dispute_charge_evidence')
        try:
            self.document(charge, found, gross, self.links[bt['id']]['sale'])
        except _Hold:
            raise
        except (ValueError, KeyError, TypeError, ArithmeticError):
            pass  # The charge's sale has its own hold; the dispute still waits for review.
        raise _Hold('dispute_requires_review')


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


def _source_capture_digest(bt, objects, policy=None):
    """Bind this movement's authority graph without unrelated captured objects.

    Expanded and separately captured references resolve through the same rules
    as allocation. Reverse invoice-payment/session relations matter too: adding
    another relation to this payment can make its original authority ambiguous.

    With *policy*, the resolver runs once more and records every document it reads and every group of
    competing sale movements. When the projection below already binds each of those documents (the usual
    case), the digest is unchanged from earlier releases; otherwise the full resolved graph is bound too.
    """
    captured = {'movement': {**_financial(bt, '', False), 'status': bt.get('status')}, 'objects': {}}
    covered = set()  # id() of each document whose fields the projection binds.

    def project(doc, fields):
        value = dict(doc) if isinstance(doc, dict) else doc
        if isinstance(doc, dict):
            if fields is not None:
                value = {key: doc.get(key) for key in fields if key in doc or key not in _OPTIONAL_DIGEST_FIELDS}
            for key in ('invoice', 'payment_intent', 'balance_transaction', 'charge', 'product'):
                if key in value:
                    value[key] = _object_id(value[key])
        return value

    def remember(kind, ref, fields=None):
        identity = _object_id(ref)
        doc = ref if isinstance(ref, dict) else objects[kind].get(identity)
        captured['objects'][kind + ':' + str(identity)] = project(doc, fields)
        if isinstance(doc, dict):
            covered.add(id(doc))
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
            if isinstance(doc, dict):
                covered.add(id(doc))

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
    if policy is not None and _identity_movement(bt):
        trace = []
        resolver = _Resolver(policy, objects['balanceTransactions'], objects, trace=trace)
        try:
            resolver.place(bt)
        except (ValueError, KeyError, TypeError, ArithmeticError, AttributeError):
            pass  # The plan reports the same failure; the documents read until then still bind the receipt.
        # A lookup is plain when it found exactly the one sale movement the charge names, and that movement
        # names the charge back; anything else (nothing, a different source, competing movements) is bound.
        lookups = sorted(([charge, list(sales), linked, source] for (_, charge), (sales, linked, source)
                          in resolver.lookups.items()
                          if not (linked is not None and source == charge and sales in ((), (linked,)))), key=json.dumps)
        if any(id(doc) not in covered for doc in trace) or lookups:
            captured['resolved'] = dict(objects=sorted({_digest(doc) for doc in trace}), lookups=lookups)
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
                  reclassified=[], reclassification_blocked=[], entry_links={}, next_cursor=None,
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
    from .accrual.store import load_record
    from .accrual.items import _find_live_entry
    claims = {}

    def claim_of(sid):
        # Invoice imports own exact processor cash events in both book views.
        if sid not in claims:
            claims[sid] = load_record(store, 'intents', 'stripe-balance:' + _digest(
                {'account': account, 'livemode': mode, 'id': validated[sid][0]['id']}))
        return claims[sid]

    def changed(sid):
        previous = pending.get(sid) or saved.get(sid)
        normalized = validated[sid][1]
        return bool(previous) and (previous.get('stripe_financial_digest') != normalized['stripe_financial_digest']
                                   or not _same_source(previous, normalized))

    def settled(sid):
        """Whether the batch treats *sid* as an unchanged posted source: it passes every source check."""
        return (sid in in_batch and sid in posted and not claim_of(sid) and sid not in reversed_ids and not changed(sid)
                and not (saved.get(sid) or {}).get('stripe_no_effect_capture_digest'))

    def held_entry(source):
        entry = entries.get(source)
        return (entry is not None and source in posted and source not in reversed_ids
                and dict(entry.meta).get('stripe-authority') == STRIPE_HOLDING_AUTHORITY)

    def reclassify_id(source):
        return STRIPE_RECLASSIFY_PREFIX + source[len('stripe:'):]

    def moved_live(source):
        moved = reclassify_id(source)
        return moved in posted and moved not in reversed_ids

    # Durable links, from live ledger entries this engine wrote: which sale movement claims each invoice, and
    # which sale movements were posted for each charge. A claim is written in the same transaction as its entry.
    prefix = f"stripe:{account}:"
    ledger_claims, ledger_sales = {}, {}
    for source, entry in entries.items():
        if source not in posted or source in reversed_ids:
            continue
        meta = dict(entry.meta)
        target = meta.get('stripe-reclassifies') or source
        if meta.get('stripe-invoice') and target.startswith(prefix):
            ledger_claims.setdefault(meta['stripe-invoice'], set()).add(target[len(prefix):])
        if meta.get('stripe-charge') and source.startswith(prefix):
            ledger_sales.setdefault(meta['stripe-charge'], set()).add(source)
    resolver = _Resolver(policy, bts, objects, ledger_claims=ledger_claims)

    def reclassification(source):
        """One entry that moves a held sale or refund out of the holding account once the evidence proves it.

        The original entry is never changed. The new entry has its own source ID, derived from the balance
        transaction, so it can post only once; ``None`` when nothing can move yet.
        """
        if source in reclassifications:
            return reclassifications[source]
        reclassifications[source] = None
        if source not in validated or not settled(source):
            return None  # Only a source the batch accepts as posted and unchanged can move.
        bt, normalized = validated[source]
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
        # The evidence proves the movement. The move still needs the authority a fresh posting needs: no open
        # review item, no per-account accrual treatment, and net and gross within the ask-first limit. A blocked
        # move is reported (``reclassification_blocked``) and the entry stays in the holding account.
        gross = {**normalized, 'amount': _amount(bt['amount'])}
        blocked = ('existing_review' if source in owned else
                   'accrual_treatment_required' if needs_treatment(normalized) or needs_treatment(gross) else
                   'above_ask_first_amount' if queue.txn_is_above_ask_first(entity, normalized)
                   or queue.txn_is_above_ask_first(entity, gross) else None)
        if blocked:
            result['reclassification_blocked'].append(dict(source_id=source, reason=blocked))
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
    def to_holding(action, reason):
        """A fresh posting goes to the holding account instead; a move is withdrawn."""
        source = action.get('reclassifies') or action['source_id']
        if action['kind'] == 'reclassification':
            result['actions'].remove(action)
            result['reclassified'] = [row for row in result['reclassified'] if row['reclassification_id'] != action['source_id']]
            return
        bt = validated[source][0]
        action.update(postings=[dict(account=name, amount=_amount(amount), currency=book)
                                for name, amount in resolver.holding(bt) if amount],
                      authority_id=STRIPE_HOLDING_AUTHORITY, review_reason=reason)
        result['uncategorized'] = [row for row in result['uncategorized'] if row['source_id'] != source]
        result['uncategorized'].append(dict(source_id=source, reason=reason))

    def claim_invoices():
        """An invoice belongs to one charge in the batch too: every sale posting or move that would claim an
        invoice that another one in this batch also claims goes to the holding account instead."""
        claimants = {}
        for action in result['actions']:
            source = action.get('reclassifies') or action['source_id']
            if source not in validated or action.get('review_reason') or validated[source][0].get('type') not in _SALE_KINDS:
                continue
            invoice = (resolver.links.get(validated[source][0]['id']) or {}).get('invoice')
            if invoice:
                claimants.setdefault(invoice, []).append(action)
        for invoice, actions in sorted(claimants.items()):
            if len(actions) > 1:
                for action in actions:
                    to_holding(action, 'invoice_claimed_by_another_payment')

    def sale_release(bt, move):
        """``None`` when the sale a refund or dispute follows is out of the holding account, else the reason.

        Proof comes from the ledger (a live sale entry outside the holding account, or a live move of it) or
        from this batch's final actions (an accepted move, or a fresh sale posted to revenue). A sale movement ID
        that the ledger does not know, or that differs from the one recorded for the charge, proves nothing.
        """
        link = resolver.links.get(bt['id']) or {}
        linked, charge_id = link.get('sale'), link.get('charge')
        if not linked:
            return 'sale_movement_unverified'
        sale = prefix + linked
        if move:
            recorded = dict(entries[prefix + bt['id']].meta).get('stripe-sale')
            if recorded is not None and recorded != linked:
                return 'sale_movement_unverified'
        if charge_id and ledger_sales.get(charge_id, set()) - {sale}:
            return 'sale_movement_unverified'
        final = {row['source_id'] for row in result['actions']}
        if sale in posted and entries.get(sale) is not None:
            meta = dict(entries[sale].meta)
            if sale in reversed_ids or meta.get('stripe-charge') not in (None, charge_id):
                return 'sale_movement_unverified'
            if (meta.get('stripe-authority') == STRIPE_HOLDING_AUTHORITY and not moved_live(sale)
                    and reclassify_id(sale) not in final):
                return 'sale_in_holding'
            return None
        fresh = next((row for row in result['actions'] if row['source_id'] == sale), None)
        if fresh is not None:
            return 'sale_in_holding' if fresh.get('review_reason') else None
        # Neither in the ledger nor posted in this batch (an earlier cutover, or a later page): a new refund
        # posts as before, but a held refund needs proof before it moves.
        return 'sale_movement_unverified' if move else None

    def release_refunds():
        """A refund leaves the holding account (or skips it) only behind its sale, judged on the final batch."""
        for action in list(result['actions']):
            source = action.get('reclassifies') or action['source_id']
            if source not in validated:
                continue
            bt = validated[source][0]
            if bt.get('type') in _REFUND_KINDS and not action.get('review_reason'):
                reason = sale_release(bt, action['kind'] == 'reclassification')
                if reason:
                    to_holding(action, reason)
            elif action.get('review_reason') == 'dispute_requires_review' and sale_release(bt, False) == 'sale_in_holding':
                to_holding(action, 'sale_in_holding')

    def link_entries():
        """Durable links for each final entry, written as entry metadata in the same transaction."""
        for action in result['actions']:
            source = action.get('reclassifies') or action['source_id']
            if source not in validated:
                continue
            bt = validated[source][0]
            link = resolver.links.get(bt['id']) or {}
            meta = {}
            if bt.get('type') in _SALE_KINDS:
                if action['kind'] != 'reclassification' and _ref_id(bt.get('source')):
                    meta['stripe-charge'] = _ref_id(bt['source'])
                if not action.get('review_reason') and link.get('invoice'):
                    meta['stripe-invoice'] = link['invoice']
            elif action['kind'] != 'reclassification' and _identity_movement(bt):
                if link.get('charge'):
                    meta['stripe-refund-of'] = link['charge']
                if link.get('sale'):
                    meta['stripe-sale'] = link['sale']
            if meta:
                result['entry_links'][action['source_id']] = sorted([key, value] for key, value in meta.items())

    for sid in selected:
        bt, normalized = validated[sid]
        # Invoice imports own exact processor cash events in both book views.
        # Never turn an imported AR settlement back into another cash sale.
        claim = claim_of(sid)
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
            result['source_capture_digests'][sid] = _source_capture_digest(bt, objects, policy)
        except (ValueError, KeyError, TypeError, AttributeError):
            # Ordinary validation below holds malformed new movements. A changed
            # posted movement must not match its previously valid authority graph.
            result['source_capture_digests'][sid] = _digest({'invalid_source': sid})
        if sid in reversed_ids:
            result['skipped'].append(dict(source_id=sid, reason='reversed')); continue
        if changed(sid):
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
    claim_invoices()
    release_refunds()
    link_entries()
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
            result['entry_links'] = {}
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
                  *((('stripe-review', action['review_reason']),) if action.get('review_reason') else ()),
                  *(tuple(pair) for pair in plan['entry_links'].get(action['source_id'], []))),
            postings=tuple(Posting(p['account'], Decimal(p['amount']), p['currency']) for p in action['postings']))
            for action in sourced]
        # A reclassification has no bank row of its own: its source ID is derived from the held movement, so the
        # ledger's unique source ID refuses a second copy, and it is written in the same transaction as the batch.
        moves = [Entry(date=date.fromisoformat(action['date']), narration=action['description'],
            meta=(('source-id', action['source_id']), ('import-session', session_id),
                  ('stripe-policy-digest', plan['policy_digest']), ('stripe-authority', action['authority_id']),
                  ('stripe-source-digest', action['source_digest']), ('stripe-reclassifies', action['reclassifies']),
                  *(tuple(pair) for pair in plan['entry_links'].get(action['source_id'], []))),
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
                    reclassified=[dict(row) for row in plan['reclassified']],
                    reclassification_blocked=[dict(row) for row in plan['reclassification_blocked']])


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
