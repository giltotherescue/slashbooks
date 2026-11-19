"""Tests for the incremental change set capture (``bookkeeping.change_capture``).

All data is synthetic. The incremental capture must give the document that a comparison of two full reads gives. The
tests run the same writes under both and compare the whole documents, then break the assumptions of the fast path
(a write that does not use a helper, a log that is gone, a stale index) and check that the document stays the same.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from bookkeeping import capture_log, change_capture, changeset, queue  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.ledger.staging import StagingStore  # noqa: E402
from bookkeeping.ledger.store import append_only_guard_problems  # noqa: E402
from test_changeset import IMPORT, Sequence_, run_cli, scenario  # noqa: E402


def capture_objects(entity: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(entity / "ledger.sqlite")) as conn:
        return [(row[0], row[1]) for row in conn.execute("SELECT type, name FROM sqlite_master")
                if row[1].startswith(change_capture.TRIGGER_PREFIX)]


def ledger_schema(entity: Path) -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(entity / "ledger.sqlite")) as conn:
        return sorted((str(a), str(b), str(c)) for a, b, c in conn.execute("SELECT type, name, sql FROM sqlite_master"))


class Dual(Sequence_):
    """Run each write under the incremental capture and, around it, under a capture of two full reads."""

    def cli(self, name, *argv, expect=0, revision=None):
        with changeset.capture(self.entity, books_revision=revision, incremental=False) as full:
            document = super().cli(name, *argv, expect=expect, revision=revision)
        if expect == 0:
            self.test.assertEqual(document, full.change_set, f"{name}: incremental and full capture differ")
        return document

    def api(self, name, call):
        with changeset.capture(self.entity, incremental=False) as full:
            document = super().api(name, call)
        self.test.assertEqual(document, full.change_set, f"{name}: incremental and full capture differ")
        return document


class Equivalence(unittest.TestCase):
    def test_every_step_of_the_change_set_scenario_gives_the_same_document_as_a_full_comparison(self) -> None:
        import test_changeset
        with mock.patch.object(test_changeset, "Sequence_", Dual):
            with tempfile.TemporaryDirectory() as tmp:
                seq = scenario(self, Path(tmp))
        self.assertEqual(len(seq.sets), 15)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.seq = Sequence_(self, self.root)
        self.entity = self.seq.entity
        self.seq.cli("init", "entity", "init", str(self.entity), "--name", "Synthetic Studio")
        self.seq.ingest("import", IMPORT)
        self.seq.cli("propose", "queue", "propose", "--entity", str(self.entity), "--source-id", "cs-001",
                     "--category", "Expenses:Software", "--reasoning", "Tool.")
        self.seq.cli("approve", "queue", "confirm", "--entity", str(self.entity), "--item", "cs-001")

    def both(self, action, apply=True):
        """Run ``action`` under both captures. Returns the incremental document after it equals the full one."""
        with changeset.capture(self.entity, incremental=False) as full:
            with changeset.capture(self.entity) as fast:
                action()
        self.assertEqual(fast.change_set, full.change_set)
        self.assertTrue(fast.change_set["complete"])
        if apply:
            self.seq._apply("step", fast.change_set)
        return fast.change_set


class FallbackTests(Fixture):
    """A write that does not use a helper must give the document of a full comparison."""

    def test_a_write_to_seen_ids_that_bypasses_the_helper(self) -> None:
        path = self.entity / "staging" / "seen-ids.json"
        seen = json.loads(path.read_text(encoding="utf-8"))
        document = self.both(lambda: path.write_text(json.dumps(seen + ["bypass-1"]), encoding="utf-8"))
        self.assertEqual(document["staging"]["seen_ids"]["added"], ["bypass-1"])

    def test_a_bypass_after_a_journaled_write_to_seen_ids_is_found(self) -> None:
        path = self.entity / "staging" / "seen-ids.json"

        def action() -> None:
            StagingStore(self.entity / "staging").mark_seen("through-helper")
            rows = json.loads(path.read_text(encoding="utf-8"))
            path.write_text(json.dumps(rows + ["bypass-2"]), encoding="utf-8")

        document = self.both(action)
        self.assertEqual(document["staging"]["seen_ids"]["added"], ["bypass-2", "through-helper"])

    def test_a_bypass_before_a_journaled_write_to_seen_ids_is_found(self) -> None:
        path = self.entity / "staging" / "seen-ids.json"

        def action() -> None:
            rows = json.loads(path.read_text(encoding="utf-8"))
            store = StagingStore(self.entity / "staging")
            path.write_text(json.dumps(rows + ["bypass-3"]), encoding="utf-8")
            store.mark_seen("through-helper")  # The store is stale: its write drops bypass-3.

        document = self.both(action)
        self.assertEqual(document["staging"]["seen_ids"]["added"], ["through-helper"])

    def test_a_bypass_after_a_journaled_pending_removal_is_found(self) -> None:
        path = self.entity / "staging" / "pending-categorization.json"
        company = load_entity(self.entity)

        def action() -> None:
            queue._remove_from_pending_categorization(company, "cs-002")
            rows = json.loads(path.read_text(encoding="utf-8"))
            rows.append({"id": "bypass-4", "date": "2026-10-01", "amount": "-1.00"})
            rows[0]["description"] = "EDITED"
            path.write_text(json.dumps(rows), encoding="utf-8")

        document = self.both(action)
        pending = document["staging"]["pending"]
        self.assertEqual(pending["removed"], ["cs-002"])
        self.assertIn("bypass-4", [row["id"] for row in pending["added"]])
        self.assertEqual(len(pending["updated"]), 1)

    def test_a_journaled_pending_removal_does_not_read_the_file_after_the_command(self) -> None:
        self.both(lambda: None)
        company = load_entity(self.entity)
        with mock.patch.object(changeset, "_load_keyed", side_effect=AssertionError("pending list was read")):
            with changeset.capture(self.entity) as receipt:
                queue._remove_from_pending_categorization(company, "cs-003")
        self.assertEqual(receipt.change_set["staging"]["pending"]["removed"], ["cs-003"])

    def test_the_helper_journal_gives_the_added_ids_without_a_read_of_the_file(self) -> None:
        self.both(lambda: None)  # Build the index.
        with mock.patch.object(changeset, "_load_ids", side_effect=AssertionError("seen-ids.json was read")):
            with changeset.capture(self.entity) as receipt:
                StagingStore(self.entity / "staging").mark_seen("cheap-1")
        self.assertEqual(receipt.change_set["staging"]["seen_ids"]["added"], ["cheap-1"])

    def test_an_in_place_edit_of_a_review_item(self) -> None:
        self.seq.cli("propose", "queue", "propose", "--entity", str(self.entity), "--source-id", "cs-002",
                     "--category", "Income:Consulting", "--reasoning", "Payout.")
        path = self.entity / "review-queue" / "cs-002.json"
        item = json.loads(path.read_text(encoding="utf-8"))
        item["reasoning"] = "Edited by hand."
        document = self.both(lambda: path.write_text(json.dumps(item, indent=2, sort_keys=True), encoding="utf-8"))
        self.assertEqual([row["id"] for row in document["review_queue"]], ["cs-002"])

    def test_a_new_and_a_removed_review_item_file(self) -> None:
        self.seq.cli("propose", "queue", "propose", "--entity", str(self.entity), "--source-id", "cs-002",
                     "--category", "Income:Consulting", "--reasoning", "Payout.")
        folder = self.entity / "review-queue"
        item = json.loads((folder / "cs-002.json").read_text(encoding="utf-8"))
        item["source_id"] = "cs-009"

        def action() -> None:
            (folder / "cs-009.json").write_text(json.dumps(item), encoding="utf-8")
            (folder / "cs-002.json").unlink()

        document = self.both(action)
        self.assertEqual([(row["id"], row["change"]) for row in document["review_queue"]],
                         [("cs-002", "removed"), ("cs-009", "created")])

    def test_two_item_files_with_one_source_id_are_read_in_full(self) -> None:
        folder = self.entity / "review-queue"
        item = json.loads((folder / "cs-001.json").read_text(encoding="utf-8"))
        item["status"] = "open"
        self.both(lambda: (folder / "zz-copy.json").write_text(json.dumps(item), encoding="utf-8"), apply=False)

    def test_a_direct_sql_write_that_does_not_use_the_ledger_store(self) -> None:
        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                conn.execute("UPDATE source_transactions SET status = 'voided' WHERE id = 'cs-001'")
                conn.execute("INSERT OR REPLACE INTO source_transactions (id, payload_json, status, imported_at) "
                             "VALUES ('cs-001x', '{\"id\": \"cs-001x\"}', 'posted', '2026-10-01T00:00:00Z')")
                conn.execute("INSERT INTO accounts (name, currency, open_date) VALUES ('Expenses:Direct', 'USD', '2000-01-01')")
                conn.commit()

        document = self.both(action)
        self.assertEqual(sorted((row["source_id"], row["change"]) for row in document["bank_rows"]),
                         [("cs-001", "status-changed"), ("cs-001x", "added")])
        self.assertEqual([row["name"] for row in document["accounts"]["opened"]], ["Expenses:Direct"])

    def test_an_account_replaced_by_its_unique_name_is_found(self) -> None:
        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                row = conn.execute("SELECT currency, open_date FROM accounts WHERE name = 'Expenses:Software'").fetchone()
                if row is None:
                    conn.execute("INSERT INTO accounts (name, currency, open_date) VALUES ('Expenses:Software', 'USD', '2000-01-01')")
                    conn.commit()
                    row = ("USD", "2000-01-01")
                conn.execute("INSERT OR REPLACE INTO accounts (name, currency, open_date) VALUES ('Expenses:Software', ?, '2001-01-01')",
                             (row[0],))
                conn.commit()

        self.both(action)

    def test_a_command_that_drops_the_capture_log_gives_the_full_comparison(self) -> None:
        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                for _, name in capture_objects(self.entity):
                    if name.endswith("_i") or name.endswith("_u"):
                        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
                conn.execute("UPDATE source_transactions SET status = 'voided' WHERE id = 'cs-001'")
                conn.commit()

        document = self.both(action)
        self.assertEqual([(row["source_id"], row["change"]) for row in document["bank_rows"]],
                         [("cs-001", "status-changed")])

    def test_a_replaced_log_table_is_not_trusted(self) -> None:
        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                conn.execute(f"DELETE FROM {change_capture.CTL_TABLE}")
                conn.execute(f"INSERT INTO {change_capture.CTL_TABLE} (token) VALUES ('forged')")
                conn.execute("UPDATE source_transactions SET status = 'voided' WHERE id = 'cs-001'")
                conn.commit()

        self.both(action)

    def test_a_write_outside_a_capture_makes_the_index_stale_and_the_next_capture_reads_in_full(self) -> None:
        self.both(lambda: None)
        # No capture: the index does not see these writes.
        run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "cs-002", "--category",
                 "Income:Consulting", "--reasoning", "Payout."])
        run_cli(["queue", "confirm", "--entity", str(self.entity), "--item", "cs-002"])
        self.seq.view = changeset.read_view(self.entity)
        document = self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "cs-003",
                                               "--category", "Expenses:Meals", "--reasoning", "Lunch."]))
        self.assertEqual([row["id"] for row in document["review_queue"]], ["cs-003"])

    def test_the_index_is_rebuilt_when_it_is_deleted_or_damaged(self) -> None:
        self.both(lambda: None)
        index = self.entity / change_capture.INDEX_NAME
        for damage in (lambda: index.unlink(), lambda: index.write_bytes(b"not a database" * 100)):
            damage()
            for leftover in self.entity.glob(change_capture.INDEX_NAME + "-*"):
                leftover.unlink()
            self.both(lambda: StagingStore(self.entity / "staging").mark_seen("after-damage"))

    def test_a_folder_that_cannot_hold_the_index_gives_the_full_comparison(self) -> None:
        with mock.patch.object(change_capture, "Index", side_effect=sqlite3.OperationalError("readonly")):
            with changeset.capture(self.entity) as fast:
                StagingStore(self.entity / "staging").mark_seen("no-index")
        with changeset.capture(self.entity, incremental=False) as full:
            StagingStore(self.entity / "staging").mark_seen("no-index-2")
        self.assertTrue(fast.change_set["complete"])
        self.assertEqual(fast.change_set["staging"]["seen_ids"]["added"], ["no-index"])
        self.assertTrue(full.change_set["complete"])

    def test_an_unreadable_item_file_before_the_command_makes_the_change_set_incomplete(self) -> None:
        (self.entity / "review-queue" / "cs-001.json").write_text("{broken", encoding="utf-8")
        with changeset.capture(self.entity) as receipt:
            pass
        self.assertFalse(receipt.change_set["complete"])
        self.assertEqual(receipt.change_set["reason"], "state_unreadable")
        self.assertEqual(capture_objects(self.entity), [])


class CostTests(Fixture):
    """After the first capture a write that goes through the helpers reads no full table.

    It still parses the learned context, which the engine rewrites in full for a write, because the engine reads it too.
    """

    def test_one_approve_reads_no_table_in_full(self) -> None:
        self.both(lambda: None)
        self.seq.cli("propose", "queue", "propose", "--entity", str(self.entity), "--source-id", "cs-002",
                     "--category", "Income:Consulting", "--reasoning", "Payout.")
        boom = AssertionError("a table was read in full")
        with mock.patch.object(changeset, "_load_accounts", side_effect=boom), \
                mock.patch.object(changeset, "_load_bank_index", side_effect=boom), \
                mock.patch.object(changeset, "load_queue_files", side_effect=boom), \
                mock.patch.object(changeset, "_load_keyed", side_effect=boom), \
                mock.patch.object(changeset, "_load_ids", side_effect=boom), \
                mock.patch.object(changeset, "_counts", side_effect=boom):
            with changeset.capture(self.entity) as receipt:
                run_cli(["queue", "confirm", "--entity", str(self.entity), "--item", "cs-002"])
        document = receipt.change_set
        self.assertTrue(document["complete"])
        self.assertEqual(len(document["entries_added"]), 1)
        self.assertEqual(len(document["bank_rows"]), 1)
        self.assertEqual([row["id"] for row in document["review_queue"]], ["cs-002"])
        self.seq._apply("approve", document)


class TriggerTests(Fixture):
    def test_the_log_and_triggers_exist_only_while_the_command_runs(self) -> None:
        before = ledger_schema(self.entity)
        self.assertEqual(capture_objects(self.entity), [])
        with changeset.capture(self.entity):
            names = {name for _, name in capture_objects(self.entity)}
            self.assertIn(change_capture.LOG_TABLE, names)
            self.assertTrue(any(name.startswith(change_capture.TRIGGER_PREFIX + "st_") for name in names))
        self.assertEqual(capture_objects(self.entity), [])
        self.assertEqual(ledger_schema(self.entity), before)

    def test_the_log_is_removed_when_the_command_raises_or_fails(self) -> None:
        before = ledger_schema(self.entity)
        with self.assertRaises(RuntimeError):
            with changeset.capture(self.entity) as receipt:
                StagingStore(self.entity / "staging").mark_seen("then-fail")
                raise RuntimeError("stop")
        self.assertFalse(receipt.change_set["complete"])
        self.assertEqual(ledger_schema(self.entity), before)

    def test_a_log_that_a_killed_command_left_is_removed_by_the_next_capture(self) -> None:
        before = ledger_schema(self.entity)
        change_capture.install_triggers(self.entity / "ledger.sqlite")  # A capture that never ended.
        self.assertTrue(capture_objects(self.entity))
        with changeset.capture(self.entity) as receipt:
            pass
        self.assertEqual(capture_objects(self.entity), [])
        self.assertEqual(ledger_schema(self.entity), before)
        self.assertTrue(receipt.change_set["complete"])
        self.assertFalse(receipt.change_set["changed"])

    def test_remove_triggers_removes_only_capture_objects(self) -> None:
        before = ledger_schema(self.entity)
        change_capture.install_triggers(self.entity / "ledger.sqlite")
        self.assertTrue(change_capture.remove_triggers(self.entity / "ledger.sqlite"))
        self.assertEqual(ledger_schema(self.entity), before)

    def test_the_append_only_triggers_still_refuse_a_rewrite_while_the_log_is_on(self) -> None:
        with changeset.capture(self.entity):
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                self.assertEqual(append_only_guard_problems(conn), [])
                for sql in ("UPDATE entries SET narration = 'x'", "DELETE FROM entries",
                            "UPDATE postings SET amount = '1.00'", "DELETE FROM postings",
                            "UPDATE audit_events SET type = 'x'", "DELETE FROM audit_events WHERE id = 1",
                            "INSERT OR REPLACE INTO entries (id, date, narration) VALUES (1, '2026-01-01', 'x')"):
                    with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                        conn.execute(sql)
                        conn.commit()
                    conn.rollback()
                # The capture triggers write only the log: they do not write a ledger row.
                before = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                conn.execute("UPDATE source_transactions SET status = status")
                conn.rollback()
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], before)

    def test_the_capture_triggers_touch_no_guarded_table(self) -> None:
        for sql in change_capture._trigger_sql().values():
            tables = {part for part in ("entries", "postings", "balance_assertions", "audit_events", "meta")
                      if f"INTO {part}" in sql or f"UPDATE {part}" in sql or f"DELETE FROM {part}" in sql}
            self.assertEqual(tables, set(), sql)
            self.assertIn(change_capture.LOG_TABLE, sql)

    def test_a_store_whose_log_was_changed_is_read_in_full(self) -> None:
        changed = {}

        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                conn.execute(f"INSERT INTO {change_capture.LOG_TABLE} (tbl, key) VALUES ('s', 'cs-002')")
                conn.commit()

        document = self.both(action)
        self.assertFalse(document["changed"])
        changed.update(document)


class CaptureLogTests(unittest.TestCase):
    def test_the_journal_does_nothing_outside_a_capture(self) -> None:
        self.assertFalse(capture_log.active())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            with capture_log.writing(path, delta={"upsert": {"a": None}}):
                path.write_text("[]", encoding="utf-8")
            self.assertEqual(path.read_text(encoding="utf-8"), "[]")

    def test_a_change_between_two_journaled_writes_breaks_the_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "staging" / "seen-ids.json"
            path.parent.mkdir()
            path.write_text("[]", encoding="utf-8")
            session = capture_log.begin(root, {"staging/seen-ids.json": capture_log.signature(path)})
            try:
                with capture_log.writing(path, delta={"upsert": {"a": None}}):
                    path.write_text('["a"]', encoding="utf-8")
                log = session.files["staging/seen-ids.json"]
                self.assertTrue(log.chain_ok)
                path.write_text('["a", "b", "c"]', encoding="utf-8")  # Not journaled.
                with capture_log.writing(path, delta={"upsert": {"d": None}}):
                    path.write_text('["a", "b", "c", "d"]', encoding="utf-8")
                self.assertFalse(log.chain_ok)
            finally:
                capture_log.end(session)
            self.assertFalse(capture_log.active())


if __name__ == "__main__":
    unittest.main()
