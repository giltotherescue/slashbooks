"""Synthetic cash opening -> tracking -> open-items cutover safety."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from bookkeeping import business, quickbooks_sync
from bookkeeping.accrual import opening
from bookkeeping.accrual.journals import JournalError
from bookkeeping.accrual.store import load_record
from bookkeeping.entity import add_account, init_entity, load_entity
from bookkeeping.ledger.importer import _atomic_ledger_write, check_integrity
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.ledger.store import LedgerStore

CUT = '2026-01-01'
AR = 'Assets:Receivable:Accounts-Receivable'
AP = 'Liabilities:Payable:Accounts-Payable'
BANK = 'Assets:Bank:Checking'
FIXTURE = Path(__file__).parent / 'fixtures/quickbooks_api/snapshot.json'


class TrackingOpeningCutoverTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'books'
        init_entity(self.root, name='Synthetic Cutover', basis='cash')
        self.entity = load_entity(self.root)
        self.store = LedgerStore(self.root / 'ledger.sqlite')
        self.request = {'operation_id': 'open-items', 'evidence_refs': ['support.txt'], 'items': [
            {'id': 'invoice', 'kind': 'receivable', 'account': AR, 'counterparty': 'Orbit Labs', 'open_amount': '900.00'},
            {'id': 'bill', 'kind': 'payable', 'account': AP, 'counterparty': 'Delta Design', 'open_amount': '100.00'}]}
        (self.root / 'support.txt').write_text('Synthetic open items')

    def enable(self):
        request = {'operation_id': 'enable', 'action': 'tracking.enable', 'tracking_start': CUT}
        business.apply(self.entity, business.preview(self.entity, request))
        self.entity = load_entity(self.root)

    def seed(self, account, amount, when=CUT, description='Synthetic balance', source='seed'):
        for name in (account, 'Equity:Opening-Balances'):
            add_account(self.root, name, open_date='2025-01-01')
        self.entity = load_entity(self.root)
        entry = Entry(date=date.fromisoformat(when), narration=description, meta=(('source-id', source),),
                      postings=(Posting(account, Decimal(amount)), Posting('Equity:Opening-Balances', -Decimal(amount))))
        _atomic_ledger_write(self.entity, [], [entry], 'seed', None, 'seed')

    def test_cash_opening_at_cutover_is_not_a_settlement_even_when_amount_matches(self):
        quickbooks_sync.sync_opening(self.entity, FIXTURE, date.fromisoformat(CUT), apply=True)
        self.enable()
        plan = opening.preview_opening(self.entity, self.request)
        self.assertEqual(plan['status'], 'ready')
        opening.apply_opening(self.entity, plan)
        self.assertEqual(opening.apply_opening(self.entity, plan)['status'], 'already_applied')
        self.assertEqual(check_integrity(load_entity(self.root)).status, 'ok')

    def test_existing_control_balance_blocks_including_unlisted_controls(self):
        for account in (AR, AP, 'Assets:Other:Unlisted'):
            with self.subTest(account=account):
                # Separate source IDs permit independent balances without resetting the company.
                self.seed(account, '40.00', '2025-12-31', source=account)
        self.enable()
        plan = opening.preview_opening(self.entity, self.request)
        self.assertEqual(plan['status'], 'blocked')
        self.assertEqual({r['account'] for r in plan['support_differences']}, {AR, AP, 'Assets:Other:Unlisted'})
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, 'do not reconcile'):
            opening.apply_opening(self.entity, plan)
        self.assertEqual(before, self.store.content_digest())

    def test_items_refuse_cash_bank_card_and_processor_accounts(self):
        self.enable()
        for account in (BANK, 'Assets:Cash', 'Assets:Cash:Till', 'Liabilities:CreditCard',
                        'Liabilities:CreditCard:Visa', 'Assets:Processor:Clearing'):
            with self.subTest(account=account):
                request = deepcopy(self.request)
                request['items'] = [dict(request['items'][0], account=account)]
                with self.assertRaises(JournalError) as error:
                    opening.preview_opening(self.entity, request)
                self.assertEqual(error.exception.code, 'opening_item_cash_account')

    def test_explicit_financial_kinds_refuse_items_on_nonstandard_names(self):
        self.enable()
        for account, kind in (('Assets:Operating:Reserve', 'bank'), ('Assets:Till', 'cash'),
                              ('Liabilities:Visa', 'credit_card')):
            with self.subTest(kind=kind):
                add_account(self.root, account, financial_kind=kind)
                request = deepcopy(self.request)
                request['items'] = [dict(request['items'][0], account=account)]
                with self.assertRaises(JournalError) as error:
                    opening.preview_opening(self.entity, request)
                self.assertEqual(error.exception.code, 'opening_item_cash_account')

    def test_posted_amount_match_refuses_opening_and_pre_start_does_not(self):
        self.seed(BANK, '900.00', '2025-12-31', source='before')
        self.enable()
        plan = opening.preview_opening(self.entity, self.request)
        self.assertEqual(plan['status'], 'ready')
        self.seed(BANK, '900.00', CUT, source='possible-receipt')
        with self.assertRaises(JournalError) as error:
            opening.apply_opening(self.entity, plan)
        self.assertEqual(error.exception.code, 'opening_possible_settlement')
        self.assertIn('possible-receipt', str(error.exception))
        self.assertIsNone(load_record(self.store, 'items', 'invoice'))

    def test_posted_counterparty_match_catches_partial_receipt(self):
        self.request['items'][0]['reference'] = 'ORBIT-INVOICE-ALPHA'
        self.seed(BANK, '12.00', '2026-01-20', 'ACH ORBIT LABS', 'partial-receipt')
        self.enable()
        with self.assertRaises(JournalError) as error:
            opening.preview_opening(self.entity, self.request)
        self.assertEqual(error.exception.code, 'opening_possible_settlement')

    def test_later_control_account_settlement_is_also_refused(self):
        self.enable()
        self.seed(AR, '-900.00', '2026-01-20', 'Synthetic settlement', 'control-settlement')
        with self.assertRaises(JournalError) as error:
            opening.preview_opening(self.entity, self.request)
        self.assertEqual(error.exception.code, 'opening_possible_settlement')

    def test_party_id_is_validated_and_carried_without_legacy_link(self):
        self.enable()
        request = {'operation_id': 'party', 'action': 'party.save', 'id': 'orbit', 'expected_revision': 0,
                   'display_name': 'Orbit Labs', 'roles': ['customer']}
        business.apply(self.entity, business.preview(self.entity, request))
        self.request['items'][0]['party_id'] = 'orbit'
        opening.apply_opening(self.entity, opening.preview_opening(self.entity, self.request))
        item = load_record(self.store, 'items', 'invoice')
        self.assertEqual(item['party_id'], 'orbit')
        payment = {'operation_id': 'receive', 'action': 'payment.record', 'id': 'receipt',
                   'party_id': 'orbit', 'date': '2026-01-20', 'currency': 'USD', 'direction': 'receive',
                   'gross_amount': '900.00', 'cash_amount': '900.00', 'federal_withholding': '0.00',
                   'payment_method': 'bank_transfer',
                   'source': {'mode': 'new', 'source_id': 'bank-receipt', 'bank_account': BANK, 'amount': '900.00'},
                   'allocations': [{'item_id': 'invoice', 'amount': '900.00'}], 'evidence_ids': item['evidence_ids']}
        business.apply(self.entity, business.preview(self.entity, payment))
        self.assertEqual(load_record(self.store, 'items', 'invoice')['remaining_amount'], '0.00')
        self.assertEqual(check_integrity(load_entity(self.root)).status, 'ok')

    def test_missing_or_wrong_role_party_is_refused(self):
        self.enable()
        business.apply(self.entity, business.preview(self.entity, {'operation_id': 'party', 'action': 'party.save',
            'id': 'vendor', 'expected_revision': 0, 'display_name': 'Synthetic Vendor', 'roles': ['vendor']}))
        for party in ('missing', 'vendor', None, []):
            with self.subTest(party=party):
                request = deepcopy(self.request)
                request['items'][0]['party_id'] = party
                with self.assertRaises(JournalError) as error:
                    opening.preview_opening(self.entity, request)
                self.assertEqual(error.exception.code, 'party_role_required')

    def test_quickbooks_receivable_balance_is_canonical_and_blocks_items(self):
        snapshot = json.loads(FIXTURE.read_text())
        snapshot['accounts'].append({'Id': '4', 'Name': 'Accounts Receivable (A/R)', 'AccountType': 'Accounts Receivable'})
        rows = snapshot['reports']['OpeningTrialBalance']['Rows']['Row']
        rows.insert(1, {'ColData': [{'value': 'Accounts Receivable (A/R)', 'id': '4'}, {'value': '40.00'}, {'value': ''}]})
        rows[2]['ColData'][2]['value'] = '140.00'
        bs = snapshot['reports']['OpeningBalanceSheet']['Rows']['Row']
        bs[0]['Rows']['Row'].append({'ColData': [{'value': 'Accounts Receivable (A/R)', 'id': '4'}, {'value': '40.00'}]})
        bs[1]['Rows']['Row'][0]['ColData'][1]['value'] = '140.00'
        source = self.root.parent / 'snapshot.json'
        source.write_text(json.dumps(snapshot))
        result = quickbooks_sync.sync_opening(self.entity, source, date.fromisoformat(CUT), apply=True)
        self.assertTrue(result['opening']['success'], result)
        self.assertIn(AR, {p.account for e in self.store.load_entries() for p in e.postings})
        self.enable()
        controls = {o.account for o in self.store.load_opens() if o.account.startswith('Assets:Receivable:')}
        self.assertEqual(controls, {AR})
        plan = opening.preview_opening(self.entity, self.request)
        self.assertEqual(plan['status'], 'blocked')
        self.assertEqual(plan['support_differences'][0]['difference'], '-40.00')

    def test_quickbooks_types_map_to_the_only_starter_controls(self):
        rows, _ = quickbooks_sync._accounts([
            {'Id': 'ar', 'Name': 'Accounts Receivable (A/R)', 'AccountType': 'Accounts Receivable'},
            {'Id': 'ap', 'Name': 'Accounts Payable (A/P)', 'AccountType': 'Accounts Payable'}])
        self.assertEqual({r['mapped_account'] for r in rows}, {AR, AP})
