"""Deterministic local races between supported workspace operations; synthetic data."""
from datetime import date
import json
from pathlib import Path
from threading import Event, Thread
import unittest
from unittest.mock import patch

from bookkeeping import compare, coverage, readiness, taxmap, tieout
from bookkeeping.connectors.csvsource import write_confirmed_mapping
from bookkeeping.entity import load_entity, record_source_coverage
from bookkeeping.ledger.importer import import_transactions
from bookkeeping.queue import confirm, propose
from bookkeeping.statement_extract import save_extracted_profile
import test_readiness as readiness_fixtures
import test_tieout as tieout_fixtures


class WorkspaceConcurrencyTests(unittest.TestCase):
    def fixture(self, cls):
        fixture = cls()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if hasattr(fixture, 'tearDown'):
            self.addCleanup(fixture.tearDown)
        return fixture

    def interleave(self, target, operation, writer):
        """Pause after an early read, then try a real writer in another thread."""
        paused, release, attempted, written = Event(), Event(), Event(), Event()
        results, errors = [], []
        original = target[0].__dict__[target[1]]

        def pause(*args, **kwargs):
            value = original(*args, **kwargs)
            paused.set()
            if not release.wait(5):
                raise AssertionError('Timed out releasing the snapshot reader.')
            return value

        def read():
            try:
                results.append(operation())
            except BaseException as exc:
                errors.append(exc)

        def write():
            attempted.set()
            try:
                writer()
            except BaseException as exc:
                errors.append(exc)
            finally:
                written.set()

        reader = Thread(target=read, daemon=True)
        mutation = Thread(target=write, daemon=True)
        with patch.object(*target, side_effect=pause):
            try:
                reader.start()
                self.assertTrue(paused.wait(5), 'the reader must reach the selected boundary')
                mutation.start()
                self.assertTrue(attempted.wait(5))
                serialized = not written.wait(0.15)
            finally:
                release.set()
                reader.join(5)
                if mutation.ident:
                    mutation.join(5)
        self.assertFalse(reader.is_alive())
        self.assertFalse(mutation.is_alive())
        if errors:
            raise errors[0]
        return results[0], serialized

    def test_tax_settings_and_statement_profile_keep_both_updates(self):
        f = self.fixture(readiness_fixtures.ReadinessTests)
        _, serialized = self.interleave(
            (taxmap, 'load_entity'),
            lambda: taxmap.update_settings(f.entity.path, return_form='1120s'),
            lambda: save_extracted_profile(f.entity.path, name='checking', ledger_account='Assets:Bank:Checking'),
        )
        config = json.loads((f.entity.path / 'entity.json').read_text())
        self.assertEqual(config['tax_handoff']['return_form'], '1120s')
        self.assertIn('statement:checking', config.get('csv_account_mappings', {}), 'a concurrent confirmed profile must survive')
        self.assertTrue(serialized, 'tax settings must lock before reading the configuration')

    def test_readiness_snapshot_excludes_a_concurrently_imported_source_until_next_read(self):
        f = self.fixture(readiness_fixtures.ReadinessTests)
        result, serialized = self.interleave(
            (readiness, '_open_work'), f.assess,
            lambda: import_transactions(f.entity, [{
                'id': 'new-review-source', 'date': '2026-09-16', 'amount': '-5.00',
                'description': 'Synthetic purchase', 'accountName': 'Checking', 'pending': False,
            }], session_id='concurrent-intake', session_date=date(2026, 10, 13)),
        )
        self.assertTrue(serialized, 'new unresolved work must wait for the complete readiness snapshot')
        self.assertEqual(result['state'], 'ready_for_review')
        self.assertEqual(f.assess()['state'], 'blocked')

    def test_tax_settings_and_source_coverage_keep_both_updates(self):
        f = self.fixture(readiness_fixtures.ReadinessTests)
        _, serialized = self.interleave(
            (taxmap, 'load_entity'),
            lambda: taxmap.update_settings(f.entity.path, return_form='1120s'),
            lambda: record_source_coverage(f.entity.path, 'Additional statement', '2026-08-01', '2026-08-31',
                                          account='Assets:Bank:Checking'),
        )
        config = json.loads((f.entity.path / 'entity.json').read_text())
        self.assertEqual(config['tax_handoff']['return_form'], '1120s')
        self.assertIn('Additional statement', config['source_coverage'])
        self.assertTrue(serialized, 'entity configuration writers must share the tax settings lock')

    def test_tax_settings_and_csv_mapping_keep_both_updates(self):
        f = self.fixture(readiness_fixtures.ReadinessTests)
        _, serialized = self.interleave(
            (taxmap, 'load_entity'),
            lambda: taxmap.update_settings(f.entity.path, return_form='1120s'),
            lambda: write_confirmed_mapping(f.entity.path, Path('new-checking.csv'),
                                            account_name='Checking', ledger_account='Assets:Bank:Checking'),
        )
        config = json.loads((f.entity.path / 'entity.json').read_text())
        self.assertEqual(config['tax_handoff']['return_form'], '1120s')
        self.assertIn('new-checking', config.get('csv_account_mappings', {}))
        self.assertTrue(serialized, 'CSV confirmation must share the configuration lock')

    def test_tax_settings_and_qbo_crosswalk_keep_both_updates(self):
        f = self.fixture(readiness_fixtures.ReadinessTests)
        _, serialized = self.interleave(
            (taxmap, 'load_entity'),
            lambda: taxmap.update_settings(f.entity.path, return_form='1120s'),
            lambda: compare.set_qbo_account_crosswalk(f.entity.path, 'Checking', 'Assets:Bank:Checking',
                                                     'Synthetic approved crosswalk'),
        )
        config = json.loads((f.entity.path / 'entity.json').read_text())
        self.assertEqual(config['tax_handoff']['return_form'], '1120s')
        self.assertEqual(config['compare']['qbo_account_crosswalk']['Checking']['local_account'], 'Assets:Bank:Checking')
        self.assertTrue(serialized, 'approved crosswalks must share the configuration lock')

    def pending_receipt(self):
        f = self.fixture(tieout_fixtures.BalanceCheckTests)
        entity = load_entity(f.company)
        import_transactions(entity, [{
            'id': 'pending-receipt', 'date': '2026-01-25', 'amount': '100.00',
            'description': 'Synthetic receipt', 'accountId': 'feed-chk', 'accountName': 'Checking', 'pending': False,
        }], session_id='receipt-intake', session_date=date(2026, 2, 1))
        propose(entity, 'pending-receipt', 'Income:Consulting', 'Synthetic receipt approval')
        return f, lambda: confirm(entity, 'pending-receipt', 'receipt-confirmed', learn=False)

    def test_bank_tieout_never_combines_old_postings_with_new_staging(self):
        f, writer = self.pending_receipt()
        result, serialized = self.interleave(
            (tieout, 'load_postings'), lambda: tieout.balance_check(f.company, [f.item('900.00')]), writer,
        )
        self.assertTrue(serialized, 'confirmation must wait until both postings and staging have been read')
        self.assertEqual(result['accounts'][0]['status'], 'waiting_in_review')
        self.assertEqual(tieout.balance_check(f.company, [f.item('900.00')])['accounts'][0]['status'], 'matches')

    def test_coverage_tieout_never_combines_old_postings_with_new_staging(self):
        f, writer = self.pending_receipt()
        coverage.record_statement(f.company, {
            'statement_id': 'jan-concurrent', 'account': tieout_fixtures.CHECKING, 'kind': 'statement',
            'period_start': '2026-01-01', 'period_end': '2026-01-31', 'opening_balance': '0.00', 'closing_balance': '900.00',
        })
        result, serialized = self.interleave(
            (tieout, 'load_postings'),
            lambda: coverage.check_coverage(f.company, date(2026, 1, 1), date(2026, 1, 31)), writer,
        )
        self.assertTrue(serialized, 'coverage must read all tieout inputs in one snapshot')
        row = next(row for row in result['accounts'] if row['account'] == tieout_fixtures.CHECKING)
        self.assertEqual(row['statements'][0]['tieout']['waiting_rows'], 1)

    def test_statement_replacement_waits_for_the_entire_coverage_snapshot(self):
        f = self.fixture(tieout_fixtures.BalanceCheckTests)
        record = {
            'statement_id': 'jan-replacement', 'account': tieout_fixtures.CHECKING, 'kind': 'statement',
            'period_start': '2026-01-01', 'period_end': '2026-01-31', 'opening_balance': '0.00', 'closing_balance': '800.00',
        }
        coverage.record_statement(f.company, record)
        result, serialized = self.interleave(
            (coverage, 'load_statements'),
            lambda: coverage.check_coverage(f.company, date(2026, 1, 1), date(2026, 1, 31)),
            lambda: coverage.record_statement(f.company, {**record, 'closing_balance': '900.00'}, replace=True),
        )
        self.assertTrue(serialized, 'statement writers must share the complete coverage snapshot lock')
        row = next(row for row in result['accounts'] if row['account'] == tieout_fixtures.CHECKING)
        self.assertEqual(row['statements'][0]['tieout']['status'], 'matches')
        after = coverage.check_coverage(f.company, date(2026, 1, 1), date(2026, 1, 31))
        row = next(row for row in after['accounts'] if row['account'] == tieout_fixtures.CHECKING)
        self.assertEqual(row['statements'][0]['tieout']['status'], 'differs')

    def test_first_balance_snapshot_only_creates_the_empty_operational_lock(self):
        f = self.fixture(tieout_fixtures.BalanceCheckTests)
        before = {p.relative_to(f.company): p.read_bytes() for p in f.company.rglob('*') if p.is_file()}
        self.assertNotIn(Path('.books.lock'), before)
        tieout.balance_check(f.company, [f.item('800.00')])
        after = {p.relative_to(f.company): p.read_bytes() for p in f.company.rglob('*') if p.is_file()}
        self.assertEqual(after.pop(Path('.books.lock')), b'')
        self.assertEqual(after, before, 'acquiring the snapshot lock must not modify accounting state')

    def test_missing_company_read_does_not_scaffold_a_directory(self):
        f = self.fixture(tieout_fixtures.BalanceCheckTests)
        missing = f.company / 'missing-company'
        with self.assertRaises(FileNotFoundError):
            tieout.balance_check(missing, [])
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
