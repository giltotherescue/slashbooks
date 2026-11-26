"""Deterministic Stripe clearing proposals under explicit trusted policy.

Balance transaction IDs own financial effects. Charge/invoice/payout objects
provide evidence only. The hosted adapter must authenticate policy and captured
bank matches; source text never supplies authority. No provider writes occur.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
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


# Price and product selectors a plain charge (one with no invoice or Checkout session) could carry. Only an
# invoice or session line passes the catalog checks, so a plain charge that names one holds for launch.
_PLAIN_CHARGE_SELECTORS = ('price', 'pricing', 'price_id', 'product', 'product_id', 'line_items')


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


# "Uncategorized Stripe activity": earlier builds of this engine posted a sale, refund or dispute they could not
# tie to one charge and one invoice here, with a ``stripe-review`` reason. Since round 9 the engine never posts to
# it (such a movement is unresolved, with no entry); it stays a reserved name so no policy can use it, and entries
# already in it are still read (a held sale never proves a refund) and corrected only through the ordinary ledger
# correction path (``books ledger correct``).
STRIPE_HOLDING_ACCOUNT = 'Income:Uncategorized:Stripe'
# Moves written by pre-release builds of the holding account; the engine no longer writes them, but the ledger
# correction guard still protects any that exist.
STRIPE_RECLASSIFY_PREFIX = 'stripe-reclassify:'

# What a sale movement claims, the entry metadata that records it, and the hold reason when another entry claims it.
_OWNED_KINDS = ('charge', 'payment_intent', 'invoice')
_OWNERSHIP_META = dict(charge='stripe-charge', payment_intent='stripe-payment-intent', invoice='stripe-invoice')
_CLAIM_REASONS = dict(charge='charge_claimed_by_another_movement',
                      payment_intent='payment_intent_claimed_by_another_payment',
                      invoice='invoice_claimed_by_another_payment')
# Structured metadata this engine writes on every Stripe entry it posts. Its kind (``sale``, ``refund``, ``dispute``
# or ``other``) is how the engine later knows what an entry was; an entry without it (written by an earlier
# release) proves nothing about what it was or owned. Names, rule IDs and postings are never read for that.
_KIND_META = 'stripe-kind'
# The Stripe account an entry's movement belongs to, written beside its kind. With the engine version below they are
# the only proof that an entry was written by this engine version: a Stripe entry missing any of them (or with a kind
# not listed below) is legacy, whatever its source ID, text or postings say.
_ACCOUNT_META = 'stripe-account'
# The engine version that wrote an entry (round 10, decision 3). Kind and account alone are not proof: the parent
# builds of this change wrote them too (a paid payout as ``other`` with no ``stripe-payout``), and nothing recovers
# what those entries owned. Only an entry that also records this exact version is outside the legacy block.
_ENGINE_META = 'stripe-engine-version'
_ENGINE_VERSION = '1'
# ``invoice_settlement`` is the invoice engine's settlement of an open invoice by a Stripe movement
# (stripe_invoices); the clearing engine never posts that movement itself (see claim_of in stripe_plan).
_ENGINE_KINDS = frozenset({'sale', 'refund', 'dispute', 'other', 'invoice_settlement'})
_REFUND_META = 'stripe-refund'  # The refund's own provider ID: one refund posts once, whatever its movement.
# A dispute's own provider ID (du_/dp_) and the movements it was recorded with, as an earlier build of this engine
# wrote them on dispute entries. Disputes no longer post (round 9, cut 2); these are read only as durable
# reservations of those movements.
_DISPUTE_META = 'stripe-dispute'
_DISPUTE_MOVEMENTS_META = 'stripe-dispute-movements'
_PAYOUT_META = 'stripe-payout'  # A payout entry's own payout ID: one paid payout books once, whatever its movement.
# On the saved source of a no-effect movement: the ``[kind, ID]`` pairs it reserves (round 10, decision 4).
_NO_EFFECT_CLAIMS = 'stripe_no_effect_claims'
_CLOSURE_META = 'stripe-closure'  # Digest of the evidence that decided a sale's revenue (see _Resolver.allocations).


def _invoice_settlement_meta(account, invoice):
    """Structured metadata stripe_invoices writes on its settlement entry for a Stripe movement of *account*."""
    return ((_KIND_META, 'invoice_settlement'), (_ACCOUNT_META, account), (_ENGINE_META, _ENGINE_VERSION),
            (_OWNERSHIP_META['invoice'], invoice))


class _Hold(ValueError):
    """The engine cannot place this activity with certainty. Since round 9 it is unresolved like any other error
    (no holding entry); the class only keeps the resolver's reasons apart from allocation errors."""


class _Conflict(Exception):
    """Two copies of one provider object disagree."""


_REFERENCE_FIELDS = frozenset({'invoice', 'payment_intent', 'balance_transaction', 'charge', 'product', 'price',
                               'latest_charge'})
_ATOMIC_FIELDS = frozenset({'metadata'})
_CHARGE_FIELDS = ('id', 'livemode', 'status', 'paid', 'captured', 'amount', 'currency', 'balance_transaction',
                  'invoice', 'payment_intent', 'metadata')
_INTENT_FIELDS = ('id', 'invoice', 'metadata', 'amount', 'currency', 'livemode', 'latest_charge', 'status')
_REFUND_FIELDS = ('id', 'status', 'amount', 'currency', 'balance_transaction', 'charge', 'payment_intent', 'livemode')
_INVOICE_TOTAL_FIELDS = ('total_tax_amounts', 'total_taxes', 'total_discount_amounts', 'shipping_cost',
                         'pre_payment_credit_notes_amount', 'post_payment_credit_notes_amount', 'starting_balance',
                         'ending_balance')
_INVOICE_FIELDS = ('id', 'status', 'amount_paid', 'amount_due', 'total', 'currency', 'livemode', 'metadata',
                   'lines', 'charge', 'payment_intent') + _INVOICE_TOTAL_FIELDS
_PRICE_FIELDS = ('id', 'product', 'metadata', 'currency')
_PRODUCT_FIELDS = ('id', 'metadata')
# Text for people, never read for accounting: two copies may word it differently and still be one object.
_DESCRIPTIVE_FIELDS = frozenset({'description', 'statement_descriptor', 'calculated_statement_descriptor', 'footer',
                                 'receipt_url', 'hosted_invoice_url', 'invoice_pdf'})
_SALE_KINDS = frozenset({'charge', 'payment'})
_REFUND_KINDS = frozenset({'refund', 'payment_refund'})


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


def _is_document(doc):
    """A copy of a provider object: an ID and at least one field. An expansion with only its ID (``{'id': ...}``)
    is a reference, never the document; it cannot stand in for an object the evidence does not hold."""
    return isinstance(doc, dict) and bool(_ref_id(doc.get('id'))) and any(key not in ('id', 'object') for key in doc)


# Fields whose value names another provider object by a bare ID string. Any object expanded in place (a dict with
# its own ``id``), in any field or array, is a link too, whatever the field is called (see _Capture.value).
_LINK_FIELDS = frozenset(_REFERENCE_FIELDS | {'source', 'transfer', 'price_id', 'product_id', 'balance_transactions'})
# References followed backwards too: who names a charge, payment intent, invoice, movement or transfer. A balance
# transaction's ``source`` counts as well (the movements of one charge); prices and products never do, so one
# catalog entry never joins the sales that use it.
_BACKLINK_FIELDS = frozenset({'charge', 'latest_charge', 'payment_intent', 'invoice', 'balance_transaction', 'transfer',
                              'balance_transactions'})
_LINK_KINDS = dict(charge='charges', latest_charge='charges', payment_intent='paymentIntents', invoice='invoices',
                   balance_transaction='balanceTransactions', balance_transactions='balanceTransactions',
                   price='prices', price_id='prices', product='products', product_id='products', transfer='transfers')
# The capture array an expanded object belongs to, by its ``object`` field, when its field name does not say.
_OBJECT_KINDS = {'charge': 'charges', 'payment_intent': 'paymentIntents', 'invoice': 'invoices', 'refund': 'refunds',
                 'dispute': 'disputes', 'payout': 'payouts', 'price': 'prices', 'product': 'products',
                 'balance_transaction': 'balanceTransactions', 'checkout.session': 'checkoutSessions',
                 'invoice_payment': 'invoicePayments', 'transfer': 'transfers', 'transfer_reversal': 'transferReversals'}
_CAPTURE_ARRAYS = ('balanceTransactions', 'charges', 'refunds', 'invoices', 'payouts', 'disputes', 'prices', 'products',
                   'paymentIntents', 'invoicePayments', 'checkoutSessions')
_FOLLOWER_KINDS = frozenset({'refunds', 'disputes'})


class _Capture:
    """Every copy of every provider object in one capture, in canonical typed JSON, built once per plan.

    **One copy per object or nothing.** An object (an ID with at least one field) may appear more than once: in its
    top-level array, and expanded at any depth inside another. Each copy is written in canonical form: sorted keys,
    typed JSON (``1``, ``true``, ``1.0`` and ``null`` all differ), every field counted, ``metadata`` exactly as given,
    and each object expanded inside it (in a link field such as ``charge`` or ``source``) replaced by its ID and kept
    as a copy of its own. Copies of one ID that are not byte-identical in that form make the ID ``conflicted``: every
    movement whose closure reaches it stays unresolved, so the order copies are merged in can never matter.

    **The connector's contract** (docs/stripe-accounting.md, "Capture contract for connectors"): every copy of one
    ID, top level or expanded at any depth (in any wrapper, other books included), carries the same fields with the
    same typed values, descriptive text byte for byte; an object is sent either in full or as its ID only (a string,
    ``{'id': ...}`` or ``{'id': ..., 'object': ...}``), never as a partial projection; every copy comes from one read.

    **Closures and their full hash.** A movement's closure is every object reachable from it through link fields,
    forwards, and backwards through the references that name a charge, payment intent, invoice, movement or transfer
    (``_BACKLINK_FIELDS``). ``digest`` hashes the canonical form of every object in it (an ID the capture names but
    does not hold is bound as absent), including the movement's own extra fields and status. For a sale
    (``sale=True``) the refunds and disputes of the charge, and the movements only they name, are left out of the
    hash (they are later movements, not evidence of the sale), but what they reference is still followed.
    """

    def __init__(self, objects):
        self.forms, self.refs, self.back, self.referrers, self.kinds, self.types = {}, {}, {}, {}, {}, {}
        self.ref_kinds = {}  # ID -> the kinds the link fields naming it imply (for IDs held only as references).
        self.dispute_docs = []  # Every copy of every dispute, top level or expanded (see stripe_plan's owner map).
        self.dispute_ids = set()  # Every ID any copy, in any array or wrapper, shows is a dispute (round 10, decision 2).
        for name in _CAPTURE_ARRAYS:
            for doc in (objects.get(name) or {}).values():
                if _is_document(doc):  # A row with only its ID is a reference, never a copy (see _is_document).
                    self.add(doc, name)
                elif name == 'disputes' or _is_dispute(doc):
                    # Round 11 (decision 4): an ID-only row still carries type evidence. It is no copy (it never
                    # joins fingerprint equality), but its ID is a dispute wherever a movement names it.
                    self.dispute_ids.add(doc['id'])
        connect = objects.get('connectEvidence') or {}
        for name in ('transfers', 'transferReversals'):
            rows = connect.get(name) if isinstance(connect, dict) else None
            for doc in rows if isinstance(rows, list) else []:
                if _is_document(doc):
                    self.add(doc, name)
        self.conflicted = frozenset(ident for ident, forms in self.forms.items() if len(forms) > 1)
        self.cache = {}
        self._dispute_bts = None

    def add(self, doc, kind):
        ident, refs = doc['id'], []
        form = {name: (item if name == 'metadata' else self.value(item, name, refs, doc, kind))
                for name, item in doc.items()}
        self.forms.setdefault(ident, set()).add(json.dumps(form, sort_keys=True, separators=(',', ':'),
                                                           ensure_ascii=False, allow_nan=False))
        self.kinds.setdefault(ident, set()).add(kind)
        if kind == 'balanceTransactions':
            self.types.setdefault(ident, set()).add(doc.get('type'))
        if kind == 'disputes' or _is_dispute(doc):
            # A dispute is a dispute wherever it appears: top level, or expanded as a fee's source, inside a charge
            # or in any other wrapper. Every copy names movements for the owner map (round 10, decision 2).
            self.dispute_docs.append(doc)
            self.dispute_ids.add(ident)
        mine = self.refs.setdefault(ident, set())
        for field, target in refs:
            if target == ident:
                continue
            if field in _LINK_KINDS:
                self.ref_kinds.setdefault(target, set()).add(_LINK_KINDS[field])
            mine.add(target)
            self.referrers.setdefault(target, set()).add(ident)
            if field in _BACKLINK_FIELDS or (field == 'source' and kind == 'balanceTransactions'):
                self.back.setdefault(target, set()).add(ident)

    def value(self, value, key, refs, owner, kind):
        """The canonical form of one field value of *owner*. An object with its own ID (expanded in any field, or
        an element of any array) is replaced by that ID and kept as a copy of its own, so an expanded copy and its
        bare ID string always compare equal; a bare ID string in a known link field is a reference too."""
        if isinstance(value, dict) and _ref_id(value.get('id')):
            target = value['id']
            refs.append((key, target))
            if _is_document(value):
                self.add(value, self.link_kind(key, owner, kind, value))
            elif _is_dispute(value):
                self.dispute_ids.add(target)  # A typed reference (``{'id', 'object': 'dispute'}``) is type evidence.
            return target
        if isinstance(value, str) and value and key in _LINK_FIELDS:
            refs.append((key, value))
            return value
        if isinstance(value, dict):
            return {name: (item if name == 'metadata' else self.value(item, name, refs, owner, kind))
                    for name, item in value.items()}
        if isinstance(value, list):
            return [self.value(item, key, refs, owner, kind) for item in value]  # Elements keep their field's name.
        return value

    @staticmethod
    def link_kind(key, owner, kind, value=None):
        if key == 'source':
            if kind != 'balanceTransactions':
                return 'sources'
            if _is_dispute(value):
                return 'disputes'  # An expanded dispute is a dispute whatever the movement's type says.
            movement = owner.get('type')
            if movement in _SALE_KINDS:
                return 'charges'
            if movement in _REFUND_KINDS:
                return 'refunds'
            if movement == 'adjustment':
                return 'disputes' if _is_dispute(owner.get('source')) else 'sources'
            return 'payouts' if movement == 'payout' else 'sources'
        if key in _LINK_KINDS:
            return _LINK_KINDS[key]
        return _OBJECT_KINDS.get((value or {}).get('object'), 'objects')

    def dispute_bts(self):
        """Every balance transaction ID any copy of any dispute lists, with the disputes that list it."""
        if self._dispute_bts is None:
            found = {}
            for doc in self.dispute_docs:
                for row in doc.get('balance_transactions') if isinstance(doc.get('balance_transactions'), list) else []:
                    if _ref_id(row):
                        found.setdefault(_ref_id(row), set()).add(doc['id'])
            self._dispute_bts = found
        return self._dispute_bts

    def dispute_source(self, bt):
        """The dispute ID movement *bt*'s own source names, or None. The source counts as a dispute when it says so
        (``object=dispute`` or a ``du_``/``dp_`` ID, as a string, an ID wrapper or a full copy) or when any copy of
        that ID anywhere in the capture is a dispute."""
        source = bt.get('source')
        ident = _ref_id(source)
        if _is_dispute(source) or (ident and ident in self.dispute_ids):
            return ident or 'dispute'
        return None

    def dispute_movement(self, bt):
        """Whether movement *bt* is a dispute movement, whatever its type (round 10, decision 2): its source is a
        dispute (see dispute_source), or any dispute document in the capture lists it in ``balance_transactions``."""
        return bool(self.dispute_source(bt)) or bt.get('id') in self.dispute_bts()

    def closure(self, start):
        if start not in self.cache:
            seen, todo = set(), [start]
            while todo:
                ident = todo.pop()
                if ident in seen:
                    continue
                seen.add(ident)
                todo.extend(self.refs.get(ident, ()))
                todo.extend(self.back.get(ident, ()))
            self.cache[start] = frozenset(seen)
        return self.cache[start]

    def covers(self, bt, trace):
        """Whether the fingerprint of movement *bt* binds every provider document its allocation read (*trace*, the
        resolver's record of its reads). When it does not, the movement is unresolved and its receipt never cleared."""
        read = {doc['id'] for doc in trace or () if _is_document(doc)}
        return read <= self.hashed(bt['id'], sale=bt.get('type') in _SALE_KINDS)

    def follower(self, ident):
        """A refund or dispute, or a movement that is not a sale."""
        return bool(self.kinds.get(ident, set()) & _FOLLOWER_KINDS) or bool(self.types.get(ident, set()) - _SALE_KINDS)

    def conflicts(self, start):
        return sorted(self.closure(start) & self.conflicted)

    def mode_mismatch(self, start, mode):
        """Whether movement *start* or any object in its closure states a ``livemode`` other than *mode* (the books'
        Stripe account mode), or copies of one object disagree on it (round 11, decision 2). Test and live never mix:
        such a movement is unresolved (``livemode_mismatch``). A copy that leaves the field out states nothing."""
        modes = self.__dict__.setdefault('_modes', {})
        for ident in self.closure(start):
            if ident not in modes:
                modes[ident] = frozenset(json.dumps(value) for value in (
                    json.loads(form).get('livemode') for form in self.forms.get(ident, ())) if value is not None)
            if modes[ident] and modes[ident] != {json.dumps(mode)}:
                return True
        return False

    def hashed(self, start, sale=False):
        """The IDs whose copies the fingerprint of movement *start* binds: its whole closure, less (for a sale) its
        refunds and disputes and the movements only they name."""
        ids = self.closure(start)
        if sale:
            ids = {ident for ident in ids if ident == start or not (
                self.follower(ident) or (not self.forms.get(ident) and self.referrers.get(ident)
                                         and all(self.follower(row) for row in self.referrers[ident])))}
        return frozenset(ids)

    def digest(self, start, sale=False):
        """The full hash of the closure of movement *start*; None when any object in it has conflicting copies."""
        if not start:
            return None
        if self.closure(start) & self.conflicted:
            return None
        return _digest(dict(closure=start, sale=sale,
                            objects=sorted([ident, sorted(self.forms.get(ident, ()))] for ident in self.hashed(start, sale))))

    def graph_ids(self, start, sale=True):
        """``{kind: ids}`` of every charge, payment intent and invoice in the fingerprinted closure of *start*, at any
        depth (an intent named only by a rival charge expanded inside an invoice included). What a sale reserves."""
        found = dict(charge=set(), payment_intent=set(), invoice=set())
        names = dict(charges='charge', paymentIntents='payment_intent', invoices='invoice')
        for ident in self.hashed(start, sale):
            for kind in self.kinds.get(ident, set()) | self.ref_kinds.get(ident, set()):
                if kind in names:
                    found[names[kind]].add(ident)
        return found


class _Index:
    """Every relationship in one evidence envelope, by the ID it names, built once per plan.

    Each relationship is indexed from both ends: a charge's ``invoice`` and an invoice whose ``charge`` names
    the charge both connect the two. Every known copy of an object (captured, or expanded anywhere in the
    envelope) is listed under its ID, so neither the order of the arrays nor the side that states a
    relationship changes what the resolver finds.
    """

    def __init__(self, bts, objects, mode=None, cur=None):
        self.copies = {'charge': {}, 'intent': {}, 'invoice': {}}
        self.names = {'charge': {}, 'intent': {}, 'invoice': {}}  # id -> [(kind, doc)] that name the object
        self.sales = {}  # charge id -> sorted sale movement ids
        seen = set()

        def same(doc):
            # A wrapper supplies nested copies only in the policy's mode and currency (a field it leaves out is not
            # a difference): a document from other books never lends authority to what it expands.
            currency = doc.get('currency')
            return ((doc.get('livemode') is None or doc.get('livemode') is mode)
                    and (currency is None or cur is None or (isinstance(currency, str) and currency.lower() == cur)))

        def copy(kind, doc):
            if _is_document(doc) and (kind, id(doc)) not in seen:
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
                if same(doc):
                    intent(doc.get('payment_intent')); invoice(doc.get('invoice'))

        def intent(doc):
            if copy('intent', doc):
                name('invoice', doc.get('invoice'), 'intent', doc)
                name('charge', doc.get('latest_charge'), 'intent', doc)
                if same(doc):
                    invoice(doc.get('invoice')); charge(doc.get('latest_charge'))

        def invoice(doc):
            if copy('invoice', doc):
                name('charge', doc.get('charge'), 'invoice', doc)
                name('intent', doc.get('payment_intent'), 'invoice', doc)
                if same(doc):
                    charge(doc.get('charge')); intent(doc.get('payment_intent'))

        def follower(kind, doc):  # A refund or dispute names its charge and maybe its intent.
            name('charge', doc.get('charge'), kind, doc)
            name('intent', doc.get('payment_intent'), kind, doc)
            if same(doc):
                charge(doc.get('charge')); intent(doc.get('payment_intent'))

        for doc in objects['charges'].values():
            charge(doc)
        for doc in objects['paymentIntents'].values():
            intent(doc)
        for doc in objects['invoices'].values():
            invoice(doc)
        for row in sorted(bts.values(), key=lambda row: row['id']):
            source = row.get('source')
            if not same(row):
                continue  # A movement in another currency expands nothing into these books.
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
            if same(doc):
                intent(doc.get('payment_intent')); invoice(doc.get('invoice'))
        for doc in objects['invoicePayments'].values():
            payment = doc.get('payment') if isinstance(doc.get('payment'), dict) else {}
            if payment.get('type') == 'charge':
                name('charge', payment.get('charge'), 'payment', doc)
            name('intent', payment.get('payment_intent'), 'payment', doc)
            name('invoice', doc.get('invoice'), 'payment', doc)
            if same(doc):
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
        self.claimant_ids = dict(charge=set(), payment_intent=set())  # Own IDs of claiming charges and intents.
        self.rivals = []  # Other live charges for the same payment intent.
        self.intent_ids, self.invoice_ids = set(), set()  # Every payment intent and invoice the closure names.

    def named_ids(self):
        """``{kind: ids}`` of every charge, payment intent and invoice named on any side of the closure: its own
        documents, what each of them references, and every rival or claimant. A sale reserves all of them, held or
        not, so an identity a conflicting side names is never left free for another payment to take."""
        ids = dict(charge={self.charge_id}, payment_intent=set(self.intent_ids), invoice=set(self.invoice_ids))
        docs = (self.charge_docs + self.followers + self.intent_docs + self.sessions + self.payments
                + self.invoice_docs + self.rivals + self.claimants)
        for doc in docs:
            payment = doc.get('payment') if isinstance(doc.get('payment'), dict) else {}
            for kind, refs in (('charge', (doc.get('charge'), doc.get('latest_charge'), payment.get('charge'))),
                               ('payment_intent', (doc.get('payment_intent'), payment.get('payment_intent'))),
                               ('invoice', (doc.get('invoice'),))):
                ids[kind] |= {_ref_id(ref) for ref in refs}
        for doc in self.charge_docs + self.rivals:
            ids['charge'].add(_ref_id(doc.get('id')))
        for doc in self.intent_docs:
            ids['payment_intent'].add(_ref_id(doc.get('id')))
        for doc in self.invoice_docs:
            ids['invoice'].add(_ref_id(doc.get('id')))
        for kind, values in self.claimant_ids.items():
            ids[kind] |= values
        return {kind: values - {None} for kind, values in ids.items()}


class _Resolver:
    """Which charge, payment intent, invoice or Checkout session a movement belongs to.

    Resolve first, then decide: :meth:`closure` follows every reference to and from one charge, through its
    payment intent, Checkout sessions, invoice payments of both types and invoices (including an invoice that
    only names the charge back), and only then :meth:`resolve` validates the whole closure. ``_Hold`` means the
    identity is uncertain; any other ``ValueError`` means a known
    sale whose allocation or settlement is not complete (it stays unposted).

    ``trace`` (a list) collects every provider document the closure used; ``lookups`` the movement IDs it looked
    up with what it found (or None), so a receipt binds exactly what decided a posting. ``links`` maps each
    movement to its charge, its sale movement and its invoice; ``closures`` each charge's closure. Ownership
    across cohorts (the ledger) is decided by the plan, after every movement is resolved.
    """

    def __init__(self, policy, bts, objects, trace=None):
        self.policy, self.mode, self.cur = policy, policy['livemode'], policy['currency'].lower()
        self.bts = bts
        self.charges, self.refunds, self.invoices = objects['charges'], objects['refunds'], objects['invoices']
        self.prices, self.products, self.intents = objects['prices'], objects['products'], objects['paymentIntents']
        self.disputes = objects['disputes']
        if objects.get('_index') is None:
            objects['_index'] = _Index(bts, objects, self.mode, self.cur)  # One index per plan, shared by every resolver.
        self.index = objects['_index']
        if objects.get('_capture') is None:
            objects['_capture'] = _Capture(objects)
        self.capture = objects['_capture']
        self.trace, self.lookups, self.competing, self.links, self.closures = trace, {}, set(), {}, {}

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

    def same_books(self, doc):
        """A referring document in the policy's mode and currency (a field it leaves out is not a difference)."""
        currency = doc.get('currency')
        return ((doc.get('livemode') is None or doc.get('livemode') is self.mode)
                and (currency is None or (isinstance(currency, str) and currency.lower() == self.cur)))

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
        found.charge_docs = self.unique([ref for ref in refs if _is_document(ref)] + index.copies['charge'].get(identity, []))
        # A refund or dispute of this charge that names a payment intent or expands the charge adds evidence, but
        # only in the policy's mode and currency: a document from another mode or currency is not imported.
        found.followers = self.unique(list(followers) + [doc for kind in ('refund', 'dispute')
                                                         for doc in index.named('charge', identity, kind)
                                                         if (doc.get('payment_intent') or isinstance(doc.get('charge'), dict))
                                                         and self.same_books(doc)])
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
        found.intent_docs = self.unique([ref for ref in found.intent_refs if _is_document(ref)]
                                        + [doc for intent in sorted(intents) for doc in index.copies['intent'].get(intent, [])])
        found.intent_ids, found.invoice_ids = set(intents), set(invoices)
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
            charges = [doc for doc in index.named('invoice', invoice, 'charge') if doc['id'] != identity and _live_charge(doc)]
            claiming = [doc for doc in index.named('invoice', invoice, 'intent')
                        if doc['id'] not in intents and doc.get('status') in (None, 'succeeded')]
            found.claimant_ids['charge'] |= {doc['id'] for doc in charges}
            found.claimant_ids['payment_intent'] |= {doc['id'] for doc in claiming}
            found.claimants += charges + claiming
            found.claimants += [doc for doc in index.named('invoice', invoice, 'session')
                                if _ref_id(doc.get('payment_intent')) not in intents | {None}
                                and doc.get('payment_status') != 'unpaid' and doc.get('status') != 'expired']
            found.claimants += [doc for doc in index.named('invoice', invoice, 'payment')
                                if doc.get('status') == 'paid' and not any(doc is row for row in found.payments)]
        found.claimants = self.unique(found.claimants)
        # A refund or dispute is read only for evidence nothing else gives: a payment intent the charge, its own
        # invoice payments and its intents do not already name (an expanded charge copy inside it counts as that
        # charge copy). What is read here is what the fingerprint must bind (see _Capture.covers).
        known = {_ref_id(doc.get('payment_intent')) for doc in found.charge_docs + own_payments}
        known |= {_ref_id((doc.get('payment') or {}).get('payment_intent')) for doc in own_payments}
        known |= {doc['id'] for doc in index.named('charge', identity, 'intent')}
        read_followers = [doc for doc in found.followers
                          if _ref_id(doc.get('payment_intent')) and _ref_id(doc.get('payment_intent')) not in known]
        self.use(*found.charge_docs, *read_followers, *found.intent_docs, *found.sessions, *found.payments,
                 *found.invoice_docs, *found.rivals, *found.claimants)
        return found

    def charge(self, refs, followers=()):
        """``(charge view, closure, sale movement)`` for the one charge *refs* name, or ``(None, None, None)``."""
        found = self.closure(refs, followers)
        if found is None:
            return None, None, None
        identity = found.charge_id
        self.closures[identity] = found
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
            if intent.get('status') not in (None, 'succeeded'):
                raise _Hold('payment_intent_not_succeeded')
        if len(found.sessions) > 1:
            raise _Hold('multiple_checkout_sessions')
        for row in found.sessions:
            self.check_mode(row)
            self.check_currency(row, 'checkout_session')
            # An expired, open or unpaid session says this payment did not complete through it: it never links
            # the payment to an invoice or lines (a paid invoice does not say which charge paid it).
            if row.get('payment_status') not in (None, 'paid') or row.get('status') not in (None, 'complete'):
                raise _Hold('checkout_session_not_paid')
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
            if found.claimants or found.rivals:
                raise _Hold('invoice_claimed_by_another_payment')
            return invoice, True, intent, invoice_id
        if found.named_charges - {identity} or found.rivals:
            raise _Hold('payment_intent_reference_conflict')
        if intent_id and not found.intent_docs and not found.payments and not found.sessions:
            # A payment intent that any copy in the closure names (the charge, a refund, a dispute) but the
            # evidence does not hold is no proof of a sale without an invoice. An ID-only expansion is not the document.
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

    @staticmethod
    def price_views(line):
        """Every copy or reference of one line's price: the line's price, its pricing details, its ``price_id``."""
        raw = line.get('price')
        details = (line.get('pricing') or {}).get('price_details')
        views = []
        if isinstance(raw, dict):
            views.append(raw if raw.get('id') or not raw.get('price') else {**raw, 'id': raw['price']})
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
        return next(iter(ids), None), views

    @staticmethod
    def line_evidence(line):
        """The provider objects (not ID-only references) a line's price views come from."""
        details = (line.get('pricing') or {}).get('price_details')
        return [doc for doc in (line.get('price'), details.get('price') if isinstance(details, dict) else None)
                if _is_document(doc)]

    def catalog_copy(self, doc, label):
        """A price or product copy used for identity or account selection must be in the policy's mode and currency."""
        if isinstance(doc, dict):
            self.check_mode(doc)
            self.check_currency(doc, label)

    def catalog(self, lines):
        """``[(price_id, product_id, price view, product view)]`` for every line.

        Every copy of one price or product, on any line of the document and in the captured catalog, is merged
        by ID before any rule is chosen: two lines never see different copies of one object, and copies that
        disagree hold the sale. Each copy must be in the policy's mode and currency.
        """
        per_line, price_copies = [], {}
        evidence = [doc for line in lines for doc in self.line_evidence(line)] + list(self.prices.values())
        for line in lines:
            price_id, views = self.price_views(line)
            per_line.append((line, price_id, views))
            if price_id is not None:
                price_copies.setdefault(price_id, []).extend(views)
        for price_id, views in price_copies.items():
            if self.prices.get(price_id) is not None:
                views.append(self.prices[price_id])
        resolved, product_refs = [], {}
        for line, price_id, views in per_line:
            views = price_copies[price_id] if price_id is not None else views
            for view in views:
                self.catalog_copy(view, 'price')
            # Every provider copy the views came from binds the receipt (synthesized ID-only views do not).
            self.use(*(view for view in views if any(view is doc for doc in evidence)))
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
            product_id, _ = _docs(products, {}, 'product')
            if product_id is not None:
                product_refs.setdefault(product_id, []).extend(ref for ref in products if isinstance(ref, dict))
            resolved.append((price_id, product_id, price))
        products = {}
        for product_id, refs in product_refs.items():
            docs = self.unique(refs + ([self.products[product_id]] if self.products.get(product_id) is not None else []))
            for doc in docs:
                self.catalog_copy(doc, 'product')
            self.use(*docs)
            products[product_id] = _combine(docs, 'product', _PRODUCT_FIELDS) if docs else {}
        return [(price_id, product_id, price, products.get(product_id, {})) for price_id, product_id, price in resolved]

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
            # Only metadata selects a plain charge's account: a price or product it names never passed the catalog's
            # mode, currency and copy checks, so it holds for launch.
            if any(charge.get(field) not in (None, '', {}, []) for field in _PLAIN_CHARGE_SELECTORS):
                raise _Hold('plain_charge_price_unsupported')
            rule = _rule(policy, None, None, context)
            allocations = [(rule['category'], gross, rule['id'])]
            link['closure_digest'] = self.capture.digest(sale_bt, sale=True)
            return allocations, invoice
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
        for line, (price_id, product_id, price, product) in zip(lines['data'], self.catalog(lines['data'])):
            rule = _rule(policy, price_id, product_id,
                         {**context, 'invoice': invoice, 'invoice_line': line, 'price': price, 'product': product})
            allocations.append((rule['category'], _minor(line['amount']), rule['id']))
        if sum(amount for _, amount, _ in allocations) != gross:
            raise ValueError('invoice_allocation_mismatch')
        # The evidence that decided the sale: the full hash of its whole closure (see _Capture.digest). A sale entry
        # records it; a later refund recomputes it from its own capture and posts only when it is unchanged.
        link['closure_digest'] = self.capture.digest(sale_bt, sale=True)
        return allocations, invoice

    def sale(self, bt):
        link = self.links.setdefault(bt['id'], {'charge': _ref_id(bt.get('source')), 'sale': bt['id']})
        if link['charge'] is None:
            # A sale names its charge itself (round 9, cut 5). A captured charge that names this movement proves
            # nothing about which charge it is; the owner map still reserves that charge for the batch.
            raise ValueError('sale_identity_unverified')
        try:
            charge, found, _ = self.charge([bt.get('source')])
        except _Hold as hold:
            if str(hold) == 'charge_reference_conflict':
                # Another movement names this charge, or the charge names another movement: nothing proves this
                # movement is not a second booking of one sale, so it stays unresolved with no entry (not held).
                raise ValueError('charge_reference_conflict') from None
            raise
        finally:
            # What this sale owns comes from its own closure, even when the closure holds it.
            link['closure'] = self.closures.get(link['charge'])
        if charge is None:
            raise _Hold('missing_charge_evidence')
        if _ref_id(charge.get('balance_transaction')) != bt['id']:
            raise ValueError('charge_identity_conflict')  # The charge says another movement booked it.
        return self.allocations(charge, found, bt['amount'], bt['id'], link)[0]

    def follow(self, bt, docs, label, missing):
        """The charge a refund or dispute belongs to: ``(charge, closure, gross)``.

        Records the charge and the sale movement it follows in ``links``; the plan decides from the ledger
        and the final batch whether that sale is booked.
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
        link['closure'] = found
        return charge, found, _minor(charge.get('amount'))

    def attribute(self, bt):
        """Record which charge and sale refund movement *bt* claims, apart from whether it may post (round 11,
        decision 3). Every identifiable claim counts toward its sale's cap, whatever later refuses it: a pending or
        failed refund document, a changed source, a wrong status. A refund document that says another movement
        carries it is that movement's claim, not this one's."""
        if bt['id'] in self.links and (self.links[bt['id']].get('charge') or self.links[bt['id']].get('sale')):
            return
        try:
            _, docs = _docs([bt.get('source')], self.refunds, 'refund')
            if not _ref_id(bt.get('source')) or not docs:
                return
            if any(_ref_id(doc.get('balance_transaction')) not in (None, bt['id']) for doc in docs):
                return
            self.follow(bt, docs, 'refund', 'missing_refund_charge_evidence')
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
            pass  # follow records the charge (and the sale when known) before any check refuses.

    def refund(self, bt):
        if not _ref_id(bt.get('source')):
            # Without the refund's own ID nothing proves it was not already booked by another movement.
            raise ValueError('refund_identity_unverified')
        _, docs = self.docs([bt.get('source')], self.refunds, 'refund')
        if not docs:
            raise _Hold('missing_refund_evidence')
        refund = _combine(docs, 'refund', _REFUND_FIELDS)
        self.check_mode(refund)
        linked = _ref_id(refund.get('balance_transaction'))
        if linked is not None and linked != bt['id']:
            # The refund says another movement carries it: this one may book it twice. Unresolved, no entry.
            raise ValueError('refund_reference_conflict')
        if (refund.get('status') != 'succeeded' or refund.get('currency') != self.cur
                or refund.get('amount') != -bt['amount'] or linked is None):
            raise ValueError('missing_or_unsettled_refund')
        charge, found, gross = self.follow(bt, docs, 'refund', 'missing_refund_charge_evidence')
        link = self.links[bt['id']]
        # A refund follows its sale: the same closure (with the refund's own copies), then the sale's treatment.
        invoice, has_invoice, _, link['invoice'] = self.document(charge, found, gross, link['sale'])
        if link['sale'] is None:
            raise _Hold('sale_movement_unverified')
        if has_invoice and (not invoice or not isinstance(invoice.get('lines'), dict)
                            or invoice['lines'].get('has_more') is not False
                            or len(invoice['lines'].get('data') or []) != 1):
            raise ValueError('mixed_line_refund_requires_allocation')
        # A single line still can contain tax, discounts, credits or
        # partial payment. Do not turn a refused original sale into an
        # apparently safe full contra-revenue refund.
        probe = dict(link)
        self.allocations(charge, found, gross, link['sale'], probe)
        link['sale_closure_digest'] = probe.get('closure_digest')

    def place(self, bt):
        """``(postings, authority)`` for a sale or refund movement; a ValueError leaves it unresolved.

        Nothing is held (round 9): a ``_Hold`` (an uncertain identity) is unresolved with its reason, with no entry
        and no holding movement, like any other error. Every dispute movement is unresolved (``dispute_needs_review``):
        no trusted policy names a dispute account, and the owner map still reserves its IDs.
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
                raise ValueError('dispute_needs_review')
        except _Hold as hold:
            raise ValueError(str(hold)) from None
        if bt['fee']:
            postings.append((policy['fee_account'], bt['fee']))
        return postings, authority


def _is_dispute(source):
    identity = _object_id(source)
    return ((isinstance(source, dict) and source.get('object') == 'dispute')
            or (isinstance(identity, str) and identity.startswith(('du_', 'dp_'))))


_FEE_KINDS = frozenset({'stripe_fee', 'stripe_fx_fee'})
_DISPUTE_CATEGORIES = frozenset({'dispute', 'dispute_reversal', 'dispute_fee'})


def _dispute_fee_signal(bt):
    """Whether movement *bt* shows it is a dispute fee without itself naming the dispute (round 11, decision 5).

    A Connect platform can pay a connected account's dispute fee; Stripe attributes such a fee to the dispute in
    fee-attribution data (``incurred_by``, ``incurred_by_type``) rather than in the movement's own ``source``. For
    launch the engine never books one as a standalone fee: any structured sign of a dispute (a dispute reporting
    category, a fee attributed to a dispute, a fee detail of a dispute type) and, for a fee movement, the word
    "dispute" in its own description or fee details leave it unresolved (``dispute_needs_review``). Text only ever
    refuses; it never decides an account."""
    if bt.get('reporting_category') in _DISPUTE_CATEGORIES or bt.get('incurred_by_type') == 'dispute':
        return True
    if _is_dispute(bt.get('incurred_by')):
        return True
    details = bt.get('fee_details') if isinstance(bt.get('fee_details'), list) else []
    for row in details:
        if isinstance(row, dict) and (row.get('incurred_by_type') == 'dispute' or _is_dispute(row.get('incurred_by'))
                                      or 'dispute' in str(row.get('type') or '').lower()):
            return True
    if bt.get('type') in _FEE_KINDS:
        text = [bt.get('description')] + [row.get('description') for row in details if isinstance(row, dict)]
        return any('dispute' in value.lower() for value in text if isinstance(value, str))
    return False


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
    """The fingerprint a finalization receipt binds for one movement: the full hash of its closure (see _Capture).

    A sale's fingerprint is the same hash its entry records as ``stripe-closure``. Every object the movement can
    reach, every field of it and every copy of it is bound, including the movement's own status and extra fields.
    When any object in the closure has copies that disagree there is no fingerprint: this raises, and the plan
    records the fingerprint as unavailable (never evidence)."""
    capture = objects.get('_capture') or _Capture(objects)  # The plan builds one; a caller without it gets a fresh one.
    digest = capture.digest(bt['id'], sale=bt.get('type') in _SALE_KINDS)
    if digest is None:
        raise ValueError('conflicting_object_copies')
    return digest


def _unavailable_fingerprint(sid):
    """The capture digest recorded when a movement's evidence cannot be fingerprinted. It is never evidence."""
    return _digest({'invalid_source': sid})


class _LedgerClaims:
    """What the ledger proves about Stripe movements, from the structured metadata this engine writes.

    **No historical proof.** Every entry this engine writes records its kind (``stripe-kind``). A Stripe entry whose
    lineage starts anywhere else (an earlier release, an import, a hand-written entry, or a correction of a Stripe
    movement whose original is not in the ledger) is never interpreted: ``legacy_present`` is set, and the plan then
    leaves every new Stripe movement unresolved with no journal entry (``legacy_stripe_entries_present``) until a
    cutover is recorded. Saved digests, names, rule IDs and postings are never read to decide what an entry was.

    **Claims are reservations.** Every entry that names a charge, payment intent or invoice in its metadata (live,
    held, corrected, reversed, a reversal or a correction) reserves it for good: ``claims`` maps ``(kind, ID)`` to
    the roots (the entry a lineage of corrections and reversals starts from) whose lineage names it. A correction
    never frees a claim and adds its own. ``refunds`` maps a refund's provider ID to the roots that posted it, and
    ``payouts`` a payout's provider ID likewise. ``dispute_movements`` maps a dispute ID an earlier build recorded to
    the movements it recorded with it; they stay reserved for that dispute.

    ``sales`` maps a sale movement this engine wrote to what a refund may rely on: its recorded links, closure
    hash and gross, whether it is ``held``, and whether it is ``pristine`` (never corrected or reversed, with no
    successor at all). ``followers`` maps the root of every refund or dispute ever posted (held, corrected or
    reversed alike) to its recorded charge, sale movement and gross; ``consumed_for`` sums them by sale movement and
    charge. A credit back (a won dispute) never adds to what may be refunded.
    ``anonymous`` lists roots with a successor that has no source ID: everything such a lineage names is unproven.
    """

    def __init__(self, all_entries, reversed_ids, prefix):
        self.prefix, self.reversed_ids = prefix, reversed_ids
        by_source = {entry.source_id: entry for entry in all_entries if entry.source_id}

        def find(source):
            return next((by_source[value] for value in sorted(source_id_variants(source)) if value in by_source), None)

        def label(source):
            return next((value for value in sorted(source_id_variants(source)) if value.startswith(prefix)), source)

        def stripe(source):
            return bool(source) and any(value.startswith('stripe:') for value in source_id_variants(source))

        def engine_written(entry):
            """Whether structured metadata proves this engine version wrote *entry* (the root of a Stripe lineage):
            a known kind and the Stripe account its own source ID names. Its account's own sales, refunds, disputes
            and other movements count, as does another account's entry of any known kind and the invoice engine's
            settlement entry (which also names its invoice). Another account's entries are outside this import's
            lineage, so they never block it; they still reserve every ID they name. Nothing else does: no text, prefix or missing field."""
            if entry is None:
                return False
            meta = dict(entry.meta)
            kind, owner = meta.get(_KIND_META), meta.get(_ACCOUNT_META)
            if meta.get(_ENGINE_META) != _ENGINE_VERSION:
                return False  # Written by an earlier build (round 10, decision 3): legacy, never interpreted.
            if kind not in _ENGINE_KINDS or not owner or ':' in owner or not any(
                    value.startswith(f'stripe:{owner}:') for value in source_id_variants(entry.source_id)):
                return False
            return kind != 'invoice_settlement' or bool(meta.get(_OWNERSHIP_META['invoice']))

        def root_of(entry):
            """``(root label, root entry or None)``: the entry a lineage starts from, or the ID it names."""
            seen, current = set(), entry
            while True:
                meta = dict(current.meta)
                parent = meta.get('reverses') or meta.get('correction-of')
                if not parent or parent in seen:
                    return (label(current.source_id) if current.source_id else None), current
                seen.add(parent)
                found = find(parent)
                if found is None:
                    return label(parent), None
                current = found

        self.claims, self.refunds, self.payouts, self.followers, self.sales = {}, {}, {}, {}, {}
        self.dispute_movements = {}  # Dispute ID -> the movements an earlier build recorded with it.
        self.anonymous, self.successors = set(), {}
        self.legacy_present = self.refunds_unproven = False
        roots = {}
        for index, entry in enumerate(all_entries):
            meta = dict(entry.meta)
            root, root_entry = root_of(entry)
            root = root or f'#entry-{index}'
            if stripe(root) and not engine_written(root_entry):
                self.legacy_present = True  # A Stripe lineage this engine did not start.
            elif stripe(entry.source_id) and not engine_written(entry) and not engine_written(root_entry):
                # Any entry of a lineage counts (round 9, cut 7): a correction that introduces a Stripe source ID
                # into a lineage this engine did not start is legacy, though its root is not a Stripe entry at all.
                self.legacy_present = True
            if root.startswith(prefix) and root_entry is not None:
                roots[root] = root_entry
            if meta.get('correction-of') and not meta.get('reverses'):  # A reversal copies its original's metadata.
                self.successors.setdefault(root, []).append(entry)
                if not entry.source_id:
                    self.anonymous.add(root)
            for kind in _OWNED_KINDS:
                for value in str(meta.get(_OWNERSHIP_META[kind], '')).split():
                    self.claims.setdefault((kind, value), set()).add(root)
            if meta.get(_REFUND_META):
                self.refunds.setdefault(meta[_REFUND_META], set()).add(root)
            if meta.get(_PAYOUT_META):
                self.payouts.setdefault(meta[_PAYOUT_META], set()).add(root)
            if meta.get(_DISPUTE_META):
                self.dispute_movements.setdefault(meta[_DISPUTE_META], set()).update(
                    str(meta.get(_DISPUTE_MOVEMENTS_META, '')).split())
        for root, entry in sorted(roots.items()):
            meta = dict(entry.meta)
            kind = meta.get(_KIND_META)
            if kind in ('refund', 'dispute'):
                charge = meta.get('stripe-refund-of')
                gross = sum((p.amount for p in entry.postings if p.account.startswith('Income:') and p.amount > 0),
                            Decimal(0))
                self.followers[root] = (charge, meta.get('stripe-sale'), gross)
                if charge or meta.get('stripe-sale'):
                    pass
                elif gross and not meta.get(_DISPUTE_META if kind == 'dispute' else _REFUND_META):
                    # Consumed some charge's revenue, but which one is not recorded. A refund or dispute that names
                    # its own provider ID needs no such flag: ``refunds`` or ``disputes`` reserves that ID (and the
                    # ledger its movement), so the same refund or dispute captured again, with a charge or without,
                    # never posts; other refunds and disputes are judged on their own sales.
                    self.refunds_unproven = True
                continue
            if kind != 'sale':
                continue
            postings = entry.postings
            self.sales[root[len(prefix):]] = dict(
                charge=set(str(meta.get(_OWNERSHIP_META['charge'], '')).split()),
                payment_intent=set(str(meta.get(_OWNERSHIP_META['payment_intent'], '')).split()),
                invoice=set(str(meta.get(_OWNERSHIP_META['invoice'], '')).split()),
                closure=meta.get(_CLOSURE_META),
                held=any(p.account == STRIPE_HOLDING_ACCOUNT for p in postings),
                pristine=(root not in self.successors and not source_id_variants(entry.source_id) & reversed_ids),
                gross=-sum((p.amount for p in postings if p.account.startswith('Income:') and p.amount < 0), Decimal(0)))

    def consumed_for(self, sale, charges):
        """The gross of every refund or dispute ever posted against sale movement *sale* or any of *charges*."""
        return sum((gross for charge, follows, gross in self.followers.values() if follows == sale or charge in charges),
                   Decimal(0))


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
    all_entries = store.load_entries()
    entries = {entry.source_id: entry for entry in all_entries if entry.source_id}
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
                  entry_links={}, next_cursor=None,
                  source_capture_digests={},
                  policy_digest=_digest(policy), evidence_digest=_digest(evidence), ledger_digest=store.content_digest())
    objects = dict(charges=charges, refunds=refunds, invoices=invoices, payouts=payouts, prices=prices,
                   products=products, paymentIntents=intents, invoicePayments=invoice_payments,
                   checkoutSessions=sessions, disputes=disputes, balanceTransactions=bts,
                   connectEvidence=evidence.get('connectEvidence') or {})
    all_ids = sorted(sid for sid in validated if sid > after_source_id)
    selected = all_ids[:limit]
    result['next_cursor'] = selected[-1] if len(all_ids) > limit else None
    # Ownership is decided over the whole supplied cohort, before any paging (round 10, decision 1): every validated
    # movement is classified and claims its IDs, whatever page it is on; only then is the result restricted to the
    # page. The same capture gives the same decision for every row whatever the limit or cursor.
    page = frozenset(selected)
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

    prefix = f"stripe:{account}:"
    ledger = _LedgerClaims(all_entries, reversed_ids, prefix)
    # A source recorded with no financial effect (a paid zero payout) has no entry, so its saved source is its durable
    # reservation (round 10, decision 4): the IDs it claimed when it was verified. One an earlier build recorded
    # names no claims; like that build's entries, it is legacy (decision 3).
    no_effect_claims = {}
    for saved_id, row in sorted(saved.items()):
        if not isinstance(row, dict) or not row.get('stripe_no_effect_capture_digest'):
            continue
        recorded = row.get(_NO_EFFECT_CLAIMS)
        if not isinstance(recorded, list):
            ledger.legacy_present = True
            continue
        for pair in recorded:
            if isinstance(pair, list) and len(pair) == 2 and all(isinstance(part, str) for part in pair):
                no_effect_claims.setdefault(tuple(pair), set()).add(saved_id)
    resolver = _Resolver(policy, bts, objects)
    capture = resolver.capture

    def to_unresolved(action, reason):
        """A known movement whose allocation is not proven stays unposted."""
        result['actions'].remove(action)
        result['uncategorized'] = [row for row in result['uncategorized'] if row['source_id'] != action['source_id']]
        result['unresolved'].append(dict(source_id=action['source_id'], reason=reason))

    def movement(action):
        """The balance transaction of a fresh sale, refund or dispute action; None for anything else."""
        if action is None or action['source_id'] not in validated:
            return None
        bt = validated[action['source_id']][0]
        return bt if action['kind'] == bt.get('type') and _identity_movement(bt) else None

    def owned_ids(bt):
        """``{kind: ids}`` a movement names: its charge, and every charge, payment intent and invoice named on any
        side of its closure (see _Closure.named_ids), so a held sale reserves everything it names."""
        link = resolver.links.get(bt['id']) or {}
        charge = link.get('charge') if bt.get('type') not in _SALE_KINDS else _ref_id(bt.get('source'))
        found = link.get('closure')
        ids = found.named_ids() if found else dict(charge=set(), payment_intent=set(), invoice=set())
        if charge:
            ids['charge'].add(charge)
        if bt.get('type') in _SALE_KINDS:
            for kind, values in capture.graph_ids(bt['id']).items():  # Every ID reachable at any depth (cut 5).
                ids[kind] |= values
        return ids

    def unresolve(sid, reason):
        """Leave *sid* unresolved with *reason* (its first reason wins), dropping any action it had."""
        action = next((row for row in result['actions'] if row['source_id'] == sid), None)
        if action is not None:
            to_unresolved(action, reason)
        elif any(row['source_id'] == sid for row in result['no_effect']):
            result['no_effect'] = [row for row in result['no_effect'] if row['source_id'] != sid]
            result['unresolved'].append(dict(source_id=sid, reason=reason))
        elif not any(row['source_id'] == sid for row in result['unresolved']):
            result['unresolved'].append(dict(source_id=sid, reason=reason))

    def owner_map():
        """One owner for every ID, decided before anything posts (round 9, cut 1).

        Every movement of the whole supplied capture (round 10, decision 1: every page, not only this one) that is
        proposed, unresolved or accepted with no financial effect, and every durable reservation (ledger entries and
        verified no-effect sources), claims the IDs it owns: its balance transaction; a sale every charge, payment intent and invoice in its fingerprinted
        closure (at any depth); a refund its refund ID; a payout its payout ID; a dispute its dispute ID and every
        balance transaction any copy of it lists (whatever that movement's type), and a movement whose source is a
        dispute is that dispute's. An ID with two owners leaves every claimant in the capture unresolved with no entry.
        """
        owners = {}

        def claim(key, owner):
            owners.setdefault(key, set()).add(owner)

        for key, roots in ledger.claims.items():
            for root in roots:
                claim(key, ('ledger', root))
        for name, index in (('refund', ledger.refunds), ('payout', ledger.payouts)):
            for value, roots in index.items():
                for root in roots:
                    claim((name, value), ('ledger', root))
        for key, sources in no_effect_claims.items():
            for value in sources:
                claim(key, ('ledger', value))  # A verified no-effect source (round 10, decision 4).
        for dispute, movements in ledger.dispute_movements.items():
            for value in movements:
                claim(('bt', value), ('dispute', dispute))
        for movement_id, listed_by in capture.dispute_bts().items():
            for dispute in listed_by:
                claim(('bt', movement_id), ('dispute', dispute))
        claimants = [row['source_id'] for row in result['actions'] + result['unresolved'] + result['no_effect']
                     if row['source_id'] in validated]
        for sid in claimants:
            bt = validated[sid][0]
            kind, source = bt.get('type'), _ref_id(bt.get('source'))
            dispute = capture.dispute_source(bt)
            if dispute:
                claim(('bt', bt['id']), ('dispute', dispute))  # A dispute link wins over the type (decision 2).
            else:
                claim(('bt', bt['id']), sid)
            if kind in _SALE_KINDS:
                for name, values in owned_ids(bt).items():
                    for value in values:
                        claim((name, value), sid)
            elif kind in _REFUND_KINDS and source:
                claim(('refund', source), sid)
            elif kind == 'payout' and source:
                claim(('payout', source), sid)
        def same(owner):
            # A source's own durable record (its no-effect reservation, or its entry when it changed) is not a rival.
            return owner[1] if isinstance(owner, tuple) and owner[0] == 'ledger' else owner

        batch, order = set(claimants), ('charge', 'payment_intent', 'invoice', 'refund', 'payout', 'bt')
        for key in sorted(owners, key=lambda key: (order.index(key[0]), key[1])):
            found = owners[key]
            if len({same(owner) for owner in found}) < 2:
                continue
            hit = {owner for owner in found if owner in batch}
            if key[0] == 'bt' and prefix + key[1] in batch:
                hit.add(prefix + key[1])  # A movement whose own ID two disputes (or a dispute and another owner) claim.
            for sid in sorted(hit):
                durable = {owner[1] for owner in found - {sid}
                           if isinstance(owner, tuple) and owner[0] == 'ledger' and owner[1] != sid}
                if key[0] in _OWNED_KINDS:
                    reason = 'ownership_unproven' if durable & ledger.anonymous else _CLAIM_REASONS[key[0]]
                elif key[0] == 'refund':
                    reason = 'refund_already_recorded' if durable else 'duplicate_refund_movement'
                elif key[0] == 'payout':
                    reason = 'payout_already_recorded' if durable else 'duplicate_payout_movement'
                else:
                    reason = 'movement_claimed_by_another_owner'
                unresolve(sid, reason)

    def booked_sale(link):
        """``(sale record, stored charges, gross)`` for the sale a refund names: a sale entry this engine wrote, or an
        approved sale action in this batch (booked in the same write); ``(None, None, None)`` otherwise."""
        sale = link.get('sale')
        record = ledger.sales.get(sale) if sale else None
        if record is not None:
            return record, set(record['charge']), record['gross']
        # Booked in the same write: an approved sale action on this page (a sale on another page is not yet booked).
        action = next((row for row in result['actions'] if row['source_id'] == prefix + str(sale)
                       and row['source_id'] in page), None)
        sale_bt = movement(action)
        if sale_bt is not None and sale_bt.get('type') in _SALE_KINDS and _ref_id(sale_bt.get('source')):
            return None, {_ref_id(sale_bt['source'])}, Decimal(_amount(sale_bt['amount']))
        return None, None, None

    def exceeds_cap(bt, link, proposed):
        """Whether refund *bt* takes more than its booked sale's gross, with every refund or dispute ever posted
        against that sale movement or its stored charges and every other refund this batch proposes for it (cut 4).
        None when the booked sale is not known. A credit back (a won dispute) never raises the cap."""
        record, charges, gross = booked_sale(link)
        if gross is None:
            return None
        sale, aliases = link.get('sale'), charges | {link.get('charge')}
        # Each identifiable claim counts once (round 11, decision 3): a refund ID the ledger already recorded counts
        # there, and two captured movements of one refund ID (or of this refund's own ID) are one claim.
        counted, others = {_ref_id(bt.get('source'))} - {None}, Decimal(0)
        for other in proposed:
            other_link = resolver.links.get(other['id']) or {}
            if other is bt or not (other_link.get('sale') == sale or other_link.get('charge') in aliases):
                continue
            refund_id = _ref_id(other.get('source'))
            if refund_id and (refund_id in counted or refund_id in ledger.refunds):
                continue
            counted.add(refund_id)
            others += Decimal(_amount(-other['amount']))
        used = ledger.consumed_for(sale, aliases) + others
        return used + Decimal(_amount(-bt['amount'])) > gross

    def refund_reason(bt, proposed):
        """Why refund *bt* may not post to contra-revenue (round 9, cut 4), or None. Nothing is ever held: a refund
        posts only when it names a sale movement booked by this engine (or approved in this batch), its charge is
        that sale's one stored charge, the sale's current fingerprint is the stored one, and the cap holds."""
        link = resolver.links.get(bt['id']) or {}
        charge, sale = link.get('charge'), link.get('sale')
        if not charge or not sale or link.get('closure') is None:
            return 'sale_movement_unverified'
        sale_sid = prefix + sale
        record, charges, gross = booked_sale(link)
        if gross is None:
            if any(row['source_id'] == sale_sid for row in result['unresolved']):
                return 'sale_not_posted'
            return 'sale_movement_unverified'
        if exceeds_cap(bt, link, proposed):
            return 'refund_exceeds_booked_sale'
        if ledger.refunds_unproven:
            return 'ownership_unproven'
        if charges != {charge}:
            return 'refund_charge_conflict'  # Contradictory attribution: not the booked sale's one charge.
        if sale_sid in validated:
            sale_bt = validated[sale_sid][0]
            if (changed(sale_sid) or sale_sid in reversed_ids or sale_bt.get('status') not in {'available', 'pending'}
                    or _ref_id(sale_bt.get('source')) != charge):
                return 'sale_movement_changed'
        ids = owned_ids(bt)
        if record is not None:
            if not record['pristine'] or sale_sid in ledger.anonymous:
                return 'sale_corrected'
            if record['held']:
                return 'sale_in_holding'
            if ((record['charge'], record['payment_intent'], record['invoice'])
                    != (ids['charge'], ids['payment_intent'], ids['invoice'])
                    or not record['closure'] or record['closure'] != link.get('sale_closure_digest')):
                return 'sale_changed_since_booking'
        else:
            sale_bt = validated[sale_sid][0]
            sale_link = resolver.links.get(sale) or {}
            if (owned_ids(sale_bt) != ids or sale_link.get('invoice') != link.get('invoice')
                    or not sale_link.get('closure_digest') or sale_link['closure_digest'] != link.get('sale_closure_digest')):
                return 'sale_links_changed'
        for kind in _OWNED_KINDS:
            for value in sorted(ids[kind]):
                if ledger.claims.get((kind, value), set()) - {sale_sid}:
                    return _CLAIM_REASONS[kind]
        return None

    def follow_sales():
        """A refund posts to contra-revenue only behind proof of its booked sale, once, within what the sale booked.

        The cap is never skipped (cut 4): every refund movement the resolver reached counts as proposed, and one the
        resolver or the owner map already refused still reports ``refund_exceeds_booked_sale`` when it would exceed
        its known booked sale."""
        rows = [row['source_id'] for row in result['actions'] + result['unresolved']
                if row['source_id'] in validated and row['source_id'] not in posted]  # A posted one counts from the ledger.
        proposed = [bt for bt in (validated[sid][0] for sid in dict.fromkeys(rows))
                    if bt.get('type') in _REFUND_KINDS and bt['id'] in resolver.links]
        for bt in proposed:
            sid = prefix + bt['id']
            action = next((row for row in result['actions'] if row['source_id'] == sid), None)
            if action is not None and movement(action) is not None:
                reason = refund_reason(bt, proposed)
                if reason:
                    to_unresolved(action, reason)
            elif exceeds_cap(bt, resolver.links[bt['id']], proposed):
                for row in result['unresolved']:
                    if row['source_id'] == sid:
                        row['reason'] = 'refund_exceeds_booked_sale'

    def link_entries():
        """Durable kind, claims and links for each final entry, written as entry metadata in the same transaction."""
        for action in result['actions']:
            if action['source_id'] not in validated:
                continue
            bt = movement(action)
            meta = {_KIND_META: 'other', _ACCOUNT_META: account, _ENGINE_META: _ENGINE_VERSION}
            raw = validated[action['source_id']][0]
            if raw.get('type') == 'payout' and _ref_id(raw.get('source')):
                meta[_PAYOUT_META] = _ref_id(raw['source'])  # One paid payout books once (round 9, cut 8).
            if bt is not None:
                link = resolver.links.get(bt['id']) or {}
                if bt.get('type') in _SALE_KINDS:
                    meta[_KIND_META] = 'sale'
                    for kind, ids in owned_ids(bt).items():
                        if ids:
                            meta[_OWNERSHIP_META[kind]] = ' '.join(sorted(ids))
                    capture_digest = result['source_capture_digests'].get(action['source_id'])
                    if link.get('closure_digest') == capture_digest:
                        meta[_CLOSURE_META] = capture_digest  # The same full hash cleanup's receipt binds.
                elif bt.get('type') in _REFUND_KINDS:
                    meta[_KIND_META] = 'refund'
                    if link.get('charge'):
                        meta['stripe-refund-of'] = link['charge']
                    if link.get('sale'):
                        meta['stripe-sale'] = link['sale']
                    if _ref_id(bt.get('source')):
                        meta[_REFUND_META] = _ref_id(bt['source'])
            result['entry_links'][action['source_id']] = sorted([key, value] for key, value in meta.items())

    for sid in sorted(validated):  # The whole cohort (decision 1); the result is restricted to the page below.
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
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
            # Ordinary validation below holds malformed new movements; a movement that would post or record
            # no effect without a fingerprint stays unresolved instead. The marker is never evidence: cleanup of
            # a posted source whose fingerprint is unavailable needs review (see _recover_stripe_cleanup).
            result['source_capture_digests'][sid] = _unavailable_fingerprint(sid)
        if bt.get('type') in _REFUND_KINDS and _identity_movement(bt) and sid not in posted:
            # Record a refund's claim on its sale before any posting check (round 10, decision 5; round 11, decision
            # 3): every identifiable refund of a sale counts toward its cap once, including one unresolved for another
            # reason (an invalid status, a pending refund document, a changed source). A posted one counts from the
            # ledger instead.
            resolver.trace = []
            resolver.attribute(bt)
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
            if _identity_movement(bt) and (bt.get('type') in _SALE_KINDS or bt.get('type') in _REFUND_KINDS):
                # Cleanup clears a receipt only when today's fingerprint binds everything the allocator reads today.
                resolver.trace = []
                try:
                    resolver.place(bt)
                except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
                    pass
                if not capture.covers(bt, resolver.trace):
                    result['source_capture_digests'][sid] = _unavailable_fingerprint(sid)
            # Posted once; an entry is never posted again.
            result['skipped'].append(dict(source_id=sid, reason='already_posted'))
            continue
        result['normalized_sources'].append(normalized)
        try:
            if ledger.legacy_present:
                # No historical proof: a ledger with any Stripe entry this engine did not write gets no new Stripe
                # journal entry at all (no holding entry, no cash movement) until a cutover is recorded.
                raise ValueError('legacy_stripe_entries_present')
            if capture.mode_mismatch(bt['id'], policy['livemode']):
                # Test and live never mix (round 11, decision 2): the movement or any object linked to it states the
                # other mode, or its copies disagree. No entry; the owner map still reserves its IDs.
                raise ValueError('livemode_mismatch')
            if capture.conflicts(bt['id']):
                raise ValueError('conflicting_object_copies')  # One copy per object or nothing (see _Capture).
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
            if capture.dispute_movement(bt) or _dispute_fee_signal(bt):
                # A dispute link always wins over the movement's type (round 10, decision 2): a fee or any other
                # movement whose source is a dispute, or that any dispute document lists, is a dispute movement. So is
                # a platform-side dispute fee that does not name its connected-account dispute (round 11, decision 5).
                raise ValueError('dispute_needs_review')
            kind = bt.get('type')
            source_id = _object_id(bt.get('source'))
            source = bt.get('source') if isinstance(bt.get('source'), dict) else None
            postings = [(policy['clearing_account'], bt['net'])]
            authority = []
            try:
                if _identity_movement(bt):
                    resolver.trace = []
                    postings, authority = resolver.place(bt)
                    if not capture.covers(bt, resolver.trace):
                        # The allocator read something the fingerprint does not bind (round 9, cut 6).
                        result['source_capture_digests'][sid] = _unavailable_fingerprint(sid)
                        raise ValueError('capture_fingerprint_incomplete')
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
                    # An ID-only wrapper ({"id", "object"}) is a reference: the full document comes from payouts.
                    payout = source if _is_document(source) else payouts.get(source_id)
                    if (not payout or _object_id(payout.get('balance_transaction')) != bt['id'] or payout.get('amount') != -bt['amount']
                            or payout.get('currency') != cur or payout.get('status') not in {'paid', 'in_transit', 'pending'}):
                        raise ValueError('missing_or_failed_payout')
                    if bt['amount'] == 0:
                        if bt['fee'] != 0 or payout.get('status') != 'paid' or payout.get('failure_balance_transaction'):
                            raise ValueError('invalid_zero_payout')
                        fingerprint = result['source_capture_digests'][sid]
                        if fingerprint == _unavailable_fingerprint(sid):
                            raise ValueError('capture_fingerprint_unavailable')
                        previous_effect = (saved.get(sid) or {}).get('stripe_no_effect_capture_digest')
                        if sid in saved and previous_effect != fingerprint:
                            raise ValueError('changed_no_effect_evidence')
                        # Its saved source durably reserves its IDs (round 10, decision 4).
                        reserved = [['payout', source_id]] if _ref_id(source_id) else []
                        result['no_effect'].append(dict(source_id=sid, already_recorded=bool(previous_effect),
                            normalized_source={**normalized, 'stripe_no_effect_capture_digest': fingerprint,
                                               _NO_EFFECT_CLAIMS: reserved}))
                        continue
                    postings.append((policy['payout_transit_account'], -bt['amount']))
                    if bt['fee']:
                        postings.append((policy['fee_account'], bt['fee']))
                    authority = ['payout-transit-policy']
                else:
                    raise ValueError('unsupported_balance_type')
            except _Hold as hold:
                raise ValueError(str(hold)) from None  # Nothing is held (round 9, cut 3).
            if sum(amount for _, amount in postings) != 0:
                raise ValueError('unbalanced_stripe_evidence')
            if result['source_capture_digests'][sid] == _unavailable_fingerprint(sid):
                raise ValueError('capture_fingerprint_unavailable')
            posting_docs = [dict(account=acc, amount=_amount(amount), currency=book) for acc, amount in postings if amount]
            for posting in posting_docs:
                opened = opens.get(posting['account'])
                if opened is None or opened.date > date.fromisoformat(normalized['date']) or (opened.currencies and book not in opened.currencies):
                    raise ValueError('account_catalog_conflict')
            action = dict(source_id=sid, source_digest=_digest(normalized), kind=kind,
                          authority_id=','.join(authority), postings=posting_docs, normalized_source=normalized)
            result['actions'].append(action)
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            # A payout match conflict holds the payout cohort too: never quietly
            # add processor detail over an old bank deposit booked as income.
            result['actions'] = [row for row in result['actions'] if row['source_id'] != sid]
            result['uncategorized'] = [row for row in result['uncategorized'] if row['source_id'] != sid]
            result['unresolved'].append(dict(source_id=sid, reason=str(exc)))
    owner_map()
    follow_sales()
    # Match bank arrivals separately: a payout may have posted to transit in a previous cohort/month before its real
    # bank deposit arrived. Every veto is decided over the whole supplied cohort, like ownership (round 11, decision
    # 1): a known bank conflict on any payout, on any page, blocks every page for every limit and cursor.
    bank_rows, bank_conflict = [], None
    for sid in sorted(validated):
        bt, normalized = validated[sid]
        payout = _object_id(bt.get('source'))
        if bt.get('type') != 'payout' or payout not in by_payout:
            continue
        bank_sid = by_payout[payout]['bank_source_id']
        bank_rows.append((sid, 'digest', bank_sid, _digest({
            'payout_capture': result['source_capture_digests'][sid], 'binding': by_payout[payout]})))
        original = saved.get(sid)
        already_applied = (sid in posted and sid not in reversed_ids and original
                           and original.get('stripe_financial_digest') == normalized['stripe_financial_digest']
                           and _same_source(original, normalized))
        if not any(row['source_id'] == sid for row in result['actions']) and not already_applied:
            continue
        if ledger.legacy_present:
            bank_rows.append((sid, 'unresolved', bank_sid, 'legacy_stripe_entries_present'))
            continue
        try:
            if not policy['enabled']:
                raise ValueError('policy_disabled')
            action = _bank_action(by_payout[payout], payout, bt['amount'], pending, posted, owned, opens, entity, cutoff,
                                  policy['payout_transit_account'], saved, entries, reversed_ids)
            bank_rows.append((sid, 'action', bank_sid, action) if action else (sid, 'skipped', bank_sid, 'already_posted'))
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            bank_conflict = dict(source_id=bank_sid, reason=str(exc))  # Prior bank income must be corrected first.
            break
    # Restrict to the page only now, after ownership, the cap and every veto were decided over the whole cohort.
    for name in ('actions', 'no_effect', 'unresolved', 'skipped', 'uncategorized'):
        result[name] = [row for row in result[name] if row['source_id'] in page]
    result['normalized_sources'] = [row for row in result['normalized_sources'] if row['id'] in page]
    result['source_capture_digests'] = {key: value for key, value in result['source_capture_digests'].items()
                                        if key in page}
    link_entries()
    for sid, what, bank_sid, value in bank_rows:
        if sid not in page:
            continue
        if what == 'digest':
            result['source_capture_digests'][bank_sid] = value
        elif what == 'action':
            result['actions'].append(value)
        else:
            result[what].append(dict(source_id=bank_sid, reason=value))
    if bank_conflict is not None:
        # The conflict is reported on every page, and no page posts anything, whichever page its payout is on.
        result['actions'], result['uncategorized'], result['entry_links'] = [], [], {}
        result['unresolved'].append(bank_conflict)
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
    for sid in sorted(eligible):
        if plan['source_capture_digests'].get(sid) == _unavailable_fingerprint(sid):
            review[sid] = 'the current evidence cannot be fingerprinted'  # Never positive evidence.
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
    if any(action['source_id'] not in staged for action in preview['actions']):
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
        sourced = plan['actions']
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
        if entries:
            actions = {action['source_id']: action for action in sourced}
            body = dict(schema_version=1, kind='stripe-finalization', policy_digest=plan['policy_digest'],
                        evidence_digest=plan['evidence_digest'], sources=[
                            dict(id=entry.source_id, pending_digest=_digest(pending[entry.source_id]),
                                 entry_digest=_entry_fingerprint(entry),
                                 source_digest=_digest(actions[entry.source_id]['normalized_source']),
                                 capture_digest=plan['source_capture_digests'][entry.source_id]) for entry in entries])
            receipt = {**body, 'operation_hash': _digest(body)}
            _atomic_ledger_write_unlocked(entity, [], entries, session_id, ts,
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
        return dict(posted=len(entries), no_financial_effect=len(plan['no_effect']),
                    skipped=len(plan['skipped']), held=len(plan['unresolved']),
                    next_cursor=plan['next_cursor'], plan_digest=plan['plan_digest'],
                    unresolved=[dict(row) for row in plan['unresolved']],
                    needs_review=[dict(row) for row in needs_review],
                    uncategorized=[dict(row) for row in plan['uncategorized']])


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
