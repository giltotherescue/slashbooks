"""Synthetic correction cases; originals and transactions remain unchanged."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from test_coverage import make_company, statement, CHECKING, CARD
from bookkeeping.coverage import check_coverage, load_statements
from bookkeeping.statement_selection import select_statement, selection_revision, load_selection
from bookkeeping.remote_contract import command_spec


class StatementSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.company = make_company(Path(self.temp.name))
        for ident, closing in [('old', '100'), ('correct', '95')]:
            statement(self.company, ident, '2026-09-01', '2026-09-30', '100', closing)

    def choose(self, ident='correct', expected=None, revision=None):
        return select_statement(self.company, ident, expected=expected or ['correct', 'old'],
                                expected_revision=revision or selection_revision(self.company))

    def test_select_replay_restore_and_originals_unchanged(self):
        directory = self.company / 'ingestion/statements'
        before = {p.name: p.read_bytes() for p in directory.glob('*.json')}
        ledger = (self.company / 'ledger.sqlite').read_bytes()
        codes = lambda: {f['code'] for f in check_coverage(self.company, date(2026,9,1), date(2026,9,30))['findings']}
        self.assertIn('statement_conflict', codes())
        revision = selection_revision(self.company)
        self.assertTrue(self.choose(revision=revision)['changed'])
        self.assertNotIn('statement_conflict', codes())
        self.assertFalse(self.choose(revision=revision)['changed'])
        self.assertEqual(['correct'], [s.statement_id for s in load_statements(self.company)[0]])
        self.choose('old')
        self.assertEqual(['old'], [s.statement_id for s in load_statements(self.company)[0]])
        with self.assertRaises(ValueError):
            self.choose(revision=revision)
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.glob('*.json')})
        self.assertEqual(ledger, (self.company / 'ledger.sqlite').read_bytes())
        self.assertEqual(2, len(load_selection(self.company)['history']))

    def test_new_statement_requires_fresh_choice(self):
        self.choose()
        statement(self.company, 'new', '2026-09-01', '2026-09-30', '100', '90')
        with self.assertRaises(ValueError): self.choose()
        self.assertEqual({'correct','new'}, {s.statement_id for s in load_statements(self.company)[0]})
        self.choose('new', ['correct','new','old'])
        self.assertEqual(['new'], [s.statement_id for s in load_statements(self.company)[0]])

    def test_cross_account_period_missing_and_closed_rejected(self):
        statement(self.company, 'card', '2026-09-01', '2026-09-30', '100', '90', account=CARD)
        statement(self.company, 'oct', '2026-10-01', '2026-10-31', '100', '90')
        for ident in ['card','oct','absent']:
            with self.assertRaises(ValueError): self.choose(ident)
        path = self.company / 'entity.json'
        cfg = json.loads(path.read_text()); cfg['closed_through'] = '2026-09-30'
        path.write_text(json.dumps(cfg))
        with self.assertRaises(ValueError): self.choose()
        self.assertEqual({}, load_selection(self.company)['replaced'])

    def test_contract_is_write_and_bad_history_fails_closed(self):
        spec = command_spec(['statement','select','--entity','@entity','--statement-id','correct',
                             '--expected','old','--expected','correct','--expected-revision','abc'])
        self.assertTrue(spec['mutates'])
        self.choose()
        path = self.company / 'ingestion/statements/history/selection.json'
        path.write_text('{bad')
        self.assertTrue(load_statements(self.company)[1])
        with self.assertRaises(ValueError): self.choose()

    def test_replaced_source_cannot_keep_old_reconciliation_clean(self):
        from decimal import Decimal
        from bookkeeping.coverage import record_statement
        from bookkeeping.reconcile import reconcile
        from bookkeeping.ledger.store import LedgerStore
        LedgerStore(self.company / "ledger.sqlite").set_meta("canonical", "true")
        directory = self.company / 'ingestion/statements'
        for ident, digest in [('old', 'a'*64), ('correct', 'b'*64)]:
            path = directory / (ident + '.json')
            record = json.loads(path.read_text()); record['evidence_sha256'] = digest
            record_statement(self.company, record, replace=True)
        reconcile(self.company, CHECKING, Decimal('100'), date(2026,9,30), source_evidence_hashes={'old': 'a'*64})
        self.choose()
        codes = {f['code'] for f in check_coverage(self.company, date(2026,9,1), date(2026,9,30))['findings']}
        self.assertIn('statement_reconciliation_outdated', codes)
        with self.assertRaises(ValueError):
            reconcile(self.company, CHECKING, Decimal('100'), date(2026,9,30), source_evidence_hashes={'old': 'a'*64})
        reconcile(self.company, CHECKING, Decimal('95'), date(2026,9,30), source_evidence_hashes={'correct': 'b'*64})
        codes = {f['code'] for f in check_coverage(self.company, date(2026,9,1), date(2026,9,30))['findings']}
        self.assertNotIn('statement_reconciliation_outdated', codes)

    def test_history_list_includes_manual_records_when_requested(self):
        from bookkeeping.statement_import import load_statement_records
        self.assertEqual([], load_statement_records(self.company))
        self.choose()
        rows = load_statement_records(self.company, include_all=True)
        self.assertEqual({'correct', 'old'}, {r['statement_id'] for r in rows})
        self.assertEqual('correct', next(r for r in rows if r['statement_id'] == 'old')['replaced_by'])
