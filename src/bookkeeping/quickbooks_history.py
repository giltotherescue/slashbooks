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
MAX_FILE_BYTES = 8 * 1024 * 1024


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


def receipts(entity):
    path = default_store_path(entity.path)
    if not path.exists():
        return []
    with LedgerStore(path).connection() as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT payload_json FROM audit_events WHERE type='qbo-history-recorded'")]


def owned(entity, account, day, *, nearby=False, _receipts=None):
    """Ownership is durable even if every imported balance and activity is zero."""
    day = date.fromisoformat(str(day)[:10])
    return next((r for r in (receipts(entity) if _receipts is None else _receipts) if account in r['account_map'].values()
                 and day <= date.fromisoformat(r['history_to']) + timedelta(days=3 if nearby and account in r['cutovers'] else 0)), None)


def refuse_owned_write(entity, entries):
    history = receipts(entity)
    if not history:
        return
    for entry in entries:
        released = [p for p in entry.postings if released_entry(entity, entry, p)]
        for posting in entry.postings:
            # A reviewed cash row permits its category counterpart, but cannot
            # authorize a second owned bank leg on a transfer.
            if released and (posting in released or not any(posting.account in r['cutovers'] for r in history)):
                continue
            if owned(entity, posting.account, entry.date, nearby=True, _receipts=history) and not released_entry(entity, entry, posting):
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
            group = groups.setdefault(key, {'date':day,'postings':[],'evidence':[]})
            if group['date'] != day:
                raise ValueError('Transaction identity has conflicting dates')
            # Preserve source dimensions as posting attribution, never interpret them.
            attribution = json.dumps(cells, sort_keys=True, separators=(',', ':'))
            group['postings'].append(Posting(mapped[source_account], debit-credit, 'USD', (('qbo-row', attribution),)))
            group['evidence'].append({'account_id':source_account,'cells':cells})
            movement[source_account] += debit-credit
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
    for identity in sorted(accounts):
        actual = opening.get(identity, Decimal(0))+movement[identity]
        target = closing.get(identity, Decimal(0))
        ok = actual == target
        root = mapped[identity].split(':')[0]
        statement_ok = (movement[identity] == pnl.get(identity, Decimal(0)) if root in {'Income','Expenses'} else
                        target == balance.get(identity, Decimal(0)))
        controls.append({'account_id':identity,'account':mapped[identity], 'opening':str(opening.get(identity,0)),
                         'movement':str(movement[identity]),'closing':str(target),'balanced':ok,'statement_agrees':statement_ok})
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
                  ('qbo-source-digest',versions[(label,identity)]),('qbo-posting-digest',_digest(group['evidence']))),
            postings=tuple(group['postings'])))
    store = LedgerStore(default_store_path(entity.path))
    digest = store.content_digest()
    source_keys = ('format','environment','realm_id','history_from','history_to','accounting_basis','currency','account_map','cutovers')
    # Collection jobs, filenames, request timestamps and partition layouts do not
    # identify transactions. Retain separate artifact hashes in the reviewed plan.
    semantic = {**{k:manifest.get(k) for k in source_keys},'account_map':mapped,'cutovers':cutovers, 'accounts':accounts,
                'versions':{k+':'+v:d for (k,v),d in versions.items()},
                'reference_versions':{k+':'+v:d for (k,v),d in reference_versions.items()},
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
def _buffer_decisions(entity):
    with LedgerStore(default_store_path(entity.path)).connection() as conn:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM audit_events WHERE type='qbo-buffer-decision' ORDER BY id")]


def buffer_decision(entity, source_id):
    return next((r for r in reversed(_buffer_decisions(entity)) if r['source_id'] == source_id), None)


def released_entry(entity, entry, posting):
    decision = buffer_decision(entity, entry.source_id)
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


def list_buffer_decisions(entity, now=None):
    """List open ownership holds with journal evidence and elapsed-time deadline."""
    path = entity.staging_dir / 'duplicate-candidates.json'
    if not path.exists():
        return []
    entries = LedgerStore(default_store_path(entity.path)).load_entries()
    result = []
    for row in json.loads(path.read_text()):
        if row.get('status') != 'duplicate-candidate' or not owned(entity, row.get('account'), row.get('date'), nearby=True):
            continue
        matches = []
        for entry in entries:
            if dict(entry.meta).get('import-source') != 'qbo-history':
                continue
            if abs((entry.date - date.fromisoformat(row['date'])).days) > 3:
                continue
            bank_legs = [p.amount for p in entry.postings if p.account == row['account']]
            # Settlement aggregation/splits can change a bank row's amount.
            # Any nearby journal touching this account needs a human decision.
            if bank_legs:
                matches.append({'source_id':entry.source_id,'date':entry.date.isoformat(),
                                'amount':str(sum(bank_legs, Decimal(0))),
                                'description':entry.narration,'account':row['account']})
        deadline = _buffer_now(row['created_at']) + timedelta(days=3)
        result.append({**row, 'held':{k:row[k] for k in ('account','date','amount','description')}, 'held_for_quickbooks_history':True, 'history_matches':matches,
                       'hold_until':deadline.isoformat()})
    return result


def resolve_buffer_decision(entity, source_id, decision, history_source_id=None, now=None):
    """Persist a source-scoped choice before retryable normal intake."""
    from .ledger.importer import _entity_write_lock, import_transactions
    from .queue import make_categorizer
    from .statement_import import _write_json_atomic
    if decision not in {'release', 'history-match'}:
        raise ValueError('Buffer decision must be release or history-match')
    with _entity_write_lock(entity.path):
        previous = buffer_decision(entity, source_id)
        row = next((r for r in list_buffer_decisions(entity) if r['source_id'] == source_id), None)
        if previous and (previous['decision'] != decision or previous.get('history_source_id') != history_source_id):
            raise ValueError('A durable buffer decision already exists')
        if row is None:
            if previous:
                return previous
            raise ValueError('No open QuickBooks buffer row')
        if decision == 'history-match' and history_source_id not in {r['source_id'] for r in row['history_matches']}:
            raise ValueError('Select a supported history journal entry')
        record = {k:row[k] for k in ('source_id','account','date','amount','transaction')}
        record.update(decision=decision, history_source_id=history_source_id, resolved_at=_buffer_now(now).isoformat())
        if not previous:
            store = LedgerStore(default_store_path(entity.path))
            with store.connection() as conn:
                store.append_audit_event('qbo-buffer-decision',record,conn,ts=record['resolved_at'])
                conn.commit()
        if decision == 'release':
            imported = import_transactions(entity,[row['transaction']],'qbo-buffer-release',
                                           categorizer=make_categorizer(entity),legacy_duplicate_guard=False)
            if imported.errors or imported.before_opening:
                raise ValueError('Released row requires intake correction: '+str(imported.errors or imported.before_opening))
        path = entity.staging_dir / 'duplicate-candidates.json'
        candidates = json.loads(path.read_text())
        for candidate in candidates:
            if candidate.get('source_id') == source_id:
                candidate.update(status='confirmed-distinct' if decision == 'release' else 'confirmed-duplicate',
                                 history_source_id=history_source_id, resolved_at=record['resolved_at'],
                                 held_for_quickbooks_history=True)
        _write_json_atomic(path,candidates)
        return {**row, **record, 'status':'confirmed-distinct' if decision == 'release' else 'confirmed-duplicate'}


def release_due_buffer(entity, now=None):
    current = _buffer_now(now)
    released, review = [], []
    for row in list_buffer_decisions(entity):
        if row['history_matches']:
            review.append(row['source_id'])
        elif current >= _buffer_now(row['hold_until']):
            resolve_buffer_decision(entity,row['source_id'],'release',now=current)
            released.append(row['source_id'])
    return {'released':released,'needs_review':review}
