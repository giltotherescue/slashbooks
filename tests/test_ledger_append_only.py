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
    PostingMark,
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

    def rename_event(self, *pairs: tuple[str, str]) -> None:
        with self.store.transaction() as conn:
            self.store.append_audit_event("account-renamed", {"accounts": [list(p) for p in pairs]}, conn)

    def test_a_meta_record_does_not_unlock_a_relabel(self) -> None:
        from bookkeeping.ledger.store import RENAME_PENDING_KEY
        with self.store.transaction() as conn:
            self.store.set_meta(RENAME_PENDING_KEY, "{}", conn)
            conn.execute("INSERT INTO balance_assertions (date, account, amount) VALUES ('2026-02-01', 'Expenses:Software', '1')")
        self.refused("UPDATE postings SET account = 'Expenses:Other'")
        self.refused("UPDATE balance_assertions SET account = 'Expenses:Other'")

    def test_a_relabel_needs_the_newest_event_to_name_exactly_this_pair(self) -> None:
        self.rename_event(("Expenses:Software", "Expenses:Other"))
        self.refused("UPDATE postings SET account = 'Expenses:Different' WHERE account = 'Expenses:Software'")
        self.refused("UPDATE postings SET account = 'Expenses:Other', amount = '99' WHERE account = 'Expenses:Software'")
        with self.store.transaction() as conn:
            conn.execute("UPDATE postings SET account = 'Expenses:Other' WHERE account = 'Expenses:Software'")
        # Once another event is the newest, the same relabel is refused again.
        self.write("later")
        self.refused("UPDATE postings SET account = 'Expenses:Third' WHERE account = 'Expenses:Other'")
        self.rename_event(("Expenses:Other", "Expenses:Third"))
        self.refused("UPDATE postings SET account = 'Expenses:Software' WHERE account = 'Expenses:Other'")

    def test_the_unlock_needs_exactly_an_array_of_two_text_pairs(self) -> None:
        for accounts in ({"x": ["Expenses:Software", "Expenses:Other"]},
                         [["Expenses:Software", "Expenses:Other", "extra"]],
                         [["Expenses:Software"]],
                         [["Expenses:Software", 5]],
                         "Expenses:Software",
                         [{"0": "Expenses:Software", "1": "Expenses:Other"}]):
            with self.store.transaction() as conn:
                self.store.append_audit_event("account-renamed", {"accounts": accounts}, conn)
            self.refused("UPDATE postings SET account = 'Expenses:Other' WHERE account = 'Expenses:Software'")
        self.rename_event(("Expenses:Software", "Expenses:Other"))
        with self.store.transaction() as conn:
            conn.execute("UPDATE postings SET account = 'Expenses:Other' WHERE account = 'Expenses:Software'")

    def test_a_store_with_the_old_meta_keyed_triggers_gets_the_current_rule(self) -> None:
        from bookkeeping.ledger.store import RENAME_PENDING_KEY
        old_rule = (f"CREATE TRIGGER postings_append_only_update BEFORE UPDATE ON postings WHEN OLD.id IS NOT NEW.id "
                    "OR OLD.entry_id IS NOT NEW.entry_id OR OLD.amount IS NOT NEW.amount "
                    "OR OLD.currency IS NOT NEW.currency OR OLD.metadata_json IS NOT NEW.metadata_json "
                    f"OR (OLD.account IS NOT NEW.account AND NOT EXISTS (SELECT 1 FROM meta WHERE key = '{RENAME_PENDING_KEY}')) "
                    "BEGIN SELECT RAISE(ABORT, 'posted postings are append-only'); END")
        with self.store.connection() as conn:
            conn.execute("DROP TRIGGER postings_append_only_update")
            conn.execute(old_rule)
            conn.commit()
            self.assertTrue(append_only_guard_problems(conn))
            self.store.set_meta(RENAME_PENDING_KEY, "{}", conn)
            conn.commit()
            conn.execute("UPDATE postings SET account = 'Expenses:Other'")  # The old rule is weak.
            conn.rollback()
        self.store.initialize()
        with self.store.connection() as conn:
            self.assertEqual(append_only_guard_problems(conn), [])
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE postings SET account = 'Expenses:Other'")

    def test_an_incremental_check_that_sees_a_rename_event_requires_the_full_check(self) -> None:
        head = self.store.verify_audit_chain_since(None).head
        self.rename_event(("Expenses:Software", "Expenses:Other"))
        result = self.store.verify_audit_chain_since(head)
        self.assertEqual((result.errors, result.head, result.full_check_required), ([], None, True))
        again = self.store.verify_audit_chain_since(None)  # The full path takes a new head.
        self.assertEqual((again.errors, again.full_check_required), ([], False))
        self.assertIsNotNone(again.head)

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
        result = self.store.check_new_postings(PostingMark(0, ""))
        self.assertEqual((result.errors, result.watermark), ([], self.store.posting_watermark()))
        self.assertEqual(self.store.check_new_postings(result.watermark).watermark, result.watermark)
        with self.store.connection() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("INSERT INTO postings (entry_id, account, amount) VALUES (999, 'x', '1')")
            conn.commit()
        failed = self.store.check_new_postings(result.watermark)
        self.assertEqual((len(failed.errors), failed.watermark), (1, None))


class WatermarkAnchorTests(StoreCase):
    def test_a_watermark_above_the_newest_posting_is_an_error(self) -> None:
        for name in "abc":
            self.write(name)  # Six postings.
        self.assertEqual(self.store.posting_watermark().position, 6)
        for bad in (10000, 7, -1):
            result = self.store.check_new_postings(PostingMark(bad, "x"))
            self.assertEqual((len(result.errors), result.watermark), (1, None), bad)
        with self.assertRaises(TypeError):
            self.store.check_new_postings(6)  # A plain id is not anchored to a file.
        self.assertEqual(self.store.check_new_postings(self.store.posting_watermark()).errors, [])

    def test_a_restore_to_an_older_file_with_new_postings_and_an_orphan_fails_the_check(self) -> None:
        for name in "abcdef":
            self.write(name)  # 12 postings
        saved = self.store.check_new_postings(self.store.posting_watermark()).watermark
        self.assertEqual(saved.position, 12)
        older = LedgerStore(Path(self.tmp.name) / "older.sqlite")
        older.initialize()
        with older.transaction() as conn:
            older.insert_opens([Open(date(2000, 1, 1), "Assets:Bank:Checking", ("USD",)),
                                Open(date(2000, 1, 1), "Expenses:Software", ("USD",))], conn)
            for name in "abc":
                older.insert_entries([_entry(name)], conn)
        self.assertEqual(older.posting_watermark().position, 6)
        with older.connection() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            for n in range(7, 15):
                conn.execute("INSERT INTO postings (entry_id, account, amount) VALUES (?, 'Other', ?)",
                             (999 if n == 10 else 1, str(n)))
            conn.commit()
        self.assertEqual(older.posting_watermark().position, 14)
        result = older.check_new_postings(saved)
        self.assertTrue(result.errors, result)
        self.assertIsNone(result.watermark)
        self.assertTrue(older.check_new_postings(PostingMark(0, "")).errors)  # The orphan is also found from the start.

    def test_a_restore_with_the_same_posting_ids_but_other_content_fails_the_anchor(self) -> None:
        self.write("a")
        saved = self.store.posting_watermark()
        other = LedgerStore(Path(self.tmp.name) / "other.sqlite")
        other.initialize()
        with other.transaction() as conn:
            other.insert_opens([Open(date(2000, 1, 1), "Assets:Bank:Checking", ("USD",)),
                                Open(date(2000, 1, 1), "Expenses:Software", ("USD",))], conn)
            other.insert_entries([_entry("a", "11.00")], conn)
        result = other.check_new_postings(saved)
        self.assertEqual((len(result.errors), result.watermark), (1, None))

    def test_a_relabel_of_the_anchor_row_forces_the_full_check(self) -> None:
        self.write("a")
        saved = self.store.posting_watermark()
        self.raw("UPDATE postings SET account = 'Expenses:Other' WHERE id = 2")
        self.assertIsNone(self.store.check_new_postings(saved).watermark)


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


# The two texts of ``entries_append_only_no_overwrite``, frozen here on purpose. SCAN is the text that released
# engines install: its source_id lookup does not repeat the partial index condition, so it reads every entry. INDEXED
# repeats it. The tests below prove that both refuse exactly the same writes.
SCAN_NO_OVERWRITE = (
    "CREATE TRIGGER entries_append_only_no_overwrite BEFORE INSERT ON entries "
    "WHEN EXISTS (SELECT 1 FROM entries WHERE id = NEW.id) OR (NEW.source_id IS NOT NULL AND NEW.source_id != '' "
    "AND EXISTS (SELECT 1 FROM entries WHERE source_id = NEW.source_id)) "
    "BEGIN SELECT RAISE(ABORT, 'entries rows are append-only: no replace'); END"
)
INDEXED_NO_OVERWRITE = SCAN_NO_OVERWRITE.replace(
    "WHERE source_id = NEW.source_id)", "WHERE source_id = NEW.source_id AND source_id IS NOT NULL AND source_id != '')")
WEAK_NO_OVERWRITE = SCAN_NO_OVERWRITE.replace("WHERE id = NEW.id", "WHERE id = NEW.id AND 0")


def _use_trigger(conn: sqlite3.Connection, sql: str) -> None:
    conn.execute("DROP TRIGGER entries_append_only_no_overwrite")
    conn.execute(sql)
    conn.commit()


def _trigger_sql(conn: sqlite3.Connection, name: str = "entries_append_only_no_overwrite") -> str:
    return " ".join(conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)).fetchone()[0].split())


class _CountingStore(LedgerStore):
    """A store whose connections count the SQLite virtual machine steps, triggers included."""

    steps = 0

    def connect(self) -> sqlite3.Connection:
        conn = super().connect()

        def tick() -> int:
            _CountingStore.steps += 1
            return 0
        conn.set_progress_handler(tick, 1)
        return conn


DATE = "2026-01-15"
# Each insert that ``entries_append_only_no_overwrite`` refused before this change. Entry 1 has source_id 'a'.
REFUSED_INSERTS = (
    f"INSERT INTO entries (id, date, narration) VALUES (1, '{DATE}', 'x')",
    f"INSERT OR IGNORE INTO entries (id, date, narration) VALUES (1, '{DATE}', 'x')",
    f"INSERT OR REPLACE INTO entries (id, date, narration, source_id) VALUES (1, '{DATE}', 'x', 'new')",
    f"REPLACE INTO entries (id, date, narration) VALUES (1, '{DATE}', 'x')",
    f"INSERT INTO entries (id, date, narration) VALUES (1, '{DATE}', 'x') ON CONFLICT(id) DO UPDATE SET narration = 'y'",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 'a')",
    f"INSERT OR IGNORE INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 'a')",
    f"INSERT OR REPLACE INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 'a')",
    f"REPLACE INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 'b')",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 'a') "
    "ON CONFLICT(source_id) WHERE source_id IS NOT NULL AND source_id != '' DO UPDATE SET narration = 'y'",
    "INSERT INTO entries (date, narration, source_id) SELECT date, narration, source_id FROM entries WHERE source_id = 'b'",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 'n5')",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 'c'), ('{DATE}', 'y', 'c')",
)
# Inserts that both triggers allow: new ids, repeated NULL and empty ids, and ids that differ only in case or spaces.
ALLOWED_INSERTS = (
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 'c')",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', NULL)",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', '')",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 'A')",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 'a ')",
    f"INSERT INTO entries (date, narration) VALUES ('{DATE}', 'x')",
    f"INSERT INTO entries (id, date, narration, source_id) VALUES (100, '{DATE}', 'x', 'd')",
    f"INSERT OR REPLACE INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 'e')",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 'f'), ('{DATE}', 'y', 'g')",
)
# Values whose type differs from the stored text. The outcome is whatever the earlier trigger gave; both must agree.
EDGE_INSERTS = (
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 5)",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 5.0)",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', X'61')",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', X'')",
    f"INSERT OR REPLACE INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', 5)",
    f"INSERT INTO entries (date, narration, source_id) VALUES ('{DATE}', 'x', ' ')",
)


class NoOverwriteTriggerUsesTheIndexTests(unittest.TestCase):
    """The index form finds a source_id by the index, and it refuses exactly the writes that the scan form refuses."""

    def make_store(self, trigger: str) -> LedgerStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = LedgerStore(Path(tmp.name) / "ledger.sqlite")
        store.initialize()
        with store.connection() as conn:
            _use_trigger(conn, trigger)
            for source_id in ("a", "b", None, "", None, "", "5", "n5"):
                conn.execute("INSERT INTO entries (date, narration, source_id) VALUES (?, 'seed', ?)", (DATE, source_id))
            conn.commit()
        return store

    def outcome(self, store: LedgerStore, sql: str) -> tuple:
        with store.connection() as conn:
            try:
                conn.execute(sql)
            except sqlite3.Error as exc:
                result: tuple = (type(exc).__name__, str(exc))
            else:
                result = ("ok", [tuple(row) for row in conn.execute(
                    "SELECT id, narration, source_id, typeof(source_id) FROM entries ORDER BY id")])
            conn.rollback()
        return result

    def test_the_frozen_texts_are_the_engine_texts_and_share_the_index_condition(self) -> None:
        from bookkeeping.ledger.store import _DDL, _SOURCE_ID_INDEX_WHERE, _entries_no_overwrite, _normal_sql
        index = " ".join(_DDL.split("CREATE UNIQUE INDEX IF NOT EXISTS entries_source_id_unique", 1)[1].split(";")[0].split())
        self.assertTrue(index.endswith("WHERE " + _SOURCE_ID_INDEX_WHERE), index)
        self.assertEqual(_normal_sql(_entries_no_overwrite(indexed=False)), SCAN_NO_OVERWRITE)
        self.assertEqual(_normal_sql(_entries_no_overwrite(indexed=True)), INDEXED_NO_OVERWRITE)
        self.assertIn(f"source_id = NEW.source_id AND {_SOURCE_ID_INDEX_WHERE}", INDEXED_NO_OVERWRITE)

    def test_every_insert_has_the_same_outcome_with_the_scan_and_the_index_form(self) -> None:
        scan, indexed = self.make_store(SCAN_NO_OVERWRITE), self.make_store(INDEXED_NO_OVERWRITE)
        with scan.connection() as conn:
            self.assertEqual(_trigger_sql(conn), SCAN_NO_OVERWRITE)
        with indexed.connection() as conn:
            self.assertEqual(_trigger_sql(conn), INDEXED_NO_OVERWRITE)
        for sql in REFUSED_INSERTS + ALLOWED_INSERTS + EDGE_INSERTS:
            with self.subTest(sql=sql):
                self.assertEqual(self.outcome(indexed, sql), self.outcome(scan, sql))

    def test_both_forms_refuse_each_overwrite_and_allow_each_new_row(self) -> None:
        for trigger in (SCAN_NO_OVERWRITE, INDEXED_NO_OVERWRITE):
            store = self.make_store(trigger)
            for sql in REFUSED_INSERTS:
                with self.subTest(trigger=trigger[-60:], sql=sql):
                    self.assertEqual(self.outcome(store, sql),
                                     ("IntegrityError", "entries rows are append-only: no replace"))
            for sql in ALLOWED_INSERTS:
                with self.subTest(trigger=trigger[-60:], sql=sql):
                    self.assertEqual(self.outcome(store, sql)[0], "ok")

    def steps(self, rows: int, trigger: str) -> tuple[int, int]:
        """The VM steps of one entry insert and of one ``source_exists`` that finds nothing, at *rows* entries."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = _CountingStore(Path(tmp.name) / "ledger.sqlite")
        store.initialize()
        with store.transaction() as conn:
            conn.executemany("INSERT INTO entries (date, narration, source_id) VALUES (?, 'seed', ?)",
                             [(DATE, f"seed-{i}") for i in range(rows)])
        with store.connection() as conn:
            _use_trigger(conn, trigger)
            _CountingStore.steps = 0
            conn.execute("INSERT INTO entries (date, narration, source_id) VALUES (?, 'x', 'new')", (DATE,))
            insert = _CountingStore.steps
            conn.rollback()
        _CountingStore.steps = 0
        self.assertFalse(store.source_exists("missing"))
        return insert, _CountingStore.steps

    def test_the_index_form_and_source_exists_do_not_grow_with_the_number_of_entries(self) -> None:
        small_insert, small_lookup = self.steps(500, INDEXED_NO_OVERWRITE)
        large_insert, large_lookup = self.steps(8000, INDEXED_NO_OVERWRITE)
        # A read of every entry costs several steps for each row: thousands of steps at 8,000 entries.
        self.assertLess(large_insert - small_insert, 100, (small_insert, large_insert))
        self.assertLess(large_lookup - small_lookup, 100, (small_lookup, large_lookup))
        scan_small, _ = self.steps(500, SCAN_NO_OVERWRITE)
        scan_large, _ = self.steps(2500, SCAN_NO_OVERWRITE)
        self.assertGreater(scan_large - scan_small, 2000)  # The scan form reads every entry: the count does grow.

    def test_source_exists_gives_the_same_answers(self) -> None:
        store = self.make_store(SCAN_NO_OVERWRITE)
        with store.transaction() as conn:
            store.insert_source_transactions([{"id": "bank-1"}], conn)
        for source_id, expected in (("a", True), ("b", True), ("5", True), ("bank-1", True), ("A", False),
                                    ("missing", False), ("", True)):
            with self.subTest(source_id=source_id):
                self.assertIs(store.source_exists(source_id), expected)
        empty = self.make_store(SCAN_NO_OVERWRITE)
        with empty.connection() as conn:
            conn.execute("DROP TRIGGER entries_append_only_delete")
            conn.execute("DELETE FROM entries WHERE source_id = ''")
            conn.commit()
        self.assertFalse(empty.source_exists(""))


class SealedStoreCase(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("a")
        with self.store.transaction() as conn:
            self.store.append_audit_event("ledger-store-sealed", {
                "session_id": "test", "entries": 1, "store_sha256": self.store.content_digest(conn)}, conn)

    def state(self) -> tuple:
        from bookkeeping.ledger.importer import check_store_integrity
        with self.store.connection() as conn:
            events = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        return (self.store.content_digest(), self.store.audit_head(), events,
                self.store.verify_audit_chain(), check_store_integrity(self.store).status)

    def schema(self) -> tuple:
        with self.store.connection() as conn:
            return (conn.execute("PRAGMA schema_version").fetchone()[0],
                    sorted(tuple(row) for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")))

    def installs(self, action) -> int:
        from unittest import mock
        from bookkeeping.ledger import store as store_module
        real, calls = store_module.install_append_only_guard, []

        def spy(conn: sqlite3.Connection) -> None:
            calls.append(1)
            real(conn)
        with mock.patch.object(store_module, "install_append_only_guard", spy):
            action()
        return len(calls)


class AcceptedTriggerTextTests(SealedStoreCase):
    """Reader step: both texts are valid. A store moves between engines that install either one with no change."""

    def test_a_new_store_gets_the_scan_form_that_released_engines_install(self) -> None:
        with self.store.connection() as conn:
            self.assertEqual(_trigger_sql(conn), SCAN_NO_OVERWRITE)

    def test_a_store_with_either_text_opens_with_no_reinstall_and_no_schema_change(self) -> None:
        from bookkeeping.hosted_migration import _schema
        from bookkeeping.ledger.store import append_only_guard_outdated
        for trigger in (INDEXED_NO_OVERWRITE, SCAN_NO_OVERWRITE):
            with self.subTest(trigger=trigger[-60:]):
                with self.store.connection() as conn:
                    _use_trigger(conn, trigger)
                before, schema = self.state(), self.schema()
                self.assertEqual(before[3:], ([], "ok"))
                with self.store.connection() as conn:
                    self.assertEqual(append_only_guard_problems(conn), [])
                    self.assertFalse(append_only_guard_outdated(conn))
                    _schema(conn)  # The hosted migration check accepts it.
                self.assertEqual(self.installs(lambda: [self.store.initialize() for _ in range(3)]), 0)
                self.assertEqual(self.schema(), schema)  # Same text, same PRAGMA schema_version.
                self.assertEqual(self.state(), before)
                with self.store.connection() as conn:
                    self.assertEqual(_trigger_sql(conn), trigger)

    def test_a_changed_trigger_is_replaced_once_under_the_entity_write_lock(self) -> None:
        from unittest import mock
        from bookkeeping.ledger import importer, store as store_module
        with self.store.connection() as conn:
            _use_trigger(conn, WEAK_NO_OVERWRITE)
            self.assertEqual(append_only_guard_problems(conn),
                             ["Append-only trigger entries_append_only_no_overwrite was changed."])
        before = self.state()
        real, held = store_module.install_append_only_guard, []

        def spy(conn: sqlite3.Connection) -> None:
            held.append(Path(self.tmp.name).resolve() in importer._held_entity_locks.get())
            real(conn)
        with mock.patch.object(store_module, "install_append_only_guard", spy):
            for _ in range(3):
                self.store.initialize()
        self.assertEqual(held, [True])
        self.assertFalse((Path(self.tmp.name) / "entity.json").exists())  # The lock repairs no company file.
        with self.store.connection() as conn:
            self.assertEqual(_trigger_sql(conn), SCAN_NO_OVERWRITE)
            self.assertEqual(append_only_guard_problems(conn), [])
        self.assertEqual(self.state(), before)  # No row, no audit event and no seal changed.

    def test_a_failed_install_keeps_the_trigger_that_was_there(self) -> None:
        class Failing(sqlite3.Connection):
            def execute(self, sql, *args):  # type: ignore[override]
                if sql.startswith("CREATE TRIGGER IF NOT EXISTS entries_append_only_no_overwrite"):
                    raise sqlite3.OperationalError("disk I/O error")
                return super().execute(sql, *args)
        with self.store.connection() as conn:
            _use_trigger(conn, WEAK_NO_OVERWRITE)
        conn = sqlite3.connect(self.store.path, factory=Failing)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                install_append_only_guard(conn)
            self.assertFalse(conn.in_transaction)
        finally:
            conn.close()
        with self.store.connection() as conn:
            self.assertEqual(_trigger_sql(conn), WEAK_NO_OVERWRITE)

    def test_inside_a_caller_transaction_the_install_commits_nothing(self) -> None:
        with self.store.connection() as conn:
            _use_trigger(conn, WEAK_NO_OVERWRITE)
            conn.execute("BEGIN IMMEDIATE")
            install_append_only_guard(conn)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(_trigger_sql(conn), SCAN_NO_OVERWRITE)
            conn.rollback()
            self.assertEqual(_trigger_sql(conn), WEAK_NO_OVERWRITE)

    def test_a_read_only_store_keeps_its_trigger_and_still_opens(self) -> None:
        import os
        import stat
        with self.store.connection() as conn:
            _use_trigger(conn, WEAK_NO_OVERWRITE)
        path = self.store.path
        mode = path.stat().st_mode
        os.chmod(path, stat.S_IRUSR)
        self.addCleanup(os.chmod, path, mode)
        if os.access(path, os.W_OK):
            self.skipTest("this user can write a read-only file")
        self.store.initialize()
        with self.store.connection() as conn:
            self.assertEqual(_trigger_sql(conn), WEAK_NO_OVERWRITE)

    def test_the_hosted_migration_check_refuses_a_trigger_with_other_rules(self) -> None:
        from bookkeeping.hosted_migration import Rejected, _schema
        with self.store.connection() as conn:
            _use_trigger(conn, WEAK_NO_OVERWRITE)
            with self.assertRaises(Rejected):
                _schema(conn)
