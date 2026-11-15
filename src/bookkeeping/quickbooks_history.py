"""Native QBO history from bounded, immutable raw accounting reports.

No document-to-journal inference. Unsupported provider reports fail closed.
"""
from collections import defaultdict
from datetime import date, timedelta, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from .ledger.model import Entry, Open, Posting, _validate_account_name
from .ledger.store import LedgerStore, default_store_path

FORMAT = 'slashbooks-qbo-history-v1'
# A bank row can clear long after QuickBooks recorded it (a deposit in transit, an outstanding
# check). A bank row up to this many days after the history end is held when ANY unmatched QuickBooks
# bank leg of the same account, amount and direction is dated up to LEG_LOOKAHEAD_DAYS after the row.
HISTORY_BANK_LAG_DAYS = 45
LEG_LOOKAHEAD_DAYS = 3
SETTLEMENT_DAYS = 3
# A deposit in transit or a check outstanding at the opening clears within this many days of the history start.
OPENING_CLEARING_DAYS = 45
MAX_FILE_BYTES = 8 * 1024 * 1024


class MissingEvidence(Exception):
    """A file named in the manifest is not there. The preview reports it as a blocker."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _money(value):
    if isinstance(value, (bool, float)):
        raise ValueError('Report monetary values must be exact decimal strings or integers')
    try:
        number = Decimal(str(value or '0').replace(',', ''))
        if not number.is_finite() or number.quantize(Decimal('.01')) != number:
            raise ValueError('Invalid monetary precision')
        return number
    except InvalidOperation as exc:
        raise ValueError('Malformed monetary value') from exc


def receipts_at(entity_path):
    path = default_store_path(Path(entity_path))
    if not path.exists():
        return []
    with LedgerStore(path).connection() as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT payload_json FROM audit_events WHERE type='qbo-history-recorded'")]


def receipts(entity):
    return receipts_at(entity.path)


def history_start_dates(entity_path):
    """First day of the recorded history for each bank or card account it owns.

    A bank row dated before it is already in the opening balance: the books start there.
    """
    found = {}
    for r in receipts_at(entity_path):
        for account in r['cutovers']:
            found.setdefault(account, date.fromisoformat(r['history_from']))
    return found


def owned(entity, account, day, *, nearby=False, _receipts=None):
    """Ownership is durable even if every imported balance and activity is zero."""
    day = date.fromisoformat(str(day)[:10])
    return next((r for r in (receipts(entity) if _receipts is None else _receipts) if account in r['account_map'].values()
                 and day <= date.fromisoformat(r['history_to']) + timedelta(days=3 if nearby and account in r['cutovers'] else 0)), None)


def refuse_owned_write(entity, entries, source_transactions=None, *, conn=None, store=None, ts=None, always=False):
    """Final guard for a write on accounts that QuickBooks history owns.

    An automated write of bank rows (it carries ``source_transactions``) must have passed intake, so only
    a row that a person released may post inside the period, and the bank leg must equal the release.
    A person's journal (a write with no source rows: a manual or adjusting journal, a reversal) is the
    person's own act: it is allowed on any account and is marked in the audit log (``qbo-history-journal``).
    The engine cannot tell a person from an agent at this layer; the host decides who may run the
    journal and reversal commands. ``always`` keeps the refusal for a write that is neither (an opening).
    """
    history = receipts(entity)
    if not history:
        return
    if not (source_transactions or always):
        touched = [{'source_id': entry.source_id, 'date': entry.date.isoformat(),
                    'accounts': sorted({p.account for p in entry.postings
                                        if owned(entity, p.account, entry.date, nearby=True, _receipts=history)})}
                   for entry in entries]
        touched = [t for t in touched if t['accounts']]
        if touched and conn is not None and store is not None:
            store.append_audit_event('qbo-history-journal', {'entries': touched}, conn, ts=ts)
        return
    decisions = {d['source_id']: d for d in _buffer_decisions(entity)}
    for entry in entries:
        released = [p for p in entry.postings if released_entry(entity, entry, p, decisions)]
        for posting in entry.postings:
            # A reviewed cash row permits its category counterpart, but cannot
            # authorize a second owned bank leg on a transfer.
            if released and (posting in released or not any(posting.account in r['cutovers'] for r in history)):
                continue
            if owned(entity, posting.account, entry.date, nearby=True, _receipts=history) and not released_entry(entity, entry, posting, decisions):
                raise ValueError('QuickBooks owns this account through the history cutover; explicit migration correction is required')


def _read(root, item):
    relative = Path(item['path'])
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('History evidence path must be confined and cannot be a symbolic link')
    # Only the parts below the manifest folder are checked. The folder itself may sit under a
    # symbolic link (for example the system temporary folder on macOS).
    root = root.resolve()
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('History evidence path must be confined and cannot be a symbolic link')
    if not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('History evidence exceeds its bounded file contract')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != item['sha256']:
        raise ValueError('History evidence hash mismatch')
    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if not isinstance(value, dict):
        raise ValueError('History evidence must be a raw QBO object')
    return value


def _foreign_currency(record):
    """True when a source record is not in the single home currency (USD)."""
    ref = record.get('CurrencyRef')
    value = ref.get('value') if isinstance(ref, dict) else ref
    if value and str(value).upper() != 'USD':
        return True
    rate = record.get('ExchangeRate')
    try:
        return rate not in (None, '') and Decimal(str(rate)) != 1
    except InvalidOperation:
        return True


def _columns(report):
    columns = report['Columns']['Column']
    result = {}
    aliases = {'tx_date':'date', 'txn_type':'type', 'tx_type':'type', 'debt_amt':'debit',
               'credit_amt':'credit', 'debit_amt':'debit'}
    for i, col in enumerate(columns):
        meta = {m['Name']:m['Value'] for m in col.get('MetaData', [])}
        key = str(meta.get('ColKey') or col.get('ColType', '')).lower()
        key = aliases.get(key, key)
        if key in result and key not in {'money','string','account'}:
            raise ValueError('Ambiguous report column')
        result[key] = i
    return result


def _header(report, name, basis, start, end):
    h = report['Header']
    if h.get('ReportName') != name or h.get('ReportBasis', '').lower() != basis.lower():
        raise ValueError('Report identity or basis mismatch')
    if h.get('EndPeriod') != end or (start is not None and h.get('StartPeriod') != start):
        raise ValueError('Report coverage mismatch')
    if h.get('Currency') != 'USD':
        raise ValueError('Only explicit USD reports are supported')
    for option in h.get('Option', []):
        if str(option.get('Name', '')).lower() in {'truncated','istruncated'} and str(option.get('Value')).lower() == 'true':
            raise ValueError('Truncated report')


def _rows(report):
    def visit(container, account=None):
        for row in container.get('Row', []):
            header = row.get('Header', {}).get('ColData', [])
            current = str(header[0].get('id') or '') if header else account
            if row.get('ColData'):
                yield row['ColData'], current
            if 'Rows' in row:
                yield from visit(row['Rows'], current)
    yield from visit(report.get('Rows', {}))


def _trial(report, known):
    cols = _columns(report)
    if not {'debit','credit'} <= cols.keys():
        # Actual QBO trial balance uses Money column titles Debit/Credit.
        for i, c in enumerate(report['Columns']['Column']):
            if c.get('ColType') == 'Money' and str(c.get('ColTitle', '')).lower() in {'debit','credit'}:
                cols[c['ColTitle'].lower()] = i
    if not {'debit','credit'} <= cols.keys():
        raise ValueError('TrialBalance requires explicit debit and credit')
    balances = {}
    for cells, _ in _rows(report):
        identity = str(cells[0].get('id') or '')
        if identity not in known or identity in balances:
            raise ValueError('TrialBalance has unknown or duplicate account identity')
        debit, credit = (_money(cells[cols[k]].get('value')) for k in ('debit','credit'))
        if debit < 0 or credit < 0 or (debit and credit):
            raise ValueError('Invalid trial balance shape')
        balances[identity] = debit-credit
    if sum(balances.values(), Decimal(0)):
        raise ValueError('TrialBalance is unbalanced')
    return balances


def _statement(report, known, mapped, kind):
    columns = report['Columns']['Column']
    if len(columns) != 2 or columns[1].get('ColType') != 'Money' or columns[1].get('ColTitle', '').lower() != 'total':
        raise ValueError('Financial statement needs a single Total column')
    values = {}
    for cells, _ in _rows(report):
        identity = str(cells[0].get('id') or '')
        if kind == 'balance_sheet' and not identity and cells[0].get('value') == 'Net Income' and len(cells) == 2:
            if '__net_income__' in values:
                raise ValueError('Duplicate derived Net Income')
            values['__net_income__'] = -_money(cells[1].get('value'))
            continue
        if identity not in known or identity in values or len(cells) != 2:
            raise ValueError('Statement has missing, unknown or duplicate account identity')
        root = mapped[identity].split(':')[0]
        if (kind == 'profit_and_loss') != (root in {'Income','Expenses'}):
            raise ValueError('Statement account has incompatible chart type')
        values[identity] = _money(cells[1].get('value')) * (-1 if root in {'Income','Liabilities','Equity'} else 1)
    return values


def _plan(entity, path):
    manifest = json.loads(path.read_text())
    if manifest.get('format') != FORMAT or manifest.get('environment') not in {'sandbox','production'}:
        raise ValueError('Unsupported history manifest')
    realm = manifest.get('realm_id')
    if not isinstance(realm, str) or not realm.isdigit():
        raise ValueError('Invalid realm identity')
    first, last = (date.fromisoformat(manifest[k]) for k in ('history_from','history_to'))
    if first > last or manifest.get('currency') != 'USD' or entity.currency != 'USD':
        raise ValueError('Invalid history dates or currency')
    basis = manifest['accounting_basis']
    if basis not in {'Cash','Accrual'}:
        raise ValueError('Invalid accounting basis')
    from .entity import resolve_basis
    if resolve_basis(entity.entity_config).lower() != basis.lower():
        raise ValueError('History basis must match the company basis')
    mapped = manifest['account_map']
    if not isinstance(mapped, dict) or len(set(mapped.values())) != len(mapped):
        raise ValueError('A complete one-to-one account map is required')
    for account in mapped.values():
        _validate_account_name(account)
    files = manifest['files']
    if not isinstance(files, list) or not files or len(files) > 100000:
        raise ValueError('Invalid evidence file manifest')
    missing = [str(item.get('path')) for item in files
               if not isinstance(item.get('path'), str) or not (path.parent / item['path']).is_file()]
    if missing:
        raise MissingEvidence(missing)
    if sum((path.parent / item['path']).stat().st_size for item in files) > 64 * 1024 * 1024:
        raise ValueError('History evidence exceeds the 64 MiB finalize bound')
    loaded = [(item, _read(path.parent, item)) for item in files]
    accounts, versions, reference_versions = {}, {}, {}
    records = 0
    foreign = []
    source_by_type = defaultdict(int)
    reference_types = {'Customer','Vendor','Item','Class','Department'}
    allowed_types = {'Purchase','Deposit','Transfer','JournalEntry','Invoice','Payment','Bill','BillPayment',
                     'SalesReceipt','RefundReceipt','CreditMemo','VendorCredit','CreditCardPayment'}
    for item, raw in loaded:
        if item['kind'] == 'accounts':
            for account in raw['QueryResponse'].get('Account', []):
                identity = str(account['Id'])
                if identity in accounts:
                    raise ValueError('Duplicate source account')
                accounts[identity] = account
                if _foreign_currency(account):
                    foreign.append({'type':'Account','id':identity})
        elif item['kind'] == 'entities':
            kind = item['entity_type']
            if kind not in allowed_types | reference_types:
                raise ValueError('Unsupported source entity type')
            source_by_type.setdefault(kind, 0)
            target_versions = reference_versions if kind in reference_types else versions
            for record in raw['QueryResponse'].get(kind, []):
                if not record.get('Id'):
                    raise ValueError('Missing source entity identity')
                key = (kind, str(record['Id']))
                if key in target_versions:
                    raise ValueError('Duplicate source entity')
                target_versions[key] = _digest(record)
                if kind not in reference_types and _foreign_currency(record):
                    foreign.append({'type':kind,'id':str(record['Id'])})
                records += 1
                source_by_type[kind] += 1
    source_by_type['Account'] = len(accounts)
    records += len(accounts)
    expected_counts = manifest.get('expected_source_counts')
    if expected_counts is not None:
        if (not isinstance(expected_counts, dict)
                or any(k not in allowed_types | reference_types | {'Account'}
                       or isinstance(v, bool) or not isinstance(v, int) or v < 0
                       for k,v in expected_counts.items())):
            raise ValueError('Invalid expected source counts')
    if not mapped:
        from .quickbooks_sync import _accounts
        chart, _ = _accounts(list(accounts.values()))
        mapped = {row['id']:row['mapped_account'] for row in chart}
        if len(set(mapped.values())) != len(mapped):
            raise ValueError('Source chart identities collide; explicit mapping is required')
    if set(accounts) != set(mapped):
        raise ValueError('Account map does not cover the full source chart')
    from .quickbooks import _QB_TYPE_ROOT
    for identity, account in accounts.items():
        source_root = _QB_TYPE_ROOT.get(str(account.get('AccountType', '')).lower())
        aliases = {'Accounts Receivable':'Assets','Accounts Payable':'Liabilities','Other Current Asset':'Assets',
                   'Other Asset':'Assets','Fixed Asset':'Assets','Other Current Liability':'Liabilities',
                   'Long Term Liability':'Liabilities','Expense':'Expenses'}
        source_root = (source_root.split(':')[0] if source_root else aliases.get(account.get('AccountType')))
        if not source_root or mapped[identity].split(':')[0] != source_root:
            raise ValueError('Account mapping changes the source financial root')
    bank = {mapped[k] for k, a in accounts.items() if a.get('AccountType') in {'Bank','Credit Card'}}
    cutovers = manifest.get('cutovers') or {account:last.isoformat() for account in bank}
    if cutovers != {account:last.isoformat() for account in bank}:
        raise ValueError('Every bank and card account needs the explicit history-end cutover')
    singles = {}
    groups, windows, movement = {}, defaultdict(list), defaultdict(Decimal)
    # QuickBooks closes each fiscal year into Retained Earnings when it makes a report. The engine
    # does the same when it makes a statement and writes no closing entry (see the balance sheet
    # in reports/statements.py), so the opening posts the trial balance as it is. The fiscal year
    # start is the company's ``fiscal_year_start`` (``MM-01``, default ``01-01``), the same value
    # the engine's reports use. Movement dated before the fiscal year that holds the history end
    # belongs to a year that QuickBooks has already closed.
    from .fiscal import year_start
    year_begin = year_start(last, entity.entity_config.get('fiscal_year_start', '01-01'))
    early = defaultdict(Decimal)
    for item, raw in loaded:
        kind = item['kind']
        if kind in {'accounts','entities'}:
            continue
        if kind != 'general_ledger':
            if kind in singles:
                raise ValueError('Duplicate control report')
            singles[kind] = raw
            continue
        source_account = str(item['account_id'])
        if source_account not in accounts:
            raise ValueError('Unknown partition account')
        lo, hi = date.fromisoformat(item['from']), date.fromisoformat(item['to'])
        if lo < first or hi > last or lo > hi:
            raise ValueError('Invalid GL partition coverage')
        windows[source_account].append((lo,hi))
        _header(raw, 'GeneralLedger', basis, lo.isoformat(), hi.isoformat())
        cols = _columns(raw)
        no_data = any(o.get('Name') == 'NoReportData' and o.get('Value') == 'true' for o in raw['Header'].get('Option', []))
        if no_data and not list(_rows(raw)):
            continue
        if not {'date','type','debit','credit'} <= cols.keys():
            raise ValueError('Unsupported GeneralLedger: explicit date/type/ID/debit/credit required')
        for cells, section in _rows(raw):
            if len(cells) != len(raw['Columns']['Column']):
                raise ValueError('Malformed GL row width')
            day_text = cells[cols['date']].get('value', '')
            if not day_text:
                # Beginning balances and totals are never activity.
                if any(_money(cells[cols[k]].get('value')) for k in ('debit','credit')):
                    raise ValueError('Undated GL monetary row is unsupported')
                continue
            day = date.fromisoformat(day_text)
            if not lo <= day <= hi or section != source_account:
                raise ValueError('GL account or date outside partition')
            cell = cells[cols['type']]
            identity, label = str(cell.get('id') or ''), str(cell.get('value') or '')
            labels = {'Sales Receipt':'SalesReceipt','Refund Receipt':'RefundReceipt',
                      'Credit Memo':'CreditMemo','Vendor Credit':'VendorCredit','Journal Entry':'JournalEntry',
                      'Bill Payment (Check)':'BillPayment','Bill Payment (Credit Card)':'BillPayment',
                      'Credit Card Payment':'CreditCardPayment','Expense':'Purchase','Check':'Purchase'}
            label = labels.get(label, label)
            if not identity or not label or (label,identity) not in versions:
                raise ValueError('GL transaction has no complete supporting source identity')
            debit, credit = (_money(cells[cols[k]].get('value')) for k in ('debit','credit'))
            if debit < 0 or credit < 0 or (debit and credit):
                raise ValueError('Invalid GL debit/credit shape')
            key = (label,identity)
            group = groups.setdefault(key, {'date':day,'postings':[],'evidence':[],'doc':'','name':''})
            for field, column_key in (('doc','doc_num'),('name','name')):
                if column_key in cols and not group[field]:
                    group[field] = str(cells[cols[column_key]].get('value') or '')[:120]
            if group['date'] != day:
                raise ValueError('Transaction identity has conflicting dates')
            # Preserve source dimensions as posting attribution, never interpret them.
            attribution = json.dumps(cells, sort_keys=True, separators=(',', ':'))
            group['postings'].append(Posting(mapped[source_account], debit-credit, 'USD', (('qbo-row', attribution),)))
            group['evidence'].append({'account_id':source_account,'cells':cells})
            movement[source_account] += debit-credit
            if day < year_begin:
                early[source_account] += debit-credit
    for identity in accounts:
        cursor = first
        for lo, hi in sorted(windows[identity]):
            if lo != cursor:
                raise ValueError('GL partition coverage has a gap or overlap')
            cursor = hi+timedelta(days=1)
        if cursor != last+timedelta(days=1):
            raise ValueError('GL partition coverage is incomplete')
    expected = {'opening_trial_balance','closing_trial_balance','profit_and_loss','balance_sheet'}
    if set(singles) != expected:
        raise ValueError('Opening/closing TrialBalance, ProfitAndLoss and BalanceSheet controls are required')
    prior = (first-timedelta(days=1)).isoformat()
    _header(singles['opening_trial_balance'], 'TrialBalance', basis, None, prior)
    _header(singles['closing_trial_balance'], 'TrialBalance', basis, None, last.isoformat())
    _header(singles['profit_and_loss'], 'ProfitAndLoss', basis, first.isoformat(), last.isoformat())
    _header(singles['balance_sheet'], 'BalanceSheet', basis, None, last.isoformat())
    opening, closing = (_trial(singles[k], accounts) for k in ('opening_trial_balance','closing_trial_balance'))
    pnl = _statement(singles['profit_and_loss'], accounts, mapped, 'profit_and_loss')
    balance = _statement(singles['balance_sheet'], accounts, mapped, 'balance_sheet')
    # Only one home currency is supported. A foreign-currency record is held back, never converted.
    blockers = [{'code':'foreign_currency_unsupported',**item} for item in sorted(foreign, key=lambda i: (i['type'], i['id']))]
    controls = []
    if expected_counts is not None:
        for kind, expected in sorted(expected_counts.items()):
            actual = source_by_type.get(kind, 0)
            if expected != actual:
                blockers.append({'code':'source_count_changed','type':kind,'expected':expected,'actual':actual})
    closing_net = sum((closing.get(k, Decimal(0)) for k in accounts if mapped[k].split(':')[0] in {'Income','Expenses'}), Decimal(0))
    if balance.get('__net_income__', Decimal(0)) != closing_net or sum(balance.values(), Decimal(0)):
        blockers.append({'code':'balance_sheet_net_income_mismatch'})
    # The opening report is dated the day before the history, in the fiscal year of that day. When a year
    # end falls between it and the history end, QuickBooks has closed that year's income and expense
    # into Retained Earnings, and so has every year the history spans.
    year_closed = year_begin > first-timedelta(days=1)
    is_pl = lambda k: mapped[k].split(':')[0] in {'Income','Expenses'}
    closed = {k:(opening.get(k, Decimal(0))+early[k] if year_closed else Decimal(0)) for k in accounts if is_pl(k)}
    rolled = sum(closed.values(), Decimal(0))
    retained = [k for k,a in accounts.items() if mapped[k].split(':')[0] == 'Equity'
                and (a.get('AccountSubType') == 'RetainedEarnings' or a.get('Name') == 'Retained Earnings')]
    if rolled and len(retained) != 1:
        blockers.append({'code':'retained_earnings_account_unclear','accounts':sorted(retained)})
    for identity in sorted(accounts):
        if is_pl(identity):
            actual = (movement[identity]-early[identity]) if year_closed else opening.get(identity, Decimal(0))+movement[identity]
        else:
            actual = opening.get(identity, Decimal(0))+movement[identity]
            if retained == [identity]:
                actual += rolled
        target = closing.get(identity, Decimal(0))
        ok = actual == target
        root = mapped[identity].split(':')[0]
        statement_ok = (movement[identity] == pnl.get(identity, Decimal(0)) if root in {'Income','Expenses'} else
                        target == balance.get(identity, Decimal(0)))
        controls.append({'account_id':identity,'account':mapped[identity], 'opening':str(opening.get(identity,0)),
                         'movement':str(movement[identity]),'closing':str(target),'balanced':ok,'statement_agrees':statement_ok,
                         'closed_into_retained_earnings':str(closed[identity] if identity in closed else (rolled if retained == [identity] else Decimal(0)))})
        if not ok or not statement_ok:
            blockers.append({'code':'control_mismatch','account_id':identity})
    namespace = f"quickbooks:{manifest['environment']}:{realm}"
    entries = []
    opening_postings = tuple(Posting(mapped[k],value,'USD') for k,value in sorted(opening.items()) if value)
    if opening_postings:
        entries.append(Entry(date.fromisoformat(prior), 'QuickBooks historical opening',
            meta=(('source-id',namespace+':history-opening:'+first.isoformat()),('import-source','qbo-history-opening')),
            postings=opening_postings))
    for (label, identity), group in sorted(groups.items()):
        if sum((p.amount for p in group['postings']),Decimal(0)) or len(group['postings']) < 2:
            blockers.append({'code':'unbalanced_transaction','type':label,'id':identity})
            continue
        entries.append(Entry(group['date'], 'QuickBooks '+label+' '+identity,
            meta=(('source-id',namespace+':'+label+':'+identity),('import-source','qbo-history'),
                  *((('qbo-doc-number',group['doc']),) if group['doc'] else ()),
                  *((('qbo-name',group['name']),) if group['name'] else ()),
                  ('qbo-source-digest',versions[(label,identity)]),('qbo-posting-digest',_digest(group['evidence']))),
            postings=tuple(group['postings'])))
    store = LedgerStore(default_store_path(entity.path))
    digest = store.content_digest()
    source_keys = ('format','environment','realm_id','history_from','history_to','accounting_basis','currency','account_map','cutovers')
    # Collection jobs, filenames, request timestamps and partition layouts do not
    # identify transactions. Retain separate artifact hashes in the reviewed plan.
    semantic = {**{k:manifest.get(k) for k in source_keys},'account_map':mapped,'cutovers':cutovers,
                # Running balances change with every later QuickBooks entry, and reference records
                # (customers, vendors, items) never post. Neither may break an exact replay.
                'accounts':{k:{f:x for f,x in a.items() if f not in {'CurrentBalance','CurrentBalanceWithSubAccounts'}}
                            for k,a in accounts.items()},
                'versions':{k+':'+v:d for (k,v),d in versions.items()},
                'groups':{k+':'+v:{'date':g['date'].isoformat(),
                    'evidence':sorted(g['evidence'],key=lambda r:json.dumps(r,sort_keys=True))}
                    for (k,v),g in groups.items()},
                'opening':{k:str(v) for k,v in opening.items()},
                'closing':{k:str(v) for k,v in closing.items()},
                'pnl':{k:str(v) for k,v in pnl.items()},'balance':{k:str(v) for k,v in balance.items()}}
    source_digest = _digest(semantic)
    old = receipts(entity)
    exact = next((r for r in old if r['source_digest'] == source_digest), None)
    if old and not exact:
        blockers.append({'code':'existing_history_requires_review'})
    open_changes = [{'account':o.account,'from':o.date.isoformat(),'to':prior} for o in store.load_opens()
                    if o.account in mapped.values() and o.date > date.fromisoformat(prior)]
    if not exact:
        for entry in store.load_entries():
            if any(p.account in mapped.values() for p in entry.postings):
                blockers.append({'code':'existing_accounting_requires_review','source_id':entry.source_id})
                break
        from .ledger.importer import _load_pending_categorization, _ledger_account_for_txn
        for txn in _load_pending_categorization(entity):
            try:
                account = _ledger_account_for_txn(txn,entity.entity_config.get('bank_account_mappings'),entity.entity_config.get('csv_account_mappings'))
            except ValueError:
                blockers.append({'code':'unmapped_pending_requires_review'})
                continue
            if account in mapped.values():
                blockers.append({'code':'pending_accounting_requires_review','source_id':txn.get('id')})
    receipt = {'source_digest':source_digest,'environment':manifest['environment'],'realm_id':realm,
               'history_from':first.isoformat(),'history_to':last.isoformat(),'account_map':mapped,
               'cutovers':cutovers,'open_date_changes':open_changes,'expected_source_counts':expected_counts,
               'reference_versions':{k+':'+v:d for (k,v),d in reference_versions.items()},'source_versions':{k+':'+v:d for (k,v),d in versions.items()},'controls':controls}
    posted_by_type = defaultdict(int)
    for label, identity in groups:
        posted_by_type[label] += 1
    not_moved = [{'type':label,'id':identity,'reason':'no_source_report_effect'}
                 for label,identity in sorted(versions) if (label,identity) not in groups]
    not_moved += [{'type':label,'id':identity,'reason':'reference_evidence_only'}
                  for label,identity in sorted(reference_versions)]
    result = {'success':True,'ready':not blockers,'applied':False,'already_applied':bool(exact),
              'history_from':first.isoformat(),'history_to':last.isoformat(),'blockers':blockers,'controls':controls,
              'counts':{'source_records_read':records,'transactions':len(groups),'entries':len(entries),'files':len(files),
                        'source_by_type':dict(source_by_type),'planned_by_type':dict(posted_by_type),'recorded_by_type':{},
                        'reference_by_type':{k:source_by_type[k] for k in sorted(reference_types) if k in source_by_type},
                        'not_moved':not_moved},
              'recorded_entries':0,'account_map':mapped,'cutovers':cutovers,'open_date_changes':open_changes,'plan_hash':_digest({'source':source_digest,'ledger':digest,'artifacts':files,'open_date_changes':open_changes,'expected_source_counts':expected_counts})}
    return result, entries, receipt, digest


def sync_transactions(entity, input_path, *, apply=False, plan_hash=None):
    from .ledger.importer import _entity_write_lock, _atomic_ledger_write_unlocked
    with _entity_write_lock(entity.path):
        try:
            result, entries, receipt, digest = _plan(entity, Path(input_path))
        except MissingEvidence as exc:
            return {'success': not apply, 'ready': False, 'applied': False, 'already_applied': False,
                    'blockers': [{'code': 'evidence_file_missing', 'path': name} for name in exc.args[0]],
                    'controls': [], 'counts': {}, 'recorded_entries': 0, 'plan_hash': None}
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ValueError('Malformed or unsupported QuickBooks report shape') from exc
        if not apply:
            return result
        if not result['ready']:
            return dict(result, success=False)
        if result['already_applied']:
            return dict(result, applied=True)
        if not plan_hash or plan_hash != result['plan_hash']:
            raise ValueError('Stale or missing reviewed history plan hash')
        store = LedgerStore(default_store_path(entity.path))
        existing = {o.account:o.date for o in store.load_opens()}
        opening_day = date.fromisoformat(receipt['history_from'])-timedelta(days=1)
        opens = [Open(opening_day,a,('USD',))
                 for a in receipt['account_map'].values() if a not in existing or existing[a] > opening_day]
        _atomic_ledger_write_unlocked(entity, opens, entries, 'qbo-history-'+receipt['source_digest'][:16], None,
                                      'Record reviewed QuickBooks history',expected_digest=digest,history_receipt=receipt,allow_earlier_open_dates=True)
        result['counts']['recorded_by_type'] = result['counts']['planned_by_type']
        return dict(result, applied=True,recorded_entries=len(entries))


# Buffer decisions are durable ledger audit events. They authorize only the
# reviewed source identity, account, date and bank amount, never a whole period.
def _audit(entity, types):
    marks = ','.join('?' for _ in types)
    with LedgerStore(default_store_path(entity.path)).connection() as conn:
        return [(r[0], json.loads(r[1])) for r in conn.execute(
            f"SELECT type, payload_json FROM audit_events WHERE type IN ({marks}) ORDER BY id", tuple(types))]


def _buffer_decisions(entity):
    """The decisions in force: a reversed decision is not in force, and a later one can replace it."""
    active = {}
    for kind, record in _audit(entity, ('qbo-buffer-decision', 'qbo-buffer-decision-reversed')):
        if kind == 'qbo-buffer-decision':
            active[record['source_id']] = record
        else:
            active.pop(record['source_id'], None)
    return list(active.values())


def buffer_decision(entity, source_id):
    return next((r for r in reversed(_buffer_decisions(entity)) if r['source_id'] == source_id), None)


# A bank row a person handled stands in the duplicate pool as an original: matched to a QuickBooks leg,
# released as new (it is an ordinary posted bank row), or marked as already in the starting balance.
ORIGINAL_KINDS = {'history-match', 'release', 'in-opening'}


def channel_of(txn, source_id):
    """The source of a bank row: its mapping key (a statement profile) or its feed name in the ID."""
    from .source_ids import source_family
    return str((txn or {}).get('csvMappingKey') or source_family(source_id))


class HistoryIndex:
    """What a bank row is checked against, loaded once for a whole batch of rows.

    A history bank leg is *unmatched* until a person matches a bank row to it (a durable
    ``history-match`` decision). Only an unmatched leg can make a later bank row a possible
    duplicate of QuickBooks history.
    """

    def __init__(self, entity, receipts_=None):
        self.entity = entity
        self.receipts = receipts(entity) if receipts_ is None else receipts_
        self.decisions = {}
        self.copies = {}
        self.copy_families = {}
        if self.receipts:
            for record in _buffer_decisions(entity):
                self.decisions[record['source_id']] = record
            # A recognized copy counts only while the match of its original stands.
            def active(original):
                return (self.decisions.get(original) or {}).get('decision') in ORIGINAL_KINDS
            for _, record in _audit(entity, ('qbo-history-copy',)):
                if active(record['original']):
                    self.copies[record['source_id']] = record
            for record in self.decisions.values():
                if record['decision'] == 'copy' and active(record.get('original')):
                    self.copies[record['source_id']] = {**record, 'family': channel_of(record.get('transaction'), record['source_id'])}
            for record in self.copies.values():
                self.copy_families.setdefault(record['original'], set()).add(record['family'])
        self._legs = None
        self._items = {}
        self.starts = history_start_dates(entity.path) if self.receipts else {}

    def handled_items(self, account):
        """Bank rows a person matched to history, as items for the ordinary duplicate pool.

        Each one stands for the bank row it consumed. Like a posted entry in the ordinary pool, it
        matches one row in one pass, so two real rows of one amount in a pass can both exist.
        """
        if account in self._items:
            return self._items[account]
        from .statement_import import normalize_description
        items = []
        for d in self.decisions.values():
            if d['decision'] not in ORIGINAL_KINDS or d['account'] != account:
                continue
            txn = d.get('transaction') or {}
            items.append({'source_id': d['source_id'], 'date': date.fromisoformat(str(d['date'])[:10]),
                          'amount': Decimal(str(d['amount'])),
                          'desc': normalize_description(str(txn.get('reference') or txn.get('description') or '')),
                          'family': channel_of(txn, d['source_id']), 'history_source_id': d.get('history_source_id')})
        self._items[account] = sorted(items, key=lambda i: (i['date'], i['source_id']))
        return self._items[account]

    def before_books(self, account, day):
        """True for a bank row dated before the history starts: the opening balance already includes it."""
        start = self.starts.get(account)
        return bool(start and date.fromisoformat(str(day)[:10]) < start)

    def decision(self, source_id):
        return self.decisions.get(source_id)

    def settled_as_duplicate(self, source_id):
        """A person matched this row to history or called it a copy: it is never posted."""
        return (self.decisions.get(source_id) or {}).get('decision') in {'history-match', 'copy', 'in-opening'}

    def copy_record(self, source_id):
        """A row recognized earlier as the copy of a matched bank row. The same source ID is the same row."""
        return self.copies.get(source_id)

    def pair(self, account, mapping_key, txn, taken, period=None):
        """Pair a row with a bank row that a person handled, by the ordinary duplicate rule.

        Returns ``(kind, original_id, family)`` or None. ``kind`` is:

        - ``copy``: the ordinary rule calls the pair an exact duplicate (the same date, amount and
          description) from another source, that source can hold the original (a feed always; a statement
          only when ``period`` covers the original's date), and the original has no copy from this source
          yet. Only this kind is skipped. A matched bank row has at most one recognized copy from each
          other source (a feed row has one statement copy, a statement row has one feed copy).
        - ``possible_copy``: the ordinary rule pairs the rows only by "the same amount within three days
          from another source", or the source cannot hold the original. The ordinary path holds such a
          pair for a person; so does this one. It is never more permissive than the ordinary path.
        - ``second_row``: an exact pair that is not the one copy (same source as the original, or the
          original already has its copy from this source): it may be a second real row.

        ``taken`` maps an original to the sources that took it earlier in this pass.
        """
        from .statement_import import _match_pool, _txn_amount, normalize_description
        items = self.handled_items(account)
        if not items:
            return None
        try:
            day = date.fromisoformat(str(txn['date'])[:10])
            amount = _txn_amount(txn)
        except (KeyError, ValueError, ArithmeticError):
            return None
        desc = normalize_description(str(txn.get('reference') or txn.get('description') or ''))
        match, rule = _match_pool(items, set(), day, amount, desc, mapping_key)
        if match is None:
            return None
        if rule != 'same_date_amount_description':
            return ('possible_copy', items[match]['source_id'], mapping_key)
        free = [i for i in items if i['family'] != mapping_key
                and mapping_key not in self.copy_families.get(i['source_id'], set()) | taken.get(i['source_id'], set())]
        free_match, free_rule = _match_pool(free, set(), day, amount, desc, mapping_key)
        if free_match is None or free_rule != 'same_date_amount_description':
            return ('second_row', items[match]['source_id'], mapping_key)
        original = free[free_match]
        if period is not None and not (period[0] <= original['date'] <= period[1]):
            return ('possible_copy', original['source_id'], mapping_key)
        return ('copy', original['source_id'], mapping_key)

    def _matched(self):
        return {(d['history_source_id'], d['account']) for d in self.decisions.values()
                if d['decision'] == 'history-match'}

    def legs(self):
        """Every QuickBooks history bank leg: one per entry and bank or card account."""
        if self._legs is None:
            bank = {a for r in self.receipts for a in r['cutovers']}
            matched = self._matched()
            found = []
            for entry in LedgerStore(default_store_path(self.entity.path)).load_entries():
                meta = dict(entry.meta)
                if meta.get('import-source') != 'qbo-history':
                    continue
                for account in sorted({p.account for p in entry.postings if p.account in bank}):
                    parts = entry.source_id.split(':')
                    found.append({'source_id': entry.source_id, 'account': account, 'date': entry.date.isoformat(),
                                  'amount': str(sum((p.amount for p in entry.postings if p.account == account), Decimal(0))),
                                  'type': parts[-2] if len(parts) >= 2 else '',
                                  'doc_number': meta.get('qbo-doc-number', ''), 'name': meta.get('qbo-name', ''),
                                  'description': entry.narration,
                                  'matched': (entry.source_id, account) in matched})
            self._legs = found
        return self._legs

    def candidates(self, account, day, amount):
        """Unmatched legs on the account with this exact signed amount, dated up to 3 days after the row.

        There is no lower bound: a check written early in a long period can clear months later. The
        closest leg in date comes first.
        """
        if amount is None:
            return []
        day = date.fromisoformat(str(day)[:10])
        hi = day + timedelta(days=LEG_LOOKAHEAD_DAYS)
        found = [leg for leg in self.legs() if leg['account'] == account and not leg['matched']
                 and Decimal(leg['amount']) == amount and date.fromisoformat(leg['date']) <= hi]
        return sorted(found, key=lambda leg: (abs((date.fromisoformat(leg['date']) - day).days), leg['date'], leg['source_id']))

    def holds(self, account, day, amount=None):
        """Why a bank row waits for a person, or None when it follows the normal path.

        ``inside_period``: dated up to the history end; QuickBooks owns the books there.
        ``unmatched_history_leg``: up to HISTORY_BANK_LAG_DAYS after the history end, and an unmatched
        leg of the same amount and direction (dated anywhere up to 3 days after the row) could be the same money.
        ``settlement_window``: within SETTLEMENT_DAYS after the history end (a split or combined
        settlement can have a different amount).
        """
        if not self.receipts:
            return None
        day = date.fromisoformat(str(day)[:10])
        for r in self.receipts:
            if account not in r['account_map'].values():
                continue
            if day < date.fromisoformat(r['history_from']):
                continue  # Before the books start: the opening balance already includes it.
            end = date.fromisoformat(r['history_to'])
            if day <= end:
                return 'inside_period'
            if account not in r['cutovers']:
                continue
            if day <= end + timedelta(days=HISTORY_BANK_LAG_DAYS) and self.candidates(account, day, amount):
                return 'unmatched_history_leg'
            if day <= end + timedelta(days=SETTLEMENT_DAYS):
                return 'settlement_window'
        return None


def allowed_decisions(index, row, amount, candidates, store=None):
    """The exact answers the engine accepts for a held row now, with the IDs it accepts.

    ``release`` (this is new): always, unless the row was already posted and reversed (the same source ID
    cannot post again). ``history-match``: only with unmatched legs of the same account, amount and direction
    (``history_source_ids``). ``copy``: only with a handled bank row on the same account of the same amount
    within three days (``original_source_ids``). ``in-opening``: a row on a mapped bank or card account,
    dated from the history start up to OPENING_CLEARING_DAYS after it, with no candidate leg: a deposit in
    transit or an outstanding check at the opening clears in that time. The engine cannot see the opening
    balance's contents, so the person decides; a row that an unmatched leg could explain uses history-match.
    """
    allowed = []
    sid = row['source_id']
    if store is None or not store.source_exists(sid):
        allowed.append({'decision': 'release'})
    if candidates:
        allowed.append({'decision': 'history-match', 'history_source_ids': [c['source_id'] for c in candidates]})
    originals = []
    try:
        day = date.fromisoformat(str(row['date'])[:10])
    except (KeyError, ValueError):
        day = None
    if day is not None and amount is not None:
        for d in index.decisions.values():
            if (d['decision'] in ORIGINAL_KINDS and d['account'] == row['account'] and d['source_id'] != sid
                    and Decimal(str(d['amount'])) == amount
                    and abs((date.fromisoformat(str(d['date'])[:10]) - day).days) <= SETTLEMENT_DAYS):
                originals.append(d['source_id'])
    if originals:
        allowed.append({'decision': 'copy', 'original_source_ids': sorted(originals)})
    start = index.starts.get(row['account'])
    if (day is not None and start and not candidates and start <= day <= start + timedelta(days=OPENING_CLEARING_DAYS)):
        allowed.append({'decision': 'in-opening'})
    return allowed


def is_history_hold(entity, candidate):
    """True for a duplicate-review item that the history holds (it needs the buffer commands)."""
    if not candidate.get('account'):
        return False
    index = HistoryIndex(entity)
    if index.before_books(candidate['account'], candidate.get('date', '')):
        return False  # Before the books start: not a history hold.
    if candidate.get('held_for_quickbooks_history') or candidate.get('match_rule') == 'quickbooks_history_ownership':
        return True
    try:
        amount = Decimal(str(candidate['amount']))
    except (KeyError, ArithmeticError):
        amount = None
    return bool(index.holds(candidate['account'], candidate.get('date', ''), amount))


def released_entry(entity, entry, posting, decisions=None):
    decision = buffer_decision(entity, entry.source_id) if decisions is None else decisions.get(entry.source_id)
    return bool(decision and decision['decision'] == 'release'
                and decision['account'] == posting.account and decision['date'] == entry.date.isoformat()
                and _money(decision['amount']) == posting.amount)


def _buffer_now(now=None):
    value = now or datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Buffer time must include a timezone')
    return value.astimezone(timezone.utc)


def list_buffer_decisions(entity, now=None, _index=None):
    """List open holds with their evidence, the hold rule and the elapsed-time deadline.

    Each row has ``history_matches`` (a journal touching the account within three days, any
    amount, as a suggestion), ``candidate_legs`` (unmatched history bank legs of the same
    amount and direction: date, amount, QuickBooks type, document number, name) and
    ``hold_rule`` (``inside_period``, ``unmatched_history_leg``, ``settlement_window``,
    ``possible_second_row``, ``possible_copy`` or None when only an earlier hold remains). A
    ``possible_second_row`` row pairs exactly with a bank row that a person matched to history and that
    already has its copy from this source (or comes from the same source). A ``possible_copy`` row pairs
    with one only by amount within three days, or from a statement that cannot hold it. For both,
    ``matched_original`` shows that bank row and its QuickBooks leg.
    """
    path = entity.staging_dir / 'duplicate-candidates.json'
    if not path.exists():
        return []
    index = _index or HistoryIndex(entity)
    result = []
    for row in json.loads(path.read_text()):
        if row.get('status') != 'duplicate-candidate' or not row.get('account'):
            continue
        try:
            amount = Decimal(str(row['amount']))
        except (KeyError, ArithmeticError):
            amount = None
        if index.before_books(row['account'], row.get('date', '')):
            continue
        original = row.get('matched_original')
        second = (row.get('history_hold_rule') in {'possible_second_row', 'possible_copy'}
                  and (index.decisions.get(original) or {}).get('decision') in ORIGINAL_KINDS)
        rule = row['history_hold_rule'] if second else index.holds(row['account'], row.get('date'), amount)
        if not (rule or row.get('held_for_quickbooks_history') or row.get('match_rule') == 'quickbooks_history_ownership'):
            continue
        day = date.fromisoformat(str(row['date'])[:10])
        matches = [{'source_id':leg['source_id'],'date':leg['date'],'amount':leg['amount'],
                    'description':leg['description'],'account':row['account']}
                   for leg in index.legs()
                   # Settlement aggregation/splits can change a bank row's amount.
                   # Any nearby journal touching this account needs a human decision.
                   if leg['account'] == row['account'] and not leg['matched']
                   and abs((date.fromisoformat(leg['date']) - day).days) <= 3]
        candidates = [{**{k:leg[k] for k in ('source_id','account','date','amount','type','doc_number','name')},
                       'days_before_row': (day - date.fromisoformat(leg['date'])).days}
                      for leg in index.candidates(row['account'], row['date'], amount)]
        deadline = _buffer_now(row['created_at']) + timedelta(days=3)
        matched_original = None
        if second:
            d = index.decisions[original]
            leg = next((l for l in index.legs() if l['source_id'] == d.get('history_source_id') and l['account'] == d['account']), None)
            matched_original = {'source_id': original, 'kind': d['decision'], 'account': d['account'], 'date': d['date'],
                                'amount': d['amount'],
                                'description': str((d.get('transaction') or {}).get('description') or ''),
                                'history_source_id': d.get('history_source_id'),
                                'leg': leg and {k: leg[k] for k in ('source_id', 'account', 'date', 'amount', 'type', 'doc_number', 'name')}}
        released = []
        if second:
            store = LedgerStore(default_store_path(entity.path))
            for d in index.decisions.values():
                if (d['decision'] == 'release' and d['account'] == row['account'] and d['source_id'] != row['source_id']
                        and amount is not None and Decimal(str(d['amount'])) == amount
                        and abs((date.fromisoformat(str(d['date'])[:10]) - day).days) <= 3):
                    released.append({'source_id': d['source_id'], 'date': d['date'], 'amount': d['amount'],
                                     'description': str((d.get('transaction') or {}).get('description') or ''),
                                     'posted_entry': d['source_id'] if store.source_exists(d['source_id']) else None})
        result.append({**row, 'held':{k:row[k] for k in ('account','date','amount','description')},
                       'held_for_quickbooks_history':True, 'hold_rule':rule, 'history_matches':matches,
                       'candidate_legs':candidates, 'matched_original':matched_original, 'released_rows':released,
                       'allowed_decisions':allowed_decisions(index, row, amount, candidates, LedgerStore(default_store_path(entity.path))),
                       'hold_until':deadline.isoformat()})
    return result


def _mark_candidates(entity, updates):
    """Set the display status of several review items with one read and one write."""
    from .statement_import import _write_json_atomic
    path = entity.staging_dir / 'duplicate-candidates.json'
    candidates = json.loads(path.read_text())
    for candidate in candidates:
        if candidate.get('source_id') in updates:
            candidate.update(updates[candidate['source_id']], held_for_quickbooks_history=True)
    _write_json_atomic(path, candidates)


def _release_rows(entity, rows, resolved_at, index):
    """Record the release decision of each row, then run the rows through normal intake.

    Returns ``(released_ids, errors)``. One failing row never stops the rows after it: if the
    batch fails, each row is tried on its own and its error is kept.
    """
    from .ledger.importer import import_transactions
    from .queue import make_categorizer
    new = [r for r in rows if r['source_id'] not in index.decisions]
    if new:
        store = LedgerStore(default_store_path(entity.path))
        with store.connection() as conn:
            for row in new:
                record = {k:row[k] for k in ('source_id','account','date','amount','transaction')}
                record.update(decision='release', history_source_id=None, resolved_at=resolved_at)
                store.append_audit_event('qbo-buffer-decision', record, conn, ts=resolved_at)
                index.decisions[row['source_id']] = record
            conn.commit()

    def intake(batch):
        imported = import_transactions(entity, [r['transaction'] for r in batch], 'qbo-buffer-release',
                                       categorizer=make_categorizer(entity), legacy_duplicate_guard=False)
        if imported.errors or imported.before_opening:
            raise ValueError('Released row requires intake correction: '+str(imported.errors or imported.before_opening))

    released, errors = [], []
    try:
        intake(rows)
        released = [r['source_id'] for r in rows]
    except Exception as exc:
        if len(rows) == 1:
            errors.append({'source_id': rows[0]['source_id'], 'error': str(exc)})
        else:
            for row in rows:
                try:
                    intake([row])
                    released.append(row['source_id'])
                except Exception as inner:
                    errors.append({'source_id': row['source_id'], 'error': str(inner)})
    if released:
        _mark_candidates(entity, {sid:{'status':'released-to-intake','history_source_id':None,'resolved_at':resolved_at}
                                  for sid in released})
    return released, errors


def resolve_buffer_decision(entity, source_id, decision, history_source_id=None, now=None, original_source_id=None):
    """Persist a source-scoped choice before retryable normal intake.

    ``release`` says the row is new and posts it through intake. ``history-match`` says it is already in
    the QuickBooks history (a leg). ``copy`` says it is another copy of a bank row that was matched to
    history (``original_source_id``, by default the row's ``matched_original``). ``in-opening`` says the row
    is already in the starting balance (for example a deposit in transit on the day before the history
    starts): it is recorded as handled and never posted, and it stands in the pairing like a matched row.
    """
    from .ledger.importer import _entity_write_lock
    if decision not in {'release', 'history-match', 'copy', 'in-opening'}:
        raise ValueError('Buffer decision must be release, history-match, copy or in-opening')
    with _entity_write_lock(entity.path):
        index = HistoryIndex(entity)
        previous = index.decision(source_id)
        row = next((r for r in list_buffer_decisions(entity, _index=index) if r['source_id'] == source_id), None)
        if decision == 'copy' and row is not None and not original_source_id:
            original_source_id = (row.get('matched_original') or {}).get('source_id')
        if previous and (previous['decision'] != decision or previous.get('history_source_id') != history_source_id
                         or (decision == 'copy' and previous.get('original') != original_source_id)):
            raise ValueError('A durable buffer decision already exists')
        if row is None:
            if previous:
                return previous
            raise ValueError('No open QuickBooks buffer row')
        if not previous:
            # The host shows only the answers in row['allowed_decisions']; the engine refuses any other.
            allowed = {a['decision']: a for a in row['allowed_decisions']}
            if decision == 'release' and decision not in allowed:
                raise ValueError('This row was posted and then reversed. The same source ID cannot be posted again. '
                                 'If the row is new, post a journal entry for it; otherwise choose a different answer.')
            if decision not in allowed:
                raise ValueError(f"The answer {decision} is not allowed for this row now. Allowed: "
                                 f"{', '.join(allowed) or 'none'}.")
            if decision == 'history-match' and history_source_id not in allowed[decision]['history_source_ids']:
                raise ValueError('Select an unmatched history journal entry with the same account, amount and direction')
            if decision == 'copy' and original_source_id not in allowed[decision]['original_source_ids']:
                raise ValueError('Name a bank row on the same account and amount, within three days, that was matched to '
                                 'QuickBooks history, released as new, or marked as already in the starting balance')
        resolved_at = _buffer_now(now).isoformat()
        record = {k:row[k] for k in ('source_id','account','date','amount','transaction')}
        record.update(decision=decision, history_source_id=history_source_id, resolved_at=resolved_at)
        if decision == 'copy':
            record['original'] = original_source_id
        if decision == 'release':
            released, errors = _release_rows(entity, [row], resolved_at, index)
            if errors:
                raise ValueError(errors[0]['error'])
            return {**row, **record, 'status':'released-to-intake'}
        if not previous:
            store = LedgerStore(default_store_path(entity.path))
            with store.connection() as conn:
                store.append_audit_event('qbo-buffer-decision',record,conn,ts=resolved_at)
                conn.commit()
        status = 'already-in-opening' if decision == 'in-opening' else 'confirmed-duplicate'
        _mark_candidates(entity, {source_id:{'status':status,'history_source_id':history_source_id,
                                             'resolved_at':resolved_at}})
        return {**row, **record, 'status':status}


def release_due_buffer(entity, now=None):
    """Release rows whose wait is over and that no QuickBooks bank leg could explain.

    A row is never released by time when it is inside the history period, when a journal touches
    the account within three days, when it may be a second real row, or when an unmatched leg of the same amount and direction is
    dated up to HISTORY_BANK_LAG_DAYS before it (``candidate_legs``). Decisions and ledger entries
    are loaded once for the whole call. Per-row failures are returned in ``errors``.
    """
    from .ledger.importer import _entity_write_lock
    current = _buffer_now(now)
    with _entity_write_lock(entity.path):
        index = HistoryIndex(entity)
        review, due = [], []
        for row in list_buffer_decisions(entity, _index=index):
            if row['history_matches'] or row['candidate_legs'] or row['hold_rule'] in {'inside_period', 'possible_second_row', 'possible_copy'}:
                review.append(row['source_id'])
            elif current >= _buffer_now(row['hold_until']):
                due.append(row)
        released, errors = _release_rows(entity, due, current.isoformat(), index) if due else ([], [])
        return {'released':released,'needs_review':review,'errors':errors}


def reverse_buffer_decision(entity, source_id, reason='', now=None):
    """A person undoes a buffer decision. Every reversal is a durable audit event.

    - ``history-match``: the leg is unmatched again, the row is held again, and a later copy of that bank
      row is no longer recognized as already handled. Every copy that was recorded for it (and every
      ``copy`` decision for it) is no longer a copy: those rows are held for a person again
      (``reopened_rows``).
    - ``release``: when the row is already posted, the posted entry is reversed first (the engine's own
      reversing entry, which keeps the original) and then the decision is reversed, so the row is held
      again and the person can decide again. A row that waits in the review queue is refused: withdraw
      that item first. A release that posted nothing is simply reversed.
    """
    from .ledger.importer import _entity_write_lock, _load_pending_categorization
    with _entity_write_lock(entity.path):
        index = HistoryIndex(entity)
        decision = index.decision(source_id)
        if decision is None:
            raise ValueError('There is no buffer decision to reverse for this source ID')
        store = LedgerStore(default_store_path(entity.path))
        stamp = _buffer_now(now).isoformat()
        reversing_entry = None
        if decision['decision'] == 'release':
            if any(str(t.get('id')) == source_id for t in _load_pending_categorization(entity)):
                raise ValueError('This row was released and is waiting in the review queue. Withdraw that item first, '
                                 'then reverse this decision.')
            from .ledger.importer import _find_live_entry, reverse_entry
            try:
                _find_live_entry(store, source_id)
                live = True
            except ValueError:
                live = False
            if live:
                # The person's own reversal of the posted entry: allowed by the write guard, and done first.
                # If the next step fails, run this command again: the entry is no longer live, so only the
                # decision is reversed.
                reversing_entry = reverse_entry(entity, source_id, 'qbo-buffer-reversal', ts=stamp)
        record = {'source_id': source_id, 'reversed_decision': decision['decision'],
                  'history_source_id': decision.get('history_source_id'), 'account': decision['account'],
                  'date': decision['date'], 'amount': decision['amount'], 'reason': str(reason or '')[:500],
                  'reversed_at': stamp, 'entry_reversed': reversing_entry is not None}
        # Rows recognized as copies of a matched bank row were never posted. When the match is undone they
        # are no longer copies: each one is held for a person again.
        reopened = []
        if decision['decision'] in ORIGINAL_KINDS:
            reopened = sorted(c['source_id'] for c in index.copies.values() if c['original'] == source_id)
        with store.connection() as conn:
            store.append_audit_event('qbo-buffer-decision-reversed', record, conn, ts=stamp)
            for copy_id in reopened:
                if index.decision(copy_id):  # a person's copy decision is reversed with its original
                    store.append_audit_event('qbo-buffer-decision-reversed',
                        {'source_id': copy_id, 'reversed_decision': 'copy', 'history_source_id': None,
                         'account': decision['account'], 'date': index.copies[copy_id]['date'],
                         'amount': index.copies[copy_id]['amount'], 'reason': 'The matched bank row was reversed',
                         'reversed_at': stamp}, conn, ts=stamp)
            conn.commit()
        _mark_candidates(entity, {source_id: {'status': 'duplicate-candidate', 'history_source_id': None, 'resolved_at': None}})
        _hold_again(entity, [index.copies[c] for c in reopened], stamp)
        return {'source_id': source_id, 'reversed_decision': decision['decision'], 'status': 'duplicate-candidate',
                'entry_reversed': reversing_entry is not None, 'reopened_rows': reopened, 'reversed_at': stamp}


def record_history_copies(entity, copies):
    """Keep each recognized copy durably: the matched original, the copy's source and channel, date,
    amount, description and when. One original has at most one copy from each other source."""
    if not copies:
        return
    store = LedgerStore(default_store_path(entity.path))
    stamp = datetime.now(timezone.utc).isoformat()
    with store.connection() as conn:
        for c in copies:
            store.append_audit_event('qbo-history-copy', {**c, 'recorded_at': stamp}, conn, ts=stamp)
        conn.commit()


def _hold_again(entity, copies, stamp):
    """Put rows that were recognized as copies back in the review list, held for a person."""
    from .statement_import import _write_json_atomic
    if not copies:
        return
    path = entity.staging_dir / 'duplicate-candidates.json'
    rows = json.loads(path.read_text()) if path.exists() else []
    by_id = {r.get('source_id'): r for r in rows}
    for c in copies:
        row = by_id.get(c['source_id'])
        if row is None:
            rows.append({'source_id': c['source_id'], 'existing_source_id': '', 'account': c['account'], 'date': c['date'],
                         'amount': c['amount'], 'description': c.get('description', ''),
                         'transaction': c.get('transaction'), 'match_rule': 'quickbooks_history_ownership',
                         'status': 'duplicate-candidate', 'held_for_quickbooks_history': True, 'created_at': stamp})
        else:
            row.update(status='duplicate-candidate', history_source_id=None, resolved_at=None, held_for_quickbooks_history=True)
    _write_json_atomic(path, rows)
