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

    def cli(self, name, *argv, expect=0):
        revision = self.view["revision"]
        with changeset.capture(self.entity, books_revision=revision + 1, base_revision=revision,
                               incremental=False) as full:
            document = super().cli(name, *argv, expect=expect)
        if expect == 0:
            self.test.assertEqual(document, full.change_set, f"{name}: incremental and full capture differ")
        return document

    def api(self, name, call):
        revision = self.view["revision"]
        with changeset.capture(self.entity, books_revision=revision + 1, base_revision=revision,
                               incremental=False) as full:
            document = super().api(name, call)
        self.test.assertEqual(document, full.change_set, f"{name}: incremental and full capture differ")
        return document


class Equivalence(unittest.TestCase):
    def test_every_step_of_the_change_set_scenario_gives_the_same_document_as_a_full_comparison(self) -> None:
        import test_changeset
        with mock.patch.object(test_changeset, "Sequence_", Dual):
            with tempfile.TemporaryDirectory() as tmp:
                seq = scenario(self, Path(tmp))
        self.assertGreaterEqual(len(seq.sets), 15)


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
        revision = self.seq.view["revision"]
        args = {"books_revision": revision + 1, "base_revision": revision}
        with changeset.capture(self.entity, incremental=False, **args) as full:
            with changeset.capture(self.entity, **args) as fast:
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
        self.seq.cli("propose", "queue", "propose", "--entity", str(self.entity), "--source-id", "cs-002",
                     "--category", "Income:Consulting", "--reasoning", "Payout.")
        folder = self.entity / "review-queue"
        item = json.loads((folder / "cs-002.json").read_text(encoding="utf-8"))
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

    def test_a_payload_that_is_not_an_object_is_unreadable_in_both_captures(self) -> None:
        for text in ("null", "not json", "[1]"):
            with self.subTest(text=text):
                def action() -> None:
                    with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                        conn.execute("INSERT OR REPLACE INTO source_transactions (id, payload_json, status, imported_at) "
                                     "VALUES ('p-bad', ?, 'posted', '2026-10-01T00:00:00Z')", (text,))
                        conn.commit()

                revision = self.seq.view["revision"]
                args = {"books_revision": revision + 1, "base_revision": revision}
                with changeset.capture(self.entity, incremental=False, **args) as full:
                    with changeset.capture(self.entity, **args) as fast:
                        action()
                self.assertEqual(fast.change_set, full.change_set)
                self.assertFalse(fast.change_set["complete"])
                self.assertEqual(fast.change_set["reason"], "state_unreadable")
                self.assertEqual(capture_objects(self.entity), [])
                with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                    conn.execute("DELETE FROM source_transactions WHERE id = 'p-bad'")
                    conn.commit()
                self.seq.view = changeset.read_view(self.entity, revision=self.seq.view["revision"])

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
                conn.execute(f"UPDATE {change_capture.CTL_TABLE} SET token = 'forged'")
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

    def test_a_decided_item_that_moves_into_an_archive_is_not_a_change_and_reads_no_full_queue(self) -> None:
        self.both(lambda: None)
        folder = self.entity / "review-queue"
        archives = sorted((folder / "archive").glob("*.json"))
        self.assertTrue(archives, "the confirm must have archived the decided item")
        self.seq.cli("propose", "queue", "propose", "--entity", str(self.entity), "--source-id", "cs-002",
                     "--category", "Income:Consulting", "--reasoning", "Payout.")
        self.seq.cli("approve", "queue", "confirm", "--entity", str(self.entity), "--item", "cs-002")
        # The confirm archived cs-002 already. Put its file back and capture the move again.
        archive = sorted(folder.joinpath("archive").glob("*.json"))[0]
        item = json.loads(archive.read_text(encoding="utf-8"))["items"]["cs-002.json"]
        (folder / "cs-002.json").write_text(json.dumps(item, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.both(lambda: None)
        revision = self.seq.view["revision"]
        with mock.patch.object(changeset, "load_queue_files", side_effect=AssertionError("queue read in full")):
            with changeset.capture(self.entity, books_revision=revision + 1, base_revision=revision) as receipt:
                queue.compact_closed_items(load_entity(self.entity))
        self.assertTrue(receipt.change_set["complete"])
        self.assertFalse(receipt.change_set["changed"])

    def test_an_archive_that_changes_without_a_helper_is_found(self) -> None:
        archive = sorted((self.entity / "review-queue" / "archive").glob("*.json"))[0]
        raw = json.loads(archive.read_text(encoding="utf-8"))
        name, item = next(iter(raw["items"].items()))
        item = {**item, "reasoning": "Edited."}
        raw["items"][name] = item
        document = self.both(lambda: archive.write_text(json.dumps(raw), encoding="utf-8"))
        self.assertEqual([row["id"] for row in document["review_queue"]], [item["source_id"]])

    def test_an_unreadable_item_file_before_the_command_makes_the_change_set_incomplete(self) -> None:
        (self.entity / "review-queue" / "cs-001.json").write_text("{broken", encoding="utf-8")
        with changeset.capture(self.entity) as receipt:
            pass
        self.assertFalse(receipt.change_set["complete"])
        self.assertEqual(receipt.change_set["reason"], "state_unreadable")
        self.assertEqual(capture_objects(self.entity), [])


class TamperTests(Fixture):
    """Edits that try to hide a change from the capture. Every one must give the document of a full comparison."""

    def drop_capture_triggers(self, conn: sqlite3.Connection, suffixes: tuple[str, ...]) -> None:
        for _, name in capture_objects(self.entity):
            if name.endswith(suffixes):
                conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')

    def test_the_log_refuses_an_edit_and_a_delete(self) -> None:
        with changeset.capture(self.entity):
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                conn.execute("UPDATE source_transactions SET status = 'voided' WHERE id = 'cs-001'")
                conn.commit()
                for sql in (f"DELETE FROM {change_capture.LOG_TABLE}", f"UPDATE {change_capture.LOG_TABLE} SET key = 'x'"):
                    with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                        conn.execute(sql)
                    conn.rollback()
                self.assertGreater(conn.execute(f"SELECT COUNT(*) FROM {change_capture.LOG_TABLE}").fetchone()[0], 0)

    def test_a_log_row_that_is_removed_after_the_triggers_are_dropped_is_a_gap(self) -> None:
        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                conn.execute("UPDATE source_transactions SET status = 'voided' WHERE id = 'cs-001'")
                conn.commit()
                self.drop_capture_triggers(conn, ("log_d", "log_u"))
                conn.execute(f"DELETE FROM {change_capture.LOG_TABLE}")
                conn.commit()

        document = self.both(action)
        self.assertEqual([(row["source_id"], row["change"]) for row in document["bank_rows"]],
                         [("cs-001", "status-changed")])

    def test_a_log_row_that_is_edited_after_the_triggers_are_dropped_is_found(self) -> None:
        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                conn.execute("UPDATE source_transactions SET status = 'voided' WHERE id = 'cs-001'")
                conn.commit()
                self.drop_capture_triggers(conn, ("log_d", "log_u"))
                conn.execute(f"UPDATE {change_capture.LOG_TABLE} SET key = 'elsewhere'")
                conn.commit()

        document = self.both(action)
        self.assertTrue(document["bank_rows"])

    def test_the_log_refuses_a_replace_and_a_number_that_does_not_rise(self) -> None:
        with changeset.capture(self.entity):
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                conn.execute("UPDATE source_transactions SET status = 'voided' WHERE id = 'cs-001'")
                conn.commit()
                for sql in (f"INSERT OR REPLACE INTO {change_capture.LOG_TABLE} (seq, tbl, key) VALUES (1, 's', 'elsewhere')",
                            f"REPLACE INTO {change_capture.LOG_TABLE} (seq, tbl, key) VALUES (1, 's', 'elsewhere')",
                            f"INSERT INTO {change_capture.LOG_TABLE} (seq, tbl, key) VALUES (0, 's', 'low')"):
                    with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                        conn.execute(sql)
                        conn.commit()
                    conn.rollback()
                self.assertEqual(conn.execute(
                    f"SELECT key FROM {change_capture.LOG_TABLE} WHERE seq = 1").fetchone()[0], "cs-001")

    def test_a_log_row_that_is_replaced_after_the_triggers_are_dropped_is_found(self) -> None:
        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                conn.execute("UPDATE source_transactions SET status = 'voided' WHERE id = 'cs-001'")
                conn.commit()
                self.drop_capture_triggers(conn, ("log_bi", "log_ai"))
                conn.execute(f"INSERT OR REPLACE INTO {change_capture.LOG_TABLE} (seq, tbl, key) VALUES (1, 's', 'elsewhere')")
                conn.commit()

        document = self.both(action)
        self.assertTrue(document["bank_rows"])

    def test_a_trigger_that_is_dropped_and_made_again_in_the_middle_is_a_schema_change(self) -> None:
        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                sql = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").fetchall())
                name = f"{change_capture.TRIGGER_PREFIX}st_u"
                conn.execute(f'DROP TRIGGER "{name}"')
                conn.execute("UPDATE source_transactions SET status = 'voided' WHERE id = 'cs-001'")  # Not logged.
                conn.execute(sql[name])  # The same text again: the log check alone would pass.
                conn.commit()

        revision = self.seq.view["revision"]
        args = {"books_revision": revision + 1, "base_revision": revision}
        with changeset.capture(self.entity, incremental=False, **args) as full:
            with changeset.capture(self.entity, **args) as fast:
                action()
        self.assertEqual(fast.change_set, full.change_set)
        self.assertEqual([row["source_id"] for row in fast.change_set["bank_rows"]], ["cs-001"])
        self.assertTrue(any("schema" in note for note in fast.path_report["fallbacks"]), fast.path_report)

    def test_a_log_that_is_dropped_and_made_again_is_not_trusted(self) -> None:
        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                conn.execute("UPDATE source_transactions SET status = 'voided' WHERE id = 'cs-001'")
                conn.commit()
                self.drop_capture_triggers(conn, ("log_d", "log_u"))
                conn.execute(f"DROP TABLE {change_capture.LOG_TABLE}")
                conn.execute(f"CREATE TABLE {change_capture.LOG_TABLE} (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                             "tbl TEXT NOT NULL, key TEXT NOT NULL)")
                conn.commit()

        self.both(action)

    def test_an_edit_of_the_same_length_with_the_old_modification_time_is_found(self) -> None:
        import os
        self.seq.cli("propose", "queue", "propose", "--entity", str(self.entity), "--source-id", "cs-002",
                     "--category", "Income:Consulting", "--reasoning", "Payout.")
        targets = {
            "seen": self.entity / "staging" / "seen-ids.json",
            "item": self.entity / "review-queue" / "cs-002.json",
            "pending": self.entity / "staging" / "pending-categorization.json",
        }
        self.both(lambda: None)
        for label, path in targets.items():
            with self.subTest(label):
                original = path.read_bytes()
                status = path.stat()
                if label == "seen":
                    edited = original.replace(b"cs-001", b"cs-00x", 1)
                elif label == "item":
                    edited = original.replace(b"Payout.", b"Payoff.", 1)
                else:
                    edited = original.replace(b"cs-003", b"cs-00y", 1)
                self.assertEqual(len(edited), len(original))
                self.assertNotEqual(edited, original)

                def action() -> None:
                    path.write_bytes(edited)
                    os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))  # The old modification time is back.

                with changeset.capture(self.entity, incremental=False) as full:
                    with changeset.capture(self.entity) as fast:
                        action()
                self.assertTrue(fast.change_set["changed"], label)
                self.assertEqual(fast.change_set, full.change_set)
                path.write_bytes(original)
                os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))
                self.both(lambda: None)

    def test_an_index_that_is_deleted_or_restored_during_the_command_gives_an_incomplete_change_set(self) -> None:
        import shutil
        index = self.entity / change_capture.INDEX_NAME
        self.both(lambda: None)
        old = self.root / "old-index.sqlite"
        with changeset.capture(self.entity) as receipt:
            pass
        shutil.copyfile(index, old)
        cases = {
            "deleted": lambda: [p.unlink() for p in self.entity.glob(change_capture.INDEX_NAME + "*")],
            "restored": lambda: shutil.copyfile(old, index),
            "replaced": lambda: (index.unlink(), index.write_bytes(old.read_bytes())),
        }
        for label, action in cases.items():
            with self.subTest(label):
                with changeset.capture(self.entity) as receipt:
                    StagingStore(self.entity / "staging").mark_seen("while-index-changes-" + label)
                    action()
                self.assertFalse(receipt.change_set["complete"], label)
                self.assertEqual(receipt.change_set["reason"], "state_unreadable")
                with self.assertRaises(changeset.ChangeSetRefused):
                    changeset.apply_change_set(self.seq.view, receipt.change_set)
                self.seq.view = changeset.read_view(self.entity, revision=self.seq.view["revision"])
                self.both(lambda: None)  # The next capture builds the index again and is complete.

    def test_a_strict_file_with_a_bad_member_gives_an_incomplete_change_set(self) -> None:
        path = self.entity / "staging" / "seen-ids.json"
        path.write_text(json.dumps(["cs-001", None]), encoding="utf-8")
        with changeset.capture(self.entity) as receipt:
            pass
        self.assertFalse(receipt.change_set["complete"])
        self.assertEqual(receipt.change_set["reason"], "state_unreadable")
        self.assertEqual(capture_objects(self.entity), [])

    def test_a_row_whose_key_is_not_its_id_in_the_view_is_refused(self) -> None:
        document = self.both(lambda: None)
        view = changeset.read_view(self.entity, revision=self.seq.view["revision"])
        key = next(iter(view["review_queue"]))
        view["review_queue"]["other-key"] = view["review_queue"].pop(key)
        with self.assertRaises(changeset.ChangeSetRefused):
            changeset.view_digest(view)


class ArchiveTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        folder = self.entity / "review-queue" / "archive"
        self.assertTrue(list(folder.glob("*.json")))
        # Many archived months.
        for month in range(24):
            name = f"2020-{1 + month % 12:02d}" if month < 12 else f"2021-{1 + month % 12:02d}"
            items = {f"old-{name}-{n}.json": {"source_id": f"old-{name}-{n}", "status": "confirmed",
                                               "date": f"{name}-10"} for n in range(30)}
            (folder / f"{name}.json").write_bytes(queue._archive_bytes(dict(sorted(items.items()))))
        self.seq.view = changeset.read_view(self.entity, revision=self.seq.view["revision"])
        self.both(lambda: None)

    def test_an_approve_reads_only_the_journaled_names_of_its_archive(self) -> None:
        self.seq.cli("propose", "queue", "propose", "--entity", str(self.entity), "--source-id", "cs-002",
                     "--category", "Income:Consulting", "--reasoning", "Payout.")
        revision = self.seq.view["revision"]
        boom = mock.Mock(side_effect=AssertionError("an archive was read by the capture"))
        with mock.patch.object(change_capture, "_archive_reader", return_value=boom), \
                mock.patch.object(changeset, "load_queue_files", side_effect=AssertionError("queue read in full")):
            with changeset.capture(self.entity, books_revision=revision + 1, base_revision=revision) as receipt:
                run_cli(["queue", "confirm", "--entity", str(self.entity), "--item", "cs-002"])
        self.assertTrue(receipt.change_set["complete"])
        self.assertEqual(receipt.change_set["staging"]["pending"]["removed"], ["cs-002"])
        self.assertEqual([row["id"] for row in receipt.change_set["review_queue"]], ["cs-002"])
        self.assertEqual(receipt.path_report["mode"], "incremental")
        self.assertEqual(receipt.path_report["fallbacks"], [])
        self.seq._apply("approve", receipt.change_set)

    def test_a_change_to_one_archive_does_not_touch_the_rows_of_another(self) -> None:
        archive = self.entity / "review-queue" / "archive" / "2020-03.json"
        raw = json.loads(archive.read_text(encoding="utf-8"))
        name = sorted(raw["items"])[0]
        raw["items"][name] = {**raw["items"][name], "reasoning": "Edited by hand."}
        document = self.both(lambda: archive.write_text(json.dumps(raw), encoding="utf-8"))
        self.assertEqual([row["id"] for row in document["review_queue"]], [raw["items"][name]["source_id"]])


class JournalTests(Fixture):
    def test_an_archive_edit_before_its_first_journaled_write_is_found(self) -> None:
        folder = self.entity / "review-queue" / "archive"
        archive = sorted(folder.glob("*.json"))[0]
        raw = json.loads(archive.read_text(encoding="utf-8"))
        name = next(iter(raw["items"]))
        edited = {**raw["items"][name], "reasoning": "Edited before the helper wrote."}
        self.seq.cli("propose", "queue", "propose", "--entity", str(self.entity), "--source-id", "cs-002",
                     "--category", "Income:Consulting", "--reasoning", "Payout.")
        self.both(lambda: None)

        def action() -> None:
            raw["items"][name] = edited
            archive.write_text(json.dumps(raw), encoding="utf-8")  # Not journaled.
            run_cli(["queue", "confirm", "--entity", str(self.entity), "--item", "cs-002"])  # Journaled archive write.

        document = self.both(action)
        self.assertIn(edited["source_id"], [row["id"] for row in document["review_queue"]])

    def test_changing_the_object_that_set_taught_returns_does_not_change_the_receipt(self) -> None:
        company = load_entity(self.entity)
        revision = self.seq.view["revision"]
        with changeset.capture(self.entity, books_revision=revision + 1, base_revision=revision) as receipt:
            taught = queue.set_taught(company, "HARBOR CAFE", "Expenses:Meals", by="u1", by_name="Pat",
                                      ts="2026-10-16T10:00:00Z")
            taught["category"] = "Expenses:Wrong"  # Not written to disk.
        entry = receipt.change_set["learned_context"][0]["entry"]
        self.assertEqual(entry["taught"]["category"], "Expenses:Meals")
        self.seq._apply("teach", receipt.change_set)  # The reducer checks the digest against the disk.


class RaceTests(Fixture):
    """Writes that arrive between the steps of a capture. The capture holds the company lock and one store transaction."""

    def test_another_writer_waits_while_a_capture_runs(self) -> None:
        import threading
        from bookkeeping.ledger.importer import _entity_write_lock
        events: list[str] = []

        def other_writer() -> None:
            with _entity_write_lock(self.entity):
                events.append("other writer ran")

        thread = threading.Thread(target=other_writer)
        with changeset.capture(self.entity):
            thread.start()
            thread.join(0.5)
            self.assertTrue(thread.is_alive(), "a second writer ran during the capture")
            self.assertEqual(events, [])
        thread.join(5)
        self.assertEqual(events, ["other writer ran"])

    def test_company_write_lock_is_re_entrant_and_blocks_another_thread(self) -> None:
        import threading
        from bookkeeping.ledger.importer import _entity_write_lock
        seen: list[str] = []
        with changeset.company_write_lock(self.entity) as held:
            self.assertTrue(held)
            with _entity_write_lock(self.entity):  # The command inside takes it again without waiting.
                with changeset.company_write_lock(self.entity) as inner:
                    self.assertTrue(inner)
            thread = threading.Thread(target=lambda: seen.append("ran") if _entity_write_lock(self.entity).__enter__() is None else None)
            thread.daemon = True
            thread.start()
            thread.join(0.3)
            self.assertEqual(seen, [])
        thread.join(3)

    def test_the_index_counter_is_part_of_its_identity(self) -> None:
        self.both(lambda: None)
        self.assertIsInstance(change_capture.sqlite_counter(self.entity / change_capture.INDEX_NAME), int)
        self.assertIsNone(change_capture.sqlite_counter(self.entity / "entity.json"))

    def test_the_command_inside_a_capture_takes_the_lock_again(self) -> None:
        with changeset.capture(self.entity) as receipt:
            run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "cs-002", "--category",
                     "Income:Consulting", "--reasoning", "Payout."])
        self.assertTrue(receipt.change_set["complete"])

    def test_a_store_write_while_the_capture_reads_at_the_end_is_refused_by_the_store(self) -> None:
        real = change_capture.Run._files
        outcome: dict[str, str] = {}

        def files_then_write(run, before, after):
            real(run, before, after)
            try:
                with closing(sqlite3.connect(self.entity / "ledger.sqlite", timeout=0.1)) as conn:
                    conn.execute("UPDATE source_transactions SET status = 'voided' WHERE id = 'cs-001'")
                    conn.commit()
                outcome["write"] = "accepted"
            except sqlite3.OperationalError as exc:
                outcome["write"] = str(exc)

        with mock.patch.object(change_capture.Run, "_files", files_then_write):
            with changeset.capture(self.entity) as receipt:
                pass
        self.assertIn("locked", outcome["write"])
        self.assertTrue(receipt.change_set["complete"])
        self.assertEqual(receipt.change_set["bank_rows"], [])

    def test_a_file_write_after_the_reads_shows_in_the_next_capture(self) -> None:
        self.both(lambda: None)
        path = self.entity / "staging" / "seen-ids.json"
        real = change_capture.Run._files

        def files_then_write(run, before, after):
            real(run, before, after)
            rows = json.loads(path.read_text(encoding="utf-8"))
            path.write_text(json.dumps(rows + ["late-1"]), encoding="utf-8")

        revision = self.seq.view["revision"]
        with mock.patch.object(change_capture.Run, "_files", files_then_write):
            with changeset.capture(self.entity, books_revision=revision + 1, base_revision=revision) as receipt:
                pass
        self.assertNotIn("late-1", receipt.change_set["staging"]["seen_ids"]["added"])
        self.seq.view = changeset.read_view(self.entity, revision=revision)  # The host reads the books again.
        later = self.both(lambda: None)
        self.assertEqual(later["staging"]["seen_ids"]["added"], [], "the write was folded into the saved tokens")
        # The capture that missed the write cannot be applied: its state.after is not the state of the books.
        self.assertNotEqual(receipt.change_set["state"]["after"], later["state"]["before"])

    def test_a_file_change_counter_that_does_not_fit_the_capture_gives_the_full_comparison(self) -> None:
        real = change_capture.Run._sql
        for label, counter in (("went back", 10 ** 9), ("did not move", None)):
            with self.subTest(label):
                def sql_with_counter(run, before, after, counter=counter):
                    # The counter that the store showed at the start is made to disagree with the one at the end.
                    run.counter_start = counter if counter is not None else run.counter_in_txn
                    return real(run, before, after)

                revision = self.seq.view["revision"]
                args = {"books_revision": revision + 1, "base_revision": revision}
                with changeset.capture(self.entity, incremental=False, **args) as full:
                    with mock.patch.object(change_capture.Run, "_sql", sql_with_counter):
                        with changeset.capture(self.entity, **args) as fast:
                            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                                conn.execute("UPDATE source_transactions SET status = 'voided' WHERE id = 'cs-001'")
                                conn.commit()
                self.assertEqual(fast.change_set, full.change_set)
                self.assertTrue(any("counter" in note for note in fast.path_report["fallbacks"]), fast.path_report)
                self.seq._apply("counter", fast.change_set)

    # Outside the threat model, and documented in docs/change-sets.md: a process that rewrites the header of the store
    # file (the schema cookie or the change counter), or that drops a trigger, writes, recreates the identical trigger
    # and resets the schema cookie, while it holds no engine lock. The capture holds the company write lock, so no
    # engine writer does this.


class JournalRaiseTests(Fixture):
    def test_a_helper_write_that_raises_records_no_delta(self) -> None:
        path = self.entity / "staging" / "seen-ids.json"
        revision = self.seq.view["revision"]
        with changeset.capture(self.entity, books_revision=revision + 1, base_revision=revision) as receipt:
            try:
                with capture_log.writing(path, delta={"upsert": {"claimed-only": None}}):
                    rows = json.loads(path.read_text(encoding="utf-8"))
                    path.write_text(json.dumps(rows + ["actually-written"]), encoding="utf-8")
                    raise RuntimeError("the helper failed after the write")
            except RuntimeError:
                pass
        added = receipt.change_set["staging"]["seen_ids"]["added"]
        self.assertEqual(added, ["actually-written"])
        self.seq._apply("raised", receipt.change_set)


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
            revision = self.seq.view["revision"]
            with changeset.capture(self.entity, books_revision=revision + 1, base_revision=revision) as receipt:
                run_cli(["queue", "confirm", "--entity", str(self.entity), "--item", "cs-002"])
        document = receipt.change_set
        self.assertTrue(document["complete"])
        self.assertEqual(len(document["entries_added"]), 1)
        self.assertEqual(len(document["bank_rows"]), 1)
        self.assertEqual([row["id"] for row in document["review_queue"]], ["cs-002"])
        self.seq._apply("approve", document)


class LedgerDigestTests(Fixture):
    def test_an_approve_updates_the_ledger_totals_from_the_new_rows_only(self) -> None:
        self.both(lambda: None)
        self.seq.cli("propose", "queue", "propose", "--entity", str(self.entity), "--source-id", "cs-002",
                     "--category", "Income:Consulting", "--reasoning", "Payout.")
        revision = self.seq.view["revision"]
        with mock.patch.object(change_capture, "ledger_totals", side_effect=AssertionError("ledger read in full")):
            with changeset.capture(self.entity, books_revision=revision + 1, base_revision=revision) as receipt:
                run_cli(["queue", "confirm", "--entity", str(self.entity), "--item", "cs-002"])
        self.assertTrue(receipt.change_set["complete"])
        self.seq._apply("approve", receipt.change_set)  # The reducer checks the digest exactly.

    def test_a_rename_reads_the_ledger_in_full_and_the_digest_stays_exact(self) -> None:
        self.both(lambda: None)
        calls = []
        real = change_capture.ledger_totals
        with mock.patch.object(change_capture, "ledger_totals", lambda path: calls.append(1) or real(path)):
            code, out, err = run_cli(["entity", "account-rename", str(self.entity), "--account", "Expenses:Software",
                                      "--to", "Expenses:Tools", "--json"])
            self.assertEqual(code, 0, err)
            plan = json.loads(out)
            calls.clear()
            self.both(lambda: run_cli(["entity", "account-rename", str(self.entity), "--account", "Expenses:Software",
                                       "--to", "Expenses:Tools", "--apply", "--plan-hash", plan["plan_hash"], "--json"]))
        self.assertTrue(calls)

    def test_a_changed_posted_row_is_found_by_the_digest_even_without_the_append_only_triggers(self) -> None:
        self.both(lambda: None)
        with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
            for name in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
                         if not r[0].startswith(change_capture.TRIGGER_PREFIX)]:
                conn.execute(f'DROP TRIGGER "{name}"')
            conn.commit()
        revision = self.seq.view["revision"]
        args = {"books_revision": revision + 1, "base_revision": revision}

        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                conn.execute("UPDATE entries SET narration = 'tampered' WHERE id = 1")
                conn.commit()

        with changeset.capture(self.entity, incremental=False, **args) as full:
            with changeset.capture(self.entity, **args) as fast:
                action()
        self.assertEqual(fast.change_set["state"], full.change_set["state"])
        self.assertNotEqual(fast.change_set["state"]["before"], fast.change_set["state"]["after"])


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
