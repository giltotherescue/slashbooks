"""Cheap per-write guarantees: append-only triggers, incremental chain check, new-posting foreign key check.

A host that runs the engine for each write can check the change, not the whole book. The full checks
(``verify_audit_chain``, ``PRAGMA integrity_check``, ``PRAGMA foreign_key_check``) stay unchanged for scheduled runs.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_reporting_goals import BANK, CONSULTING, CompanyFixture, entry  # noqa: E402

from bookkeeping.ledger.importer import check_integrity, reverse_entry  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import (  # noqa: E402
    LedgerStore,
    append_only_guard_problems,
    install_append_only_guard,
)


def _entry(source_id: str, amount: str = "10.00") -> Entry:
    value = Decimal(amount)
    return Entry(date=date(2026, 1, 15), narration="Test", meta=(("source-id", source_id),),
                 postings=(Posting("Expenses:Software", value), Posting("Assets:Bank:Checking", -value)))


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LedgerStore(Path(self.tmp.name) / "ledger.sqlite")
        self.store.initialize()
        with self.store.transaction() as conn:
            self.store.insert_opens([Open(date(2000, 1, 1), "Assets:Bank:Checking", ("USD",)),
                                     Open(date(2000, 1, 1), "Expenses:Software", ("USD",))], conn)

    def write(self, source_id: str) -> None:
        with self.store.transaction() as conn:
            self.store.insert_entries([_entry(source_id)], conn)
            self.store.append_audit_event("entry-written", {"source_id": source_id}, conn)

    def raw(self, sql: str, *args) -> None:
        """Run SQL as a person with direct database access, with the triggers removed first."""
        with self.store.connection() as conn:
            for name in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]:
                conn.execute(f"DROP TRIGGER {name}")
            conn.execute(sql, args)
            conn.commit()


class IncrementalChainTests(StoreCase):
    def test_incremental_check_walks_only_new_links_and_returns_the_new_head(self) -> None:
        self.assertIsNone(self.store.audit_head())
        first = self.store.verify_audit_chain_since(None)
        self.assertEqual((first.errors, first.head), ([], None))
        self.write("a")
        self.write("b")
        head = self.store.verify_audit_chain_since(None).head
        self.assertEqual(head, self.store.audit_head())
        self.write("c")
        self.write("d")
        result = self.store.verify_audit_chain_since(head)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.head.position, head.position + 2)
        self.assertEqual(self.store.verify_audit_chain_since(result.head).head, result.head)

    def test_tampered_new_link_is_caught_by_the_incremental_check(self) -> None:
        self.write("a")
        head = self.store.audit_head()
        self.write("b")
        self.write("c")
        self.raw("UPDATE audit_events SET payload_json = ? WHERE id = ?", '{"source_id":"x"}', head.position + 1)
        result = self.store.verify_audit_chain_since(head)
        self.assertTrue(any(f"Audit event {head.position + 1}:" in e for e in result.errors), result.errors)
        self.assertIsNone(result.head)

    def test_tampered_middle_link_is_caught_by_the_full_check(self) -> None:
        for name in "abcd":
            self.write(name)
        self.assertEqual(self.store.verify_audit_chain(), [])
        self.raw("UPDATE audit_events SET payload_json = ? WHERE id = 2", '{"source_id":"x"}')
        self.assertTrue(any("Audit event 2:" in e for e in self.store.verify_audit_chain()))

    def test_incremental_check_does_not_read_below_the_verified_head(self) -> None:
        # The documented limit: a change below the verified head is the job of the triggers and the full check.
        for name in "abc":
            self.write(name)
        head = self.store.audit_head()
        self.write("d")
        self.raw("UPDATE audit_events SET payload_json = ? WHERE id = 1", '{"source_id":"x"}')
        self.assertEqual(self.store.verify_audit_chain_since(head).errors, [])
        self.assertTrue(self.store.verify_audit_chain())
        self.assertTrue(append_only_guard_problems(sqlite3.connect(self.store.path)))

    def test_a_head_that_changed_or_vanished_is_an_error(self) -> None:
        self.write("a")
        head = self.store.audit_head()
        self.assertTrue(self.store.verify_audit_chain_since(type(head)(head.position, "0" * 64)).errors)
        self.assertTrue(self.store.verify_audit_chain_since(type(head)(head.position + 5, head.record_hash)).errors)


class AppendOnlyTriggerTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("a")

    def refused(self, sql: str, *args) -> None:
        with self.store.connection() as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only|extend the newest"):
                conn.execute(sql, args)

    def test_update_of_a_posted_row_is_refused_by_the_database(self) -> None:
        self.refused("UPDATE entries SET narration = 'changed'")
        self.refused("UPDATE entries SET source_id = 'z'")
        self.refused("UPDATE postings SET amount = '99.00'")
        self.refused("UPDATE postings SET account = 'Expenses:Software'")  # An account change needs a pending rename.
        self.refused("UPDATE audit_events SET payload_json = '{}'")
        with self.store.transaction() as conn:
            self.store.insert_balances([], conn)
            conn.execute("INSERT INTO balance_assertions (date, account, amount) VALUES ('2026-02-01', 'Assets:Bank:Checking', '1')")
        self.refused("UPDATE balance_assertions SET amount = '2'")

    def test_delete_of_a_posted_row_is_refused_by_the_database(self) -> None:
        self.refused("DELETE FROM entries")
        self.refused("DELETE FROM postings")
        self.refused("DELETE FROM audit_events")
        self.assertEqual(self.store.counts().entries, 1)

    def test_an_audit_event_must_extend_the_newest_record(self) -> None:
        self.refused("INSERT INTO audit_events (type, ts, payload_json, prev_hash, record_hash) "
                     "VALUES ('x', '2026-01-01T00:00:00Z', '{}', ?, ?)", "1" * 64, "2" * 64)

    def test_the_account_of_a_posting_can_change_only_while_a_rename_is_pending(self) -> None:
        from bookkeeping.ledger.store import RENAME_PENDING_KEY
        with self.store.transaction() as conn:
            self.store.set_meta(RENAME_PENDING_KEY, "{}", conn)
            conn.execute("UPDATE postings SET account = 'Expenses:Other' WHERE account = 'Expenses:Software'")
        self.refused("UPDATE postings SET amount = '99.00'")  # Only the account may change, even then.

    def test_the_correction_path_still_works(self) -> None:
        # A correction reverses and replaces: it adds entries and never edits the original.
        with self.store.transaction() as conn:
            self.store.insert_entries([_entry("a-reversal", "-10.00"), _entry("a-fixed", "12.00")], conn)
            self.store.append_audit_event("entry-written", {"source_id": "a-fixed"}, conn)
        self.assertEqual(self.store.counts().entries, 3)
        self.assertEqual(self.store.verify_audit_chain(), [])

    def test_the_newest_feed_intake_event_can_still_be_discarded(self) -> None:
        with self.store.transaction() as conn:
            record_hash = self.store.append_audit_event("feed-intake", {"rows": 1}, conn)
        self.assertTrue(self.store._discard_last_audit_event("feed-intake", record_hash))
        self.assertEqual(self.store.verify_audit_chain(), [])

    def test_guard_check_reports_a_missing_or_changed_trigger_and_install_repairs_it(self) -> None:
        with self.store.connection() as conn:
            self.assertEqual(append_only_guard_problems(conn), [])
            conn.execute("DROP TRIGGER entries_append_only_update")
            conn.execute("DROP TRIGGER postings_append_only_delete")
            conn.execute("CREATE TRIGGER postings_append_only_delete BEFORE DELETE ON postings BEGIN SELECT 1; END")
            problems = append_only_guard_problems(conn)
            self.assertEqual(len(problems), 2, problems)
            conn.execute("DROP TRIGGER postings_append_only_delete")
            install_append_only_guard(conn)
            self.assertEqual(append_only_guard_problems(conn), [])

    def test_an_older_store_gets_the_triggers_when_it_is_opened_for_writing(self) -> None:
        self.raw("DELETE FROM meta WHERE key = 'never'")  # Removes every trigger.
        with self.store.connection() as conn:
            self.assertTrue(append_only_guard_problems(conn))
        self.store.initialize()
        with self.store.connection() as conn:
            self.assertEqual(append_only_guard_problems(conn), [])

    def test_triggers_change_no_content_digest(self) -> None:
        digest = self.store.content_digest()
        self.raw("DELETE FROM meta WHERE key = 'never'")
        self.assertEqual(self.store.content_digest(), digest)


class ReplaceAndLowIdTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("a")
        self.write("b")

    def refused(self, sql: str, *args) -> None:
        with self.store.connection() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(sql, args)
            conn.rollback()

    def test_replace_forms_are_refused_for_every_posted_table(self) -> None:
        entry_cols = "(id, date, narration, source_id) VALUES (1, '2026-01-01', 'x', 'new')"
        self.refused("INSERT OR REPLACE INTO entries " + entry_cols)
        self.refused("REPLACE INTO entries " + entry_cols)
        self.refused("INSERT OR REPLACE INTO entries (date, narration, source_id) VALUES ('2026-01-01', 'x', 'a')")
        self.refused("INSERT INTO entries (id, date, narration) VALUES (1, '2026-01-01', 'x') "
                     "ON CONFLICT(id) DO UPDATE SET narration = 'changed'")
        self.refused("INSERT INTO entries (date, narration, source_id) VALUES ('2026-01-01', 'x', 'a') "
                     "ON CONFLICT(source_id) WHERE source_id IS NOT NULL AND source_id != '' DO UPDATE SET narration = 'changed'")
        self.refused("REPLACE INTO postings (id, entry_id, account, amount) VALUES (1, 1, 'Expenses:Software', '99')")
        self.refused("INSERT OR REPLACE INTO postings (id, entry_id, account, amount) VALUES (1, 1, 'Expenses:Software', '99')")
        self.refused("INSERT INTO postings (id, entry_id, account, amount) VALUES (1, 1, 'Expenses:Software', '99') "
                     "ON CONFLICT(id) DO UPDATE SET amount = '99'")
        self.refused("REPLACE INTO audit_events (id, type, ts, payload_json, prev_hash, record_hash) "
                     "VALUES (1, 'x', 't', '{}', ?, 'h')", "0" * 64)
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO balance_assertions (date, account, amount) VALUES ('2026-02-01', 'a', '1')")
        self.refused("REPLACE INTO balance_assertions (id, date, account, amount) VALUES (1, '2026-01-01', 'a', '1')")
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT narration FROM entries WHERE id = 1").fetchone()[0], "Test")
            self.assertEqual(conn.execute("SELECT amount FROM postings WHERE id = 1").fetchone()[0], "10.00")

    def test_replace_is_refused_even_with_recursive_triggers_off_and_on(self) -> None:
        for setting in ("OFF", "ON"):
            with self.store.connection() as conn:
                conn.execute(f"PRAGMA recursive_triggers={setting}")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("REPLACE INTO postings (id, entry_id, account, amount) VALUES (1, 1, 'x', '1')")

    def test_a_low_id_insert_is_refused_so_a_saved_position_cannot_skip_it(self) -> None:
        self.raw("DELETE FROM postings WHERE id = 1")
        install = self.store.connection()
        with install as conn:
            install_append_only_guard(conn)
            conn.commit()
            conn.execute("INSERT INTO postings (id, entry_id, account, amount) VALUES (50, 2, 'Expenses:Software', '1')")
            conn.commit()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "ids must rise"):
                conn.execute("INSERT INTO postings (id, entry_id, account, amount) VALUES (1, 2, 'Expenses:Software', '1')")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "ids must rise"):
                conn.execute("INSERT INTO postings (id, entry_id, account, amount) VALUES (-5, 2, 'Expenses:Software', '1')")
            conn.rollback()
        head = self.store.audit_head()
        with self.store.connection() as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "extend the newest|ids must rise"):
                conn.execute("INSERT INTO audit_events (id, type, ts, payload_json, prev_hash, record_hash) "
                             "VALUES (1, 'x', 't', '{}', 'bogus', 'bogus')")
        self.assertEqual(self.store.audit_head(), head)


class CheckpointTests(StoreCase):
    def test_a_failed_check_returns_no_head_and_no_watermark(self) -> None:
        self.write("a")
        self.raw("UPDATE audit_events SET payload_json = '{}' WHERE id = 1")
        self.assertIsNone(self.store.verify_audit_chain_since(None).head)
        self.assertIsNotNone(self.store.verify_audit_chain_since(None).errors)
        self.assertIsNone(self.store.verify_audit_chain_since(self.store.audit_head().__class__(99, "x")).head)

    def test_the_head_is_the_last_event_that_the_check_read(self) -> None:
        self.write("a")
        first = self.store.verify_audit_chain_since(None)
        self.write("b")
        again = self.store.verify_audit_chain_since(first.head)
        self.assertEqual((again.errors, again.head.position), ([], first.head.position + 1))

    def test_the_posting_watermark_comes_from_the_rows_that_the_check_read(self) -> None:
        self.write("a")
        result = self.store.check_new_postings(0)
        self.assertEqual((result.errors, result.watermark), ([], self.store.posting_watermark()))
        self.assertEqual(self.store.check_new_postings(result.watermark).watermark, result.watermark)
        with self.store.connection() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("INSERT INTO postings (entry_id, account, amount) VALUES (999, 'x', '1')")
            conn.commit()
        failed = self.store.check_new_postings(result.watermark)
        self.assertEqual((len(failed.errors), failed.watermark), (1, None))


class NewPostingForeignKeyTests(StoreCase):
    def test_only_new_postings_are_checked_and_an_orphan_is_found(self) -> None:
        self.write("a")
        mark = self.store.posting_watermark()
        self.assertEqual(self.store.new_posting_foreign_key_errors(mark), [])
        self.write("b")
        self.assertEqual(self.store.new_posting_foreign_key_errors(mark), [])
        with self.store.connection() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("INSERT INTO postings (entry_id, account, amount) VALUES (999, 'Expenses:Software', '1')")
            conn.commit()
        errors = self.store.new_posting_foreign_key_errors(mark)
        self.assertEqual(len(errors), 1)
        self.assertIn("entry 999", errors[0])
        self.assertEqual(self.store.new_posting_foreign_key_errors(self.store.posting_watermark()), [])
        with self.store.connection() as conn:  # The full check agrees.
            self.assertEqual(len(conn.execute("PRAGMA foreign_key_check").fetchall()), 1)


class CorrectionPathTests(CompanyFixture):
    def test_reverse_entry_still_works_and_the_original_row_is_not_rewritten(self) -> None:
        self.post(entry("a", "2026-02-03", "100.00"))
        store = LedgerStore(self.root / "ledger.sqlite")
        head = store.audit_head()
        mark = store.posting_watermark()
        with store.connection() as conn:
            self.assertEqual(append_only_guard_problems(conn), [])
            before = conn.execute("SELECT * FROM entries WHERE source_id = 'a'").fetchone()
        reverse_entry(self.entity, "a", "undo")
        self.assertEqual(store.counts().entries, 2)
        check = store.verify_audit_chain_since(head)
        self.assertEqual(check.errors, [])
        self.assertGreater(check.head.position, head.position)
        self.assertEqual(store.new_posting_foreign_key_errors(mark), [])
        with store.connection() as conn:
            self.assertEqual(tuple(conn.execute("SELECT * FROM entries WHERE source_id = 'a'").fetchone()), tuple(before))
        self.assertEqual(check_integrity(self.entity).status, "ok")
        self.assertEqual(store.verify_audit_chain(), [])


if __name__ == "__main__":
    unittest.main()
