"""Read-only routine eligibility for a trusted hosted operator.

Policy and decisions are authenticated by the host, not by the books agent.
This module plans from existing staged evidence; it neither writes nor learns.
Apply actions in one candidate workspace using queue.propose/confirm(learn=False).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .ledger.importer import _ledger_account_for_txn, _load_pending_categorization, _validate_normalized_transactions
from .ledger.store import LedgerStore, default_store_path
from .queue import _counterparty_key


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a non-empty string without surrounding whitespace.")
    return value


def _money(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("A signed amount is required.")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount.quantize(Decimal('0.01')) != amount:
            raise ValueError("Amount must be finite with cent precision.")
        return amount
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid amount.") from exc


def _source(txn):
    _validate_normalized_transactions([txn])
    sid = _text(txn.get('id'), 'id')
    account_id = _text(txn.get('accountId'), 'accountId')
    raw_date = _text(txn.get('date'), 'date')
    if date.fromisoformat(raw_date).isoformat() != raw_date:
        raise ValueError('Date must use YYYY-MM-DD.')
    amount = _money(txn.get('amount'))
    currency = txn.get('currency', 'USD')
    _text(currency, 'currency')
    counterparty = _counterparty_key(str(txn.get('description') or ''), str(txn.get('counterparty') or ''))
    return dict(id=sid, date=raw_date, amount=f'{amount:.2f}', accountId=account_id,
                currency=currency, counterparty_key=counterparty)


def routine_source_digest(transactions):
    """Digest exact captured raw identities, independent of row/JSON key order.

    Mapping authority is separately frozen as approved_ledger_account on both
    rules and decisions. Missing currency means USD, matching the normal writer.
    """
    rows = sorted((_source(txn) for txn in transactions), key=lambda row: row['id'])
    if not rows or len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Digest requires non-empty, unique source IDs.')
    payload = json.dumps(rows, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _state(entity):
    pending = _load_pending_categorization(entity)
    ids = [txn['id'] for txn in pending]
    if any(not isinstance(sid, str) or not sid for sid in ids) or len(set(ids)) != len(ids):
        raise ValueError('Review state contains invalid or duplicate source IDs.')
    path = default_store_path(entity.path)
    if not path.exists():
        raise ValueError('Routine planning requires the canonical account catalog.')
    store = LedgerStore(path)
    opens = {item.account: item for item in store.load_opens()}
    refs = store.load_source_refs()
    posted = {ref.source_id for ref in refs}
    # Reversals deliberately have no source-id, so load_source_refs excludes
    # them. Inspect entry metadata as well rather than reviving an original.
    reversed_ids = {value for entry in store.load_entries() for key, value in entry.meta if key == 'reverses'}
    # Reading review ownership must never quarantine or rewrite malformed data.
    owned = set()
    for path in sorted((entity.path / 'review-queue').glob('*.json')):
        try:
            item = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError('Unreadable existing review proposal.') from exc
        if not isinstance(item, dict):
            raise ValueError('Invalid existing review proposal.')
        if item.get('status') in {'open', 'reopened'}:
            ids = item.get('source_ids') or [item.get('source_id')]
            if not isinstance(ids, list) or not ids or not all(isinstance(sid, str) and sid for sid in ids):
                raise ValueError('Existing review proposal has invalid source ownership.')
            owned.update(ids)
    seen_path = entity.staging_dir / 'seen-ids.json'
    if seen_path.exists():
        try:
            seen = json.loads(seen_path.read_text(encoding='utf-8'))
            if not isinstance(seen, list) or not all(isinstance(sid, str) for sid in seen):
                raise ValueError('Invalid seen source IDs.')
            posted.update(seen)
        except (OSError, ValueError) as exc:
            raise ValueError('Unreadable seen source IDs.') from exc
    return {txn['id']: txn for txn in pending}, opens, posted, reversed_ids, owned


def _mapping(entity, txn, opens):
    account_id = txn.get('accountId')
    mappings = entity.entity_config.get('bank_account_mappings') or {}
    ledger_account = mappings.get(account_id)
    if not isinstance(ledger_account, str) or ledger_account not in opens:
        raise ValueError('mapping_required')
    try:
        resolved = _ledger_account_for_txn(txn, mappings, entity.entity_config.get('csv_account_mappings'))
    except ValueError as exc:
        raise ValueError('mapping_conflict') from exc
    if resolved != ledger_account:
        raise ValueError('mapping_conflict')
    kind = str(txn.get('accountType') or '').lower().replace('_', '').replace('-', '').replace(' ', '')
    if kind in {'creditcard', 'credit'}:
        valid_role = ledger_account == 'Liabilities:CreditCard' or ledger_account.startswith('Liabilities:CreditCard:')
    elif kind in {'checking', 'savings', 'cash', 'depository', 'bank'}:
        valid_role = ledger_account.startswith('Assets:Bank:')
    else:
        raise ValueError('source_account_type_unknown')
    if not valid_role:
        raise ValueError('source_account_role_conflict')
    return ledger_account


def _category(category, opens):
    return (isinstance(category, str) and category in opens
            and category not in {'Assets:Bank', 'Liabilities:CreditCard', 'Assets:Uncategorized'}
            and not category.startswith(('Assets:Bank:', 'Liabilities:CreditCard:', 'Assets:Uncategorized:')))


def routine_context(entity):
    """Bounded trusted option data from the staged worklist and catalog."""
    pending, opens, posted, _, _ = _state(entity)
    accounts, counterparties = {}, {}
    for txn in pending.values():
        sid = txn.get('accountId')
        if isinstance(sid, str) and sid:
            accounts[sid] = dict(id=sid, name=str(txn.get('accountName') or sid),
                                 ledger_account=(entity.entity_config.get('bank_account_mappings') or {}).get(sid),
                                 account_type=str(txn.get('accountType') or ''))
        key = _counterparty_key(str(txn.get('description') or ''), str(txn.get('counterparty') or ''))
        if key:
            counterparties[key] = dict(key=key, label=key)
    return dict(source_accounts=[accounts[key] for key in sorted(accounts)[:500]],
                counterparties=[counterparties[key] for key in sorted(counterparties)[:500]],
                categories=[dict(name=name, label=name) for name in sorted(opens) if _category(name, opens)][:500],
                counts=dict(posted=len(posted), held=len(pending)))


def _authority(policy, decisions, pending, opens, posted):
    if not isinstance(policy, dict) or type(policy.get('enabled')) is not bool:
        raise ValueError('Policy enabled must be boolean.')
    if type(policy.get('version')) is not int or policy['version'] < 1:
        raise ValueError('Policy version must be a positive integer.')
    rules = policy.get('rules', [])
    categories = policy.get('review_categories', [])
    roles = policy.get('answer_roles', [])
    if not isinstance(rules, list) or not isinstance(categories, list) or not isinstance(roles, list):
        raise ValueError('Policy rules, review_categories and answer_roles must be arrays.')
    if any(role not in {'admin', 'accountant', 'client'} for role in roles):
        raise ValueError('Invalid answer role.')
    if any(not _category(category, opens) for category in categories):
        raise ValueError('Review categories must be permitted catalog accounts.')
    rule_ids = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError('Rules must be objects.')
        for field in ('id', 'source_account_id', 'counterparty_key', 'category', 'approved_ledger_account'):
            _text(rule.get(field), field)
        if rule['id'] in rule_ids or not _category(rule['category'], opens):
            raise ValueError('Rules require unique IDs and permitted catalog categories.')
        rule_ids.add(rule['id'])
        if rule['counterparty_key'] != _counterparty_key(rule['counterparty_key'], ''):
            raise ValueError('Rule counterparty_key must be normalized.')
        if 'max_abs_amount' in rule and _money(rule['max_abs_amount']) < 0:
            raise ValueError('Rule max_abs_amount must be nonnegative.')
    if not isinstance(decisions, list):
        raise ValueError('Decisions must be an array.')
    by_source = {}
    decision_ids = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError('Decisions must be objects.')
        for field in ('id', 'source_digest', 'approved_ledger_account'):
            _text(decision.get(field), field)
        if 'category' not in decision:
            raise ValueError('Decision category is required (null means human hold).')
        if decision['category'] is not None:
            _text(decision['category'], 'category')
        ids = decision.get('source_ids')
        if (not isinstance(ids, list) or not ids or not all(isinstance(sid, str) and sid for sid in ids)
                or len(set(ids)) != len(ids) or decision['id'] in decision_ids):
            raise ValueError('Decisions require unique IDs and non-empty unique source_ids.')
        decision_ids.add(decision['id'])
        digests = decision.get('source_digests')
        if (not isinstance(digests, dict) or set(digests) != set(ids)
                or any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value) for value in digests.values())
                or not re.fullmatch('[0-9a-f]{64}', decision['source_digest'])):
            raise ValueError('Decision requires frozen group and per-source digests.')
        valid = decision['category'] is None or decision['category'] in categories
        captured = [pending[sid] for sid in ids if sid in pending]
        try:
            valid = valid and all(sid in pending or sid in posted for sid in ids)
            valid = valid and all(routine_source_digest([txn]) == digests[txn['id']] for txn in captured)
            # Before any members are consumed, also check the complete question
            # digest. Later batches rely on trusted frozen singleton digests.
            if len(captured) == len(ids):
                valid = valid and routine_source_digest(captured) == decision['source_digest']
            groups = {(_source(txn)['accountId'], _source(txn)['counterparty_key'], _source(txn)['currency'],
                       'positive' if _money(txn.get('amount')) > 0 else 'negative') for txn in captured}
            valid = valid and len(groups) <= 1
        except ValueError:
            valid = False
        for sid in ids:
            if sid in by_source:
                raise ValueError('Overlapping decisions are ambiguous.')
            by_source[sid] = (decision, valid)
    return rules, by_source


def routine_plan(entity, policy, source_ids=None, decisions=None, *, closed_through=None,
                 limit=50, after_source_id=''):
    """Plan a lexical batch without writes. The host supplies trusted authority.

    Only explicit rules or exact captured decisions permit actions. The host
    must revalidate state/closed period at atomic publication and authenticate
    the human answer role before constructing decisions. Editable company data
    never supplies policy, decisions, or closed_through authority.
    """
    if type(limit) is not int or not 1 <= limit <= 500 or not isinstance(after_source_id, str):
        raise ValueError('Invalid routine cursor/limit.')
    closed_date = date.fromisoformat(closed_through) if closed_through else None
    if closed_date and closed_date.isoformat() != closed_through:
        raise ValueError('closed_through must use YYYY-MM-DD.')
    pending, opens, posted, reversed_ids, owned = _state(entity)
    rules, answers = _authority(policy, [] if decisions is None else decisions, pending, opens, posted)
    if source_ids is not None and (not isinstance(source_ids, list) or not all(isinstance(sid, str) and sid for sid in source_ids)
                                   or len(set(source_ids)) != len(source_ids)):
        raise ValueError('source_ids must be unique non-empty strings.')
    ids = sorted(sid for sid in (pending if source_ids is None else source_ids) if sid > after_source_id)
    selected = ids[:limit]
    result = dict(actions=[], unresolved=[], skipped=[], next_cursor=selected[-1] if len(ids) > limit else None)
    for sid in selected:
        if sid in reversed_ids or sid in posted or sid not in pending:
            reason = 'reversed' if sid in reversed_ids else 'already_posted' if sid in posted else 'not_staged'
            result['skipped'].append(dict(source_id=sid, reason=reason))
            continue
        txn = pending[sid]
        detail = dict(source_id=sid, source_account_id=str(txn.get('accountId') or ''),
                      account_name=str(txn.get('accountName') or ''), date=str(txn.get('date') or ''),
                      description=str(txn.get('description') or ''), amount=str(txn.get('amount') or ''),
                      currency=str(txn.get('currency', 'USD')), counterparty_key='', sign='', ledger_account=None)
        reason = 'needs_category'
        try:
            source = _source(txn)
            detail.update(counterparty_key=source['counterparty_key'], amount=source['amount'],
                          sign='positive' if _money(source['amount']) > 0 else 'negative')
            if txn.get('pending'):
                raise ValueError('pending')
            if closed_date and date.fromisoformat(source['date']) <= closed_date:
                raise ValueError('closed_period')
            if source['currency'] != 'USD' or _money(source['amount']) == 0:
                raise ValueError('unsupported_currency_or_zero_amount')
            mapped = _mapping(entity, txn, opens)
            detail['ledger_account'] = mapped
            if opens[mapped].date > date.fromisoformat(source['date']) or (opens[mapped].currencies and 'USD' not in opens[mapped].currencies):
                raise ValueError('source_catalog_conflict')
            if sid in owned:
                raise ValueError('existing_review')
            if not policy['enabled']:
                raise ValueError('policy_disabled')
            eligible = []
            if sid in answers:
                decision, valid = answers[sid]
                if not valid:
                    raise ValueError('stale_decision')
                if decision['approved_ledger_account'] != mapped:
                    raise ValueError('mapping_changed')
                if decision['category'] is None:
                    raise ValueError('human_hold')
                eligible.append((decision['category'], decision['id']))
            else:
                for rule in rules:
                    if rule['source_account_id'] != source['accountId'] or rule['counterparty_key'] != source['counterparty_key']:
                        continue
                    if rule['approved_ledger_account'] != mapped:
                        raise ValueError('mapping_changed')
                    if 'max_abs_amount' in rule and abs(_money(source['amount'])) > _money(rule['max_abs_amount']):
                        continue
                    eligible.append((rule['category'], rule['id']))
            if len(eligible) > 1:
                raise ValueError('ambiguous_authority')
            if eligible:
                category, authority_id = eligible[0]
                if opens[category].date > date.fromisoformat(source['date']) or (opens[category].currencies and 'USD' not in opens[category].currencies):
                    raise ValueError('category_catalog_conflict')
                result['actions'].append(dict(source_id=sid, category=category, authority_id=authority_id))
                continue
        except ValueError as exc:
            reason = str(exc) if str(exc) in {'pending', 'closed_period', 'unsupported_currency_or_zero_amount',
                'mapping_required', 'mapping_conflict', 'source_account_type_unknown', 'source_account_role_conflict',
                'source_catalog_conflict', 'category_catalog_conflict', 'existing_review', 'policy_disabled',
                'stale_decision', 'mapping_changed', 'ambiguous_authority', 'human_hold'} else 'invalid_source'
        detail['reason'] = reason
        result['unresolved'].append(detail)
    return result
