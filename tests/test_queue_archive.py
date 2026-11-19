"""Decided review items in one archive file for each month (queue.compact_closed_items).

A company that reviews every bank row kept one small file for each row, and a file never left the folder. These tests prove
that the archive changes only where the items are kept: every read, the ledger and the audit log are the same, a decided item
can still be shown and corrected, an interrupted move loses nothing, and the file count grows by one for each month.
All data is synthetic."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from bookkeeping import queue, work  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from test_queue import SESSION, TS, _add_pending_categorization, _make_entity  # noqa: E402

ROWS = 12


def decided_files(entity):
    return sorted(p.name for p in (entity.path / "review-queue").glob("*.json"))


def archives(entity):
    return sorted(p.name for p in (entity.path / "review-queue" / "archive").glob("*.json"))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.entity = _make_entity(Path(self.tmp.name))
        for number in range(ROWS + 2):
            _add_pending_categorization(self.entity, f"row-{number}", f"Synthetic vendor {number}", amount=f"{10 + number}.00",
                                        txn_date=f"2026-{1 + number % 3:02d}-{10 + number}")
        for number in range(ROWS):
            queue.propose(self.entity, f"row-{number}", "Expenses:Software", "Synthetic service")
            queue.confirm(self.entity, f"row-{number}", SESSION, ts=TS)
        queue.propose(self.entity, f"row-{ROWS}", "Expenses:Software", "Synthetic service")  # stays open
        queue.withdraw(self.entity, f"row-{ROWS}", "Synthetic")
        queue.propose(self.entity, f"row-{ROWS + 1}", "Expenses:Office", "Synthetic service")  # stays open

    def everything(self):
        return json.dumps(queue.list_queue_items(self.entity), sort_keys=True)

    def test_compaction_moves_only_decided_items_and_keeps_every_read(self):
        before_list = self.everything()
        before_open = json.dumps(queue.list_queue_items(self.entity, status="open"), sort_keys=True)
        before_confirmed = json.dumps(queue.list_queue_items(self.entity, status="confirmed"), sort_keys=True)
        before_files = {name: (self.entity.path / "review-queue" / name).read_bytes() for name in decided_files(self.entity)}
        before_summary = json.dumps(queue.summarize_queue_items(self.entity, status=None), sort_keys=True)
        before_ledger = (self.entity.path / "ledger.sqlite").read_bytes()
        result = queue.compact_closed_items(self.entity)
        self.assertEqual(result["archived"], ROWS)
        self.assertEqual(archives(self.entity), ["2026-01.json", "2026-02.json", "2026-03.json"])
        # The open item and the withdrawn item keep their files.
        self.assertEqual(decided_files(self.entity), [f"row-{ROWS}.json", f"row-{ROWS + 1}.json"])
        self.assertEqual(self.everything(), before_list)
        self.assertEqual(json.dumps(queue.list_queue_items(self.entity, status="open"), sort_keys=True), before_open)
        self.assertEqual(json.dumps(queue.list_queue_items(self.entity, status="confirmed"), sort_keys=True), before_confirmed)
        self.assertEqual(json.dumps(queue.summarize_queue_items(self.entity, status=None), sort_keys=True), before_summary)
        # A moved item is the same document, byte for byte, when it is written again as a file.
        for name in before_files:
            if name in queue._archived_items(self.entity)[0]:
                again = (json.dumps(queue._archived_items(self.entity)[0][name], indent=2, sort_keys=True) + "\n").encode()
                self.assertEqual(again, before_files[name], name)
        self.assertEqual((self.entity.path / "ledger.sqlite").read_bytes(), before_ledger, "the ledger is not touched")

    def test_the_file_count_does_not_grow_with_the_number_of_decided_items(self):
        queue.compact_closed_items(self.entity)
        count = len(list((self.entity.path / "review-queue").rglob("*.json")))
        self.assertEqual(count, 2 + 3)
        # More rows in the same months: still one archive for each month.
        for number in range(ROWS + 2, ROWS + 10):
            _add_pending_categorization(self.entity, f"row-{number}", f"Synthetic vendor {number}", txn_date=f"2026-0{1 + number % 3}-20")
            queue.propose(self.entity, f"row-{number}", "Expenses:Software", "Synthetic service")
            queue.confirm(self.entity, f"row-{number}", SESSION, ts=TS)
        queue.compact_closed_items(self.entity)
        self.assertEqual(len(list((self.entity.path / "review-queue").rglob("*.json"))), 2 + 3)
        self.assertEqual(len([i for i in queue.list_queue_items(self.entity, status="confirmed")]), ROWS + 8)

    def test_the_archive_is_the_same_bytes_each_time_and_a_second_run_changes_nothing(self):
        queue.compact_closed_items(self.entity)
        first = {name: (self.entity.path / "review-queue" / "archive" / name).read_bytes() for name in archives(self.entity)}
        self.assertEqual(queue.compact_closed_items(self.entity), {"archived": 0, "files_removed": 0, "archives_written": 0})
        self.assertEqual({name: (self.entity.path / "review-queue" / "archive" / name).read_bytes() for name in archives(self.entity)}, first)

    def test_a_decided_item_can_be_shown_and_a_confirmed_source_cannot_be_proposed_again(self):
        queue.compact_closed_items(self.entity)
        shown = queue.read_queue_item(self.entity, "row-3")
        self.assertEqual((shown["status"], shown["confirmed_category"]), ("confirmed", "Expenses:Software"))
        self.assertEqual(queue._load_item(self.entity, "row-3")["status"], "confirmed")
        with self.assertRaises(FileNotFoundError):
            queue.read_queue_item(self.entity, "no-such-row")
        with self.assertRaises(ValueError):
            queue.propose(self.entity, "row-3", "Expenses:Office", "Synthetic again")

    def test_an_item_file_wins_over_its_archive_entry_and_the_next_move_folds_it_in(self):
        queue.compact_closed_items(self.entity)
        # A file for an archived name is a newer write (for example an item that is open again).
        reopened = {**queue.read_queue_item(self.entity, "row-3"), "status": "reopened"}
        queue._save_item(self.entity, reopened)
        self.assertEqual(queue.read_queue_item(self.entity, "row-3")["status"], "reopened", "the file is newer than the archive entry")
        self.assertEqual([item["status"] for item in queue.list_queue_items(self.entity) if item["source_id"] == "row-3"], ["reopened"])
        self.assertEqual(len(work.take_snapshot(self.entity.path).queue), ROWS + 2)
        self.assertEqual(work.take_snapshot(self.entity.path).queue["row-3"]["status"], "reopened")
        # The move drops the stale archive entry and keeps the open item in its file.
        queue.compact_closed_items(self.entity)
        self.assertIn("row-3.json", decided_files(self.entity))
        self.assertNotIn("row-3.json", queue._archived_items(self.entity)[0])
        self.assertEqual(queue.read_queue_item(self.entity, "row-3")["status"], "reopened")
        # When it is decided again, it goes back into its archive.
        queue._save_item(self.entity, {**reopened, "status": "confirmed"})
        queue.compact_closed_items(self.entity)
        self.assertNotIn("row-3.json", decided_files(self.entity))
        self.assertEqual(queue.read_queue_item(self.entity, "row-3")["status"], "confirmed")
        self.assertEqual(sum(1 for item in queue.list_queue_items(self.entity) if item["source_id"] == "row-3"), 1)

    def test_an_interrupted_move_loses_nothing_and_the_next_run_finishes(self):
        expected = self.everything()
        real = queue._replace_group_destination
        calls = []

        def stop_after_one(path, payload):
            real(path, payload)
            calls.append(path.name)
            if len(calls) == 1:
                raise OSError("synthetic stop after the first archive")

        with patch.object(queue, "_replace_group_destination", stop_after_one):
            with self.assertRaises(OSError):
                queue.compact_closed_items(self.entity)
        # An archive is on disk and every item file is still there: a read sees each item one time.
        self.assertEqual(len(archives(self.entity)), 1)
        self.assertEqual(len(decided_files(self.entity)), ROWS + 2)
        self.assertEqual(self.everything(), expected)
        queue.compact_closed_items(self.entity)
        self.assertEqual(self.everything(), expected)
        self.assertEqual(decided_files(self.entity), [f"row-{ROWS}.json", f"row-{ROWS + 1}.json"])

    def test_an_archive_that_is_not_valid_is_shown_as_corrupt_and_is_never_rewritten(self):
        queue.compact_closed_items(self.entity)
        bad = self.entity.path / "review-queue" / "archive" / "2026-02.json"
        bad.write_text("{not json", encoding="utf-8")
        listed = queue.list_queue_items(self.entity)
        corrupt = [item for item in listed if item.get("status") == "corrupt"]
        self.assertEqual([item["diagnostic"]["file"] for item in corrupt], ["2026-02.json"])
        with self.assertRaises(ValueError):
            queue.compact_closed_items(self.entity)
        self.assertEqual(bad.read_text(encoding="utf-8"), "{not json")

    def test_the_cli_moves_decided_items_and_work_events_count_them_the_same(self):
        before = work.take_snapshot(self.entity.path)
        queue.compact_closed_items(self.entity)
        after = work.take_snapshot(self.entity.path)
        self.assertEqual(before.queue, after.queue)

    def test_the_queue_command_that_decides_an_item_moves_it(self):
        from bookkeeping import cli
        _add_pending_categorization(self.entity, "cli-row", "Synthetic cli", txn_date="2026-02-02")
        queue.propose(self.entity, "cli-row", "Expenses:Software", "Synthetic")
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["queue", "confirm", "--entity", str(self.entity.path), "--item", "cli-row", "--session", SESSION])
        self.assertEqual(code, 0, err.getvalue())
        self.assertNotIn("cli-row.json", decided_files(self.entity))
        self.assertEqual(queue.read_queue_item(self.entity, "cli-row")["status"], "confirmed")
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["queue", "compact", "--entity", str(self.entity.path)]), 0)
        self.assertIn("Archived", out.getvalue())

    def test_an_item_file_with_other_bytes_is_not_moved(self):
        path = self.entity.path / "review-queue" / "row-2.json"
        item = json.loads(path.read_text())
        path.write_text(json.dumps(item), encoding="utf-8")  # not the bytes that the engine writes
        queue.compact_closed_items(self.entity)
        self.assertEqual(json.loads(path.read_text()), item)
        self.assertIn("row-2.json", decided_files(self.entity))
        self.assertNotIn("row-2.json", queue._archived_items(self.entity)[0])

    def test_load_all_items_returns_files_and_archives_with_files_first(self):
        queue.compact_closed_items(self.entity)
        every = queue.load_all_items(self.entity)
        self.assertEqual(sorted(every), sorted(f"row-{n}.json" for n in range(ROWS + 2)))

    def test_the_audit_chain_is_whole_after_the_move(self):
        queue.compact_closed_items(self.entity)
        self.assertEqual(LedgerStore(self.entity.path / "ledger.sqlite").verify_audit_chain(), [])


class MigrationTests(unittest.TestCase):
    def test_a_company_with_archives_exports_and_validates_as_a_bundle(self):
        from datetime import date
        from bookkeeping import hosted_migration as capture, migration_bundle as bundle
        from bookkeeping.entity import init_entity, load_entity
        from bookkeeping.ledger.importer import import_transactions
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "company"
            init_entity(root, name="Synthetic", business_type="consulting")
            entity = load_entity(root)
            rows = [{"id": f"mig-{n}", "date": f"2026-0{1 + n % 2}-1{n}", "description": f"Synthetic {n}", "amount": f"-{10 + n}.00",
                     "accountName": "Checking", "pending": False} for n in range(4)]
            import_transactions(entity, rows, "s", categorizer=lambda _: (None, None), session_date=date(2026, 1, 15), ts="2026-01-15T00:00:00Z")
            for row in rows:
                queue.propose(entity, row["id"], "Expenses:Software", "Synthetic")
                queue.confirm(entity, row["id"], SESSION, ts=TS)
            queue.compact_closed_items(entity)
            self.assertEqual(sorted(p.name for p in (root / "review-queue").rglob("*.json")), ["2026-01.json", "2026-02.json"])
            destination = Path(tmp).resolve() / "company.zip"
            result = bundle.build_bundle(root, destination)
            self.assertEqual(result["status"], "exported", result)
            loaded = bundle.load_bundle(destination)
            self.assertEqual(sorted(n for n in loaded["files"] if n.startswith("review-queue/")),
                             ["review-queue/archive/2026-01.json", "review-queue/archive/2026-02.json"])
            bundle.validate_files(loaded["manifest"], loaded["files"])
            restored = Path(tmp).resolve() / "restored"
            for name, data in loaded["files"].items():
                (restored / name).parent.mkdir(parents=True, exist_ok=True)
                (restored / name).write_bytes(data)
            self.assertEqual([i["source_id"] for i in queue.list_queue_items(load_entity(restored))],
                             [i["source_id"] for i in queue.list_queue_items(entity)])


class AccountCheckTests(unittest.TestCase):
    def test_account_removal_does_not_scan_decided_items_in_an_archive(self):
        from bookkeeping import account_remove
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            store = LedgerStore(entity.path / "ledger.sqlite")
            item = {"source_id": "a", "status": "confirmed", "confirmed_category": "Expenses:Software"}
            loose = {"review-queue/a.json": item}
            archived = {"review-queue/archive/2026-01.json": {"version": 1, "items": {"a.json": item}}}
            catalog = {"Expenses:Software", "Expenses:Office"}
            # A decided item was never scanned as a file, and is not scanned in an archive.
            self.assertEqual(account_remove.references(loose, store, "Expenses:Software", catalog), [])
            self.assertEqual(account_remove.references(archived, store, "Expenses:Software", catalog), [])
            # An open item still counts.
            opened = {"review-queue/b.json": {**item, "source_id": "b", "status": "open", "proposed_category": "Expenses:Software"}}
            self.assertTrue(account_remove.references(opened, store, "Expenses:Software", catalog))


if __name__ == "__main__":
    unittest.main()
