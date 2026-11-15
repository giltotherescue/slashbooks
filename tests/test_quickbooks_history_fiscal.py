"""History that starts on a fiscal year start: QuickBooks closes each year into Retained Earnings.

The expected QuickBooks reports are made by walking the entries and closing at each fiscal year
start, not by the engine's own formula. Synthetic data only."""
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from bookkeeping.entity import init_entity, load_entity
from bookkeeping.fiscal import year_start
from bookkeeping.ledger.store import LedgerStore, default_store_path
from bookkeeping.quickbooks_history import sync_transactions
from bookkeeping.reports.statements import balance_sheet, profit_and_loss

from test_quickbooks_history import column, entity_record, report

CHART = {'1':('Bank','Assets:Bank:Checking'), '4':('Income','Income:Sales'), '5':('Expense','Expenses:Supplies'),
         '7':('Equity','Equity:Capital'), '10':('Equity','Equity:Retained-Earnings')}
PL = {'4','5'}


def d(text):
    return date.fromisoformat(text)


def simulate(opening, events, first, last, fy):
    """Closing balances (debit minus credit) as QuickBooks reports them: each year is closed into 10."""
    balance = {k:Decimal(v) for k,v in opening.items()}
    year = year_start(first-timedelta(days=1), fy)
    def close():
        balance['10'] = balance.get('10',Decimal(0))+sum((balance.get(k,Decimal(0)) for k in PL), Decimal(0))
        for k in PL: balance[k] = Decimal(0)
    for day,legs in sorted(events, key=lambda e:e[0]):
        if year_start(day, fy) > year:
            close(); year = year_start(day, fy)
        for k,v in legs.items(): balance[k] = balance.get(k,Decimal(0))+Decimal(v)
    if year_start(last, fy) > year:
        close()
    return balance


class FiscalCases:
    basis = 'Accrual'

    def build(self, *, first, last, opening, events, fy='01-01'):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        init_entity(root/'books',name='Synthetic fiscal acceptance',basis=self.basis.lower())
        config = root/'books'/'entity.json';data = json.loads(config.read_text())
        data['fiscal_year_start'] = fy;config.write_text(json.dumps(data))
        self.entity = load_entity(root/'books')
        self.first,self.last,self.fy = d(first),d(last),fy
        source = root/'source';source.mkdir()
        prior = (self.first-timedelta(days=1)).isoformat()
        self.opening = {k:Decimal(v) for k,v in opening.items()}
        self.events = [(d(day),legs) for day,legs in events]
        self.assertEqual(sum(self.opening.values()),0)
        self.closing = simulate(self.opening,self.events,self.first,self.last,fy)
        movement = {k:Decimal(0) for k in CHART}
        for _,legs in self.events:
            for k,v in legs.items(): movement[k] += Decimal(v)
        manifest = {'format':'slashbooks-qbo-history-v1','environment':'sandbox','realm_id':'456','history_from':first,'history_to':last,
                    'accounting_basis':self.basis,'currency':'USD','account_map':{k:v[1] for k,v in CHART.items()},
                    'cutovers':{'Assets:Bank:Checking':last},'files':[]}
        def add(kind,raw,**extra):
            if 'Header' in raw: raw['Header']['ReportBasis'] = self.basis
            path = source/(str(len(manifest['files']))+'.json');path.write_text(json.dumps(raw,default=float))
            manifest['files'].append({'kind':kind,'path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),**extra})
        accounts = [{'Id':k,'Name':'Retained Earnings' if k=='10' else 'Synthetic '+k,'AccountType':v[0],
                     **({'AccountSubType':'RetainedEarnings'} if k=='10' else {})} for k,v in CHART.items()]
        add('accounts',{'QueryResponse':{'Account':accounts}})
        for number,(day,legs) in enumerate(events,1):
            add('entities',{'QueryResponse':{'JournalEntry':[entity_record('JournalEntry',str(number),day,{k:Decimal(v) for k,v in legs.items()})]}},entity_type='JournalEntry')
        for account in CHART:
            rows = []
            for number,(day,legs) in enumerate(events,1):
                if account in legs:
                    amount = Decimal(legs[account])
                    rows.append({'type':'Data','ColData':[{'value':day},{'value':'JournalEntry','id':str(number)},
                                 {'value':str(max(amount,0))},{'value':str(max(-amount,0))}]})
            raw = report('GeneralLedger',first,last,[{'Header':{'ColData':[{'value':'Synthetic','id':account}]},'Rows':{'Row':rows}}],
                         [column(k) for k in ['tx_date','txn_type','debit','credit']])
            add('general_ledger',raw,account_id=account,**{'from':first,'to':last})
        for kind,end,values in [('opening_trial_balance',prior,self.opening),('closing_trial_balance',last,self.closing)]:
            rows = [{'ColData':[{'value':'Synthetic','id':k},{'value':str(max(v,0))},{'value':str(max(-v,0))}]} for k,v in values.items() if v]
            add(kind,report('TrialBalance',end,end,rows,[column('account'),column('debit'),column('credit')]))
        self.pl_net = -sum((movement[k] for k in PL), Decimal(0))
        for kind,name in [('profit_and_loss','ProfitAndLoss'),('balance_sheet','BalanceSheet')]:
            rows = []
            for k,(_,account) in CHART.items():
                root = account.split(':')[0]
                if (kind=='profit_and_loss') != (k in PL): continue
                v = movement[k] if kind=='profit_and_loss' else self.closing.get(k,Decimal(0))
                if root in {'Income','Liabilities','Equity'}: v = -v
                if v: rows.append({'ColData':[{'value':'Synthetic','id':k},{'value':str(v)}]})
            if kind=='balance_sheet':
                rows.append({'ColData':[{'value':'Net Income'},{'value':str(-sum((self.closing.get(k,Decimal(0)) for k in PL),Decimal(0)))}]})
            add(kind,report(name,first,last,rows,[column('account'),{'ColType':'Money','ColTitle':'Total'}]))
        self.path = source/'manifest.json';self.path.write_text(json.dumps(manifest))

    def check(self):
        plan = sync_transactions(self.entity,self.path)
        self.assertTrue(plan['ready'],plan['blockers'])
        self.assertTrue(all(c['balanced'] and c['statement_agrees'] for c in plan['controls']),plan['controls'])
        sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])
        view = self.basis.lower()
        cents = Decimal('0.01')
        # Tie-out 1, profit and loss: the engine statement for the history period equals QuickBooks.
        pnl = profit_and_loss(self.entity.path,self.first,self.last,basis=view)
        self.assertEqual(pnl.totals['net_income'].quantize(cents),self.pl_net.quantize(cents))
        # Tie-out 2, balance sheet at the history end: assets, liabilities plus equity, current-year
        # net income, and retained earnings (the account plus the roll the engine computes).
        bs = balance_sheet(self.entity.path,self.last,view=view)
        assets = sum((v for k,v in self.closing.items() if CHART[k][1].startswith('Assets')),Decimal(0))
        self.assertEqual(bs.totals['total_assets'].quantize(cents),assets.quantize(cents))
        self.assertEqual(bs.totals['total_liabilities_and_equity'].quantize(cents),assets.quantize(cents))
        equity = [r for s in bs.sections if s['label']=='Equity' for r in s['rows']]
        current = next(r['amount'] for r in equity if r.get('key')=='net_income_current_period')
        qbo_current = -sum((self.closing.get(k,Decimal(0)) for k in PL),Decimal(0))
        self.assertEqual(current.quantize(cents),qbo_current.quantize(cents))
        retained = sum((r['amount'] for r in equity if r.get('account')=='Equity:Retained-Earnings' or r.get('key')=='retained_earnings_prior'),Decimal(0))
        self.assertEqual(retained.quantize(cents),(-self.closing.get('10',Decimal(0))).quantize(cents))
        # The opening day: the engine balance sheet equals the opening trial balance.
        before = balance_sheet(self.entity.path,self.first-timedelta(days=1),view=view)
        opening_assets = sum((v for k,v in self.opening.items() if CHART[k][1].startswith('Assets')),Decimal(0))
        self.assertEqual(before.totals['total_assets'].quantize(cents),opening_assets.quantize(cents))
        self.assertEqual(before.totals['total_liabilities_and_equity'].quantize(cents),opening_assets.quantize(cents))
        entries = LedgerStore(default_store_path(self.entity.path)).load_entries()
        self.assertTrue(all(sum(p.amount for p in e.postings)==0 for e in entries))
        return plan

    BASE = {'1':'1500','7':'-1000','10':'-200','4':'-400','5':'100'}

    def test_calendar_year_starts_on_january_first_with_prior_profit(self):
        self.build(first='2026-01-01',last='2026-02-28',opening=self.BASE,
                   events=[('2026-01-05',{'1':'250.50','4':'-250.50'}),('2026-02-10',{'5':'80.25','1':'-80.25'})])
        plan = self.check()
        retained = next(c for c in plan['controls'] if c['account_id']=='10')
        self.assertEqual(retained['closed_into_retained_earnings'],'-300')

    def test_july_fiscal_year_starts_on_july_first(self):
        self.build(first='2026-07-01',last='2026-09-30',fy='07-01',opening=self.BASE,
                   events=[('2026-07-15',{'1':'999.99','4':'-999.99'}),('2026-09-01',{'5':'10.01','1':'-10.01'})])
        self.check()

    def test_july_fiscal_year_with_a_january_start_is_mid_year_and_unchanged(self):
        self.build(first='2026-01-01',last='2026-03-31',fy='07-01',opening=self.BASE,
                   events=[('2026-01-05',{'1':'40','4':'-40'}),('2026-03-02',{'5':'15','1':'-15'})])
        plan = self.check()
        self.assertEqual({c['closed_into_retained_earnings'] for c in plan['controls']},{'0'})

    def test_period_that_starts_mid_year_is_unchanged(self):
        self.build(first='2026-04-01',last='2026-06-30',opening=self.BASE,
                   events=[('2026-04-20',{'1':'70','4':'-70'}),('2026-06-01',{'5':'20','1':'-20'})])
        plan = self.check()
        self.assertEqual({c['closed_into_retained_earnings'] for c in plan['controls']},{'0'})

    def test_period_that_spans_two_year_ends(self):
        self.build(first='2024-07-01',last='2026-02-28',opening=self.BASE,
                   events=[('2024-09-01',{'1':'500','4':'-500'}),('2024-11-01',{'5':'120','1':'-120'}),
                           ('2025-03-03',{'1':'800.40','4':'-800.40'}),('2025-12-31',{'5':'33.33','1':'-33.33'}),
                           ('2026-01-01',{'1':'60','4':'-60'}),('2026-02-14',{'5':'9.99','1':'-9.99'})])
        self.check()

    def test_zero_prior_income(self):
        self.build(first='2026-01-01',last='2026-02-28',opening={'1':'1000','7':'-1000'},
                   events=[('2026-01-09',{'1':'12.34','4':'-12.34'})])
        plan = self.check()
        self.assertEqual({c['closed_into_retained_earnings'] for c in plan['controls']},{'0'})

    def test_prior_year_net_loss(self):
        self.build(first='2026-01-01',last='2026-02-28',opening={'1':'700','4':'-100','5':'400','7':'-1000'},
                   events=[('2026-02-02',{'1':'45','4':'-45'})])
        plan = self.check()
        retained = next(c for c in plan['controls'] if c['account_id']=='10')
        self.assertEqual(retained['closed_into_retained_earnings'],'300')

    def test_missing_retained_earnings_account_blocks_when_a_year_is_closed(self):
        self.build(first='2026-01-01',last='2026-02-28',opening=self.BASE,events=[('2026-01-05',{'1':'1','4':'-1'})])
        path = Path(self.path).parent/'0.json';raw = json.loads(path.read_text())
        for account in raw['QueryResponse']['Account']:
            account.pop('AccountSubType',None);account['Name'] = 'Synthetic equity'
        path.write_text(json.dumps(raw))
        manifest = json.loads(self.path.read_text());manifest['files'][0]['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.path.write_text(json.dumps(manifest))
        plan = sync_transactions(self.entity,self.path)
        self.assertFalse(plan['ready'])
        self.assertIn('retained_earnings_account_unclear',[b['code'] for b in plan['blockers']])


class AccrualFiscalTests(FiscalCases, unittest.TestCase):
    basis = 'Accrual'


class CashFiscalTests(FiscalCases, unittest.TestCase):
    basis = 'Cash'


if __name__ == '__main__':
    unittest.main()
