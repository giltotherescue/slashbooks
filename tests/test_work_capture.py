"""Tests for the incremental work event capture (``work.capture``).

All data is synthetic. The incremental capture must give the events that two full snapshots give. The tests run the
same writes under both and compare the events, then break the assumptions of the fast path (a write that does not use a
helper, a replaced ledger, a stale or broken index, a file that the command corrupts) and check that the events stay the
same and that the capture says which path it took.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from bookkeeping import change_capture, changeset, queue, work  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
import test_work  # noqa: E402
from test_work import CHECKING, FIRST_IMPORT, Books, _txn, run_cli  # noqa: E402


class Paths:
    """Record the path that each ``work.capture`` of the CLI took."""

    def __init__(self) -> None:
        self.taken: list[str] = []
        self._real = work.capture

    def __enter__(self) -> "Paths":
        paths, real = self, self._real

        @contextmanager
        def spy(entity_path, **kwargs):
            with real(entity_path, **kwargs) as holder:
                yield holder
            if kwargs.get("incremental") is not False:
                paths.taken.append(holder.path)

        self._patch = mock.patch.object(work, "capture", spy)
        self._patch.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._patch.stop()


class Dual(Books):
    """Run each command through the CLI (incremental capture) inside a capture of two full snapshots."""

    test: unittest.TestCase
    with_change_set = False
    steps = 0

    def run(self, *argv: str, expect: int = 0) -> list[dict]:
        Dual.steps += 1
        extra: list[str] = []
        revisions = {"books_revision": 1, "base_revision": 0}
        if self.with_change_set:
            self.counter += 1
            out = self.root / f"cs-{self.counter}.json"
            extra = ["--change-set-out", str(out), "--change-set-revision", "1", "--change-set-base-revision", "0"]
        with work.capture(self.entity, incremental=False) as full:
            with changeset.capture(self.entity, incremental=False, **revisions) as full_set:
                events = super().run(*extra, *argv, expect=expect)
        self.test.assertEqual(events, full.events, f"{argv}: incremental and full work events differ")
        if self.with_change_set:
            self.test.assertEqual(json.loads(out.read_text(encoding="utf-8")), full_set.change_set,
                                  f"{argv}: the change set differs when it shares the run")
        return events


class WithChangeSet(Dual):
    with_change_set = True


class Equivalence(unittest.TestCase):
    def scenario(self, books: type[Dual]) -> list[str]:
        books.test = self
        with mock.patch.object(test_work, "Books", books), Paths() as paths:
            with tempfile.TemporaryDirectory() as tmp:
                steps = test_work.run_scenario(Path(tmp))
        self.assertGreaterEqual(len(steps), 19)
        return paths.taken

    def test_every_write_type_gives_the_events_of_two_full_snapshots(self) -> None:
        taken = self.scenario(Dual)
        self.assertEqual(taken[0], "full")  # The company folder does not exist yet.
        self.assertEqual(set(taken[1:]), {"incremental"})

    def test_every_write_type_when_the_change_set_shares_the_run(self) -> None:
        taken = self.scenario(WithChangeSet)
        self.assertEqual(set(taken[1:]), {"incremental"})


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.books = Books(self.root)
        self.entity = self.books.entity
        self.books.init()
        self.books.ingest("first.json", FIRST_IMPORT)
        self.books.run("queue", "propose", "--entity", str(self.entity), "--source-id", "syn-001",
                       "--category", "Expenses:Software", "--reasoning", "Tool.")
        self.books.run("queue", "confirm", "--entity", str(self.entity), "--item", "syn-001")

    def both(self, action, *, path: str = "incremental", nonempty: bool = True):
        """Run ``action`` under both captures. Returns the events after they equal the full events."""
        with work.capture(self.entity, incremental=False) as full:
            with work.capture(self.entity) as fast:
                action()
        self.assertEqual(fast.events, full.events)
        self.assertEqual(fast.path, path)
        self.assertEqual(full.path, "full")
        if nonempty:
            self.assertTrue(fast.events)
        return fast.events

    def kinds(self, events: list[dict]) -> list[str]:
        return sorted(event["kind"] for event in events)

    @property
    def pending_path(self) -> Path:
        return self.entity / "staging" / "pending-categorization.json"


class FastPathTests(Fixture):
    def test_the_capture_reads_no_snapshot_and_no_ledger_source_list(self) -> None:
        self.both(lambda: None, nonempty=False)  # Build the index.
        with mock.patch.object(work, "take_snapshot", side_effect=AssertionError("full snapshot")), \
                mock.patch.object(work, "_load_ledger", side_effect=AssertionError("ledger read in full")):
            with work.capture(self.entity) as receipt:
                run_cli(["queue", "confirm", "--entity", str(self.entity), "--item", "syn-001-none"])
                run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-002",
                         "--category", "Income:Consulting", "--reasoning", "Payout."])
        self.assertEqual(receipt.path, "incremental")
        self.assertEqual(self.kinds(receipt.events), ["categorized"])

    def test_a_second_capture_finds_the_index_up_to_date(self) -> None:
        self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-002",
                                   "--category", "Income:Consulting", "--reasoning", "Payout."]))
        with mock.patch.object(change_capture, "_rebuild", side_effect=AssertionError("index rebuilt")):
            self.both(lambda: run_cli(["queue", "confirm", "--entity", str(self.entity), "--item", "syn-002"]))

    def test_an_import_of_known_rows_gives_no_events(self) -> None:
        events = self.both(lambda: self.books.ingest("again.json", FIRST_IMPORT), nonempty=False)
        self.assertEqual(events, [])

    def test_a_pending_source_that_a_ledger_entry_already_holds_is_not_imported_again(self) -> None:
        # syn-001 is in the ledger. A hand edit puts it in the pending list again: it is no new source.
        row = {"id": "syn-001", "date": "2026-10-01", "amount": "-54.99", "description": "DESIGN TOOL MONTHLY",
               "accountName": "Operating Checking", "accountType": "checking"}
        rows = json.loads(self.pending_path.read_text(encoding="utf-8"))
        self.both(lambda: self.pending_path.write_text(json.dumps(rows + [row]), encoding="utf-8"), nonempty=False)

    def test_an_entry_for_a_source_that_stays_in_the_pending_list_is_not_an_import(self) -> None:
        # A direct ledger entry for a source that was already pending (and stays there) has a known source id.
        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                conn.execute("INSERT INTO entries (date, narration, source_id) VALUES ('2026-10-04', 'x', 'syn-004')")
                entry_id = conn.execute("SELECT id FROM entries WHERE source_id = 'syn-004'").fetchone()[0]
                conn.execute("INSERT INTO postings (entry_id, account, amount) VALUES (?, ?, '-42.00')",
                             (entry_id, CHECKING))
                conn.execute("INSERT INTO postings (entry_id, account, amount) VALUES (?, 'Expenses:Fees', '42.00')",
                             (entry_id,))
                conn.commit()

        events = self.both(action)
        self.assertEqual(self.kinds(events), ["recorded"])

    def test_the_capture_with_no_company_folder(self) -> None:
        with work.capture(self.root / "missing") as receipt:
            pass
        self.assertEqual((receipt.events, receipt.path), ([], "full"))
        with work.capture(None) as receipt:
            pass
        self.assertEqual((receipt.events, receipt.path), ([], "none"))


class FallbackTests(Fixture):
    """A write that does not use a helper, and a state that the index cannot describe, give the full events."""

    def test_a_write_to_the_pending_list_that_bypasses_the_helpers(self) -> None:
        row = _txn("hand-1", "2026-10-09", "-9.00", "HAND ADDED")
        rows = json.loads(self.pending_path.read_text(encoding="utf-8"))
        events = self.both(lambda: self.pending_path.write_text(json.dumps(rows + [row]), encoding="utf-8"))
        self.assertEqual(self.kinds(events), ["imported"])

    def test_a_bypass_after_a_journaled_pending_removal(self) -> None:
        company = load_entity(self.entity)

        def action() -> None:
            queue._remove_from_pending_categorization(company, "syn-002")
            rows = json.loads(self.pending_path.read_text(encoding="utf-8"))
            rows.append(_txn("hand-2", "2026-10-09", "-9.00", "HAND ADDED"))
            self.pending_path.write_text(json.dumps(rows), encoding="utf-8")

        self.assertEqual(self.kinds(self.both(action)), ["imported"])

    def test_an_import_through_the_helpers(self) -> None:
        events = self.both(lambda: self.books.ingest("more.json", FIRST_IMPORT + [_txn("syn-009", "2026-10-09", "-5.00", "NEW")]))
        self.assertEqual(self.kinds(events), ["imported"])

    def test_an_in_place_edit_of_a_review_item(self) -> None:
        self.books.run("queue", "propose", "--entity", str(self.entity), "--source-id", "syn-002",
                       "--category", "Income:Consulting", "--reasoning", "Payout.")
        path = self.entity / "review-queue" / "syn-002.json"
        item = json.loads(path.read_text(encoding="utf-8"))
        item["proposed_category"] = "Income:Other"
        events = self.both(lambda: path.write_text(json.dumps(item, indent=2, sort_keys=True), encoding="utf-8"))
        self.assertEqual(self.kinds(events), ["categorized"])

    def test_a_status_change_by_hand(self) -> None:
        self.books.run("queue", "propose", "--entity", str(self.entity), "--source-id", "syn-002",
                       "--category", "Income:Consulting", "--reasoning", "Payout.")
        path = self.entity / "review-queue" / "syn-002.json"
        item = json.loads(path.read_text(encoding="utf-8"))
        item["status"] = "withdrawn"
        events = self.both(lambda: path.write_text(json.dumps(item), encoding="utf-8"))
        self.assertEqual(self.kinds(events), ["corrected"])

    def test_a_new_and_a_removed_review_item_file(self) -> None:
        self.books.run("queue", "propose", "--entity", str(self.entity), "--source-id", "syn-002",
                       "--category", "Income:Consulting", "--reasoning", "Payout.")
        folder = self.entity / "review-queue"
        item = json.loads((folder / "syn-002.json").read_text(encoding="utf-8"))
        item["source_id"] = "syn-009"

        def action() -> None:
            (folder / "syn-009.json").write_text(json.dumps(item), encoding="utf-8")
            (folder / "syn-002.json").unlink()

        self.both(action)

    def test_two_item_files_with_one_source_id_are_read_in_full(self) -> None:
        self.books.run("queue", "propose", "--entity", str(self.entity), "--source-id", "syn-002",
                       "--category", "Income:Consulting", "--reasoning", "Payout.")
        folder = self.entity / "review-queue"
        item = json.loads((folder / "syn-002.json").read_text(encoding="utf-8"))
        item["proposed_category"] = "Income:Other"
        self.both(lambda: (folder / "zz-copy.json").write_text(json.dumps(item), encoding="utf-8"))

    def test_a_review_item_that_the_queue_archives_and_the_archive_after_it(self) -> None:
        self.books.run("queue", "propose", "--entity", str(self.entity), "--source-id", "syn-002",
                       "--category", "Income:Consulting", "--reasoning", "Payout.")
        self.books.run("queue", "confirm", "--entity", str(self.entity), "--item", "syn-002")
        company = load_entity(self.entity)
        self.both(lambda: queue.compact_closed_items(company), nonempty=False)
        self.assertTrue((self.entity / "review-queue" / "archive").is_dir())
        events = self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                                            "--category", "Expenses:Software", "--reasoning", "Hosting."]))
        self.assertEqual(self.kinds(events), ["categorized"])

    def test_a_hand_edit_of_the_reconciliation_file(self) -> None:
        self.books.run("reconcile", "--entity", str(self.entity), "--account", CHECKING,
                       "--source-balance", "100.00", "--as-of", "2026-10-12")
        path = self.entity / "reports" / "reconciliation.json"
        records = json.loads(path.read_text(encoding="utf-8"))
        records[0]["status"] = "clean"
        records[0]["discrepancy"] = "0.00"
        events = self.both(lambda: path.write_text(json.dumps(records), encoding="utf-8"))
        self.assertEqual(self.kinds(events), ["reconciled"])

    def test_a_hand_edit_of_the_duplicate_candidates(self) -> None:
        path = self.entity / "staging" / "duplicate-candidates.json"
        rows = [{"source_id": "dup-1", "status": "duplicate-candidate", "date": "2026-10-08", "account": CHECKING}]
        events = self.both(lambda: path.write_text(json.dumps(rows), encoding="utf-8"))
        self.assertEqual(self.kinds(events), ["flagged"])

    def test_a_direct_ledger_write_that_does_not_use_the_store(self) -> None:
        def action() -> None:
            with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
                conn.execute("INSERT INTO entries (date, narration, source_id) VALUES ('2026-10-20', 'direct', 'direct-1')")
                entry_id = conn.execute("SELECT id FROM entries WHERE source_id = 'direct-1'").fetchone()[0]
                for account, amount in ((CHECKING, "-3.00"), ("Expenses:Fees", "3.00")):
                    conn.execute("INSERT INTO postings (entry_id, account, amount) VALUES (?, ?, ?)",
                                 (entry_id, account, amount))
                conn.execute("INSERT INTO accounts (name, currency, open_date) VALUES ('Expenses:Direct', 'USD', '2000-01-01')")
                conn.commit()

        events = self.both(action)
        self.assertEqual(self.kinds(events), ["imported", "recorded"])

    def test_a_ledger_that_the_command_replaces(self) -> None:
        other = Books(self.root / "other")
        other.root.mkdir()
        other.init()
        other.ingest("other.json", [_txn("syn-001", "2026-10-01", "-1.00", "OTHER"), _txn("oth-2", "2026-10-02", "-2.00", "OTHER TWO")])
        other.run("queue", "propose", "--entity", str(other.entity), "--source-id", "oth-2",
                  "--category", "Expenses:Fees", "--reasoning", "x.")
        other.run("queue", "confirm", "--entity", str(other.entity), "--item", "oth-2")
        other.run("queue", "propose", "--entity", str(other.entity), "--source-id", "syn-001",
                  "--category", "Expenses:Fees", "--reasoning", "x.")
        other.run("queue", "confirm", "--entity", str(other.entity), "--item", "syn-001")
        self.both(lambda: shutil.copy(other.entity / "ledger.sqlite", self.entity / "ledger.sqlite"))

    def test_a_ledger_that_the_command_replaces_with_an_older_one(self) -> None:
        copy = self.root / "older.sqlite"
        shutil.copy(self.entity / "ledger.sqlite", copy)
        self.books.run("queue", "propose", "--entity", str(self.entity), "--source-id", "syn-002",
                       "--category", "Income:Consulting", "--reasoning", "Payout.")
        self.books.run("queue", "confirm", "--entity", str(self.entity), "--item", "syn-002")
        self.both(lambda: shutil.copy(copy, self.entity / "ledger.sqlite"), nonempty=False)

    def test_a_command_that_ran_without_a_capture_leaves_a_stale_index(self) -> None:
        run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-003",
                 "--category", "Expenses:Meals", "--reasoning", "Lunch."])  # No capture: the index is stale.
        run_cli(["queue", "confirm", "--entity", str(self.entity), "--item", "syn-003"])
        events = self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                                            "--category", "Expenses:Software", "--reasoning", "Hosting."]))
        self.assertEqual(self.kinds(events), ["categorized"])

    def test_a_missing_index_is_built_again(self) -> None:
        (self.entity / change_capture.INDEX_NAME).unlink()
        events = self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                                            "--category", "Expenses:Software", "--reasoning", "Hosting."]))
        self.assertEqual(self.kinds(events), ["categorized"])

    def test_an_index_that_is_not_a_database_gives_the_full_comparison(self) -> None:
        (self.entity / change_capture.INDEX_NAME).write_bytes(b"this is not a database" * 100)
        self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                                   "--category", "Expenses:Software", "--reasoning", "Hosting."]), path="full")

    def test_a_folder_that_cannot_hold_the_index_gives_the_full_comparison(self) -> None:
        with mock.patch.object(change_capture, "Index", side_effect=sqlite3.OperationalError("readonly")):
            self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                                       "--category", "Expenses:Software", "--reasoning", "Hosting."]), path="full")

    def test_a_file_that_cannot_be_read_before_the_command_gives_the_full_comparison(self) -> None:
        (self.entity / "staging" / "duplicate-candidates.json").write_text("{not json", encoding="utf-8")
        self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                                   "--category", "Expenses:Software", "--reasoning", "Hosting."]), path="full")

    def test_a_file_that_the_command_corrupts_is_compared_with_the_saved_copy(self) -> None:
        path = self.entity / "staging" / "duplicate-candidates.json"

        def action() -> None:
            run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                     "--category", "Expenses:Software", "--reasoning", "Hosting."])
            path.write_text("{not json", encoding="utf-8")

        events = self.both(action, path="saved")
        self.assertEqual(self.kinds(events), ["categorized"])

    def test_a_saved_copy_compare_after_a_pending_removal_and_a_ledger_entry(self) -> None:
        path = self.entity / "staging" / "duplicate-candidates.json"

        def action() -> None:
            self.books.run("queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                           "--category", "Expenses:Software", "--reasoning", "Hosting.")
            self.books.run("queue", "confirm", "--entity", str(self.entity), "--item", "syn-004")
            path.write_text("[1, 2", encoding="utf-8")

        events = self.both(action, path="saved")
        self.assertEqual(self.kinds(events), ["recorded"])

    def test_a_failed_probe_gives_the_saved_copy_compare(self) -> None:
        with mock.patch.object(work, "_events_from_probe", side_effect=RuntimeError("boom")):
            self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                                       "--category", "Expenses:Software", "--reasoning", "Hosting."]), path="saved")


class SharedRunTests(Fixture):
    def propose(self, source_id: str = "syn-004") -> None:
        run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", source_id,
                 "--category", "Expenses:Software", "--reasoning", "Hosting."])

    def test_a_work_capture_inside_a_change_set_capture(self) -> None:
        with changeset.capture(self.entity, incremental=False) as full_set, work.capture(self.entity, incremental=False) as full:
            with changeset.capture(self.entity) as fast_set:
                with work.capture(self.entity) as fast:
                    self.propose()
        self.assertEqual(fast.path, "incremental")
        self.assertEqual(fast.events, full.events)
        self.assertEqual(fast_set.change_set, full_set.change_set)
        self.assertTrue(fast.events)

    def test_a_change_set_capture_inside_a_work_capture(self) -> None:
        with changeset.capture(self.entity, incremental=False) as full_set, work.capture(self.entity, incremental=False) as full:
            with work.capture(self.entity) as fast:
                with changeset.capture(self.entity) as fast_set:
                    self.propose()
        self.assertEqual(fast.path, "incremental")
        self.assertEqual(fast.events, full.events)
        self.assertEqual(fast_set.change_set, full_set.change_set)

    def test_the_outer_capture_sees_a_write_after_the_inner_capture_ended(self) -> None:
        with work.capture(self.entity, incremental=False) as full:
            with work.capture(self.entity) as outer:
                with work.capture(self.entity) as inner:
                    self.propose("syn-004")
                self.propose("syn-005")
        self.assertEqual(outer.path, "incremental")
        self.assertEqual(inner.path, "incremental")
        self.assertEqual(outer.events, full.events)
        self.assertEqual(len(outer.events[0]["subjects"]), 2)
        self.assertEqual(len(inner.events[0]["subjects"]), 1)

    def test_the_index_is_up_to_date_after_a_shared_run(self) -> None:
        with work.capture(self.entity) as _outer:
            with changeset.capture(self.entity):
                self.propose()
        with mock.patch.object(change_capture, "_rebuild", side_effect=AssertionError("index rebuilt")):
            with work.capture(self.entity) as receipt:
                self.propose("syn-005")
        self.assertEqual(receipt.path, "incremental")

    def test_a_capture_that_starts_after_a_write_cannot_join_and_reads_in_full(self) -> None:
        with changeset.capture(self.entity) as outer:
            self.propose("syn-004")
            with work.capture(self.entity) as late:
                self.propose("syn-005")
        self.assertEqual(late.path, "full")
        self.assertEqual(len(late.events[0]["subjects"]), 1)
        self.assertTrue(outer.change_set["complete"])
        self.assertEqual(len(outer.change_set["review_queue"]), 2)

    def test_the_capture_triggers_are_gone_after_a_shared_run(self) -> None:
        with work.capture(self.entity):
            with changeset.capture(self.entity):
                self.propose()
        with closing(sqlite3.connect(self.entity / "ledger.sqlite")) as conn:
            names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master")]
        self.assertFalse([name for name in names if name.startswith(change_capture.TRIGGER_PREFIX)])


class FailureTests(Fixture):
    """When the books cannot be read the receipt says so. It is never an empty receipt that looks complete."""

    def propose(self) -> None:
        run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                 "--category", "Expenses:Software", "--reasoning", "Hosting."])

    def test_a_locked_ledger_gives_the_saved_read_once_and_the_same_events(self) -> None:
        real = work._load_new_entries
        calls = []

        def flaky(path, after):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(path, after)

        with work.capture(self.entity, incremental=False) as full:
            with mock.patch.object(work, "_load_new_entries", flaky), work.capture(self.entity) as fast:
                self.propose()
        self.assertEqual(fast.path, "saved")
        self.assertTrue(fast.complete)
        self.assertEqual(fast.events, full.events)

    def test_a_ledger_that_cannot_be_read_twice_gives_an_incomplete_receipt_and_no_commit(self) -> None:
        self.both(lambda: None, nonempty=False)  # Index is current.
        with mock.patch.object(work, "_load_new_entries", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.Error), work.capture(self.entity) as receipt:
                self.propose()
        self.assertEqual((receipt.complete, receipt.path, receipt.events), (False, "failed", []))
        self.assertEqual(receipt.reason, "state_unreadable")  # A locked ledger is an unreadable state.
        with closing(sqlite3.connect(self.entity / change_capture.INDEX_NAME)) as conn:
            saved = conn.execute("SELECT COUNT(*) FROM rows WHERE section = 'queue' AND key = 'syn-004'").fetchone()[0]
        self.assertEqual(saved, 0)  # The new state of the command was not saved.
        # The index was not committed: the next capture finds the files changed and still gives the right events.
        events = self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-005",
                                            "--category", "Expenses:Software", "--reasoning", "Office."]))
        self.assertEqual(self.kinds(events), ["categorized"])

    def test_the_cli_writes_complete_false_for_an_incomplete_capture(self) -> None:
        out = self.root / "incomplete.json"
        with mock.patch.object(work, "_load_new_entries", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.Error):
                run_cli(["--work-out", str(out), "queue", "propose", "--entity", str(self.entity),
                         "--source-id", "syn-004", "--category", "Expenses:Software", "--reasoning", "x."])
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((document["complete"], document["work"]), (False, []))
        self.assertIn("reason", document)
        self.books.run("queue", "propose", "--entity", str(self.entity), "--source-id", "syn-005",
                       "--category", "Expenses:Software", "--reasoning", "x.")  # A complete document has no new fields

    def probe_failure(self):
        return mock.patch.object(work, "_events_from_probe", side_effect=RuntimeError("boom"))

    def test_a_deleted_index_gives_an_incomplete_receipt_not_an_empty_one(self) -> None:
        def action() -> None:
            self.propose()
            (self.entity / change_capture.INDEX_NAME).unlink()

        with self.probe_failure(), self.assertRaises(Exception), work.capture(self.entity) as receipt:
            action()
        self.assertEqual((receipt.complete, receipt.path, receipt.events), (False, "failed", []))

    def test_a_replaced_index_gives_an_incomplete_receipt(self) -> None:
        other = self.root / "other-index.sqlite"

        def action() -> None:
            self.propose()
            path = self.entity / change_capture.INDEX_NAME
            shutil.copy(path, other)
            path.unlink()
            shutil.copy(other, path)  # Same content, new file: another inode.

        with self.probe_failure(), self.assertRaises(Exception), work.capture(self.entity) as receipt:
            action()
        self.assertEqual((receipt.complete, receipt.path), (False, "failed"))

    def test_a_changed_index_gives_an_incomplete_receipt(self) -> None:
        def action() -> None:
            self.propose()
            with closing(sqlite3.connect(self.entity / change_capture.INDEX_NAME)) as conn:
                conn.execute("DELETE FROM rows WHERE section = 'pending'")
                conn.commit()

        with self.probe_failure(), self.assertRaises(Exception), work.capture(self.entity) as receipt:
            action()
        self.assertEqual((receipt.complete, receipt.path), (False, "failed"))

    def test_an_index_that_was_not_touched_still_gives_the_saved_read(self) -> None:
        with self.probe_failure(), work.capture(self.entity) as receipt:
            self.propose()
        self.assertEqual((receipt.complete, receipt.path), (True, "saved"))
        self.assertEqual(self.kinds(receipt.events), ["categorized"])


class FinalizeFailureTests(Fixture):
    def test_a_failure_in_the_full_path_gives_an_incomplete_receipt(self) -> None:
        with mock.patch.object(work, "work_events", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError), work.capture(self.entity, incremental=False) as receipt:
                pass
        self.assertEqual((receipt.complete, receipt.path, receipt.events), (False, "failed", []))

    def test_the_cli_document_of_a_failed_full_path_is_incomplete(self) -> None:
        (self.entity / "staging" / "duplicate-candidates.json").write_text("{not json", encoding="utf-8")  # Full path.
        out = self.root / "full-failed.json"
        with mock.patch.object(work, "work_events", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            run_cli(["--work-out", str(out), "queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                     "--category", "Expenses:Software", "--reasoning", "x."])
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((document["complete"], document["work"]), (False, []))

    def test_a_failure_in_the_full_snapshot_after_the_command(self) -> None:
        real = work.take_snapshot
        calls = []

        def second_fails(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("disk")
            return real(*args, **kwargs)

        with mock.patch.object(work, "take_snapshot", second_fails):
            with self.assertRaises(OSError), work.capture(self.entity, incremental=False) as receipt:
                pass
        self.assertEqual((receipt.complete, receipt.reason), (False, "state_unreadable"))


class SourceTableTests(Fixture):
    """A ledger source table that was dropped, emptied or changed is built again, never trusted."""

    def known_pending_row(self) -> list:
        row = {"id": "syn-001", "date": "2026-10-01", "amount": "-54.99", "description": "DESIGN TOOL MONTHLY",
               "accountName": "Operating Checking", "accountType": "checking"}
        return json.loads(self.pending_path.read_text(encoding="utf-8")) + [row]

    def damage(self, sql: str) -> None:
        with closing(sqlite3.connect(self.entity / change_capture.INDEX_NAME)) as conn:
            conn.execute(sql)
            conn.commit()

    def check(self, sql: str) -> None:
        self.both(lambda: None, nonempty=False)
        self.damage(sql)
        rows = self.known_pending_row()
        events = self.both(lambda: self.pending_path.write_text(json.dumps(rows), encoding="utf-8"), nonempty=False)
        self.assertEqual(events, [])  # syn-001 is in the ledger: it is no new source.

    def test_a_dropped_table(self) -> None:
        self.check("DROP TABLE ledger_sources")

    def test_an_emptied_table(self) -> None:
        self.check("DELETE FROM ledger_sources")

    def test_a_same_count_edit_of_one_id(self) -> None:
        self.check("UPDATE ledger_sources SET source_id = 'syn-001-other' WHERE source_id = 'syn-001'")

    def test_a_same_count_edit_that_adds_a_wrong_id_for_a_removed_one(self) -> None:
        self.check("UPDATE ledger_sources SET source_id = 'invented-1' WHERE source_id = 'syn-001'")

    def test_a_missing_signature_file_builds_the_index_again(self) -> None:
        self.both(lambda: None, nonempty=False)
        (self.entity / change_capture.SIGNATURE_NAME).unlink()
        rows = self.known_pending_row()
        self.both(lambda: self.pending_path.write_text(json.dumps(rows), encoding="utf-8"), nonempty=False)

    def test_an_untouched_index_is_trusted_without_a_scan(self) -> None:
        self.both(lambda: None, nonempty=False)
        with mock.patch.object(change_capture.Index, "all_sources", side_effect=AssertionError("scan")), \
                mock.patch.object(change_capture, "_rebuild", side_effect=AssertionError("rebuilt")):
            self.both(lambda: None, nonempty=False)

    def test_a_table_with_one_id_removed(self) -> None:
        self.check("DELETE FROM ledger_sources WHERE source_id = 'syn-001'")

    def test_the_stored_count_follows_the_table(self) -> None:
        self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                                   "--category", "Expenses:Software", "--reasoning", "x."]), nonempty=False)
        self.books.run("queue", "confirm", "--entity", str(self.entity), "--item", "syn-004")
        index = change_capture.Index(self.entity)
        try:
            self.assertTrue(index.sources_intact())
        finally:
            index.close()


class IndexSealTests(SourceTableTests):
    """The signature of the index cannot certify an edit that it did not see."""

    test_a_table_with_one_id_removed = test_a_dropped_table = test_an_emptied_table = None
    test_a_same_count_edit_of_one_id = test_a_same_count_edit_that_adds_a_wrong_id_for_a_removed_one = None
    test_a_missing_signature_file_builds_the_index_again = test_an_untouched_index_is_trusted_without_a_scan = None
    test_the_stored_count_follows_the_table = None

    def wrong_id_check(self, path: str = "incremental") -> None:
        rows = self.known_pending_row()
        events = self.both(lambda: self.pending_path.write_text(json.dumps(rows), encoding="utf-8"),
                           path=path, nonempty=False)
        self.assertEqual(events, [])  # syn-001 is in the ledger: it is no new source.

    def test_the_index_is_never_in_wal_mode(self) -> None:
        self.both(lambda: None, nonempty=False)
        with closing(sqlite3.connect(self.entity / change_capture.INDEX_NAME)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_a_write_in_wal_mode_after_the_signature_is_not_trusted(self) -> None:
        self.both(lambda: None, nonempty=False)
        path = self.entity / change_capture.INDEX_NAME
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA wal_autocheckpoint = 0")
            change_capture._write_stamp(self.entity, change_capture._index_stamp(self.entity))  # The old attack: seal first.
            conn.execute("UPDATE ledger_sources SET source_id = 'invented-1' WHERE source_id = 'syn-001'")
            self.assertTrue(Path(str(path) + "-wal").exists())
            self.assertFalse(change_capture.stamp_matches(self.entity))  # The side file alone makes it untrusted.
            self.wrong_id_check("full")  # The index cannot be opened for a write while the other connection holds it.
        finally:
            conn.close()

    def test_a_write_between_the_last_commit_and_the_seal_leaves_no_signature(self) -> None:
        self.both(lambda: None, nonempty=False)
        path = self.entity / change_capture.INDEX_NAME
        done: list[int] = []

        class Spy:
            def __init__(self, conn) -> None:
                self.conn = conn

            def execute(self, sql, *args):
                result = self.conn.execute(sql, *args)
                if sql == "COMMIT" and not done:
                    done.append(1)  # Another process writes after the commit and before the seal.
                    with closing(sqlite3.connect(path, timeout=5)) as other:
                        other.execute("UPDATE ledger_sources SET source_id = 'invented-1' WHERE source_id = 'syn-001'")
                        other.commit()
                return result

            def __getattr__(self, name):
                return getattr(self.conn, name)

        real_init = change_capture.Index.__init__

        def init(index, *args, **kwargs):
            real_init(index, *args, **kwargs)
            index.conn = Spy(index.conn)

        with mock.patch.object(change_capture.Index, "__init__", init):
            with work.capture(self.entity) as receipt:
                pass
        self.assertTrue(done)
        self.assertEqual(receipt.path, "full")  # The seal failed: the capture did not trust the index.
        self.assertFalse(change_capture.stamp_matches(self.entity))
        self.wrong_id_check()


class IndexLockTests(IndexSealTests):
    """The index is checked, sealed and snapshotted under one exclusive hold."""

    test_a_table_with_one_id_removed = test_a_dropped_table = test_an_emptied_table = None
    test_a_same_count_edit_of_one_id = test_a_same_count_edit_that_adds_a_wrong_id_for_a_removed_one = None
    test_a_missing_signature_file_builds_the_index_again = test_an_untouched_index_is_trusted_without_a_scan = None
    test_the_stored_count_follows_the_table = None

    def other_write(self) -> str:
        path = self.entity / change_capture.INDEX_NAME
        try:
            with closing(sqlite3.connect(path, timeout=0.05)) as other:
                other.execute("UPDATE ledger_sources SET source_id = 'invented-1' WHERE source_id = 'syn-001'")
                other.commit()
        except sqlite3.OperationalError as exc:
            return str(exc)
        return "written"

    def test_no_connection_can_write_the_index_between_the_signature_check_and_the_seal(self) -> None:
        self.both(lambda: None, nonempty=False)
        seen: list[str] = []
        real_check = change_capture.stamp_matches
        real_commit = change_capture.Index.commit

        def check(root):
            seen.append(("check", self.other_write()))
            return real_check(root)

        def commit(index):
            seen.append(("before commit", self.other_write()))
            return real_commit(index)

        with mock.patch.object(change_capture, "stamp_matches", check), \
                mock.patch.object(change_capture.Index, "commit", commit):
            with work.capture(self.entity) as receipt:
                pass
        self.assertTrue(seen)
        self.assertEqual({what for _, what in seen[:2]}, {"database is locked"})
        self.assertEqual(receipt.path, "incremental")

    def test_a_write_to_the_index_after_the_seal_and_before_the_command_is_found(self) -> None:
        self.both(lambda: None, nonempty=False)
        done: list[str] = []
        real = change_capture.Run.begin

        def begin(run) -> None:
            real(run)
            done.append(self.other_write())  # The start-side hold ended: this write goes through.

        with mock.patch.object(change_capture.Run, "begin", begin):
            with self.assertRaises(Exception), work.capture(self.entity) as receipt:
                pass
        self.assertEqual(done, ["written"])
        self.assertEqual((receipt.complete, receipt.events), (False, []))  # Never a sealed, trusted wrong index.
        self.wrong_id_check()

    def test_a_write_to_the_index_during_the_command_is_found(self) -> None:
        self.both(lambda: None, nonempty=False)
        with self.assertRaises(Exception), work.capture(self.entity) as receipt:
            self.assertEqual(self.other_write(), "written")
        self.assertEqual((receipt.complete, receipt.events), (False, []))
        self.wrong_id_check()

    def test_a_write_in_the_same_clock_tick_is_found_by_the_change_counter(self) -> None:
        """Simulate a file system whose change time has not moved: inode, ctime and generation stay equal."""
        import os
        import types
        self.both(lambda: None, nonempty=False)
        real_stat = os.stat
        frozen: list[int] = []

        def stat(path, *args, **kwargs):
            status = real_stat(path, *args, **kwargs)
            if not str(path).endswith(change_capture.INDEX_NAME):
                return status
            if not frozen:
                frozen.append(status.st_ctime_ns)
            return types.SimpleNamespace(st_ino=status.st_ino, st_size=status.st_size, st_mtime_ns=status.st_mtime_ns,
                                         st_ctime_ns=frozen[0], st_mode=status.st_mode)

        with mock.patch.object(change_capture.os, "stat", stat):
            with self.assertRaises(Exception), work.capture(self.entity) as receipt:
                self.assertEqual(self.other_write(), "written")
        self.assertTrue(frozen)
        self.assertEqual((receipt.complete, receipt.events), (False, []))
        self.wrong_id_check()

    def test_the_saved_read_also_checks_the_change_counter(self) -> None:
        import os
        import types
        self.both(lambda: None, nonempty=False)
        real_stat = os.stat
        frozen: list[int] = []

        def stat(path, *args, **kwargs):
            status = real_stat(path, *args, **kwargs)
            if not str(path).endswith(change_capture.INDEX_NAME):
                return status
            if not frozen:
                frozen.append(status.st_ctime_ns)
            return types.SimpleNamespace(st_ino=status.st_ino, st_size=status.st_size, st_mtime_ns=status.st_mtime_ns,
                                         st_ctime_ns=frozen[0], st_mode=status.st_mode)

        with mock.patch.object(change_capture.os, "stat", stat), \
                mock.patch.object(work, "_events_from_probe", side_effect=RuntimeError("boom")):
            with self.assertRaises(Exception), work.capture(self.entity) as receipt:
                self.assertEqual(self.other_write(), "written")
        self.assertEqual((receipt.complete, receipt.path), (False, "failed"))

    def test_an_index_that_cannot_be_opened_leaks_no_connection(self) -> None:
        opened: list[sqlite3.Connection] = []
        real = sqlite3.connect

        def connect(*args, **kwargs):
            conn = real(*args, **kwargs)
            opened.append(conn)
            return conn

        with mock.patch.object(change_capture.sqlite3, "connect", connect), \
                mock.patch.object(change_capture, "stamp_matches", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                change_capture.Index(self.entity, exclusive=True)
        self.assertTrue(opened)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")  # Closed.

    def test_the_saved_read_holds_the_index_lock_from_the_identity_check_to_the_end(self) -> None:
        results: list[str] = []
        real = change_capture.Index.all_sources

        def all_sources(index):
            results.append(self.other_write())  # A write while the saved copy is read must be refused.
            return real(index)

        with mock.patch.object(work, "_events_from_probe", side_effect=RuntimeError("boom")), \
                mock.patch.object(change_capture.Index, "all_sources", all_sources):
            with work.capture(self.entity) as receipt:
                run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                         "--category", "Expenses:Software", "--reasoning", "x."])
        self.assertEqual(results, ["database is locked"])
        self.assertEqual((receipt.complete, receipt.path), (True, "saved"))

    def test_stray_side_files_are_removed_and_the_next_capture_is_warm(self) -> None:
        self.both(lambda: None, nonempty=False)
        path = self.entity / change_capture.INDEX_NAME
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).write_bytes(b"stray")
        self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                                   "--category", "Expenses:Software", "--reasoning", "x."]))  # Rebuilds.
        for suffix in ("-wal", "-shm"):
            self.assertFalse(Path(str(path) + suffix).exists())
        with mock.patch.object(change_capture, "_rebuild", side_effect=AssertionError("rebuilt")):
            self.both(lambda: run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-005",
                                       "--category", "Expenses:Software", "--reasoning", "x."]))


class CompanyLockTests(Fixture):
    def test_the_company_write_lock_is_held_for_the_whole_capture(self) -> None:
        order: list[str] = []
        real = changeset.company_write_lock
        real_start = change_capture.start

        @contextmanager
        def spy(path):
            order.append("lock")
            with real(path) as held:
                yield held
            order.append("unlock")

        def start(root):
            order.append("start")
            return real_start(root)

        with mock.patch.object(changeset, "company_write_lock", spy), mock.patch.object(change_capture, "start", start):
            with work.capture(self.entity) as receipt:
                order.append("command")
                run_cli(["queue", "propose", "--entity", str(self.entity), "--source-id", "syn-004",
                         "--category", "Expenses:Software", "--reasoning", "x."])  # Takes the lock again: re-entrant.
                order.append("command end")
        self.assertEqual(order, ["lock", "start", "command", "command end", "unlock"])
        self.assertEqual((receipt.complete, receipt.path), (True, "incremental"))

    def test_a_folder_that_cannot_be_locked_gives_the_full_comparison(self) -> None:
        @contextmanager
        def unlocked(path):
            yield False

        with mock.patch.object(changeset, "company_write_lock", unlocked):
            with work.capture(self.entity) as receipt:
                pass
        self.assertEqual(receipt.path, "full")


class FullFallbackLockTests(Fixture):
    def lock(self):
        """A second connection that holds the ledger exclusively."""
        conn = sqlite3.connect(self.entity / "ledger.sqlite", timeout=0.05, isolation_level=None)
        conn.execute("BEGIN EXCLUSIVE")
        return conn

    def test_a_locked_ledger_before_the_command_gives_an_incomplete_receipt(self) -> None:
        locker = self.lock()
        try:
            with mock.patch.object(work, "_connect_read_only", lambda path: _short(path)):
                with self.assertRaises(sqlite3.Error), work.capture(self.entity, incremental=False) as receipt:
                    pass
        finally:
            locker.close()
        self.assertEqual((receipt.complete, receipt.path, receipt.events), (False, "failed", []))

    def test_a_locked_ledger_after_the_command_gives_an_incomplete_receipt(self) -> None:
        locker = None
        try:
            with mock.patch.object(work, "_connect_read_only", lambda path: _short(path)):
                with self.assertRaises(sqlite3.Error), work.capture(self.entity, incremental=False) as receipt:
                    locker = self.lock()
        finally:
            if locker is not None:
                locker.close()
        self.assertEqual((receipt.complete, receipt.events), (False, []))

    def test_the_cli_document_is_incomplete_for_a_locked_ledger_in_the_full_path(self) -> None:
        out = self.root / "locked.json"
        real = work.capture

        def full(path, **kwargs):
            return real(path, incremental=False)

        locker = self.lock()
        try:
            with mock.patch.object(work, "capture", full), \
                    mock.patch.object(work, "_connect_read_only", lambda path: _short(path)):
                with self.assertRaises(sqlite3.Error):
                    run_cli(["--work-out", str(out), "queue", "propose", "--entity", str(self.entity),
                             "--source-id", "syn-004", "--category", "Expenses:Software", "--reasoning", "x."])
        finally:
            locker.close()
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((document["complete"], document["work"]), (False, []))


def _short(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    unittest.main()
