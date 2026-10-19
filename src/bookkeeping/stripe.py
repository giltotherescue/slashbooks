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
from .ledger.importer import (_atomic_ledger_write_unlocked, _entity_write_lock,
                             _load_pending_categorization, import_transactions)
from .ledger.model import Entry, Posting
from .ledger.staging import StagingStore
from .ledger.store import LedgerStore, default_store_path
from .routine import _state, routine_source_digest


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


def _policy(policy, opens):
    if not isinstance(policy, dict) or type(policy.get('version')) is not int or policy['version'] < 1 or type(policy.get('enabled')) is not bool:
        raise ValueError('Stripe policy requires a positive version and enabled boolean.')
    _id(policy.get('stripe_account_id'), 'Stripe account')
    if type(policy.get('livemode')) is not bool or policy.get('currency') != 'USD':
        raise ValueError('Stripe policy requires explicit mode and USD currency.')
    roles = {'clearing_account': 'Assets:Processors:', 'payout_transit_account': 'Assets:Transfers:',
             'fee_account': 'Expenses:', 'refund_account': 'Income:'}
    for field, prefix in roles.items():
        account = policy.get(field)
        if not isinstance(account, str) or account not in opens or not account.startswith(prefix):
            raise ValueError(f'{field} must be a catalog {prefix[:-1]} account.')
        if opens[account].currencies and 'USD' not in opens[account].currencies:
            raise ValueError(f'{field} must support USD.')
    if len({policy[field] for field in roles}) != len(roles):
        raise ValueError('Stripe clearing, transit, fees and refunds require distinct accounts.')
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
        if category not in opens or not category.startswith('Income:') or category == policy['refund_account']:
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


def _invoice_for_charge(charge, invoices, invoice_payments, gross, mode):
    ref = charge.get('invoice')
    intent_id = _object_id(charge.get('payment_intent'))
    related = [row for row in invoice_payments.values()
               if intent_id and (row.get('payment') or {}).get('type') == 'payment_intent'
               and _object_id((row.get('payment') or {}).get('payment_intent')) == intent_id]
    if related:
        if (len(related) != 1 or related[0].get('status') != 'paid'
                or related[0].get('amount_paid') != gross or related[0].get('amount_requested') != gross
                or related[0].get('currency') != 'usd' or related[0].get('livemode') is not mode):
            raise ValueError('partial_or_multiple_invoice_payments')
        resolved = _object_id(related[0].get('invoice'))
        if not resolved or (ref and _object_id(ref) != resolved):
            raise ValueError('invoice_payment_identity_conflict')
        ref = related[0]['invoice']
    return (ref if isinstance(ref, dict) else invoices.get(ref)), bool(ref)


def _sale_document(charge, invoices, invoice_payments, sessions, gross, mode):
    invoice, found = _invoice_for_charge(charge, invoices, invoice_payments, gross, mode)
    if found:
        return invoice, True
    intent = _object_id(charge.get('payment_intent'))
    matches = [row for row in sessions.values() if intent and _object_id(row.get('payment_intent')) == intent]
    if not matches:
        return None, False
    if len(matches) != 1:
        raise ValueError('multiple_checkout_sessions')
    session = matches[0]
    if (session.get('payment_status') != 'paid' or session.get('status') != 'complete'
            or session.get('amount_total') != gross or session.get('amount_subtotal') != gross
            or session.get('currency') != 'usd'):
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


def _sale_allocations(charge, invoices, gross, policy, prices, products, intents, invoice_payments, sessions):
    if charge.get('status') != 'succeeded' or charge.get('paid') is not True or charge.get('captured') is not True:
        raise ValueError('unsettled_charge')
    if _minor(charge.get('amount')) != gross or charge.get('currency') != 'usd':
        raise ValueError('charge_amount_conflict')
    invoice, has_invoice = _sale_document(charge, invoices, invoice_payments, sessions, gross, policy['livemode'])
    intent = charge.get('payment_intent')
    context = {'charge': charge, 'payment_intent': intent if isinstance(intent, dict) else intents.get(intent, {})}
    if has_invoice:
        if not invoice:
            raise ValueError('missing_invoice_evidence')
        lines = invoice.get('lines')
        if (not isinstance(lines, dict) or lines.get('has_more') is not False
                or not isinstance(lines.get('data'), list) or not 1 <= len(lines['data']) <= 100):
            raise ValueError('incomplete_invoice_lines')
        if invoice.get('status') != 'paid' or invoice.get('amount_paid') != gross or invoice.get('amount_due') != gross or invoice.get('total') != gross:
            raise ValueError('partial_or_adjusted_invoice')
        if any(invoice.get(field) for field in ('total_tax_amounts', 'total_taxes', 'total_discount_amounts', 'shipping_cost', 'pre_payment_credit_notes_amount', 'post_payment_credit_notes_amount', 'starting_balance', 'ending_balance')):
            raise ValueError('tax_discount_or_credit_requires_review')
        allocations = []
        for line in lines['data']:
            if not isinstance(line, dict) or line.get('currency') != 'usd' or _minor(line.get('amount')) <= 0:
                raise ValueError('unsupported_invoice_line')
            if any(line.get(field) for field in ('discount_amounts', 'taxes', 'tax_amounts', 'pretax_credit_amounts')):
                raise ValueError('tax_discount_or_credit_requires_review')
            parent = line.get('parent') or {}
            if line.get('proration') or any((parent.get(key) or {}).get('proration') for key in ('invoice_item_details', 'subscription_item_details')):
                raise ValueError('proration_requires_review')
            raw_price = line.get('price') or (line.get('pricing') or {}).get('price_details') or {}
            price_id = raw_price if isinstance(raw_price, str) else raw_price.get('id') or raw_price.get('price')
            price = prices.get(price_id) or (raw_price if isinstance(raw_price, dict) else {})
            product_id = _object_id(price.get('product'))
            product = products.get(product_id, {})
            rule = _revenue(line, policy, {**context, 'invoice': invoice, 'invoice_line': line, 'price': price, 'product': product})
            allocations.append((rule['category'], _minor(line['amount']), rule['id']))
        if sum(amount for _, amount, _ in allocations) != gross:
            raise ValueError('invoice_allocation_mismatch')
        return allocations
    rule = _revenue(charge, policy, context)
    return [(rule['category'], gross, rule['id'])]


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
        elif kind == 'stripe_fee' and amount < 0 and fee == 0:
            row['fees'] -= amount
        elif kind == 'payout' and amount < 0 and fee == 0:
            row['payouts'] -= amount
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
    return dict(stripe_account_id=(evidence.get('account') or {}).get('id'),
                livemode=evidence.get('livemode', (evidence.get('account') or {}).get('livemode')),
                coverage=evidence.get('coverage'), transaction_count=len(bts), balance_transaction_ids=sorted(bts),
                by_currency=[totals[key] for key in sorted(totals)], unsupported=unsupported,
                metadata_options=metadata[:500], prices=list(_objects(evidence, 'prices').values())[:500],
                products=list(_objects(evidence, 'products').values())[:500])


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
    for field, identity in (('prices', 'id'), ('products', 'id'), ('unsupported', 'balance_transaction_id')):
        rows = {}
        for summary in (previous, current):
            if not isinstance(summary.get(field), list): raise ValueError('Invalid preview discovery array.')
            for row in summary[field]:
                if not isinstance(row, dict): raise ValueError('Invalid preview discovery row.')
                key = _id(row.get(identity), identity)
                if key in rows and rows[key] != row: raise ValueError('Conflicting preview discovery evidence.')
                rows[key] = row
        result[field] = list(rows.values())
    metadata = {_digest(row): row for summary in (previous, current) for row in summary.get('metadata_options', [])}
    result['metadata_options'] = list(metadata.values())[:500]
    return result


def stripe_prepare(entity, *, opening_date):
    """Human-authorized catalog setup using normal audited account creation.

    Creates no journals, source mappings, revenue rules, or opening balances.
    The host obtains the explicit cutover/opening date and publishes via CAS.
    """
    from .entity import add_account
    when = date.fromisoformat(opening_date)
    if when.isoformat() != opening_date:
        raise ValueError('Stripe opening_date must use YYYY-MM-DD.')
    accounts = dict(clearing_account='Assets:Processors:Stripe',
                    payout_transit_account='Assets:Transfers:Stripe-Payouts',
                    fee_account='Expenses:Payment-Processing:Stripe', refund_account='Income:Refunds:Stripe')
    store = LedgerStore(default_store_path(entity.path))
    opened = {item.account: item for item in store.load_opens()}
    for name in accounts.values():
        if name in opened and ((opened[name].currencies and 'USD' not in opened[name].currencies) or opened[name].date > when):
            raise ValueError('Existing Stripe account currency/opening date conflicts; review the catalog explicitly.')
    created, existing = [], []
    for name in accounts.values():
        if name in opened:
            existing.append(name)
        else:
            add_account(entity.path, name, open_date=opening_date, currency='USD'); created.append(name)
    return dict(accounts=accounts, created=created, existing=existing, opening_date=opening_date,
                opening_balance_supplied=False, policy_approved=False)


def stripe_configure(entity, policy):
    """Validate and map only under the host's explicit human policy approval.

    The host publishes this candidate and its D1 policy in one transaction.
    Processor/transit roles cannot reuse another feed's mapped asset account.
    """
    from .entity import map_bank_account
    _, opens, _, _, _ = _state(entity)
    _policy(policy, opens)
    mappings = entity.entity_config.get('bank_account_mappings') or {}
    targets = {policy['clearing_account'], policy['payout_transit_account']}
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
    if sid in posted:
        original = saved.get(sid)
        entry = entries.get(sid)
        expected = {(match['approved_ledger_account'], Decimal(_amount(-net)), 'USD'),
                    (transit, Decimal(_amount(net)), 'USD')}
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
    if ((entity.entity_config.get('bank_account_mappings') or {}).get(bank.get('accountId')) != account
            or account not in opens or not account.startswith('Assets:Bank:')
            or bank.get('accountType') not in {'checking', 'savings'} or bank.get('pending')
            or bank.get('currency', 'USD') != 'USD' or Decimal(str(bank['amount'])) != Decimal(_amount(-net))):
        raise ValueError('bank_payout_match_conflict')
    when = date.fromisoformat(bank['date'])
    if cutoff and when <= cutoff:
        raise ValueError('closed_bank_period')
    if opens[account].date > when or (opens[account].currencies and 'USD' not in opens[account].currencies):
        raise ValueError('bank_account_catalog_conflict')
    if sid in owned:
        raise ValueError('existing_bank_review')
    return dict(source_id=sid, source_digest=routine_source_digest([bank]), kind='bank_payout',
                authority_id=payout, postings=[dict(account=account, amount=f"{Decimal(str(bank['amount'])):.2f}", currency='USD'),
                dict(account=transit, amount=_amount(net), currency='USD')], normalized_source=dict(bank))


def stripe_plan(entity, evidence, policy, *, bank_matches=None, closed_through=None,
                limit=50, after_source_id=''):
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
    _policy(policy, opens)
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
    mapping = (entity.entity_config.get('bank_account_mappings') or {}).get(account)
    if mapping != policy['clearing_account']:
        raise ValueError('Stripe account mapping changed; approve the current processor mapping.')
    cutoff = date.fromisoformat(closed_through) if closed_through else None
    bts = _objects(evidence, 'balanceTransactions')
    charges, refunds, invoices, payouts = (_objects(evidence, name) for name in ('charges', 'refunds', 'invoices', 'payouts'))
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
    result = dict(actions=[], normalized_sources=[], unresolved=[], skipped=[], next_cursor=None,
                  policy_digest=_digest(policy), evidence_digest=_digest(evidence), ledger_digest=store.content_digest())
    all_ids = sorted(sid for sid in validated if sid > after_source_id)
    selected = all_ids[:limit]
    result['next_cursor'] = selected[-1] if len(all_ids) > limit else None
    for sid in selected:
        bt, normalized = validated[sid]
        if sid in reversed_ids:
            result['skipped'].append(dict(source_id=sid, reason='reversed')); continue
        previous = pending.get(sid) or saved.get(sid)
        if previous and (previous.get('stripe_financial_digest') != normalized['stripe_financial_digest'] or not _same_source(previous, normalized)):
            result['unresolved'].append(dict(source_id=sid, reason='changed_source')); continue
        if sid in posted:
            result['skipped'].append(dict(source_id=sid, reason='already_posted')); continue
        result['normalized_sources'].append(normalized)
        try:
            if not policy['enabled']:
                raise ValueError('policy_disabled')
            if cutoff and date.fromisoformat(normalized['date']) <= cutoff:
                raise ValueError('closed_period')
            if bt['currency'] != 'usd':
                raise ValueError('unsupported_currency')
            if bt['status'] not in {'available', 'pending'}:
                raise ValueError('invalid_balance_status')
            if sid in owned:
                raise ValueError('existing_review')
            # Per-account accrual: hold processor activity that could settle an open invoice or fund a schedule.
            if needs_treatment(normalized) or (bt.get('amount') and needs_treatment({**normalized, 'amount': _amount(bt['amount'])})):
                raise ValueError('accrual_treatment_required')
            kind = bt.get('type')
            source_id = _object_id(bt.get('source'))
            source = bt.get('source') if isinstance(bt.get('source'), dict) else None
            postings = [(policy['clearing_account'], bt['net'])]
            authority = []
            if kind in {'charge', 'payment'} and bt['amount'] > 0:
                charge = source or charges.get(source_id)
                if not charge:
                    raise ValueError('missing_charge_evidence')
                if charge.get('livemode') is not policy['livemode'] or _object_id(charge.get('balance_transaction')) != bt['id']:
                    raise ValueError('charge_identity_conflict')
                allocations = _sale_allocations(charge, invoices, bt['amount'], policy, prices, products, intents, invoice_payments, sessions)
                postings.extend((category, -amount) for category, amount, _ in allocations)
                authority = sorted({rule for _, _, rule in allocations})
                if bt['fee']:
                    postings.append((policy['fee_account'], bt['fee']))
            elif kind in {'refund', 'payment_refund'} and bt['amount'] < 0:
                refund = source or refunds.get(source_id)
                if (not refund or refund.get('status') != 'succeeded' or refund.get('currency') != 'usd'
                        or refund.get('amount') != -bt['amount'] or _object_id(refund.get('balance_transaction')) != bt['id']):
                    raise ValueError('missing_or_unsettled_refund')
                charge_ref = refund.get('charge')
                original_charge = charge_ref if isinstance(charge_ref, dict) else charges.get(charge_ref)
                if not original_charge or original_charge.get('livemode') is not policy['livemode']:
                    raise ValueError('missing_refund_charge_evidence')
                invoice, has_invoice = _sale_document(original_charge, invoices, invoice_payments, sessions,
                                                      _minor(original_charge.get('amount')), policy['livemode'])
                if has_invoice and (not invoice or not isinstance(invoice.get('lines'), dict)
                                    or invoice['lines'].get('has_more') is not False
                                    or len(invoice['lines'].get('data') or []) != 1):
                    raise ValueError('mixed_line_refund_requires_allocation')
                # A single line still can contain tax, discounts, credits or
                # partial payment. Do not turn a refused original sale into an
                # apparently safe full contra-revenue refund.
                _sale_allocations(original_charge, invoices, _minor(original_charge.get('amount')),
                                  policy, prices, products, intents, invoice_payments, sessions)
                postings.append((policy['refund_account'], -bt['amount']))
                if bt['fee']:
                    postings.append((policy['fee_account'], bt['fee']))
                authority = ['refund-account-policy']
            elif kind == 'stripe_fee' and bt['amount'] < 0 and bt['fee'] == 0:
                postings.append((policy['fee_account'], -bt['amount'])); authority = ['fee-account-policy']
            elif kind == 'payout' and bt['amount'] < 0 and bt['fee'] == 0:
                payout = source or payouts.get(source_id)
                if (not payout or _object_id(payout.get('balance_transaction')) != bt['id'] or payout.get('amount') != -bt['amount']
                        or payout.get('currency') != 'usd' or payout.get('status') not in {'paid', 'in_transit', 'pending'}):
                    raise ValueError('missing_or_failed_payout')
                postings.append((policy['payout_transit_account'], -bt['amount'])); authority = ['payout-transit-policy']
            else:
                raise ValueError('unsupported_balance_type')
            if sum(amount for _, amount in postings) != 0:
                raise ValueError('unbalanced_stripe_evidence')
            posting_docs = [dict(account=acc, amount=_amount(amount), currency='USD') for acc, amount in postings if amount]
            for posting in posting_docs:
                opened = opens[posting['account']]
                if opened.date > date.fromisoformat(normalized['date']) or (opened.currencies and 'USD' not in opened.currencies):
                    raise ValueError('account_catalog_conflict')
            result['actions'].append(dict(source_id=sid, source_digest=_digest(normalized), kind=kind,
                authority_id=','.join(authority), postings=posting_docs, normalized_source=normalized))
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            # A payout match conflict holds the payout cohort too: never quietly
            # add processor detail over an old bank deposit booked as income.
            result['actions'] = [row for row in result['actions'] if row['source_id'] != sid]
            result['unresolved'].append(dict(source_id=sid, reason=str(exc)))
    # Match bank arrivals separately: a payout may have posted to transit in a
    # previous cohort/month before its real bank deposit arrived.
    for sid in selected:
        bt, normalized = validated[sid]
        payout = _object_id(bt.get('source'))
        if bt.get('type') != 'payout' or payout not in by_payout:
            continue
        original = saved.get(sid)
        already_applied = (sid in posted and sid not in reversed_ids and original
                           and original.get('stripe_financial_digest') == normalized['stripe_financial_digest']
                           and _same_source(original, normalized))
        if not any(row['source_id'] == sid for row in result['actions']) and not already_applied:
            continue
        try:
            if not policy['enabled']:
                raise ValueError('policy_disabled')
            action = _bank_action(by_payout[payout], payout, bt['net'], pending, posted, owned, opens, entity, cutoff,
                                  policy['payout_transit_account'], saved, entries, reversed_ids)
            if action:
                result['actions'].append(action)
            else:
                result['skipped'].append(dict(source_id=by_payout[payout]['bank_source_id'], reason='already_posted'))
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            result['actions'] = []  # Prior bank income must be corrected first.
            result['unresolved'].append(dict(source_id=by_payout[payout]['bank_source_id'], reason=str(exc)))
            break
    missing = set(by_payout) - {_object_id(bt.get('source')) for bt in bts.values() if bt.get('type') == 'payout'}
    if missing:
        raise ValueError('Bank binding refers to a payout absent from captured evidence.')
    result['plan_digest'] = _digest(result)
    return result


def stripe_stage(entity, evidence, policy, **kwargs):
    """Stage validated USD sources only; returned proposals need trusted approval."""
    plan = stripe_plan(entity, evidence, policy, **kwargs)
    rows = [row for row in plan['normalized_sources'] if row['currency'] == 'USD']
    imported = import_transactions(entity, rows, 'stripe-intake', intake_only=True)
    return dict(staged=imported.pending_categorization, duplicates=imported.skipped_duplicate,
                plan=stripe_plan(entity, evidence, policy, **kwargs))


def stripe_apply(entity, evidence, policy, *, expected_plan_digest, bank_matches=None,
                 closed_through=None, limit=50, after_source_id='', session_id='stripe-approved', ts=None):
    """Apply an exact recomputed approved batch through the ordinary atomic writer.

    The host authenticates policy/bank bindings and rechecks financial head CAS
    at publication. This API does not accept arbitrary journal postings.
    """
    plan = stripe_plan(entity, evidence, policy, bank_matches=bank_matches, closed_through=closed_through,
                       limit=limit, after_source_id=after_source_id)
    if plan['plan_digest'] != expected_plan_digest:
        raise ValueError('Stripe plan changed; refresh the captured plan before applying.')
    pending = {row['id']: row for row in _load_pending_categorization(entity)}
    if any(action['source_id'] not in pending for action in plan['actions']):
        raise ValueError('Stage every selected source before applying the approved Stripe plan.')
    entries = [Entry(date=date.fromisoformat(action['normalized_source']['date']),
        narration=action['normalized_source']['description'],
        meta=(('source-id', action['source_id']), ('import-session', session_id),
              ('stripe-policy-digest', plan['policy_digest']), ('stripe-authority', action['authority_id']),
              ('stripe-source-digest', action['source_digest'])),
        postings=tuple(Posting(p['account'], Decimal(p['amount']), p['currency']) for p in action['postings']))
        for action in plan['actions']]
    if entries:
        with _entity_write_lock(entity.path):
            current = stripe_plan(entity, evidence, policy, bank_matches=bank_matches, closed_through=closed_through,
                                  limit=limit, after_source_id=after_source_id)
            if current['plan_digest'] != expected_plan_digest:
                raise ValueError('Stripe state changed before atomic apply.')
            _atomic_ledger_write_unlocked(entity, [], entries, session_id, ts, 'Apply approved Stripe clearing batch',
                                         [action['normalized_source'] for action in plan['actions']])
        ids = [action['source_id'] for action in plan['actions']]
        StagingStore(entity.staging_dir).bulk_mark_seen(ids)
        queue._remove_many_from_pending_categorization(entity, ids)
    return dict(posted=len(entries), skipped=len(plan['skipped']), held=len(plan['unresolved']),
                next_cursor=plan['next_cursor'], plan_digest=plan['plan_digest'])
