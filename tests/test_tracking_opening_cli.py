"""Cutover review regressions through the books command dispatcher in fresh processes.

Every account, party and amount is synthetic. The legacy replay harness selects
only the deterministic pre-PR-F account mapper when first writing evidence.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bookkeeping.ledger.store import LedgerStore

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / 'tests/fixtures/quickbooks_api/snapshot.json'
AR = 'Assets:Receivable:Accounts-Receivable'
BANK = 'Assets:Operating:Checking'


class TrackingOpeningCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name)
        self.root = self.work / 'books'
        self.n = 0
        self.ok('entity', 'init', self.root, '--name', 'Synthetic Cutover', '--basis', 'cash')
        (self.root / 'support.txt').write_text('Synthetic open invoice')
        self.request = {'operation_id': 'opening', 'evidence_refs': ['support.txt'], 'items': [
            {'id': 'invoice', 'kind': 'receivable', 'account': AR,
             'counterparty': 'Orbit Labs', 'open_amount': '900.00'}]}

    def file(self, payload):
        self.n += 1
        path = self.work / f'input-{self.n}.json'
        path.write_text(json.dumps(payload))
        return path

    def command(self, *args, legacy=False):
        env = dict(os.environ, PYTHONPATH=str(REPO / 'src'))
        command = [sys.executable, '-m', 'bookkeeping.cli']
        if legacy:
            # Model the earlier engine's derived evidence without copying private
            # fixtures or accepting arbitrary persisted mappings in the product.
            command = [sys.executable, '-c',
                       'from bookkeeping import quickbooks_sync as s; '
                       'old=s._accounts; s._accounts=lambda raw, **kw: old(raw, canonical_controls=False); '
                       'from bookkeeping.cli import main; import sys; sys.exit(main(sys.argv[1:]))']
        result = subprocess.run(command + [str(arg) for arg in args], cwd=self.work,
                                env=env, text=True, capture_output=True)
        try:
            output = json.loads(result.stdout)
        except ValueError:
            output = result.stdout
        return result.returncode, output, result.stderr

    def ok(self, *args, **kwargs):
        code, output, error = self.command(*args, **kwargs)
        self.assertEqual(code, 0, (args, output, error))
        return output

    def enable(self):
        plan = self.ok('business', 'preview', '--entity', self.root, '--input', self.file({
            'operation_id': 'enable', 'action': 'tracking.enable', 'tracking_start': '2026-01-01'}))
        self.ok('business', 'apply', '--entity', self.root, '--input', self.file(plan))

    def preview(self, request=None):
        return self.command('accrual', 'opening', 'preview', '--entity', self.root,
                            '--input', self.file(request or self.request))

    def posted_receipt(self, amount='900.00', description='Orbit Labs', bank=BANK):
        self.ok('entity', 'account-add', self.root, '--account', bank, '--open-date', '2026-01-01')
        self.ok('entity', 'account-add', self.root, '--account', 'Income:Services', '--open-date', '2026-01-01')
        config = json.loads((self.root / 'entity.json').read_text())
        config['bank_account_mappings'] = {'checking': {'ledger_account': bank}}
        (self.root / 'entity.json').write_text(json.dumps(config))
        self.ok('ingest', self.file([{'id': 'receipt', 'date': '2026-01-20', 'amount': amount,
                                    'description': description, 'accountId': 'checking',
                                    'accountName': 'Checking', 'accountType': 'checking'}]),
                '--entity', self.root, '--source', 'banksync', '--no-feed-window')
        self.ok('queue', 'propose', '--entity', self.root, '--source-id', 'banksync:receipt',
                '--category', 'Income:Services', '--reasoning', 'Synthetic receipt')
        self.ok('queue', 'confirm', '--entity', self.root, '--item', 'banksync:receipt')
        self.assertEqual(len(LedgerStore(self.root / 'ledger.sqlite').load_entries()), 1)

    def test_mapped_bank_refuses_open_items_and_possible_settlements(self):
        self.posted_receipt()
        self.enable()
        code, output, error = self.preview()
        self.assertEqual(code, 1, (output, error))
        self.assertEqual(output['errors'][0]['code'], 'opening_possible_settlement')
        self.ok('ledger', 'reverse', '--entity', self.root, '--source-id', 'receipt')
        request = deepcopy(self.request)
        request['items'][0]['account'] = BANK
        code, output, error = self.preview(request)
        self.assertEqual(code, 1, (output, error))
        self.assertEqual(output['errors'][0]['code'], 'opening_item_cash_account')

    def test_exact_reversal_clears_settlement_refusal(self):
        self.posted_receipt()
        self.enable()
        self.assertEqual(self.preview()[1]['errors'][0]['code'], 'opening_possible_settlement')
        self.ok('ledger', 'reverse', '--entity', self.root, '--source-id', 'receipt')
        code, plan, error = self.preview()
        self.assertEqual((code, plan.get('status')), (0, 'ready'), (plan, error))
        self.ok('accrual', 'opening', 'apply', '--entity', self.root, '--input', self.file(plan))

    def test_corrected_live_replacement_still_requires_review(self):
        self.posted_receipt()
        self.enable()
        self.ok('ledger', 'correct', '--entity', self.root, '--source-id', 'receipt',
                '--new-source-id', 'replacement', '--date', '2026-01-21',
                '--description', 'Orbit Labs corrected receipt',
                '--posting', BANK + '=900.00', '--posting', 'Income:Services=-900.00')
        code, output, error = self.preview()
        self.assertEqual(code, 1, (output, error))
        self.assertEqual(output['errors'][0]['code'], 'opening_possible_settlement')
        self.assertIn('replacement', json.dumps(output))

    def test_unrelated_same_amount_is_deliberately_refused(self):
        self.posted_receipt(description='Unrelated synthetic sale')
        self.enable()
        code, output, error = self.preview()
        self.assertEqual(code, 1, (output, error))
        self.assertEqual(output['errors'][0]['code'], 'opening_possible_settlement')

    def test_snapshot_replay_preserves_pre_upgrade_mapping_and_evidence(self):
        snapshot = json.loads(FIXTURE.read_text())
        # Zero balances still affected the old manifest, even without ledger postings.
        snapshot['accounts'] += [
            {'Id': 'ar', 'Name': 'Accounts Receivable (A/R)', 'AccountType': 'Accounts Receivable'},
            {'Id': 'ap', 'Name': 'Accounts Payable (A/P)', 'AccountType': 'Accounts Payable'}]
        source = self.file(snapshot)
        first = self.ok('qb', 'sync-opening', source, '--entity', self.root,
                        '--cutover', '2026-01-01', '--apply', legacy=True)
        evidence = self.root / 'sources/quickbooks' / first['snapshot_id']
        before = {p.relative_to(evidence): p.read_bytes() for p in evidence.rglob('*') if p.is_file()}
        digest = LedgerStore(self.root / 'ledger.sqlite').content_digest()
        replay = self.ok('qb', 'sync-opening', source, '--entity', self.root,
                         '--cutover', '2026-01-01', '--apply')
        self.assertTrue(replay['already_applied'])
        self.assertEqual(LedgerStore(self.root / 'ledger.sqlite').content_digest(), digest)
        self.assertEqual(before, {p.relative_to(evidence): p.read_bytes() for p in evidence.rglob('*') if p.is_file()})

    def test_inactive_zero_control_does_not_block_import(self):
        snapshot = json.loads(FIXTURE.read_text())
        snapshot['accounts'] += [
            {'Id': 'ar', 'Name': 'Trade Receivables', 'AccountType': 'Accounts Receivable'},
            {'Id': 'old-ar', 'Name': 'Accounts Receivable', 'AccountType': 'Accounts Receivable', 'Active': False}]
        source = self.file(snapshot)
        result = self.ok('qb', 'sync-opening', source, '--entity', self.root,
                         '--cutover', '2026-01-01', '--apply')
        accounts = {row['id']: row['mapped_account'] for row in result['accounts']}
        self.assertEqual(accounts['ar'], 'Assets:Receivable:Trade-Receivables')
        self.assertNotEqual(accounts['ar'], accounts['old-ar'])

    def test_multiple_active_controls_keep_distinct_mappings(self):
        snapshot = json.loads(FIXTURE.read_text())
        snapshot['accounts'] += [
            {'Id': 'ar-1', 'Name': 'Trade Receivables', 'AccountType': 'Accounts Receivable'},
            {'Id': 'ar-2', 'Name': 'Other Receivables', 'AccountType': 'Accounts Receivable'}]
        result = self.ok('qb', 'sync-opening', self.file(snapshot), '--entity', self.root,
                         '--cutover', '2026-01-01', '--apply')
        controls = [row['mapped_account'] for row in result['accounts'] if row['id'].startswith('ar-')]
        self.assertEqual(len(set(controls)), 2)

    def test_multiple_controls_accept_reviewed_crosswalk(self):
        snapshot = json.loads(FIXTURE.read_text())
        snapshot['accounts'] += [
            {'Id': 'ar-1', 'Name': 'Trade Receivables', 'AccountType': 'Accounts Receivable'},
            {'Id': 'ar-2', 'Name': 'Other Receivables', 'AccountType': 'Accounts Receivable'}]
        source = self.file(snapshot)
        choice = {'decision': 'rebuild', 'targets': {
            '1': 'Assets:Bank:Checking', '2': 'Equity:Opening-Balances', '3': 'Expenses:Software',
            'ar-1': AR, 'ar-2': 'Assets:Receivable:Other'}}
        plan = self.ok('qb', 'sync-opening', source, '--entity', self.root,
                       '--cutover', '2026-01-01', '--chart-choice', self.file(choice))
        choice['plan_hash'] = plan['chart']['plan_hash']
        result = self.ok('qb', 'sync-opening', source, '--entity', self.root,
                         '--cutover', '2026-01-01', '--chart-choice', self.file(choice), '--apply')
        self.assertTrue(result['opening']['success'])
        controls = {o.account for o in LedgerStore(self.root / 'ledger.sqlite').load_opens()
                    if o.account.startswith('Assets:Receivable:')}
        self.assertEqual(controls, {AR, 'Assets:Receivable:Other'})

    def test_legacy_preserve_chart_replay_keeps_the_same_operation(self):
        snapshot = json.loads(FIXTURE.read_text())
        snapshot['accounts'].append({'Id': 'ar', 'Name': 'Trade Receivables', 'AccountType': 'Accounts Receivable'})
        source = self.file(snapshot)
        choice = {'decision': 'preserve'}
        plan = self.ok('qb', 'sync-opening', source, '--entity', self.root,
                       '--cutover', '2026-01-01', '--chart-choice', self.file(choice), legacy=True)
        choice['plan_hash'] = plan['chart']['plan_hash']
        first = self.ok('qb', 'sync-opening', source, '--entity', self.root, '--cutover', '2026-01-01',
                        '--chart-choice', self.file(choice), '--apply', legacy=True)
        store = LedgerStore(self.root / 'ledger.sqlite')
        before = store.content_digest()
        evidence = self.root / 'sources/quickbooks' / first['snapshot_id']
        saved = {p.relative_to(evidence): p.read_bytes() for p in evidence.rglob('*') if p.is_file()}
        replay = self.ok('qb', 'sync-opening', source, '--entity', self.root, '--cutover', '2026-01-01',
                         '--chart-choice', self.file(choice), '--apply')
        self.assertTrue(replay['already_applied'])
        self.assertEqual(replay['chart']['status'], 'already_applied')
        self.assertEqual(store.content_digest(), before)
        self.assertEqual(saved, {p.relative_to(evidence): p.read_bytes() for p in evidence.rglob('*') if p.is_file()})

    def test_normalized_control_names_accept_distinct_reviewed_targets(self):
        snapshot = json.loads(FIXTURE.read_text())
        snapshot['accounts'] += [
            {'Id': 'ar-1', 'Name': 'Accounts Receivable', 'AccountType': 'Accounts Receivable'},
            {'Id': 'ar-2', 'Name': 'Accounts-Receivable', 'AccountType': 'Accounts Receivable', 'Active': False}]
        source = self.file(snapshot)
        choice = {'decision': 'rebuild', 'targets': {
            '1': 'Assets:Bank:Checking', '2': 'Equity:Opening-Balances', '3': 'Expenses:Software',
            'ar-1': AR, 'ar-2': 'Assets:Receivable:Retired'}}
        plan = self.ok('qb', 'sync-opening', source, '--entity', self.root,
                       '--cutover', '2026-01-01', '--chart-choice', self.file(choice))
        choice['plan_hash'] = plan['chart']['plan_hash']
        result = self.ok('qb', 'sync-opening', source, '--entity', self.root, '--cutover', '2026-01-01',
                         '--chart-choice', self.file(choice), '--apply')
        self.assertTrue(result['opening']['success'])
        replay = self.ok('qb', 'sync-opening', source, '--entity', self.root, '--cutover', '2026-01-01',
                         '--chart-choice', self.file(choice), '--apply')
        self.assertTrue(replay['already_applied'])

    def test_numeric_invoice_reference_identifies_partial_receipt(self):
        self.posted_receipt(amount='37.00', description='Payment invoice 1001')
        self.enable()
        for reference in ('1001', 'INV-1001'):
            with self.subTest(reference=reference):
                self.request['items'][0].update(reference=reference, original_amount='1000.00')
                code, output, error = self.preview()
                self.assertEqual(code, 1, (output, error))
                self.assertEqual(output['errors'][0]['code'], 'opening_possible_settlement')

    def test_partial_receipt_matches_original_minus_open_amount(self):
        self.posted_receipt(amount='100.00', description='Unidentified deposit')
        self.enable()
        self.request['items'][0].update(reference='1001', original_amount='1000.00')
        code, output, error = self.preview()
        self.assertEqual(code, 1, (output, error))
        self.assertEqual(output['errors'][0]['code'], 'opening_possible_settlement')

    def test_receipt_matches_original_amount(self):
        self.posted_receipt(amount='1000.00', description='Unidentified deposit')
        self.enable()
        self.request['items'][0].update(reference='1001', original_amount='1000.00')
        code, output, error = self.preview()
        self.assertEqual(code, 1, (output, error))
        self.assertEqual(output['errors'][0]['code'], 'opening_possible_settlement')

    def test_bare_numeric_reference_alone_does_not_match(self):
        self.posted_receipt(amount='37.00', description='Route 12 station')
        self.enable()
        self.request['items'][0].update(reference='12', original_amount='1000.00')
        code, output, error = self.preview()
        self.assertEqual((code, output.get('status')), (0, 'ready'), (output, error))

    def test_numeric_reference_requires_whole_tokens(self):
        self.posted_receipt(amount='37.00', description='Payment invoice 10010')
        self.enable()
        self.request['items'][0].update(reference='1001', original_amount='1000.00')
        code, output, error = self.preview()
        self.assertEqual((code, output.get('status')), (0, 'ready'), (output, error))
