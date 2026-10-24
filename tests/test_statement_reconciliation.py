"""Reconcile immutable, confirmed statement evidence without transcribing it."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from bookkeeping.cli import main
from bookkeeping.coverage import record_statement
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.reconcile import list_discrepancies
from bookkeeping.statement_selection import select_statement, selection_revision
from test_coverage import make_company, post, CHECKING, CARD


class SavedStatementReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.company = make_company(Path(self.temp.name))
        LedgerStore(self.company / 'ledger.sqlite').set_meta('canonical', 'true')
        post(self.company, CHECKING, ['2026-01-15'])
        self.saved()

    def saved(self, ident='january', account=CHECKING, **changes):
        content = f'Date,Description,Amount\n2026-01-15,Synthetic {ident},10.00\n'.encode()
        digest = hashlib.sha256(content).hexdigest()
        ref = f'ingestion/statements/files/{digest}.csv'
        archive = self.company / ref
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(content)
        doc = dict(statement_id=ident, account=account, kind='statement',
                   period_start='2026-01-01', period_end='2026-01-31',
                   opening_balance='0.00', closing_balance='10.00',
                   evidence_ref=ref, evidence_sha256=digest,
                   period_confirmed=True, transactions_total='10.00', currency='USD',
                   opening_balance_date='2026-01-01', closing_balance_date='2026-01-31')
        doc.update(changes)
        record_statement(self.company, doc, replace=True, extra=doc)
        return doc

    def run_cli(self, *extra):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main(['reconcile', '--entity', str(self.company), '--statement-id', 'january', *extra])

    def reconcile(self):
        from bookkeeping.reconcile import reconcile_statement
        return reconcile_statement(self.company, 'january')

    def test_cli_uses_original_source_and_observes_it_without_changing_books(self):
        original = (self.company / 'ingestion/statements/january.json').read_bytes()
        ledger = (self.company / 'ledger.sqlite').read_bytes()
        before = datetime.now(timezone.utc)
        self.assertEqual(self.run_cli(), 0)
        after = datetime.now(timezone.utc)
        [record] = list_discrepancies(self.company)
        doc = json.loads(original)
        self.assertEqual(record['source_balance'], '10.00')
        self.assertEqual(record['source_opening_balance'], '0.00')
        self.assertEqual(record['source_transaction_total'], '10.00')
        self.assertEqual(record['discrepancy'], '0.00')
        self.assertEqual(record['status'], 'clean')
        self.assertEqual(record['source_through'], '2026-01-31')
        self.assertEqual(record['source_evidence_refs'], [doc['evidence_ref']])
        self.assertEqual(record['source_evidence_hashes'], {doc['evidence_ref']: doc['evidence_sha256']})
        self.assertTrue(before <= datetime.fromisoformat(record['source_snapshot_at']) <= after)
        self.assertEqual(original, (self.company / 'ingestion/statements/january.json').read_bytes())
        self.assertEqual(ledger, (self.company / 'ledger.sqlite').read_bytes())

    def test_real_csv_import_supplies_the_saved_source_fields(self):
        from bookkeeping import statement_import
        from bookkeeping.reconcile import reconcile_statement
        (self.company / 'ingestion/statements/january.json').unlink()
        csv = Path(self.temp.name) / 'source.csv'
        csv.write_text('Date,Description,Amount\n2026-01-15,Synthetic receipt,10.00\n')
        statement_import.save_profile(self.company, csv, name='checking', ledger_account=CHECKING,
            overrides={'date_format': '%Y-%m-%d', 'sign': 'standard'})
        imported = statement_import.import_file(self.company, csv, profile='checking',
            period_start='2026-01-01', period_end='2026-01-31', opening_balance='0.00', closing_balance='10.00')
        saved = imported['statements'][0]['statement']
        self.assertEqual(reconcile_statement(self.company, saved['statement_id']).status, 'clean')
        [proof] = list_discrepancies(self.company)
        self.assertEqual(proof['source_transaction_total'], '10.00')
        self.assertEqual(proof['source_evidence_hashes'], {saved['evidence_ref']: hashlib.sha256(csv.read_bytes()).hexdigest()})

    def test_card_balances_convert_to_ledger_sign_once(self):
        store = LedgerStore(self.company / 'ledger.sqlite')
        with store.transaction() as conn:
            store.insert_entries([Entry(date=date(2026, 1, 15), narration='Synthetic card charge',
                postings=(Posting(CARD, Decimal('-10.00')), Posting('Expenses:Software', Decimal('10.00'))))], conn)
        self.saved(account=CARD, transactions_total='-10.00')
        self.assertEqual(self.reconcile().discrepancy, Decimal('0.00'))
        [record] = list_discrepancies(self.company)
        self.assertEqual(record['source_balance'], '-10.00')
        self.assertEqual(record['source_transaction_total'], '-10.00')

    def test_missing_or_inconsistent_source_facts_do_not_become_proof(self):
        for changes in [dict(transactions_total=None), dict(transactions_total='9.00'),
                        dict(period_confirmed=False), dict(opening_balance=None),
                        dict(closing_balance_date='2026-01-30'), dict(transactions_total='NaN'),
                        dict(currency='EUR'), dict(currency=None)]:
            with self.subTest(changes=changes):
                self.saved(**changes)
                self.assertNotEqual(self.run_cli(), 0)
                self.assertFalse((self.company / 'reports/reconciliation.json').exists())

    def test_archived_source_must_exist_match_and_stay_inside_its_directory(self):
        doc = self.saved()
        archive = self.company / doc['evidence_ref']
        archive.write_bytes(b'changed')
        self.assertNotEqual(self.run_cli(), 0)
        archive.unlink()
        self.assertNotEqual(self.run_cli(), 0)
        outside = Path(self.temp.name) / 'outside.csv'
        outside.write_bytes(b'outside')
        archive.symlink_to(outside)
        self.assertNotEqual(self.run_cli(), 0)
        self.saved(evidence_ref='../outside.csv')
        self.assertNotEqual(self.run_cli(), 0)
        self.assertFalse((self.company / 'reports/reconciliation.json').exists())

    def test_conflicting_or_replaced_statement_requires_current_selection(self):
        self.saved('replacement')
        self.assertNotEqual(self.run_cli(), 0)
        select_statement(self.company, 'replacement', expected=['january', 'replacement'],
                         expected_revision=selection_revision(self.company))
        self.assertNotEqual(self.run_cli(), 0)
        from bookkeeping.reconcile import reconcile_statement
        self.assertEqual(reconcile_statement(self.company, 'replacement').status, 'clean')

    def test_saved_source_cannot_be_combined_with_manual_overrides(self):
        for extra in [('--account', CHECKING), ('--as-of', '2026-01-31'),
                      ('--source-balance', '11.00'), ('--source-snapshot-at', '2026-02-01T00:00:00Z'),
                      ('--source-evidence-ref', 'invented')]:
            with self.subTest(extra=extra):
                self.assertNotEqual(self.run_cli(*extra), 0)
        self.assertFalse((self.company / 'reports/reconciliation.json').exists())
