"""Display decisions retain financial identities and immutable source evidence."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bookkeeping.chart_adoption import adopt, inspect
from bookkeeping.chart_presentation import update
from bookkeeping.entity import init_entity, load_entity
from bookkeeping.ledger.importer import _atomic_ledger_write, check_store_integrity
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.reports.statements import profit_and_loss, general_ledger
from bookkeeping.reports.workbook import _build_general_ledger_rows
from bookkeeping.taxmap import update_settings, mapping_report, build_handoff


class ChartPresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'entity'
        init_entity(self.root, name='Synthetic')
        self.store = LedgerStore(self.root / 'ledger.sqlite')
        self.account = 'Income:Design'
        self.manifest = dict(version=1, operation_id='adopt', source='example', decision='preserve',
                             scope='partial', presentation='activate', open_date='2026-01-01', accounts=[
                                 dict(source_id='a', name='Design & München', number='001 • A',
                                      parent_id=None, type='Income', account=self.account)])
        plan = adopt(self.root, self.manifest)
        adopt(self.root, self.manifest, apply=True, plan_hash=plan['plan_hash'])

    def test_labels_change_without_changing_history_or_economic_proof(self):
        entry = Entry(date=date(2026, 2, 1), narration='Synthetic', meta=(('source-id', 'sample'),), postings=(
            Posting('Assets:Bank:Checking', Decimal('100')), Posting(self.account, Decimal('-100'))))
        _atomic_ledger_write(load_entity(self.root), [], [entry], 'presentation-test', None, 'synthetic')
        mapped = next(r for r in mapping_report(self.root, form="schc")['accounts'] if r['account'] == self.account)
        self.assertEqual(mapped['account_number'], '001 • A')
        handoff = build_handoff(self.root, date(2026, 1, 1), date(2026, 12, 31), form="schc")
        line = next(r for r in handoff['trial_balance'] if r['account'] == self.account)
        self.assertEqual((line['account_name'], line['account_number']), ('Design & München', '001 • A'))
        before_entries = self.store.load_entries()
        report = lambda: profit_and_loss(self.root, date(2026, 1, 1), date(2026, 12, 31))
        proof = report().to_json(identity_fields=False)
        request = dict(operation_id='rename', account=self.account, display_name='Retainers — exact', number=None)
        plan = update(self.root, request)
        update(self.root, request, apply=True, plan_hash=plan['plan_hash'])
        handoff = build_handoff(self.root, date(2026, 1, 1), date(2026, 12, 31), form="schc")
        line = next(r for r in handoff['trial_balance'] if r['account'] == self.account)
        self.assertEqual((line['account_name'], line['account_number']), ('Retainers — exact', ''))
        self.assertEqual(self.store.load_entries(), before_entries)
        self.assertEqual(report().to_json(identity_fields=False), proof)
        row = next(r for s in report().sections for r in s['rows'] if r.get('account') == self.account)
        self.assertEqual(row['display_name'], request['display_name'])
        from bookkeeping.reports.structure import build_structure
        line = next(r for r in build_structure(report())['lines'] if r.get('account') == self.account)
        self.assertEqual(line['display_name'], request['display_name'])
        ledger = general_ledger(self.root, date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(next(s for s in ledger.sections if s['account'] == self.account)['display_name'], request['display_name'])
        rows = _build_general_ledger_rows(self.root, date(2026, 1, 1), date(2026, 12, 31))
        self.assertTrue(any(r and len(r) > 5 and r[1] == request['display_name'] and r[5] == 'Account Total' for r in rows))
        with self.assertRaisesRegex(ValueError, 'chart-present'):
            update_settings(self.root, account=self.account, account_number='123')
        self.assertEqual(check_store_integrity(self.store).status, 'ok')
        self.assertEqual(update(self.root, request)['status'], 'already_applied')
        changed = dict(request, display_name='Other')
        with self.assertRaisesRegex(ValueError, 'Operation identity'):
            update(self.root, changed)

    def test_atomic_failure_and_metadata_staleness(self):
        request = dict(operation_id='rename', account=self.account, display_name='New', number='2')
        plan = update(self.root, request)
        before = self.store.content_digest()
        with patch.object(LedgerStore, 'append_audit_event', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                update(self.root, request, apply=True, plan_hash=plan['plan_hash'])
        self.assertEqual(self.store.content_digest(), before)
        other = dict(request, operation_id='other', display_name='Else')
        update(self.root, other, apply=True, plan_hash=update(self.root, other)['plan_hash'])
        with self.assertRaisesRegex(ValueError, 'Stale'):
            update(self.root, request, apply=True, plan_hash=plan['plan_hash'])
