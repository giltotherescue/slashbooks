"""Read-only cash schedules from recorded cash and explicit timing assumptions.

This is a schedule, not a prediction. No P&L extrapolation, probability weighting,
bank verification, or ledger posting occurs here.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .entity import load_entity
from .ledger.store import LedgerStore, default_store_path

PAYMENT_GROUPS = ('cogs', 'overhead', 'tax', 'debt', 'owner', 'other')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _date(value):
    if not isinstance(value, str):
        raise ValueError('Use canonical YYYY-MM-DD dates.')
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError('Use canonical YYYY-MM-DD dates.')
    return result


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000 or '\x00' in value:
        raise ValueError(f'Invalid {label}.')
    return value


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError('amount_remaining must be a positive decimal string in cents.')
    try:
        result = Decimal(value)
        if not result.is_finite() or result <= 0 or result > Decimal('1000000000000') or result != result.quantize(Decimal('.01')):
            raise ValueError('amount_remaining must be positive, finite, bounded and in cents.')
    except InvalidOperation as exc:
        raise ValueError('Invalid amount_remaining.') from exc
    return result


def _cash_account(name):
    return (name == 'Assets:Cash' or name.startswith(('Assets:Bank:', 'Assets:Cash:'))) and 'restricted' not in name.casefold()


def _schedule(items, opening, start, weeks, delay):
    rows, outside = [], []
    dated = []
    end = start + timedelta(days=weeks * 7 - 1)
    for item in items:
        when = _date(item['expected_date']) + timedelta(days=delay if item['direction'] == 'receipt' else 0)
        shifted = dict(item, scheduled_date=when.isoformat())
        if when > end:
            outside.append(dict(shifted, reason='beyond_horizon'))
        else:
            dated.append(shifted)
    balance = opening
    for index in range(weeks):
        first = start + timedelta(days=index * 7); last = first + timedelta(days=6)
        receipts = Decimal(0); payments = {group: Decimal(0) for group in PAYMENT_GROUPS}
        ids = []
        for item in dated:
            if first <= _date(item['scheduled_date']) <= last:
                ids.append(item['id'])
                if item['direction'] == 'receipt': receipts += Decimal(item['amount_remaining'])
                else: payments[item['group']] += Decimal(item['amount_remaining'])
        ending = balance + receipts - sum(payments.values())
        rows.append(dict(from_date=first.isoformat(), to_date=last.isoformat(), opening_cash=f'{balance:.2f}',
                         receipts=f'{receipts:.2f}', payments={key: f'{val:.2f}' for key, val in payments.items()},
                         ending_cash=f'{ending:.2f}', assumption_ids=ids))
        balance = ending
    return dict(weeks=rows, scheduled_items=dated, outside_horizon=outside)


def cash_schedule(entity, assumptions, *, as_of, weeks=13):
    """Compute a bounded USD schedule without changing any company file.

    Referencing a recorded receipt requires an explicit remaining-balance basis;
    this permits a deposit already in actual cash plus an outstanding balance.
    Source references establish provenance, not independent payment verification.
    """
    cutoff = _date(as_of)
    if type(weeks) is not int or not 1 <= weeks <= 13:
        raise ValueError('weeks must be between 1 and 13.')
    if not isinstance(assumptions, dict) or assumptions.get('version') != 1 or type(assumptions.get('version')) is not int or assumptions.get('currency') != 'USD':
        raise ValueError('Cash schedule assumptions require version 1 and USD.')
    accounts = assumptions.get('cash_accounts')
    if (not isinstance(accounts, list) or not 1 <= len(accounts) <= 50
            or any(not isinstance(name, str) for name in accounts) or len(set(accounts)) != len(accounts)):
        raise ValueError('Select distinct catalog bank/cash accounts.')
    store = LedgerStore(default_store_path(entity.path))
    if not store.path.exists():
        raise ValueError('Recorded ledger is required for opening cash.')
    with store.connection() as conn:
        opens = {row.account: row for row in store.load_opens(conn)}
        for name in accounts:
            if (name not in opens or not _cash_account(name) or opens[name].date > cutoff
                    or (opens[name].currencies and opens[name].currencies != ('USD',))):
                raise ValueError('Selected accounts must be open USD bank/cash accounts; processor, transit, receivable and restricted assets are excluded.')
        entries = store.load_entries(conn=conn)
        ledger_digest = store.content_digest(conn)
    opening = Decimal(0); history = Decimal(0); history_from = cutoff - timedelta(days=90)
    recorded_ids = set(); future_count = 0
    for entry in entries:
        legs = [posting for posting in entry.postings if posting.account in accounts]
        if any(posting.currency != 'USD' for posting in legs):
            raise ValueError('Selected cash contains non-USD postings.')
        if entry.date > cutoff:
            future_count += bool(legs); continue
        if entry.source_id: recorded_ids.add(entry.source_id)
        movement = sum((posting.amount for posting in legs), Decimal(0))
        opening += movement
        if entry.date >= history_from: history += movement
    items = assumptions.get('items')
    if not isinstance(items, list) or len(items) > 5000:
        raise ValueError('items must be a bounded array of explicit assumptions.')
    accepted, excluded, ids = [], [], set()
    for item in items:
        if not isinstance(item, dict): raise ValueError('Each assumption must be an object.')
        sid = _text(item.get('id'), 'assumption ID')
        if sid in ids: raise ValueError('Assumption IDs must be unique.')
        ids.add(sid)
        when, amount = _date(item.get('expected_date')), _money(item.get('amount_remaining'))
        direction, group = item.get('direction'), item.get('group')
        if direction not in {'receipt', 'payment'} or (direction == 'receipt' and group != 'revenue') or (direction == 'payment' and group not in PAYMENT_GROUPS):
            raise ValueError('Use receipt/revenue or payment with an explicit payment group.')
        if any(type(item.get(flag)) is not bool for flag in ('confirmed', 'already_settled')):
            raise ValueError('Each assumption requires confirmed and already_settled booleans.')
        refs = item.get('evidence_refs', [])
        if not isinstance(refs, list) or len(refs) > 100 or any(not isinstance(ref, str) or not ref or len(ref) > 1000 for ref in refs):
            raise ValueError('evidence_refs must be bounded text references.')
        for field in ('source', 'author', 'date', 'label'):
            if field in item: _text(item[field], field)
        if 'date' in item: _date(item['date'])
        normalized = {**item, 'amount_remaining': f'{amount:.2f}'}
        reason = ('already_settled' if item['already_settled'] else 'unconfirmed_pipeline' if not item['confirmed']
                  else 'overdue_timing_requires_update' if when <= cutoff
                  else 'recorded_source_requires_remaining_basis' if set(refs) & recorded_ids
                       and item.get('amount_basis') != 'remaining_after_recorded_receipts' else None)
        if reason: excluded.append(dict(normalized, reason=reason))
        else: accepted.append(normalized)
    start = cutoff + timedelta(days=1)
    base = _schedule(accepted, opening, start, weeks, 0)
    scenarios, scenario_ids = [], set()
    raw_scenarios = assumptions.get('scenarios', [])
    if not isinstance(raw_scenarios, list) or len(raw_scenarios) > 10:
        raise ValueError('scenarios must be a bounded array.')
    for scenario in raw_scenarios:
        if not isinstance(scenario, dict): raise ValueError('Invalid scenario.')
        sid = _text(scenario.get('id'), 'scenario ID'); label = _text(scenario.get('label'), 'scenario label')
        delay = scenario.get('receipt_delay_days')
        if sid in scenario_ids or type(delay) is not int or not 0 <= delay <= 365:
            raise ValueError('Scenarios require unique IDs and receipt_delay_days from 0 to 365.')
        scenario_ids.add(sid)
        scenarios.append(dict(id=sid, label=label, receipt_delay_days=delay, **_schedule(accepted, opening, start, weeks, delay)))
    warnings = ['Recorded ledger cash is not independently verified bank cash or proof of unrestricted availability.',
                'Missing opening balances, unimported transactions and incomplete reconciliation can change this baseline.',
                'Future items are supplied timing assumptions, not predicted revenue; actual profit is not cash.']
    if future_count: warnings.append('Future-dated recorded cash entries are excluded from opening cash; review overlap with supplied assumptions.')
    if excluded or base['outside_horizon']: warnings.append('Some supplied items are excluded from the base horizon; inspect exclusions before relying on the schedule.')
    return dict(currency='USD', as_of=as_of, horizon=dict(from_date=start.isoformat(), to_date=(start + timedelta(days=weeks * 7 - 1)).isoformat(), weeks=weeks),
                actual=dict(label='recorded cash as of', as_of=as_of, cash_accounts=accounts, opening_cash=f'{opening:.2f}',
                            history_window=dict(from_date=history_from.isoformat(), to_date=as_of), history_cash_movements=f'{history:.2f}',
                            baseline_quality=dict(verified_bank_cash=False, warnings=warnings)),
                **base, excluded=excluded, scenarios=scenarios, assumptions_digest=_digest(assumptions), ledger_digest=ledger_digest)


def _cell(value):
    text = str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def schedule_csv(report):
    """Portable sections; untrusted text is spreadsheet-formula neutralized."""
    stream = io.StringIO(newline=''); writer = csv.writer(stream)
    writer.writerow(['section', 'scenario', 'from', 'to', 'opening_cash', 'receipts', *PAYMENT_GROUPS, 'ending_cash'])
    for sid, schedule in [('base', report), *[(row['id'], row) for row in report['scenarios']]]:
        for row in schedule['weeks']:
            writer.writerow(['week', _cell(sid), row['from_date'], row['to_date'], row['opening_cash'], row['receipts'],
                             *[row['payments'][group] for group in PAYMENT_GROUPS], row['ending_cash']])
    writer.writerow(['section', 'id', 'label', 'scheduled_or_expected_date', 'amount_remaining', 'reason', 'evidence_refs', 'author', 'source', 'scenario'])
    details = [('assumption', report['scheduled_items'], 'base'), ('excluded', report['excluded'], 'base'), ('outside_horizon', report['outside_horizon'], 'base')]
    for scenario in report['scenarios']:
        writer.writerow(['scenario', _cell(scenario['id']), _cell(scenario['label']), 'receipt_delay_days', scenario['receipt_delay_days']])
        details.extend([('assumption', scenario['scheduled_items'], scenario['id']), ('outside_horizon', scenario['outside_horizon'], scenario['id'])])
    for section, rows, scenario in details:
        for row in rows:
            writer.writerow([section, _cell(row['id']), _cell(row.get('label', '')), row.get('scheduled_date', row['expected_date']),
                             row['amount_remaining'], row.get('reason', ''), _cell('; '.join(row.get('evidence_refs', []))),
                             _cell(row.get('author', '')), _cell(row.get('source', '')), _cell(scenario)])
    for warning in report['actual']['baseline_quality']['warnings']: writer.writerow(['warning', warning])
    writer.writerow(['baseline', report['actual']['label'], report['as_of'], report['actual']['opening_cash'], '; '.join(report['actual']['cash_accounts'])])
    return stream.getvalue()


def add_parser(subparsers):
    parser = subparsers.add_parser('forecast', help='Schedule cash from recorded cash and explicit future timing assumptions')
    parser.add_argument('--entity', required=True)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--weeks', type=int, default=13)
    parser.add_argument('--format', choices=('json', 'csv'), default='json')
    parser.add_argument('--output', type=Path)


def run(args):
    try:
        entity = load_entity(args.entity)
        if args.output:
            target = args.output.resolve()
            if target == args.input.resolve(): raise ValueError('Report output cannot replace its assumption input.')
            if target.is_relative_to(entity.path) and target.relative_to(entity.path).parts[0] != 'reports':
                raise ValueError('Company report output must be inside reports/; recorded books and context cannot be overwritten.')
        report = cash_schedule(entity, json.loads(args.input.read_text(encoding='utf-8')), as_of=args.as_of, weeks=args.weeks)
        output = schedule_csv(report) if args.format == 'csv' else json.dumps(report, indent=2, ensure_ascii=False) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(output, encoding='utf-8')
        else: print(output, end='')
        return 0
    except (ValueError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr); return 1
