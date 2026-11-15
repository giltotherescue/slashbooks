"""Synthetic raw QBO report acceptance; no live company data."""
import copy
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from bookkeeping.entity import init_entity, load_entity
from bookkeeping.quickbooks_history import sync_transactions, owned
from bookkeeping.ledger.store import LedgerStore, default_store_path
from bookkeeping.ledger.importer import _atomic_ledger_write, import_transactions
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.statement_import import plan_duplicates


def column(key, title=None):
    return {'ColType':'Money' if key in {'debit','credit'} else 'String', 'ColTitle':title or key,
            'MetaData':[{'Name':'ColKey','Value':key}]}


def entity_record(label, identity, day, legs):
    total=sum(v for v in legs.values() if v>0)
    result={'Id':identity,'SyncToken':'0','TxnDate':day,'TotalAmt':total,'CurrencyRef':{'value':'USD'},
            'MetaData':{'CreateTime':day+'T09:00:00Z','LastUpdatedTime':day+'T09:00:00Z'}}
    expense={'Id':'1','Amount':total,'DetailType':'AccountBasedExpenseLineDetail',
             'AccountBasedExpenseLineDetail':{'AccountRef':{'value':'5','name':'Supplies'},'ClassRef':{'value':'class-1','name':'North'}}}
    sale={'Id':'1','Amount':100 if label=='Invoice' else total,'DetailType':'SalesItemLineDetail',
          'SalesItemLineDetail':{'ItemRef':{'value':'item-1','name':'Synthetic consulting'},'TaxCodeRef':{'value':'TAX'}}}
    if label in {'Invoice','SalesReceipt','RefundReceipt','CreditMemo'}:
        result.update(CustomerRef={'value':'customer-1','name':'Synthetic customer'},Line=[sale])
        if label in {'Invoice','CreditMemo'}:result['ARAccountRef']={'value':'2'}
        else:result['DepositToAccountRef']={'value':'1'}
        result['TxnTaxDetail']={'TotalTax':10 if label=='Invoice' else 2 if label=='SalesReceipt' else 1 if label=='RefundReceipt' else 0}
    elif label in {'Bill','VendorCredit','Purchase'}:
        result.update(VendorRef={'value':'vendor-1','name':'Synthetic supplier'},Line=[expense])
        if label=='Purchase':result.update(PaymentType='CreditCard',Credit=False,AccountRef={'value':'8'})
        else:result['APAccountRef']={'value':'3'}
    elif label=='Payment':
        result.update(CustomerRef={'value':'customer-1'},DepositToAccountRef={'value':'1'},
                      Line=[{'Amount':total,'LinkedTxn':[{'TxnId':'101','TxnType':'Invoice'}]}])
    elif label=='BillPayment':
        result.update(VendorRef={'value':'vendor-1'},PayType='Check',CheckPayment={'BankAccountRef':{'value':'1'}},
                      Line=[{'Amount':total,'LinkedTxn':[{'TxnId':'103','TxnType':'Bill'}]}])
    elif label=='Transfer':result.update(FromAccountRef={'value':'1'},ToAccountRef={'value':'9'},Amount=total)
    elif label=='CreditCardPayment':result.update(BankAccountRef={'value':'1'},CCAccountRef={'value':'8'},Amount=total)
    elif label=='Deposit':result.update(DepositToAccountRef={'value':'1'},Line=[{'Id':'1','Amount':total,'DetailType':'DepositLineDetail','DepositLineDetail':{'AccountRef':{'value':'4'}}}])
    elif label=='JournalEntry':
        result['Line']=[{'Id':str(i),'Amount':abs(v),'DetailType':'JournalEntryLineDetail',
                         'JournalEntryLineDetail':{'PostingType':'Debit' if v>0 else 'Credit','AccountRef':{'value':k}}}
                        for i,(k,v) in enumerate(legs.items(),1)]
    return result


def report(name, start, end, rows, columns, basis='Accrual'):
    return {'Header':{'ReportName':name,'StartPeriod':start,'EndPeriod':end,'ReportBasis':basis,'Currency':'USD'},
            'Columns':{'Column':columns},'Rows':{'Row':rows}}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.basis = getattr(self, 'basis', 'Accrual')
        init_entity(self.root/'books',name='Synthetic history acceptance',basis=self.basis.lower())
        self.entity = load_entity(self.root/'books')
        self.source = self.root/'source';self.source.mkdir()
        self.manifest = {'format':'slashbooks-qbo-history-v1','environment':'sandbox','realm_id':'123',
            'history_from':'2026-01-01','history_to':'2026-02-28','accounting_basis':self.basis,'currency':'USD',
            'account_map':{'1':'Assets:Bank:Checking','2':'Assets:Receivable:Trade','3':'Liabilities:Payable:Trade',
                           '4':'Income:Sales','5':'Expenses:Supplies','6':'Liabilities:Tax','7':'Equity:Capital',
                           '8':'Liabilities:CreditCard:Card','9':'Assets:Bank:Savings'},
            'cutovers':{'Assets:Bank:Checking':'2026-02-28','Assets:Bank:Savings':'2026-02-28',
                        'Liabilities:CreditCard:Card':'2026-02-28'},'files':[]}
        types = ['Bank','Accounts Receivable','Accounts Payable','Income','Expense','Other Current Liability','Equity','Credit Card','Bank']
        self.add('accounts',{'QueryResponse':{'Account':[{'Id':str(i+1),'Name':'Synthetic '+str(i),'AccountType':t} for i,t in enumerate(types)]}})
        # Raw accounting legs, not document conversion: AR/AP and tax are explicit.
        self.transactions = [
            ('Invoice','101','2026-01-02',{'2':110,'4':-100,'6':-10}),
            ('Payment','102','2026-01-03',{'1':110,'2':-110}),
            ('Bill','103','2026-01-04',{'5':40,'3':-40}),
            ('BillPayment','104','2026-01-05',{'3':40,'1':-40}),
            ('Transfer','105','2026-01-06',{'1':-20,'9':20}),
            ('Purchase','106','2026-01-07',{'5':10,'8':-10}),
            ('CreditCardPayment','107','2026-02-01',{'8':10,'1':-10}),
            ('SalesReceipt','108','2026-02-02',{'1':22,'4':-20,'6':-2}),
            ('RefundReceipt','109','2026-02-03',{'1':-11,'4':10,'6':1}),
            ('CreditMemo','110','2026-02-04',{'2':-5,'4':5}),
            ('VendorCredit','111','2026-02-05',{'3':-3,'5':3}),
            ('Deposit','112','2026-02-06',{'1':30,'4':-30}),
            ('JournalEntry','113','2026-02-07',{'5':2,'7':-2})]
        if self.basis == 'Cash':
            self.transactions = [t for t in self.transactions if t[0] not in {'Invoice','Bill','CreditMemo','VendorCredit'}]
            self.transactions[0] = ('Payment','102','2026-01-03',{'1':110,'4':-100,'6':-10})
            self.transactions[1] = ('BillPayment','104','2026-01-05',{'5':40,'1':-40})
            self.add('entities',{'QueryResponse':{'Invoice':[{'Id':'unpaid','SyncToken':'0','TxnDate':'2026-01-10','TotalAmt':500}]}},entity_type='Invoice')
        for label,identity,day,legs in self.transactions:
            self.add('entities',{'QueryResponse':{label:[entity_record(label,identity,day,legs)]}},entity_type=label)
        movement = {k:Decimal(0) for k in self.manifest['account_map']}
        for label,identity,day,legs in self.transactions:
            for k,v in legs.items(): movement[k]+=Decimal(v)
        opening = {'1':Decimal(1000),'7':Decimal(-1000)}
        closing = {k:opening.get(k,0)+v for k,v in movement.items()}
        for account in self.manifest['account_map']:
            for lo,hi in [('2026-01-01','2026-01-31'),('2026-02-01','2026-02-28')]:
                rows=[]
                for label,identity,day,legs in self.transactions:
                    if account not in legs or not lo<=day<=hi: continue
                    amount=legs[account]
                    cells=[{'value':day},{'value':label,'id':identity},
                           {'value':str(max(amount,0))},{'value':str(max(-amount,0))},
                           {'value':'North','id':'class-1'}]
                    rows.append({'type':'Data','ColData':cells})
                raw=report('GeneralLedger',lo,hi,[{'Header':{'ColData':[{'value':'Synthetic','id':account}]},'Rows':{'Row':rows}}],
                           [column(k) for k in ['tx_date','txn_type','debit','credit','class']])
                self.add('general_ledger',raw,account_id=account,**{'from':lo,'to':hi})
        for kind,end,values in [('opening_trial_balance','2025-12-31',opening),('closing_trial_balance','2026-02-28',closing)]:
            rows=[{'ColData':[{'value':'Synthetic','id':k},{'value':str(max(v,0))},{'value':str(max(-v,0))}]} for k,v in values.items() if v]
            self.add(kind,report('TrialBalance',end,end,rows,[column('account'),column('debit'),column('credit')]))
        for kind,name in [('profit_and_loss','ProfitAndLoss'),('balance_sheet','BalanceSheet')]:
            rows=[]
            for k,account in self.manifest['account_map'].items():
                root=account.split(':')[0]
                if (kind=='profit_and_loss') != (root in {'Income','Expenses'}):continue
                v=movement[k] if kind=='profit_and_loss' else closing[k]
                if root in {'Income','Liabilities','Equity'}:v=-v
                if v:rows.append({'ColData':[{'value':'Synthetic','id':k},{'value':str(v)}]})
            if kind=='balance_sheet':
                rows.append({'ColData':[{'value':'Net Income'},{'value':'88' if self.basis=='Cash' else '80'}]})
            self.add(kind,report(name,'2026-01-01','2026-02-28',rows,[column('account'),{'ColType':'Money','ColTitle':'Total'}]))
        self.write()

    def add(self,kind,raw,**extra):
        if 'Header' in raw:
            raw['Header']['ReportBasis'] = self.basis
        path=self.source/(str(len(self.manifest['files']))+'.json')
        path.write_text(json.dumps(raw))
        self.manifest['files'].append({'kind':kind,'path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),**extra})

    def write(self):
        self.path=self.source/'manifest.json';self.path.write_text(json.dumps(self.manifest))

    def mutate_file(self,kind,mutate):
        item=next(i for i in self.manifest['files'] if i['kind']==kind)
        path=self.source/item['path'];raw=json.loads(path.read_text());mutate(raw);path.write_text(json.dumps(raw))
        item['sha256']=hashlib.sha256(path.read_bytes()).hexdigest();self.write()

    def apply(self):
        plan=sync_transactions(self.entity,self.path)
        self.assertTrue(plan['ready'],plan)
        result=sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])
        self.assertTrue(result['applied']);return result

    def test_real_posting_all_entities_controls_dimensions_idempotence(self):
        result=self.apply();self.assertEqual(result['recorded_entries'],14)
        store=LedgerStore(default_store_path(self.entity.path))
        entries=store.load_entries()
        self.assertEqual(entries[0].date,date(2025,12,31))
        self.assertTrue(all(sum(p.amount for p in e.postings)==0 for e in entries))
        self.assertTrue(any('class-1' in dict(p.meta).get('qbo-row','') for e in entries for p in e.postings))
        self.assertTrue(all(c['balanced'] and c['statement_agrees'] for c in result['controls']))
        from bookkeeping.reports.statements import profit_and_loss,balance_sheet
        pnl=profit_and_loss(self.entity.path,date(2026,1,1),date(2026,2,28),basis='accrual')
        bs=balance_sheet(self.entity.path,date(2026,2,28),view='accrual')
        self.assertEqual(pnl.totals['net_income'],Decimal(80))
        self.assertEqual(bs.totals['total_assets'],Decimal(1096))
        self.assertEqual(bs.totals['total_liabilities_and_equity'],Decimal(1096))
        before=store.content_digest();again=sync_transactions(self.entity,self.path,apply=True,plan_hash='old')
        self.assertTrue(again['already_applied']);self.assertEqual(before,store.content_digest())

    def test_statement_and_direct_write_blocked_after_history(self):
        self.apply()
        for day in ['2026-01-03','2026-03-03']:
            txn={'id':'stmt:'+day,'date':day,'description':'Changed bank description','amount':'110'}
            plan=plan_duplicates(self.entity,[txn],'Assets:Bank:Checking','statement')
            self.assertEqual(len(plan['held']),1)
        entry=Entry(date(2026,1,3),'Duplicate',meta=(('source-id','plaid:new'),),
                    postings=(Posting('Assets:Bank:Checking',Decimal(110)),Posting('Income:Sales',Decimal(-110))))
        with self.assertRaisesRegex(ValueError,'owns'):
            _atomic_ledger_write(self.entity,[],[entry],'duplicate',None,'duplicate')

    def test_existing_bank_first_refuses_history(self):
        from bookkeeping.ledger.model import Open
        entry=Entry(date(2026,1,3),'Existing statement',meta=(('source-id','stmt:old'),),
                    postings=(Posting('Assets:Bank:Checking',Decimal(110)),Posting('Income:Sales',Decimal(-110))))
        _atomic_ledger_write(self.entity,[Open(date(2026,1,1),'Assets:Bank:Checking'),Open(date(2026,1,1),'Income:Sales')],[entry],'old',None,'existing')
        self.assertIn('existing_accounting_requires_review',[b['code'] for b in sync_transactions(self.entity,self.path)['blockers']])

    def test_control_mismatch_and_changed_source(self):
        self.mutate_file('profit_and_loss',lambda r:r['Rows']['Row'][0]['ColData'][1].update(value='999'))
        self.assertFalse(sync_transactions(self.entity,self.path)['ready'])

    def test_changed_payload_after_recording_conflicts(self):
        # A first-import void with no GL effect remains source evidence only.
        self.add('entities',{'QueryResponse':{'Purchase':[{'Id':'void-no-effect','SyncToken':'1',
                 'TxnDate':'2026-01-08','TotalAmt':0,'status':'Voided','sparse':True,'Line':[]}]}},entity_type='Purchase')
        self.write()
        initial=sync_transactions(self.entity,self.path)
        self.assertIn({'type':'Purchase','id':'void-no-effect','reason':'no_source_report_effect'},
                      initial['counts']['not_moved'])
        self.apply()
        store=LedgerStore(default_store_path(self.entity.path));before=store.content_digest()
        self.assertFalse(any((e.source_id or '').endswith(':void-no-effect') for e in store.load_entries()))
        item=next(i for i in self.manifest['files'] if i['kind']=='entities' and i.get('entity_type')=='Purchase')
        path=self.source/item['path']
        for status in ('Deleted','Voided'):
            with self.subTest(status=status):
                raw=json.loads(path.read_text())
                raw['QueryResponse']['Purchase'][0].update(SyncToken='1',TotalAmt=0,status=status,sparse=True,Line=[])
                path.write_text(json.dumps(raw));item['sha256']=hashlib.sha256(path.read_bytes()).hexdigest();self.write()
                plan=sync_transactions(self.entity,self.path)
                self.assertFalse(plan['ready'])
                self.assertIn('existing_history_requires_review',[b['code'] for b in plan['blockers']])
                result=sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])
                self.assertFalse(result['applied']);self.assertFalse(result['success'])
                self.assertEqual(store.content_digest(),before)

    def test_missing_identity_columns_coverage_and_forged_hash_refused(self):
        self.mutate_file('general_ledger',lambda r:r['Rows']['Row'][0]['Rows']['Row'][0]['ColData'][1].pop('id'))
        with self.assertRaisesRegex(ValueError,'identity'):
            sync_transactions(self.entity,self.path)

    def test_missing_partition_refused(self):
        self.manifest['files']=[i for i in self.manifest['files'] if not(i['kind']=='general_ledger' and i['account_id']=='9')]
        self.write()
        with self.assertRaisesRegex(ValueError,'incomplete'):
            sync_transactions(self.entity,self.path)

    def test_fresh_collection_metadata_is_idempotent(self):
        self.apply()
        self.manifest['job_id']='a-new-job';self.manifest['fetched_at']='2026-11-15T00:00:00Z';self.write()
        self.assertTrue(sync_transactions(self.entity,self.path)['already_applied'])

    def test_feed_holds_every_many_to_one_candidate(self):
        self.apply()
        self.entity.entity_config['bank_account_mappings']={'feed-1':'Assets:Bank:Checking'}
        rows=[{'id':'plaid:'+str(i),'date':'2026-03-02','description':'Different settlement wording',
               'amount':'110','accountId':'feed-1','accountName':'Checking','pending':False} for i in range(2)]
        result=import_transactions(self.entity,rows,'late-feed',categorizer=lambda t:('Income:Sales','reviewed'))
        self.assertEqual(result.new_entries,0)
        self.assertEqual(result.duplicate_candidates,2)
        held=json.loads((self.entity.staging_dir/'duplicate-candidates.json').read_text())
        self.assertEqual(len(held),2)

    def test_nearby_staged_approval_cannot_bypass_ownership(self):
        self.apply()
        entry=Entry(date(2026,3,2),'Previously staged settlement',meta=(('source-id','plaid:staged'),),
                    postings=(Posting('Assets:Bank:Checking',Decimal(110)),Posting('Income:Sales',Decimal(-110))))
        with self.assertRaisesRegex(ValueError,'owns'):
            _atomic_ledger_write(self.entity,[],[entry],'staged',None,'approve staged row')

    def test_raw_trial_money_columns_and_display_label(self):
        for item in self.manifest['files']:
            path=self.source/item['path'];raw=json.loads(path.read_text())
            if item['kind'] in {'opening_trial_balance','closing_trial_balance'}:
                raw['Columns']['Column']=[{'ColType':'Account'},{'ColType':'Money','ColTitle':'Debit'},{'ColType':'Money','ColTitle':'Credit'}]
            if item['kind']=='general_ledger':
                for section in raw['Rows']['Row']:
                    for row in section['Rows']['Row']:
                        if row['ColData'][1]['value']=='SalesReceipt':row['ColData'][1]['value']='Sales Receipt'
            path.write_text(json.dumps(raw));item['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        self.write();self.apply()

    def test_wrong_chart_root_and_file_hash_refused(self):
        self.manifest['account_map']['1']='Expenses:Wrong';self.write()
        with self.assertRaisesRegex(ValueError,'financial root'):
            sync_transactions(self.entity,self.path)
        self.manifest['account_map']['1']='Assets:Bank:Checking';self.manifest['files'][0]['sha256']='0'*64;self.write()
        with self.assertRaisesRegex(ValueError,'hash'):
            sync_transactions(self.entity,self.path)

    def test_partial_write_failure_rolls_back_and_rerun_succeeds(self):
        from unittest.mock import patch
        from bookkeeping.quickbooks_history import receipts
        store=LedgerStore(default_store_path(self.entity.path));before=store.content_digest()
        plan=sync_transactions(self.entity,self.path)
        original=LedgerStore.insert_entries
        def insert_then_interrupt(store,*args,**kwargs):
            original(store,*args,**kwargs)
            raise RuntimeError('synthetic interruption after insertion')
        with patch.object(LedgerStore,'insert_entries',insert_then_interrupt):
            with self.assertRaisesRegex(RuntimeError,'interruption'):
                sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])
        self.assertEqual(store.content_digest(),before)
        self.assertEqual(receipts(self.entity),[])
        self.apply()
        after=store.content_digest()
        self.assertTrue(sync_transactions(self.entity,self.path,apply=True,plan_hash='retry')['already_applied'])
        self.assertEqual(store.content_digest(),after)
        self.assertEqual(len(receipts(self.entity)),1)

    def test_pending_first_refuses_history(self):
        self.entity.entity_config['bank_account_mappings']={'feed-1':'Assets:Bank:Checking'}
        (self.entity.path/'staging'/'pending-categorization.json').write_text(json.dumps([
            {'id':'plaid:waiting','date':'2026-02-01','description':'Pending review','amount':'10',
             'accountId':'feed-1','accountName':'Checking','pending':False}]))
        result=sync_transactions(self.entity,self.path)
        self.assertIn('pending_accounting_requires_review',[b['code'] for b in result['blockers']])

    def test_normal_cutover_opening_refused(self):
        from bookkeeping.quickbooks import check_opening_overlap
        self.apply()
        state,error=check_opening_overlap(self.entity,date(2026,2,28),[
            {'account':'Assets:Bank:Checking','amount':'1081','currency':'USD'}])
        self.assertEqual(state,'conflict');self.assertIn('twice',error)

    def test_engine_resolves_empty_chart_map_and_cutovers(self):
        self.manifest['account_map']={};self.manifest['cutovers']={};self.write()
        plan=sync_transactions(self.entity,self.path)
        self.assertTrue(plan['ready'],plan)
        self.assertEqual(len(plan['account_map']),9)
        self.assertEqual(len(plan['cutovers']),3)
        self.assertEqual(set(plan['cutovers'].values()),{'2026-02-28'})
        self.assertTrue(sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])['applied'])

    def test_committed_raw_provider_fixture(self):
        fixture=Path(__file__).parent/'fixtures'/'quickbooks_history'/(self.basis.lower()+'.json')
        payload=json.loads(fixture.read_text());self.manifest=payload['manifest']
        for name,raw in payload['files'].items():
            (self.source/name).write_text(json.dumps(raw,sort_keys=True,separators=(',',':')))
        self.write();self.apply()

    def test_committed_fixture_trial_balance_profit_loss_balance_sheet_tie_out(self):
        # Frozen provider-shaped reports, independent of the test's generated legs.
        fixture=Path(__file__).parent/'fixtures'/'quickbooks_history'/(self.basis.lower()+'.json')
        payload=json.loads(fixture.read_text());self.manifest=payload['manifest']
        for name,raw in payload['files'].items():
            (self.source/name).write_text(json.dumps(raw,sort_keys=True,separators=(',',':')))
        self.write();result=self.apply()
        from bookkeeping.reports.statements import trial_balance,profit_and_loss,balance_sheet
        tb=trial_balance(self.entity.path,date(2026,2,28),view=self.basis.lower())
        pnl=profit_and_loss(self.entity.path,date(2026,1,1),date(2026,2,28),basis=self.basis.lower())
        bs=balance_sheet(self.entity.path,date(2026,2,28),view=self.basis.lower())
        expected_tb=Decimal('1153' if self.basis=='Cash' else '1156')
        expected_profit=Decimal('88' if self.basis=='Cash' else '80')
        expected_assets=Decimal('1101' if self.basis=='Cash' else '1096')
        self.assertEqual(tb.totals,{'total_debits':expected_tb,'total_credits':expected_tb,'net':Decimal(0)})
        self.assertEqual(pnl.totals['net_income'],expected_profit)
        self.assertEqual(bs.totals['total_assets'],expected_assets)
        self.assertEqual(bs.totals['total_liabilities_and_equity'],expected_assets)
        self.assertTrue(all(c['balanced'] and c['statement_agrees'] for c in result['controls']))

    def test_three_day_feed_and_statement_hold_boundaries(self):
        self.apply()
        self.entity.entity_config['bank_account_mappings']={'feed-1':'Assets:Bank:Checking'}
        store=LedgerStore(default_store_path(self.entity.path));before=store.content_digest()
        for day in ['2026-02-28','2026-03-01','2026-03-02','2026-03-03']:
            with self.subTest(day=day):
                row={'id':'plaid:boundary:'+day,'date':day,'description':'Distinct-looking settlement',
                     'amount':'7.13','accountId':'feed-1','accountName':'Checking','pending':False}
                result=import_transactions(self.entity,[row],'boundary',categorizer=lambda _:('Income:Sales','high'))
                self.assertEqual(result.new_entries,0);self.assertEqual(result.duplicate_candidates,1)
                statement=self.upload_statement(day=day,amount='7.13')
                self.assertEqual(statement['statements'][0]['counts']['held_for_review'],1)
                self.assertEqual(store.content_digest(),before)
        # T+4 is outside this ownership buffer (ordinary review rules still apply).
        self.assertIsNone(owned(self.entity,'Assets:Bank:Checking','2026-03-04',nearby=True))
        row={'id':'outside-buffer','date':'2026-03-04','description':'Later settlement','amount':'7.13'}
        self.assertEqual(len(plan_duplicates(self.entity,[row],'Assets:Bank:Checking','statement')['import']),1)

    def test_transfer_and_split_hold_each_owned_bank_leg(self):
        self.apply()
        store=LedgerStore(default_store_path(self.entity.path));before=store.content_digest()
        for account,amount in [('Assets:Bank:Checking','-20'),('Assets:Bank:Savings','20')]:
            with self.subTest(account=account):
                row={'id':'transfer:'+account,'date':'2026-01-06','description':'Different transfer text','amount':amount}
                held=plan_duplicates(self.entity,[row],account,'statement')['held']
                self.assertEqual(len(held),1)
                self.assertEqual(held[0]['account'],account)
        # Multiple legs cannot evade the final writer, even if net bank effect is zero.
        split=Entry(date(2026,3,3),'Split transfer',meta=(('source-id','split:new'),),postings=(
            Posting('Assets:Bank:Checking',Decimal('-10')),
            Posting('Assets:Bank:Checking',Decimal('-10')),
            Posting('Assets:Bank:Savings',Decimal('20'))))
        with self.assertRaisesRegex(ValueError,'owns'):
            _atomic_ledger_write(self.entity,[],[split],'split',None,'owner-reviewed split')
        self.assertEqual(store.content_digest(),before)

    def test_owned_hold_stays_visible_until_duplicate_resolution(self):
        from bookkeeping.queue import list_duplicate_candidates,resolve_duplicate_candidate
        self.apply()
        self.entity.entity_config['bank_account_mappings']={'feed-1':'Assets:Bank:Checking'}
        row={'id':'plaid:owner-review','date':'2026-03-03','description':'Synthetic settlement',
             'amount':'7.13','accountId':'feed-1','accountName':'Checking','pending':False}
        import_transactions(self.entity,[row],'hold',categorizer=lambda _:('Income:Sales','high'))
        store=LedgerStore(default_store_path(self.entity.path));before=store.content_digest()
        candidates=list_duplicate_candidates(self.entity)
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]['source_id'],row['id'])
        self.assertEqual(candidates[0]['held']['date'],'2026-03-03')
        with self.assertRaisesRegex(ValueError,'migration correction'):
            resolve_duplicate_candidate(self.entity,row['id'],'distinct')
        self.assertEqual(len(list_duplicate_candidates(self.entity)),1)
        self.assertEqual(store.content_digest(),before)
        with self.assertRaisesRegex(ValueError,'migration correction'):
            resolve_duplicate_candidate(self.entity,row['id'],'duplicate')
        self.assertEqual(len(list_duplicate_candidates(self.entity)),1)
        self.assertEqual(store.content_digest(),before)

    def test_quickbooks_edit_between_runs_preserves_posted_ledger(self):
        self.apply();store=LedgerStore(default_store_path(self.entity.path));before=store.content_digest()
        self.mutate_file('entities',lambda r:next(iter(r['QueryResponse'].values()))[0].update(SyncToken='2',PrivateNote='Edited synthetic memo'))
        plan=sync_transactions(self.entity,self.path)
        self.assertIn('existing_history_requires_review',[b['code'] for b in plan['blockers']])
        self.assertFalse(sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])['applied'])
        self.assertEqual(store.content_digest(),before)

    def test_reference_entities_are_versioned_nonposting_evidence(self):
        references={'Customer':[{'Id':'customer-1','SyncToken':'1','DisplayName':'Synthetic customer'}],
                    'Vendor':[{'Id':'vendor-1','SyncToken':'0','DisplayName':'Synthetic supplier'}],
                    'Item':[{'Id':'item-1','SyncToken':'0','Name':'Synthetic consulting','Type':'Service'}],
                    'Class':[{'Id':'class-1','SyncToken':'0','Name':'North'}], 'Department':[]}
        for kind,rows in references.items():
            self.add('entities',{'QueryResponse':{kind:rows}},entity_type=kind)
        self.write()
        plan=sync_transactions(self.entity,self.path)
        self.assertTrue(plan['ready'],plan)
        self.assertEqual(plan['counts']['source_by_type']['Account'],9)
        self.assertEqual(plan['counts']['reference_by_type'],{'Customer':1,'Vendor':1,'Item':1,'Class':1,'Department':0})
        self.assertEqual(len([r for r in plan['counts']['not_moved'] if r['reason']=='reference_evidence_only']),4)
        result=sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])
        self.assertEqual(result['recorded_entries'],10 if self.basis=='Cash' else 14)
        from bookkeeping.quickbooks_history import receipts
        self.assertEqual(len(receipts(self.entity)[0]['reference_versions']),4)

    def test_expected_source_counts_matching_records(self):
        plan=sync_transactions(self.entity,self.path)
        self.manifest['expected_source_counts']=plan['counts']['source_by_type']
        self.write()
        result=self.apply()
        self.assertEqual(result['counts']['source_by_type'],self.manifest['expected_source_counts'])

    def test_changed_expected_source_counts_block_before_recording(self):
        before=LedgerStore(default_store_path(self.entity.path)).content_digest()
        self.manifest['expected_source_counts']={'Account':9,'Purchase':2,'Customer':1}
        self.write()
        plan=sync_transactions(self.entity,self.path)
        self.assertFalse(plan['ready'])
        differences={b['type']:(b['expected'],b['actual']) for b in plan['blockers'] if b['code']=='source_count_changed'}
        self.assertEqual(differences,{'Customer':(1,0),'Purchase':(2,1)})
        result=sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])
        self.assertFalse(result['success']);self.assertFalse(result['applied'])
        self.assertEqual(LedgerStore(default_store_path(self.entity.path)).content_digest(),before)

    def test_expected_counts_bind_review_and_account_count(self):
        plan=sync_transactions(self.entity,self.path)
        self.manifest['expected_source_counts']={'Account':9};self.write()
        with self.assertRaisesRegex(ValueError,'Stale'):
            sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])
        self.manifest['expected_source_counts']={'Account':10};self.write()
        plan=sync_transactions(self.entity,self.path)
        self.assertIn({'code':'source_count_changed','type':'Account','expected':10,'actual':9},plan['blockers'])

    def upload_statement(self, account='Assets:Bank:Checking', day='2026-01-03', amount='110.00'):
        from bookkeeping import statement_import as si
        path=self.root/'synthetic-bank-statement.csv'
        path.write_text('Date,Description,Amount\n'+day+',Synthetic bank settlement,'+amount+'\n')
        si.save_profile(self.entity.path,path,name='synthetic-upload',ledger_account=account,
                        overrides={'date_format':'%Y-%m-%d','sign':'standard'})
        result=si.import_file(self.entity.path,path,profile='synthetic-upload',ts='2026-03-10T00:00:00Z')
        self.entity=load_entity(self.entity.path)
        return result

    def confirm_intake_row(self, identity):
        from bookkeeping import queue
        category=next(o.account for o in LedgerStore(default_store_path(self.entity.path)).load_opens()
                      if o.account.startswith('Income:'))
        queue.propose(self.entity,identity,category,'Owner reviewed synthetic statement or feed settlement')
        queue.confirm(self.entity,identity,'owner-review',accrual_treatment='new_cash_event' if self.basis=='Accrual' else None)

    def test_real_bank_feed_intake_first_posts_and_history_refuses(self):
        self.entity.entity_config['bank_account_mappings']={'feed-1':'Assets:Bank:Checking'}
        (self.entity.path/'entity.json').write_text(json.dumps(self.entity.entity_config))
        category=next(o.account for o in LedgerStore(default_store_path(self.entity.path)).load_opens()
                      if o.account.startswith('Income:'))
        row={'id':'plaid:posted-before-history','date':'2026-01-03','description':'Synthetic bank settlement',
             'amount':'110','accountId':'feed-1','accountName':'Checking','pending':False}
        result=import_transactions(self.entity,[row],'actual-feed-first',categorizer=lambda _: (category,'high'))
        if self.basis=='Accrual':
            self.assertEqual(result.pending_categorization,1)
            self.confirm_intake_row(row['id'])
        else:
            self.assertEqual(result.new_entries,1)
        store=LedgerStore(default_store_path(self.entity.path))
        self.assertTrue(any(e.source_id==row['id'] for e in store.load_entries()))
        before=store.content_digest();plan=sync_transactions(self.entity,self.path)
        self.assertIn('existing_accounting_requires_review',[b['code'] for b in plan['blockers']])
        self.assertFalse(sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])['applied'])
        self.assertEqual(store.content_digest(),before)

    def test_real_statement_upload_first_posts_and_history_refuses(self):
        result=self.upload_statement()
        self.assertEqual(result['status'],'imported',result)
        self.assertEqual(result['statements'][0]['counts']['queued_for_review'],1)
        staged=json.loads((self.entity.staging_dir/'pending-categorization.json').read_text())
        self.assertEqual(len(staged),1)
        identity=staged[0]['id'];self.confirm_intake_row(identity)
        store=LedgerStore(default_store_path(self.entity.path))
        self.assertTrue(any(e.source_id==identity for e in store.load_entries()))
        before=store.content_digest();plan=sync_transactions(self.entity,self.path)
        self.assertIn('existing_accounting_requires_review',[b['code'] for b in plan['blockers']])
        self.assertFalse(sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])['applied'])
        self.assertEqual(store.content_digest(),before)

    def test_real_statement_upload_after_history_is_retained_without_posting(self):
        self.apply()
        store=LedgerStore(default_store_path(self.entity.path));before=store.content_digest()
        result=self.upload_statement()
        self.assertEqual(result['status'],'imported',result)
        statement=result['statements'][0]
        self.assertEqual(statement['counts']['held_for_review'],1)
        self.assertEqual(statement['held'][0]['match_rule'],'quickbooks_history_ownership')
        self.assertEqual(store.content_digest(),before)
        self.assertTrue((self.entity.path/statement['statement']['evidence_ref']).exists())
        again=self.upload_statement()
        self.assertEqual(again['statements'][0]['counts']['already_held_for_review'],1)
        self.assertEqual(store.content_digest(),before)

    def test_zero_activity_bank_account_still_owns_actual_statement_upload(self):
        account='Assets:Bank:Zero-Activity'
        self.manifest['account_map']['10']=account
        self.manifest['cutovers'][account]='2026-02-28'
        self.mutate_file('accounts',lambda r:r['QueryResponse']['Account'].append(
            {'Id':'10','Name':'Synthetic zero bank','AccountType':'Bank'}))
        for lo,hi in [('2026-01-01','2026-01-31'),('2026-02-01','2026-02-28')]:
            raw=report('GeneralLedger',lo,hi,[{'Header':{'ColData':[{'value':'Synthetic zero bank','id':'10'}]},'Rows':{'Row':[]}}],
                       [column(k) for k in ['tx_date','txn_type','debit','credit','class']])
            self.add('general_ledger',raw,account_id='10',**{'from':lo,'to':hi})
        self.write();self.apply()
        store=LedgerStore(default_store_path(self.entity.path))
        self.assertFalse(any(p.account==account for e in store.load_entries() for p in e.postings))
        self.assertIsNotNone(owned(self.entity,account,'2026-02-01'))
        before=store.content_digest();result=self.upload_statement(account,'2026-02-01','7.00')
        self.assertEqual(result['statements'][0]['counts']['held_for_review'],1)
        self.assertEqual(store.content_digest(),before)

    def test_actual_existing_opening_refuses_history(self):
        from bookkeeping.statement_opening import record_opening
        result=record_opening(self.entity.path,account='Assets:Bank:Checking',on='2026-01-01',amount='1000')
        self.assertEqual(result['status'],'recorded',result)
        store=LedgerStore(default_store_path(self.entity.path));before=store.content_digest()
        self.assertTrue(any(dict(e.meta).get('import-source')=='statement-opening' for e in store.load_entries()))
        plan=sync_transactions(self.entity,self.path)
        self.assertIn('existing_accounting_requires_review',[b['code'] for b in plan['blockers']])
        self.assertFalse(sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])['applied'])
        self.assertEqual(store.content_digest(),before)

    def test_report_float_and_nonfinite_values_refuse_without_posting(self):
        store=LedgerStore(default_store_path(self.entity.path));before=store.content_digest()
        self.mutate_file('general_ledger',lambda r:r['Rows']['Row'][0]['Rows']['Row'][0]['ColData'][2].update(value=0.10000000000000000001))
        with self.assertRaisesRegex(ValueError,'exact decimal'):
            sync_transactions(self.entity,self.path)
        self.assertEqual(store.content_digest(),before)
        self.mutate_file('general_ledger',lambda r:r['Rows']['Row'][0]['Rows']['Row'][0]['ColData'][2].update(value='NaN'))
        with self.assertRaisesRegex(ValueError,'precision'):
            sync_transactions(self.entity,self.path)
        self.assertEqual(store.content_digest(),before)

    def test_buffer_case7_explicit_release_normal_intake_exactly_once(self):
        from bookkeeping.quickbooks_history import resolve_buffer_decision, list_buffer_decisions
        self.apply()
        self.entity.entity_config['bank_account_mappings']={'feed-1':'Assets:Bank:Checking'}
        row={'id':'plaid:buffer7','date':'2026-02-10','amount':'7.13','description':'Synthetic new receipt',
             'accountId':'feed-1','accountName':'Checking','pending':False}
        import_transactions(self.entity,[row],'hold')
        self.assertEqual(len(list_buffer_decisions(self.entity)),1)
        resolve_buffer_decision(self.entity,row['id'],'release')
        staged=json.loads((self.entity.staging_dir/'pending-categorization.json').read_text())
        self.assertEqual(sum(t['id']==row['id'] for t in staged),1)
        self.confirm_intake_row(row['id'])
        resolve_buffer_decision(self.entity,row['id'],'release')
        import_transactions(self.entity,[row],'reimport')
        entries=LedgerStore(default_store_path(self.entity.path)).load_entries()
        self.assertEqual(sum(e.source_id==row['id'] for e in entries),1)
        forged=Entry(date(2026,2,10),'Synthetic altered released amount',meta=(('source-id',row['id']),),
                     postings=(Posting('Assets:Bank:Checking',Decimal('8'),'USD'),Posting('Income:Sales',Decimal('-8'),'USD')))
        from bookkeeping.quickbooks_history import refuse_owned_write
        with self.assertRaisesRegex(ValueError,'migration correction'):
            refuse_owned_write(self.entity,[forged])

    def test_buffer_case8_history_match_persists_selected_journal(self):
        from bookkeeping.quickbooks_history import resolve_buffer_decision, list_buffer_decisions, buffer_decision
        self.apply();self.upload_statement()
        row=list_buffer_decisions(self.entity)[0]
        self.assertTrue(row['history_matches'])
        self.upload_statement()
        self.assertEqual(list_buffer_decisions(self.entity)[0]['created_at'],row['created_at'])
        from bookkeeping.quickbooks_history import release_due_buffer
        self.assertEqual(release_due_buffer(self.entity,now='2026-04-01T00:00:00Z')['needs_review'],[row['source_id']])
        self.assertEqual(len(list_buffer_decisions(self.entity)),1)
        journal=row['history_matches'][0]['source_id']
        before=LedgerStore(default_store_path(self.entity.path)).content_digest()
        resolve_buffer_decision(self.entity,row['source_id'],'history-match',journal)
        self.assertEqual(buffer_decision(self.entity,row['source_id'])['history_source_id'],journal)
        self.upload_statement()
        self.assertEqual(list_buffer_decisions(self.entity),[])
        self.assertEqual(LedgerStore(default_store_path(self.entity.path)).content_digest(),before)

    def test_buffer_split_amounts_stay_held_with_actual_journal_amount(self):
        from bookkeeping.quickbooks_history import list_buffer_decisions,release_due_buffer,resolve_buffer_decision
        self.apply()
        self.entity.entity_config['bank_account_mappings']={'feed-1':'Assets:Bank:Checking'}
        rows=[{'id':'plaid:split-'+str(i),'date':'2026-01-03','amount':amount,
               'description':'Synthetic split settlement','accountId':'feed-1','accountName':'Checking','pending':False}
              for i,amount in enumerate(('55.00','220.00'))]
        import_transactions(self.entity,rows,'split-hold')
        decisions=list_buffer_decisions(self.entity)
        self.assertEqual(len(decisions),2)
        for row in decisions:
            self.assertTrue(row['history_matches'])
            self.assertIn(Decimal('110.00'),{Decimal(m['amount']) for m in row['history_matches']})
            self.assertNotIn(row['amount'],{m['amount'] for m in row['history_matches']})
        due=release_due_buffer(self.entity,now='2027-01-01T00:00:00Z')
        self.assertEqual(due['released'],[])
        self.assertEqual(set(due['needs_review']),{r['id'] for r in rows})
        resolve_buffer_decision(self.entity,rows[0]['id'],'release')
        self.assertEqual(len(list_buffer_decisions(self.entity)),1)

    def test_buffer_case9_elapsed_no_match_statement_release(self):
        from bookkeeping.quickbooks_history import list_buffer_decisions,release_due_buffer
        self.apply();self.upload_statement(day='2026-03-03',amount='7.13')
        row=list_buffer_decisions(self.entity)[0]
        self.assertEqual(row['history_matches'],[])
        self.assertEqual(row['held'],{k:row[k] for k in ('account','date','amount','description')})
        from bookkeeping.queue import _update_learned_context, _counterparty_key
        key=_counterparty_key(row['description'],'')
        for _ in range(self.entity.auto_post_threshold):
            _update_learned_context(self.entity,key,'Income:Sales',corrected=False)
        self.assertEqual(release_due_buffer(self.entity,now='2026-03-12T23:59:59Z')['released'],[])
        self.assertEqual(release_due_buffer(self.entity,now='2026-03-13T00:00:00Z')['released'],[row['source_id']])
        self.assertEqual(list_buffer_decisions(self.entity),[])
        if self.basis == 'Accrual':
            staged=json.loads((self.entity.staging_dir/'pending-categorization.json').read_text())
            self.assertEqual(sum(t['id']==row['source_id'] for t in staged),1)
            self.confirm_intake_row(row['source_id'])
        else:
            entries=LedgerStore(default_store_path(self.entity.path)).load_entries()
            self.assertEqual(sum(e.source_id==row['source_id'] for e in entries),1)
        self.upload_statement(day='2026-03-03',amount='7.13')
        self.assertEqual(release_due_buffer(self.entity,now='2026-03-14T00:00:00Z')['released'],[])
        entries=LedgerStore(default_store_path(self.entity.path)).load_entries()
        self.assertEqual(sum(e.source_id==row['source_id'] for e in entries),1)

    def test_manifest_folder_under_symbolic_link_works_but_linked_evidence_is_refused(self):
        link=self.root/'linked-source';link.symlink_to(self.source,target_is_directory=True)
        self.assertTrue(sync_transactions(self.entity,link/'manifest.json')['ready'])
        first=self.source/self.manifest['files'][0]['path'];real=self.source/'real-evidence.json'
        first.rename(real);first.symlink_to(real)
        with self.assertRaisesRegex(ValueError,'symbolic link'):
            sync_transactions(self.entity,self.path)

    def test_foreign_currency_records_and_accounts_are_held_never_converted(self):
        store=LedgerStore(default_store_path(self.entity.path));before=store.content_digest()
        self.mutate_file('entities',lambda r:next(iter(r['QueryResponse'].values()))[0].update(CurrencyRef={'value':'EUR'},ExchangeRate=1.08))
        plan=sync_transactions(self.entity,self.path)
        self.assertFalse(plan['ready'])
        self.assertEqual([b['code'] for b in plan['blockers']],['foreign_currency_unsupported'])
        self.mutate_file('accounts',lambda r:r['QueryResponse']['Account'][4].update(CurrencyRef={'value':'GBP'}))
        plan=sync_transactions(self.entity,self.path)
        self.assertIn('Account',{b['type'] for b in plan['blockers']})
        result=sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])
        self.assertFalse(result['applied']);self.assertEqual(store.content_digest(),before)

    def test_later_balance_and_reference_changes_do_not_break_an_exact_replay(self):
        self.add('entities',{'QueryResponse':{'Customer':[{'Id':'customer-1','SyncToken':'0','Balance':10}]}},entity_type='Customer')
        self.write();self.apply()
        store=LedgerStore(default_store_path(self.entity.path));before=store.content_digest()
        # QuickBooks activity after the history end moves running balances and customer balances.
        self.mutate_file('accounts',lambda r:[a.update(CurrentBalance=999) for a in r['QueryResponse']['Account']])
        item=next(i for i in self.manifest['files'] if i.get('entity_type')=='Customer')
        path=self.source/item['path'];raw=json.loads(path.read_text());raw['QueryResponse']['Customer'][0].update(Balance=500,SyncToken='4')
        path.write_text(json.dumps(raw));item['sha256']=hashlib.sha256(path.read_bytes()).hexdigest();self.write()
        again=sync_transactions(self.entity,self.path)
        self.assertTrue(again['already_applied']);self.assertEqual(again['blockers'],[])
        self.assertEqual(sync_transactions(self.entity,self.path,apply=True,plan_hash='any')['recorded_entries'],0)
        self.assertEqual(store.content_digest(),before)

    def test_unmatched_row_inside_the_history_period_waits_for_a_person(self):
        from bookkeeping.quickbooks_history import list_buffer_decisions,release_due_buffer,resolve_buffer_decision
        self.apply()
        self.entity.entity_config['bank_account_mappings']={'feed-1':'Assets:Bank:Checking'}
        row={'id':'plaid:inside-period','date':'2026-02-20','amount':'7.13','description':'Synthetic unmatched row',
             'accountId':'feed-1','accountName':'Checking','pending':False}
        import_transactions(self.entity,[row],'hold')
        held=list_buffer_decisions(self.entity)
        self.assertEqual(len(held),1);self.assertEqual(held[0]['history_matches'],[])
        due=release_due_buffer(self.entity,now='2027-01-01T00:00:00Z')
        self.assertEqual(due,{'released':[],'needs_review':[row['id']]})
        self.assertEqual(len(list_buffer_decisions(self.entity)),1)
        resolve_buffer_decision(self.entity,row['id'],'release')
        self.assertEqual(list_buffer_decisions(self.entity),[])

    def test_stale_plan_refused(self):
        plan=sync_transactions(self.entity,self.path)
        self.manifest['environment']='production';self.write()
        with self.assertRaisesRegex(ValueError,'Stale'):
            sync_transactions(self.entity,self.path,apply=True,plan_hash=plan['plan_hash'])

class CashHistoryTests(HistoryTests):
    basis = 'Cash'

    def test_buffer_case7_cash_learned_release_posts_without_confirmation(self):
        from bookkeeping.quickbooks_history import resolve_buffer_decision
        from bookkeeping.queue import _update_learned_context, _counterparty_key
        self.apply()
        self.entity.entity_config['bank_account_mappings']={'feed-1':'Assets:Bank:Checking'}
        row={'id':'plaid:buffer7-auto','date':'2026-02-10','amount':'7.13','description':'Synthetic learned receipt',
             'accountId':'feed-1','accountName':'Checking','pending':False}
        key=_counterparty_key(row['description'],'')
        for _ in range(self.entity.auto_post_threshold):
            _update_learned_context(self.entity,key,'Income:Sales',corrected=False)
        import_transactions(self.entity,[row],'hold')
        from unittest.mock import patch
        # Simulate an interruption after the ledger write, before display status.
        with patch('bookkeeping.statement_import._write_json_atomic',side_effect=OSError('synthetic interruption')):
            with self.assertRaisesRegex(OSError,'synthetic interruption'):
                resolve_buffer_decision(self.entity,row['id'],'release')
        resolve_buffer_decision(self.entity,row['id'],'release')
        resolve_buffer_decision(self.entity,row['id'],'release')
        entries=LedgerStore(default_store_path(self.entity.path)).load_entries()
        self.assertEqual(sum(e.source_id==row['id'] for e in entries),1)

    def test_real_posting_all_entities_controls_dimensions_idempotence(self):
        result=self.apply()
        self.assertEqual(result['recorded_entries'],10)
        from bookkeeping.reports.statements import profit_and_loss,balance_sheet
        pnl=profit_and_loss(self.entity.path,date(2026,1,1),date(2026,2,28),basis='cash')
        bs=balance_sheet(self.entity.path,date(2026,2,28),view='cash')
        self.assertEqual(pnl.totals['net_income'],Decimal(88))
        self.assertEqual(bs.totals['total_assets'],Decimal(1101))
        self.assertEqual(bs.totals['total_liabilities_and_equity'],Decimal(1101))
        self.assertFalse(any(':Invoice:' in (e.source_id or '') for e in LedgerStore(default_store_path(self.entity.path)).load_entries()))

if __name__=='__main__':unittest.main()
