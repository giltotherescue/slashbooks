"""Synthetic cash schedules: timing, remaining balances, roles and read-only proof."""
import copy
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bookkeeping.cashflow import cash_schedule, schedule_csv
from bookkeeping.cli import main
from bookkeeping.entity import Entity
from bookkeeping.ledger.model import Entry, Open, Posting
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.remote_contract import command_spec, rewrite_paths

BANK = 'Assets:Bank:Checking'
SAVINGS = 'Assets:Bank:Savings'


def item(sid='future-balance', **changes):
    return dict(id=sid, direction='receipt', group='revenue', expected_date='2026-02-02', amount_remaining='200.00',
                confirmed=True, already_settled=False, evidence_refs=[], **changes)


class CashflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.company = self.root / 'company'; self.company.mkdir()
        self.entity = Entity(self.company, {}, {})
        (self.company / 'entity.json').write_text('{}')
        self.store = LedgerStore(self.company / 'ledger.sqlite'); self.store.initialize()
        names = [BANK, SAVINGS, 'Assets:Processors:Stripe', 'Assets:Receivables', 'Assets:Bank:Restricted',
                 'Income:Services', 'Expenses:Software', 'Equity:Opening']
        with self.store.transaction() as conn:
            self.store.insert_opens([Open(date(2026, 1, 1), name, ('USD',)) for name in names], conn)
            self.store.insert_entries([
                self.entry('2026-01-01', 'opening', BANK, '1000', 'Equity:Opening'),
                self.entry('2026-01-15', 'deposit', BANK, '100', 'Income:Services'),
                self.entry('2026-01-20', 'cost', BANK, '-50', 'Expenses:Software'),
                self.entry('2026-01-25', 'transfer', BANK, '-300', SAVINGS),
                self.entry('2026-02-05', 'future-recorded', BANK, '10', 'Income:Services'),
            ], conn)
        self.assumptions = dict(version=1, currency='USD', cash_accounts=[BANK, SAVINGS], items=[item()])

    def entry(self, when, sid, account, amount, other):
        return Entry(date.fromisoformat(when), sid, meta=(('source-id', sid),),
                     postings=(Posting(account, Decimal(amount)), Posting(other, -Decimal(amount))))

    def files(self):
        return {str(p.relative_to(self.company)): p.read_bytes() for p in self.company.rglob('*') if p.is_file()}

    def schedule(self, **kwargs):
        return cash_schedule(self.entity, self.assumptions, as_of='2026-01-31', **kwargs)

    def test_recorded_cash_internal_transfers_net_zero_future_excluded_and_no_files_changed(self):
        before = self.files(); result = self.schedule()
        self.assertEqual('1050.00', result['actual']['opening_cash'])
        self.assertEqual('1050.00', result['actual']['history_cash_movements'])
        self.assertEqual('1250.00', result['weeks'][0]['ending_cash'])
        self.assertEqual('2026-02-01', result['horizon']['from_date'])
        self.assertEqual('2026-05-02', result['horizon']['to_date'])
        self.assertFalse(result['actual']['baseline_quality']['verified_bank_cash'])
        self.assertIn('Future-dated', ' '.join(result['actual']['baseline_quality']['warnings']))
        self.assertEqual(before, self.files())

    def test_pipeline_settled_overdue_and_full_recorded_receipt_excluded_partial_balance_allowed(self):
        original = item()
        self.assumptions['items'] = [dict(original, id='pipeline', confirmed=False),
            dict(original, id='settled', already_settled=True), dict(original, id='overdue', expected_date='2026-01-30'),
            dict(original, id='repeat', evidence_refs=['deposit']),
            dict(original, id='remaining', evidence_refs=['deposit'], amount_basis='remaining_after_recorded_receipts', author='Owner', date='2026-01-31')]
        result = self.schedule()
        self.assertEqual({'already_settled', 'unconfirmed_pipeline', 'overdue_timing_requires_update', 'recorded_source_requires_remaining_basis'}, {row['reason'] for row in result['excluded']})
        self.assertEqual(['remaining'], result['weeks'][0]['assumption_ids'])
        self.assertEqual('Owner', result['scheduled_items'][0]['author'])

    def test_payment_groups_and_delayed_receipts_preserve_outside_horizon(self):
        self.assumptions['items'] += [dict(item('pay'), direction='payment', group='tax', amount_remaining='50.00'),
                                     dict(item('late'), expected_date='2026-05-01')]
        self.assumptions['scenarios'] = [dict(id='delay', label='Receipts delayed', receipt_delay_days=7)]
        result = self.schedule(); scenario = result['scenarios'][0]
        self.assertEqual(('200.00', '50.00', '1200.00'), (result['weeks'][0]['receipts'], result['weeks'][0]['payments']['tax'], result['weeks'][0]['ending_cash']))
        self.assertEqual(('0.00', '50.00', '1000.00'), (scenario['weeks'][0]['receipts'], scenario['weeks'][0]['payments']['tax'], scenario['weeks'][0]['ending_cash']))
        self.assertEqual('200.00', scenario['weeks'][1]['receipts'])
        self.assertEqual(('late', '2026-05-08'), (scenario['outside_horizon'][0]['id'], scenario['outside_horizon'][0]['scheduled_date']))
        csv_rows = list(csv.reader(io.StringIO(schedule_csv(result))))
        self.assertTrue(any(row[0] == 'outside_horizon' and row[1] == 'late' and row[3] == '2026-05-08' and row[-1] == 'delay' for row in csv_rows))

    def test_wrong_cash_roles_currency_and_unknown_catalog_rejected(self):
        for name in ['Assets:Processors:Stripe', 'Assets:Receivables', 'Assets:Bank:Restricted', 'Assets:Bank:Unknown']:
            self.assumptions['cash_accounts'] = [name]
            with self.assertRaisesRegex(ValueError, 'bank/cash'): self.schedule()
        self.assumptions['cash_accounts'] = [BANK]
        with self.store.transaction() as conn:
            conn.execute('UPDATE accounts SET currency=? WHERE name=?', ('EUR', BANK))
        with self.assertRaisesRegex(ValueError, 'USD bank/cash'): self.schedule()

    def test_bad_amounts_duplicate_ids_and_scenarios_rejected_without_writes(self):
        original = copy.deepcopy(self.assumptions)
        for value in ('NaN', 'Infinity', '-1', '0', '1.001', 1.5, True):
            self.assumptions = copy.deepcopy(original); self.assumptions['items'][0]['amount_remaining'] = value
            before = self.files()
            with self.assertRaises(ValueError): self.schedule()
            self.assertEqual(before, self.files())
        self.assumptions = copy.deepcopy(original); self.assumptions['items'] *= 2
        with self.assertRaisesRegex(ValueError, 'unique'): self.schedule()
        self.assumptions = copy.deepcopy(original); self.assumptions['scenarios'] = [dict(id='x', label='X', receipt_delay_days=-1)]
        with self.assertRaises(ValueError): self.schedule()

    def test_csv_formula_neutralization_preserves_numeric_negative_balances(self):
        self.assumptions['items'][0].update(id='=payload', label='  +SUM(1)', evidence_refs=['@evil'], author='-danger', source='=evil')
        self.assumptions['items'].append(dict(item('expense'), direction='payment', group='overhead', amount_remaining='5000'))
        rows = list(csv.reader(io.StringIO(schedule_csv(self.schedule()))))
        detail = next(row for row in rows if row[:2] == ['assumption', "'=payload"])
        self.assertEqual("'  +SUM(1)", detail[2]); self.assertEqual("'@evil", detail[6])
        self.assertEqual("'-danger", detail[7]); self.assertEqual("'=evil", detail[8])
        self.assertEqual('-3750.00', rows[1][-1])

    def test_cli_and_remote_contract_are_read_only_with_input_output_paths(self):
        path = self.root / 'assumptions.json'; path.write_text(json.dumps(self.assumptions))
        target = self.root / 'schedule.csv'; before = self.files()
        argv = ['forecast', '--entity', str(self.company), '--input', str(path), '--as-of', '2026-01-31', '--format', 'csv', '--output', str(target)]
        self.assertEqual(0, main(argv)); self.assertIn('recorded cash as of', target.read_text())
        self.assertEqual(before, self.files())
        spec = command_spec(argv)
        self.assertFalse(spec['mutates']); self.assertFalse(spec['network'])
        self.assertEqual(['input'], spec['input_fields']); self.assertEqual(['output'], spec['output_fields'])
        rewritten = rewrite_paths(argv, {str(path): '@input/assumptions.json', str(target): '@output/schedule.csv'})
        self.assertIn('--input=@input/assumptions.json', rewritten)
        self.assumptions['cash_accounts'] = ['Assets:Receivables']; path.write_text(json.dumps(self.assumptions))
        stderr = io.StringIO()
        with redirect_stderr(stderr): self.assertEqual(1, main(argv))
        self.assertIn('Error:', stderr.getvalue()); self.assertNotIn('Traceback', stderr.getvalue())

    def test_cli_output_cannot_replace_company_records_or_assumption_input(self):
        path = self.root / 'assumptions.json'; path.write_text(json.dumps(self.assumptions))
        before = self.files()
        for target in (self.company / 'entity.json', self.company / 'ledger.sqlite', path):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(1, main(['forecast', '--entity', str(self.company), '--input', str(path), '--as-of', '2026-01-31', '--output', str(target)]))
            self.assertIn('Error:', stderr.getvalue())
        self.assertEqual(before, self.files())


if __name__ == '__main__': unittest.main()
