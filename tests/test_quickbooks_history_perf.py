"""One audit read and one ledger read for a whole intake call, the audit-type index, and the held list in a backup.
Synthetic data only."""
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from bookkeeping import migration_bundle
from bookkeeping import quickbooks_history as qh
from bookkeeping.ledger.store import LedgerStore, default_store_path
from bookkeeping.quickbooks_history import list_buffer_decisions

from test_quickbooks_history_second_rows import SecondRowCases


class PerfCases(SecondRowCases):
    def counted(self, run):
        counts = {'audit': 0, 'entries': 0}
        real_audit, real_entries = qh._audit, LedgerStore.load_entries

        def audit(*args, **kwargs):
            counts['audit'] += 1
            return real_audit(*args, **kwargs)

        def entries(store, *args, **kwargs):
            if 'conn' not in kwargs:
                counts['entries'] += 1
            return real_entries(store, *args, **kwargs)
        with mock.patch.object(qh, '_audit', audit), mock.patch.object(LedgerStore, 'load_entries', entries):
            run()
        return counts

    def test_an_intake_call_reads_the_audit_log_and_the_ledger_a_fixed_number_of_times(self):
        self.matched('2026-03-04')
        few = self.counted(lambda: self.ingest(*[self.row('a%d' % i, '2026-03-20', '%d.07' % (i + 1)) for i in range(20)]))
        many = self.counted(lambda: self.ingest(*[self.row('b%d' % i, '2026-03-21', '%d.09' % (i + 1)) for i in range(200)]))
        self.assertEqual(few, many)
        self.assertLessEqual(many['audit'], 6)
        self.assertLessEqual(many['entries'], 3)

    def test_held_rows_in_one_intake_call_do_not_multiply_the_reads(self):
        self.matched('2026-03-04')
        few = self.counted(lambda: self.ingest(*[self.row('h%d' % i, '2026-03-04', '30') for i in range(5)]))
        many = self.counted(lambda: self.ingest(*[self.row('k%d' % i, '2026-03-04', '30') for i in range(60)]))
        self.assertEqual(few, many)
        self.assertEqual(len(list_buffer_decisions(self.entity)), 65)

    def test_a_statement_file_reads_the_audit_log_and_the_ledger_a_fixed_number_of_times(self):
        self.matched('2026-03-04')
        one = self.counted(lambda: self.statement(('2026-03-20', 'ONE', '1.07')))
        ten = self.counted(lambda: self.statement(*[('2026-03-21', 'ROW %d' % i, '%d.11' % (i + 1)) for i in range(10)]))
        self.assertEqual(one, ten)

    # the audit index

    def test_an_older_store_without_the_audit_type_index_gets_it_and_keeps_its_seal(self):
        self.history(self.DEPOSIT)
        path = default_store_path(self.entity.path)
        with sqlite3.connect(path) as conn:
            conn.execute('DROP INDEX IF EXISTS audit_events_type_idx')
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='audit_events_type_idx'").fetchone())
        store = LedgerStore(path)
        before = store.content_digest()
        store.initialize()
        with sqlite3.connect(path) as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='audit_events_type_idx'").fetchone())
        self.assertEqual(store.content_digest(), before)
        from bookkeeping.ledger.importer import check_store_integrity
        self.assertEqual(check_store_integrity(store).status, 'ok')

    # the held list in a backup

    def test_the_held_list_is_carried_by_the_company_snapshot(self):
        from bookkeeping import hosted_migration
        self.matched('2026-03-04')
        self.ingest(self.row('second', '2026-03-04', '30'))
        held_file = self.entity.staging_dir / 'duplicate-candidates.json'
        held = held_file.read_bytes()
        destination = Path(self.temp.name).resolve() / 'snapshot.zip'
        result = hosted_migration.build_bundle(self.entity.path, destination)
        self.assertEqual(result['status'], 'exported', result)
        bundle = migration_bundle.load_bundle(destination)
        self.assertEqual(bundle['files']['staging/duplicate-candidates.json'], held)
        self.assertIn('ledger.sqlite', bundle['files'])
        # The audit log with the decisions is in the ledger file, so the decisions and the held rows travel together.
        restored = Path(self.temp.name) / 'restored.sqlite'
        restored.write_bytes(bundle['files']['ledger.sqlite'])
        with sqlite3.connect(restored) as conn:
            kinds = {row[0] for row in conn.execute('SELECT type FROM audit_events')}
        self.assertIn('qbo-buffer-decision', kinds)
        self.assertIn('qbo-history-recorded', kinds)


for _name in [n for n in dir(SecondRowCases) if n.startswith('test_') and n not in PerfCases.__dict__]:
    setattr(PerfCases, _name, None)


class AccrualPerfTests(PerfCases, unittest.TestCase):
    basis = 'Accrual'


class CashPerfTests(PerfCases, unittest.TestCase):
    basis = 'Cash'


if __name__ == '__main__':
    unittest.main()
