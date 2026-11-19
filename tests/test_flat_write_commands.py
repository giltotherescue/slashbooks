"""Write commands read what they change: indexed lookups, a chain check from a saved head, a digest built in SQLite."""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping import queue  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.entity import add_account, init_entity, load_entity, map_bank_account  # noqa: E402
from bookkeeping.ledger.importer import _find_live_entry  # noqa: E402
from bookkeeping.ledger.store import LedgerStore, _json, _table_json, default_store_path  # noqa: E402
from bookkeeping.source_ids import (VERIFIED_HEAD_FILE, feed_intake_for, feed_intake_index,  # noqa: E402
                                    feed_intake_payload)

CHECKING = "Assets:Bank:Operating-Checking"


def _run(*argv: str) -> int:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return main(list(argv))


def _company(tmp: str, rows: int = 3, source: str = "banksync") -> Path:
    root = Path(tmp).resolve() / "company"
    init_entity(root, name="Harbor Studio", business_type="consulting", legal_structure="llc")
    add_account(root, CHECKING, open_date="2025-01-01")
    map_bank_account(root, "acct_1", CHECKING)
    feed = [{"id": f"r{i}", "date": "2026-01-15", "description": f"Synthetic vendor {i}", "amount": "-25.00",
             "accountId": "acct_1", "accountName": "Checking", "accountType": "checking", "pending": False}
            for i in range(rows)]
    path = root.parent / "feed.json"
    path.write_text(json.dumps({"transactions": feed}), encoding="utf-8")
    assert _run("ingest", str(path), "--entity", str(root), "--source", source, "--no-feed-window") == 0
    return root


class FeedLookupTests(unittest.TestCase):
    def test_lookup_matches_the_full_index_for_the_rows_asked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            full = feed_intake_index(root)
            self.assertTrue(full)
            asked = [key for key in full if key.endswith("r1")]
            self.assertTrue(asked)
            self.assertEqual(feed_intake_for(root, asked), {key: full[key] for key in asked})
            self.assertEqual(feed_intake_for(root, ["no-such-row"]), {})

    def test_a_large_batch_reads_every_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self.assertEqual(feed_intake_for(root, [f"x{i}" for i in range(500)] + list(feed_intake_index(root))),
                             feed_intake_index(root))

    def test_the_first_call_saves_a_verified_head_and_later_calls_check_only_new_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            (root / VERIFIED_HEAD_FILE).write_text("", encoding="utf-8")
            feed_intake_for(root, ["banksync:r1"])
            saved = json.loads((root / VERIFIED_HEAD_FILE).read_text(encoding="utf-8"))
            store = LedgerStore(default_store_path(root))
            self.assertEqual(saved["position"], store.audit_head().position)
            calls: list[object] = []
            original = LedgerStore.verify_audit_chain
            LedgerStore.verify_audit_chain = lambda self: calls.append(1) or original(self)  # type: ignore[method-assign]
            try:
                feed_intake_for(root, ["banksync:r1"])
            finally:
                LedgerStore.verify_audit_chain = original  # type: ignore[method-assign]
            self.assertEqual(calls, [])

    def test_an_edited_event_gives_no_mark_even_with_a_saved_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self.assertTrue(feed_intake_for(root, ["banksync:r1"]))
            conn = sqlite3.connect(default_store_path(root))
            conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                conn.execute(f'DROP TRIGGER "{name}"')
            # A new head that the saved head does not match: the saved head is no longer in the chain.
            conn.execute("UPDATE audit_events SET payload_json = replace(payload_json, 'banksync', 'forged') WHERE type = 'feed-intake'")
            conn.execute("UPDATE audit_events SET record_hash = 'x' || substr(record_hash, 2) WHERE id = (SELECT MAX(id) FROM audit_events)")
            conn.commit()
            conn.close()
            self.assertEqual(feed_intake_for(root, ["banksync:r1"]), {})
            self.assertEqual(feed_intake_index(root), {})

    def test_a_missing_or_damaged_head_file_costs_a_full_check_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            (root / VERIFIED_HEAD_FILE).write_text("not json", encoding="utf-8")
            self.assertEqual(feed_intake_for(root, ["banksync:r1"]), {k: v for k, v in feed_intake_index(root).items() if k.endswith("r1")})


class UndoLookupTests(unittest.TestCase):
    def test_find_live_entry_uses_the_source_id_and_the_reversals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp, rows=0)
            entity = load_entity(root)
            for name in ("a", "b"):
                path = root.parent / f"{name}.json"
                path.write_text(json.dumps({"transactions": [
                    {"id": name, "date": "2026-01-15", "description": "Vendor", "amount": "-5.00", "accountId": "acct_1",
                     "accountName": "Checking", "accountType": "checking", "pending": False}]}), encoding="utf-8")
            from bookkeeping.ledger.importer import reverse_entry
            from bookkeeping.ledger.model import Entry, Posting
            from decimal import Decimal
            from datetime import date
            from bookkeeping.ledger.importer import _atomic_ledger_write
            for name in ("a", "b"):
                _atomic_ledger_write(entity=entity, new_opens=[], new_entries=[Entry(
                    date=date(2026, 1, 15), narration=name, flag="*", meta=(("source-id", name),),
                    postings=(Posting(CHECKING, Decimal("-5.00"), "USD"), Posting("Expenses:Misc", Decimal("5.00"), "USD")))],
                    session_id="t", ts=None, intent_description="test")
            store = LedgerStore(default_store_path(root))
            self.assertEqual(_find_live_entry(store, "a").narration, "a")
            reverse_entry(entity, "a", "undo")
            with self.assertRaisesRegex(ValueError, "already been reversed"):
                _find_live_entry(store, "a")
            self.assertEqual(_find_live_entry(store, "b").narration, "b")
            with self.assertRaisesRegex(ValueError, "No entry"):
                _find_live_entry(store, "missing")
            self.assertTrue(store.is_reversed(["a"]))
            self.assertFalse(store.is_reversed(["b"]))
            with store.connection() as conn:
                plan = " ".join(str(row[3]) for row in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT metadata_json FROM entries WHERE instr(metadata_json, '\"reverses\"') > 0"))
            self.assertIn("entries_reverses_idx", plan)


class DigestTests(unittest.TestCase):
    def _compare(self, rows: list[tuple]) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, n INTEGER, v)")
        conn.executemany("INSERT INTO t (id, a, n, v) VALUES (?, ?, ?, ?)", rows)
        sql = "SELECT id, a, n, v FROM t ORDER BY id"
        self.assertEqual(_table_json(conn, "t", sql), _json([dict(row) for row in conn.execute(sql)]))

    def test_plain_rows_give_the_same_text(self) -> None:
        self.assertEqual(_table_json(sqlite3.connect(":memory:"), "sqlite_master", "SELECT name FROM sqlite_master ORDER BY name"), "[]")
        self._compare([(1, 'plain "quoted" \\ back\nline\ttab', 5, None), (2, None, 0, "x"), (3, "", -7, "y/z")])

    def test_every_ascii_control_character(self) -> None:
        self._compare([(i + 1, f"a{chr(i)}b", i, None) for i in range(0, 128)])

    def test_one_non_ascii_or_real_row_is_written_again_alone(self) -> None:
        from bookkeeping.ledger import store as store_module
        rows = [(i + 1, f"vendor {i}", i, None) for i in range(200)]
        rows[17] = (18, "Café Zürich ’", 17, None)
        rows[90] = (91, "plain", 90, 2.5)
        rows[150] = (151, "del \x7f", 150, "x")
        calls: list[object] = []
        original = store_module._json
        store_module._json = lambda value: calls.append(value) or original(value)  # type: ignore[assignment]
        try:
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, n INTEGER, v)")
            conn.executemany("INSERT INTO t (id, a, n, v) VALUES (?, ?, ?, ?)", rows)
            sql = "SELECT id, a, n, v FROM t ORDER BY id"
            text = _table_json(conn, "t", sql)
        finally:
            store_module._json = original  # type: ignore[assignment]
        self.assertEqual(text, _json([dict(row) for row in conn.execute(sql)]))
        self.assertEqual(len(calls), 3)  # the three rows that SQLite cannot write as json.dumps does

    def test_non_ascii_real_and_blob_values_use_the_json_module(self) -> None:
        self._compare([(1, "café \U0001F600", 1, None)])
        self._compare([(1, "x", 1, 0.1 + 0.2), (2, "y", 2, 1e20)])
        self._compare([(1, "x", 1, "text")])
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, n INTEGER)")
        conn.execute("INSERT INTO t VALUES (1, 'x', 1.5)")
        sql = "SELECT id, a, n FROM t ORDER BY id"
        self.assertEqual(_table_json(conn, "t", sql), _json([dict(row) for row in conn.execute(sql)]))


class CompactTests(unittest.TestCase):
    def test_a_command_compaction_leaves_untouched_months_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp, rows=0)
            entity = load_entity(root)
            folder = queue._archive_dir(entity)
            folder.mkdir(parents=True, exist_ok=True)
            old = {"a-1.json": {"source_id": "a-1", "status": "confirmed", "date": "2024-01-15"}}
            (folder / "2024-01.json").write_bytes(queue._archive_bytes(old))
            (folder / "2024-02.json").write_bytes(b"not an archive")
            before = (folder / "2024-02.json").read_bytes()
            q_dir = queue._queue_dir(entity)
            q_dir.mkdir(parents=True, exist_ok=True)
            item = {"source_id": "n-1", "status": "confirmed", "date": "2026-03-01"}
            (q_dir / "n-1.json").write_text(json.dumps(item, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                queue.compact_closed_items(entity)  # The full run still stops at an archive that cannot be read.
            self.assertEqual(queue.compact_closed_items(entity, full=False)["archived"], 1)
            self.assertEqual((folder / "2024-02.json").read_bytes(), before)
            self.assertTrue((folder / "2026-03.json").exists())
            self.assertFalse((q_dir / "n-1.json").exists())


def _drop_ledger_triggers(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        conn.execute(f'DROP TRIGGER "{name}"')
    return conn


class VerifiedHeadFileTests(unittest.TestCase):
    """The saved audit head is runtime state: never written through a link, never in the lock, never in a digest."""

    def test_a_symlinked_lock_or_head_file_is_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            outside = Path(tmp).resolve() / "outside"
            outside.mkdir()
            for name in (".books.lock", VERIFIED_HEAD_FILE):
                victim = outside / f"victim-{name.strip('.')}"
                victim.write_text("keep", encoding="utf-8")
                link = root / name
                link.unlink(missing_ok=True)
                link.symlink_to(victim)
            self.assertTrue(feed_intake_for(root, ["banksync:r1"]))
            for victim in outside.iterdir():
                self.assertEqual(victim.read_text(encoding="utf-8"), "keep", victim.name)
            # The head file is now a regular file of its own (the link was replaced, not followed).
            self.assertFalse((root / VERIFIED_HEAD_FILE).is_symlink())
            self.assertTrue((root / ".books.lock").is_symlink())

    def test_a_head_read_through_a_link_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            feed_intake_for(root, ["banksync:r1"])
            real = root.parent / "head-copy"
            real.write_bytes((root / VERIFIED_HEAD_FILE).read_bytes())
            (root / VERIFIED_HEAD_FILE).unlink()
            (root / VERIFIED_HEAD_FILE).symlink_to(real)
            calls: list[object] = []
            original = LedgerStore.verify_audit_chain_since
            LedgerStore.verify_audit_chain_since = (  # type: ignore[method-assign]
                lambda self, head: calls.append(head) or original(self, head))
            try:
                feed_intake_for(root, ["banksync:r1"])
            finally:
                LedgerStore.verify_audit_chain_since = original  # type: ignore[method-assign]
            self.assertEqual(calls, [None])  # one full check, from the first event

    def test_the_verified_head_changes_no_lock_and_no_workspace_digest(self) -> None:
        from bookkeeping.ledger.books_policy_check import workspace_digest
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            (root / ".books.lock").write_bytes(b"")
            (root / VERIFIED_HEAD_FILE).unlink(missing_ok=True)
            before = workspace_digest(root, ignore_empty_lock=True)
            self.assertTrue(feed_intake_for(root, ["banksync:r1"]))
            self.assertTrue((root / VERIFIED_HEAD_FILE).is_file())
            self.assertEqual((root / ".books.lock").read_bytes(), b"")
            self.assertEqual(workspace_digest(root, ignore_empty_lock=True), before)

    def test_the_head_file_is_runtime_state_for_a_migration(self) -> None:
        from bookkeeping.hosted_migration import _excluded
        from bookkeeping.ledger.books_policy_check import RUNTIME_STATE_FILES
        self.assertIn(VERIFIED_HEAD_FILE, RUNTIME_STATE_FILES)
        self.assertNotEqual(VERIFIED_HEAD_FILE, ".books.lock")
        self.assertEqual(_excluded(VERIFIED_HEAD_FILE), "runtime_state")


class FeedTrustTests(unittest.TestCase):
    """An intake record that ``feed_intake_for`` reads is re-hashed, so an edit before the saved head gives no mark."""

    @staticmethod
    def _later_event(root: Path) -> None:
        """An event after the intake record, so the record is inside the chain and not its newest link."""
        store = LedgerStore(default_store_path(root))
        with store.transaction() as conn:
            store.append_audit_event("intent", {"session_id": "later", "description": "later", "entries": 0}, conn)

    def _edit_intake(self, root: Path, *, rehash: bool) -> None:
        from bookkeeping.ledger.store import _hash_record, install_append_only_guard
        conn = _drop_ledger_triggers(default_store_path(root))
        row = conn.execute("SELECT id, type, ts, payload_json, prev_hash FROM audit_events WHERE type = 'feed-intake'").fetchone()
        payload = row[3].replace('"provider":"banksync"', '"provider":"forged"')
        self.assertNotEqual(payload, row[3])
        conn.execute("UPDATE audit_events SET payload_json = ? WHERE id = ?", (payload, row[0]))
        if rehash:  # A consistent record of its own: only the link to the next event shows the edit.
            conn.execute("UPDATE audit_events SET record_hash = ? WHERE id = ?",
                         (_hash_record(row[1], row[2], payload, row[4]), row[0]))
        install_append_only_guard(conn)  # The guard is back: a trigger check alone cannot see the edit.
        conn.commit()
        conn.close()

    def test_an_edited_intake_payload_before_the_saved_head_gives_no_mark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._later_event(root)
            self.assertIn("banksync", feed_intake_for(root, ["banksync:r1"])["banksync:r1"])  # saves a head
            self._edit_intake(root, rehash=False)
            self.assertEqual(feed_intake_for(root, ["banksync:r1"]), {})
            self.assertEqual(feed_intake_index(root), {})

    def test_an_intake_record_hashed_again_still_breaks_the_link_to_the_next_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._later_event(root)
            self.assertTrue(feed_intake_for(root, ["banksync:r1"]))
            self._edit_intake(root, rehash=True)
            self.assertEqual(feed_intake_for(root, ["banksync:r1"]), {})

    def test_the_first_call_runs_one_full_chain_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            (root / VERIFIED_HEAD_FILE).unlink(missing_ok=True)
            full: list[object] = []
            since: list[object] = []
            original_full, original_since = LedgerStore.verify_audit_chain, LedgerStore.verify_audit_chain_since
            LedgerStore.verify_audit_chain = lambda self: full.append(1) or original_full(self)  # type: ignore[method-assign]
            LedgerStore.verify_audit_chain_since = (  # type: ignore[method-assign]
                lambda self, head: since.append(head) or original_since(self, head))
            try:
                feed_intake_for(root, ["banksync:r1"])
            finally:
                LedgerStore.verify_audit_chain = original_full  # type: ignore[method-assign]
                LedgerStore.verify_audit_chain_since = original_since  # type: ignore[method-assign]
            self.assertEqual((full, since), ([], [None]))


class FirstCaptureTests(unittest.TestCase):
    def test_the_first_capture_on_a_store_without_the_new_index_stays_incremental(self) -> None:
        import time
        from bookkeeping import changeset
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            conn = sqlite3.connect(default_store_path(root))
            conn.execute("DROP INDEX entries_reverses_idx")  # a store that an older engine wrote
            conn.commit()
            conn.close()
            commands = [["queue", "list", "--entity", str(root)],
                        ["queue", "propose", "--entity", str(root), "--source-id", "banksync:r1", "--category",
                         "Expenses:Software", "--reasoning", "Test."],
                        ["queue", "confirm", "--entity", str(root), "--item", "banksync:r1"]]
            reports = []
            for revision, argv in enumerate(commands):
                time.sleep(0.03)
                with changeset.capture(root, books_revision=revision + 1, base_revision=revision) as receipt:
                    self.assertEqual(_run(*argv), 0, argv)
                reports.append(receipt.path_report)
            for report in reports[1:]:
                self.assertEqual((report.get("mode"), report.get("fallbacks")), ("incremental", []))
            with sqlite3.connect(default_store_path(root)) as check:
                self.assertTrue(check.execute("SELECT 1 FROM sqlite_master WHERE name = 'entries_reverses_idx'").fetchone())


class StaleArchiveTests(unittest.TestCase):
    """An item decided again in another month never stays in two archives, and every read gives the same copy."""

    def _setup(self, tmp: str) -> tuple:
        root = _company(tmp, rows=0)
        entity = load_entity(root)
        folder = queue._archive_dir(entity)
        folder.mkdir(parents=True, exist_ok=True)
        q_dir = queue._queue_dir(entity)
        q_dir.mkdir(parents=True, exist_ok=True)
        return entity, folder, q_dir

    @staticmethod
    def _item(date: str, updated: str, category: str) -> dict:
        return {"source_id": "x-1", "status": "confirmed", "date": date, "updated_at": updated,
                "confirmed_category": category, "amount": "-5.00"}

    def test_an_item_decided_again_in_another_month_leaves_no_stale_archive_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity, folder, q_dir = self._setup(tmp)
            first = self._item("2026-01-15", "2026-01-20T00:00:00Z", "Expenses:Software")
            other = {"source_id": "y-1", "status": "confirmed", "date": "2026-01-03", "updated_at": "2026-01-04T00:00:00Z"}
            (folder / "2026-01.json").write_bytes(queue._archive_bytes({"x-1.json": first, "y-1.json": other}))
            # The item was opened again (a changed amount), proposed on the next month's date and decided again.
            again = self._item("2026-02-03", "2026-02-05T00:00:00Z", "Expenses:Fees")
            (q_dir / "x-1.json").write_text(json.dumps(again, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            self.assertEqual(queue.compact_closed_items(entity, full=False)["archived"], 1)
            january = json.loads((folder / "2026-01.json").read_text(encoding="utf-8"))["items"]
            february = json.loads((folder / "2026-02.json").read_text(encoding="utf-8"))["items"]
            self.assertEqual(sorted(january), ["y-1.json"])
            self.assertEqual(february, {"x-1.json": again})
            self.assertEqual(queue._load_item(entity, "x-1"), again)
            self.assertEqual(queue.load_all_items(entity)["x-1.json"], again)

    def test_a_full_compact_keeps_only_the_latest_decided_copy(self) -> None:
        for earlier_is_newer in (False, True):
            with self.subTest(earlier_is_newer=earlier_is_newer), tempfile.TemporaryDirectory() as tmp:
                entity, folder, _ = self._setup(tmp)
                january = self._item("2026-01-15", "2026-03-01T00:00:00Z" if earlier_is_newer else "2026-01-20T00:00:00Z",
                                     "Expenses:Software")
                february = self._item("2026-02-03", "2026-02-05T00:00:00Z", "Expenses:Fees")
                (folder / "2026-01.json").write_bytes(queue._archive_bytes({"x-1.json": january}))
                (folder / "2026-02.json").write_bytes(queue._archive_bytes({"x-1.json": february}))
                latest = january if earlier_is_newer else february
                # Every read gives the latest decided copy, before and after the repair.
                self.assertEqual(queue._load_item(entity, "x-1"), latest)
                self.assertEqual(queue.load_all_items(entity)["x-1.json"], latest)
                queue.compact_closed_items(entity)
                holders = [path.stem for path in sorted(folder.glob("*.json"))
                           if "x-1.json" in json.loads(path.read_text(encoding="utf-8"))["items"]]
                self.assertEqual(holders, ["2026-01" if earlier_is_newer else "2026-02"])
                self.assertEqual(queue._load_item(entity, "x-1"), latest)


if __name__ == "__main__":
    unittest.main()
