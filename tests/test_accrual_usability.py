"""Familiar journal input, explicit due dates, historical aging and readable reports."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date, timedelta
from io import StringIO
import csv
import json
import unittest
from unittest.mock import patch
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from decimal import Decimal

from bookkeeping.accrual import journals, items, reports, migration
from bookkeeping.accrual.intake import canonical_hash
from bookkeeping.accrual.render import render_report
from bookkeeping.cli import main
from bookkeeping.ledger.importer import check_integrity
from bookkeeping.remote_contract import command_spec, parse_command, rewrite_paths
from bookkeeping.reports import workbook
from tests import test_accrual_journals as journal_fixtures
from tests import test_accrual_migration as opening_fixtures
from tests import test_accrual_items as item_fixtures
from tests.test_accrual_schedules import AccrualFixture, AR, AP, INCOME, EXPENSE


class ReportLanguageTests(unittest.TestCase):
    def test_asset_backed_accrual_uses_neutral_balance_label(self):
        document = {'kind': 'outstanding', 'as_of': '2026-01-31', 'currency': 'USD',
                    'rows': [{'id': 'earned-service', 'reference': 'Unbilled January work', 'kind': 'accrual',
                              'account': 'Assets:Accrued-Revenue', 'remaining_amount': '100.00', 'status': 'open'}],
                    'issues': []}
        text = render_report(document)
        self.assertIn('Accrued balance', text)
        self.assertNotIn('Accrued liability', text)

    def test_readable_labels_preserve_document_and_missing_data(self):
        document = {'kind': 'schedules', 'as_of': '2026-01-31', 'currency': 'USD',
                    'rows': [{'id': 'internal-schedule', 'reference': 'Annual software',
                              'kind': 'prepaid_expense', 'account': 'Assets:Prepaid',
                              'recognized_amount': '100.00', 'remaining_amount': '1100.00',
                              'status': 'active'}], 'issues': []}
        original = deepcopy(document)
        text = render_report(document)
        self.assertIn('Annual software', text)
        self.assertIn('Prepaid expense', text)
        self.assertIn('1100.00', text)
        self.assertNotIn('internal-schedule', text)
        self.assertIn('No outstanding review items.', text)
        self.assertNotIn('authorize close', text)
        self.assertEqual(document, original)
        document['rows'][0].pop('reference')
        document['issues'] = [{'code': 'missing_evidence', 'message': 'Provide the supplier agreement.'}]
        text = render_report(document)
        self.assertIn('internal-schedule', text)
        self.assertIn('Provide the supplier agreement.', text)
        self.assertNotIn('missing_evidence:', text)


class JournalUsabilityTests(unittest.TestCase):
    setUp = journal_fixtures.AccrualJournalTests.setUp

    def test_debit_credit_normalizes_to_identical_signed_preview_and_exact_retry(self):
        signed = journals.preview_journal(self.entity, self.request)
        request = deepcopy(self.request)
        request['postings'] = [{'account': self.request['postings'][0]['account'], 'debit': {'minor_units': 1000}},
                               {'account': self.request['postings'][1]['account'], 'credit': '10.00', 'debit': '0.00'}]
        plan = journals.preview_journal(self.entity, request)
        self.assertEqual(plan, signed)
        self.assertEqual(plan['entries'][0]['postings'][0]['debit'], '10.00')
        self.assertEqual(plan['entries'][0]['postings'][0]['credit'], '0.00')
        expected = {'debits': '10.00', 'credits': '10.00', 'difference': '0.00', 'currency': 'USD'}
        self.assertEqual(plan['entries'][0]['totals'], expected)
        self.assertEqual(plan['details']['journal_totals'], expected)
        receipt = journals.apply_journal(self.entity, plan)
        self.assertEqual(journals.apply_journal(self.entity, journals.preview_journal(self.entity, request)), receipt)
        self.assertEqual(self.store.counts().entries, 1)
        self.assertEqual(check_integrity(self.entity).status, 'ok')

    def test_invalid_side_inputs_and_display_tampering_never_post(self):
        before = self.store.content_digest()
        for fields in ({'amount': '10', 'debit': '10'}, {'debit': '-10'}, {'credit': '-10'},
                       {'debit': '10', 'credit': '1'}, {'debit': '0', 'credit': '0'},
                       {'debit': 10.0}, {'debit': '10.001'}, {'debit': 'NaN'}, {}):
            with self.subTest(fields=fields):
                request = deepcopy(self.request)
                request['postings'][0] = {'account': 'Expenses:Software', **fields}
                with self.assertRaises(ValueError):
                    journals.preview_journal(self.entity, request)
                self.assertEqual(self.store.content_digest(), before)
        plan = journals.preview_journal(self.entity, self.request)
        for field in ('totals', 'posting'):
            changed = deepcopy(plan)
            if field == 'totals': changed['details']['journal_totals']['debits'] = '11.00'
            else: changed['entries'][0]['postings'][0]['debit'] = '11.00'
            with self.assertRaisesRegex(journals.JournalError, 'changed'):
                journals.apply_journal(self.entity, changed)
        self.assertEqual(self.store.content_digest(), before)

    def test_pre_upgrade_committed_plan_replays_without_new_display_fields(self):
        entry_document, preview_operation = journals._entry_document, journals._preview_operation
        def legacy_entry(entry):
            result = entry_document(entry)
            result.pop('totals')
            for posting in result['postings']:
                posting.pop('debit'); posting.pop('credit')
            return result
        def legacy_preview(*args, **kwargs):
            result = preview_operation(*args, **kwargs)
            result['details'].pop('journal_totals')
            result.pop('plan_hash')
            result['plan_hash'] = canonical_hash(result)
            return result
        with patch.object(journals, '_entry_document', legacy_entry), patch.object(journals, '_preview_operation', legacy_preview):
            old_plan = journals.preview_journal(self.entity, self.request)
            old_receipt = journals.apply_journal(self.entity, old_plan)
        before = self.store.content_digest()
        self.assertEqual(journals.apply_journal(self.entity, old_plan), old_receipt)
        self.assertEqual(journals.preview_journal(self.entity, self.request), old_plan)
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(self.store.counts().entries, 1)


class AgingUsabilityTests(AccrualFixture):
    item = item_fixtures.ItemTests.item
    settlement = item_fixtures.ItemTests.settlement
    create = item_fixtures.ItemTests.create

    def make_item(self, identity, due=None, *, kind='receivable', amount='100.00', counterparty='Synthetic Customer'):
        request = self.item(operation_id='create-' + identity, id=identity, kind=kind, original_amount=amount,
            account=AP if kind == 'payable' else AR, counterparty=counterparty, reference='REF-' + identity,
            origin={'mode': 'new', 'source_id': 'source-' + identity, 'counter_account': EXPENSE if kind == 'payable' else INCOME},
            evidence_ids=[self.evidence(identity)])
        if due is not None: request['due_date'] = due
        return items.apply_item(self.entity, items.preview_item(self.entity, request))

    def test_due_date_boundaries_and_unknown_dates_have_exact_separate_buckets(self):
        as_of = date(2026, 4, 1)
        expected = {0: 'current', -1: 'current', 1: '1_30', 30: '1_30', 31: '31_60', 60: '31_60',
                    61: '61_90', 90: '61_90', 91: 'over_90', None: 'unknown_due_date'}
        for days in expected:
            self.make_item('age-' + str(days), (as_of - timedelta(days=days)).isoformat() if days is not None else None)
        result = reports.report(self.entity, 'aging', as_of=as_of)
        by_id = {row['id']: row for row in result['rows']}
        for days, bucket in expected.items():
            row = by_id['age-' + str(days)]
            self.assertEqual(row['bucket'], bucket)
            self.assertEqual(row['days_past_due'], max(days, 0) if days is not None else None)
        summary = result['summary'][0]
        self.assertEqual(summary['total'], '1000.00')
        self.assertEqual(summary['buckets'], {'current': '200.00', '1_30': '200.00', '31_60': '200.00',
            '61_90': '200.00', 'over_90': '100.00', 'unknown_due_date': '100.00', 'unapplied_credit': '0.00'})
        self.assertFalse(result['credits_netted'])

    def test_aging_uses_historical_remainder_and_separates_payables_and_unapplied_credits(self):
        self.create(due_date='2026-01-10', counterparty='Synthetic Customer')
        items.apply_settlement(self.entity, items.preview_settlement(self.entity, self.settlement()))
        items.apply_settlement(self.entity, items.preview_settlement(self.entity, self.settlement(operation_id='settle-rest', date='2026-02-15',
            source={'mode': 'new', 'source_id': 'bank-rest', 'bank_account': self.settlement()['source']['bank_account'], 'amount': '600.00'},
            allocations=[{'item_id': 'invoice-1', 'amount': '600.00'}], evidence_ids=[self.evidence('rest-payment')])))
        self.make_item('vendor', '2026-01-25', kind='payable', counterparty='Synthetic Vendor')
        credit = self.item(operation_id='unapplied-credit', id='unapplied-credit', kind='credit', direction='credit', original_amount='50.00',
            counterparty='Synthetic Customer', origin={'mode': 'new', 'source_id': 'credit-source', 'counter_account': INCOME}, evidence_ids=[self.evidence('credit-source')])
        items.apply_item(self.entity, items.preview_item(self.entity, credit))
        january = reports.report(self.entity, 'aging', as_of='2026-01-31')
        invoice = next(row for row in january['rows'] if row['id'] == 'invoice-1')
        self.assertEqual(invoice['remaining_amount'], '600.00')
        self.assertEqual(invoice['days_past_due'], 21)
        groups = {group['kind']: group for group in january['summary']}
        self.assertEqual(groups['receivable']['total'], '600.00')
        self.assertEqual(groups['payable']['total'], '100.00')
        self.assertEqual(groups['credit']['buckets']['unapplied_credit'], '-50.00')
        credit_row = next(row for row in january['rows'] if row['kind'] == 'credit')
        self.assertIsNone(credit_row['days_past_due'])
        self.assertNotIn('invoice-1', {row['id'] for row in reports.report(self.entity, 'aging', as_of='2026-02-28')['rows']})
        self.assertEqual(next(row for row in reports.report(self.entity, 'aging', as_of='2026-01-14')['rows'] if row['id'] == 'invoice-1')['remaining_amount'], '1000.00')

    def test_invalid_due_dates_and_foreign_currency_cannot_create_or_mix_balances(self):
        before = self.store.content_digest()
        for due in ('2026-02-30', '20260131', '', None):
            with self.subTest(due=due), self.assertRaises(ValueError):
                items.preview_item(self.entity, self.item(due_date=due))
            self.assertEqual(self.store.content_digest(), before)
        with self.assertRaisesRegex(ValueError, 'USD'):
            reports.aging([{'kind': 'receivable', 'currency': 'EUR', 'remaining_amount': '100.00'}], date(2026, 1, 31))

    def test_json_default_and_readable_text_reports_preserve_read_only_transport(self):
        self.create(counterparty='Synthetic Customer')
        argv = ['accrual', 'report', '--entity', str(self.root), '--kind', 'aging', '--as-of', '2026-01-31']
        before = self.store.content_digest()
        output = StringIO()
        with redirect_stdout(output): self.assertEqual(main(argv), 0)
        self.assertEqual(json.loads(output.getvalue())['kind'], 'aging')
        for kind in ('aging', 'outstanding', 'schedules', 'adjustments', 'support', 'reconciliation'):
            args = list(argv); args[5] = kind
            output = StringIO()
            with redirect_stdout(output): self.assertEqual(main(args + ['--format', 'text']), 0)
            text = output.getvalue()
            self.assertIn('2026-01-31', text)
            self.assertIn('Accrual basis', text)
            self.assertIn('Review issues:', text)
            if kind == 'aging':
                self.assertIn('Due date not provided', text)
                self.assertIn('Synthetic Customer', text)
        remote = ['accrual', 'report', '--entity', '@entity', '--kind', 'aging', '--as-of', '2026-01-31', '--format', 'text']
        self.assertFalse(command_spec(remote)['mutates'])
        self.assertEqual(parse_command(remote)[0].format, 'text')
        self.assertIn('--format=text', rewrite_paths(remote, {'@entity': str(self.root)}))
        self.assertEqual(self.store.content_digest(), before)

    def test_accountant_export_contains_readable_aging_detail_and_engine_summary(self):
        self.create(due_date='2026-01-10', counterparty='Synthetic Customer', reference='INV-001')
        package = workbook.generate_accountant_package(self.root, date(2026, 1, 1), date(2026, 1, 31), override=True,
            sheets=['receivables-payables-aging'])
        self.assertTrue(package.success, package.error)
        rows = list(csv.reader(StringIO(package.csv_files[0].read_text())))
        self.assertIn(['Report as of', '2026-01-31'], rows)
        self.assertIn(['Accounting basis', 'accrual'], rows)
        header = next(index for index, row in enumerate(rows) if row[:2] == ['Type', 'Customer / vendor'])
        self.assertEqual(rows[header][:5], ['Type', 'Customer / vendor', 'Reference', 'Accounting date', 'Due date'])
        self.assertEqual(rows[header + 1][1:7], ['Synthetic Customer', 'INV-001', '2026-01-01', '2026-01-10', '1–30 days overdue', '1000.00'])
        self.assertTrue(any('Unapplied credit' in row for row in rows))
        self.assertTrue(any(row and row[0] == 'receivable' and row[-1] == '1000.00' for row in rows))
        self.assertIn(['No outstanding review items'], rows)

    @unittest.skipUnless(workbook._xlsxwriter_available(), 'xlsx extra is not installed')
    def test_aging_xlsx_money_is_numeric_in_both_sections_and_identifiers_stay_text(self):
        self.create(id='0007', due_date='2026-01-10', counterparty='00042', reference='00123')
        credit = self.item(operation_id='credit-create', id='0008', kind='credit', direction='credit',
            original_amount='50.00', reference='=1+1', counterparty='00042',
            origin={'mode': 'new', 'source_id': 'credit-source', 'counter_account': INCOME},
            evidence_ids=[self.evidence('credit-source')])
        items.apply_item(self.entity, items.preview_item(self.entity, credit))
        package = workbook.generate_accountant_package(self.root, date(2026, 1, 1), date(2026, 1, 31), override=True,
            sheets=['receivables-payables-aging'])
        self.assertTrue(package.success, package.error)
        self.assertIsNotNone(package.xlsx_file)
        csv_rows = list(csv.reader(StringIO(package.csv_files[0].read_text())))
        with ZipFile(package.xlsx_file) as archive:
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            strings = [''.join(node.itertext()) for node in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', ns)]
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            cells = {cell.attrib['r']: cell for cell in sheet.findall('.//s:sheetData/s:row/s:c', ns)}
            def value(cell):
                raw = cell.findtext('s:v', namespaces=ns)
                return strings[int(raw)] if cell.get('t') == 's' else raw
            header = next(index for index, row in enumerate(csv_rows) if row[:2] == ['Type', 'Customer / vendor'])
            detail_numbers = []
            for index in range(header + 1, header + 3):
                cell = cells[f'G{index + 1}']
                self.assertIn(cell.get('t'), (None, 'n'))
                self.assertIsNotNone(cell.get('s'))
                detail_numbers.append(Decimal(value(cell)))
                self.assertEqual(cells[f'H{index + 1}'].get('t'), 's')
            # These are real numeric cells: Excel SUM includes both the debt
            # and the separately presented unapplied credit.
            self.assertEqual(sum(detail_numbers), Decimal('950.00'))
            summary_header = next(index for index, row in enumerate(csv_rows) if row[:2] == ['Type', 'Account'])
            for index in range(summary_header + 1, summary_header + 3):
                for column in 'EFGHIJKL':
                    cell = cells[f'{column}{index + 1}']
                    self.assertIn(cell.get('t'), (None, 'n'))
                    self.assertEqual(Decimal(value(cell)), Decimal(csv_rows[index][ord(column) - ord('A')]))
            for literal in ('0007', '0008', '00042', '00123', '=1+1'):
                matches = [cell for cell in cells.values() if value(cell) == literal]
                self.assertTrue(matches, literal)
                self.assertTrue(all(cell.get('t') == 's' and cell.find('s:f', ns) is None for cell in matches), literal)
            self.assertIsNone(sheet.find('s:tableParts', ns))
            self.assertIsNone(sheet.find('s:autoFilter', ns))


class OpeningDueDateTests(unittest.TestCase):
    setUp = opening_fixtures.AccrualMigrationTests.setUp
    preview = opening_fixtures.AccrualMigrationTests.preview

    def test_standalone_aging_export_retains_coverage_and_review_issues(self):
        migration.apply(self.entity, self.preview())
        request = {'id': 'unsupported-extra-receivable', 'date': '2026-07-01', 'description': 'Synthetic support gap',
            'estimate_basis': 'Synthetic independently supported estimate', 'postings': [
                {'account': 'Assets:Receivable:Accounts-Receivable', 'amount': '10.00'},
                {'account': 'Income:Services', 'amount': '-10.00'}]}
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, request))
        package = workbook.generate_accountant_package(self.root, date(2026, 7, 1), date(2026, 7, 1), override=True,
            sheets=['receivables-payables-aging'])
        self.assertTrue(package.success, package.error)
        rows = list(csv.reader(StringIO(package.csv_files[0].read_text())))
        self.assertIn(['Report as of', '2026-07-01'], rows)
        self.assertIn(['Imported opening as of', '2026-06-30'], rows)
        self.assertIn(['Detailed activity begins', '2026-07-01'], rows)
        self.assertIn(['Imported fiscal summary', '2026-01-01', '2026-06-30'], rows)
        self.assertIn(['Historical coverage', 'Earlier monthly detail is unavailable'], rows)
        self.assertTrue(any(row and 'unexplained by supporting detail' in row[0] for row in rows))
        self.assertNotIn(['No outstanding review items'], rows)

    def test_opening_due_date_precedes_cutover_and_is_preserved_for_historical_aging(self):
        self.support['items'][0]['due_date'] = '2026-06-10'
        plan = self.preview()
        self.assertEqual(plan['status'], 'ready')
        migration.apply(self.entity, plan)
        row = next(row for row in reports.report(self.entity, 'aging', as_of='2026-07-01')['rows'] if row['id'] == self.support['items'][0]['id'])
        self.assertEqual(row['due_date'], '2026-06-10')
        self.assertEqual(row['days_past_due'], 21)
        self.assertEqual(row['as_of'], '2026-06-30')

    def test_invalid_opening_due_date_is_rejected_without_mutation(self):
        self.support['items'][0]['due_date'] = '2026-02-30'
        before = self.store.content_digest()
        with self.assertRaises(ValueError): self.preview()
        self.assertEqual(self.store.content_digest(), before)
