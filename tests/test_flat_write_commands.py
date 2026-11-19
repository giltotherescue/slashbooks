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


if __name__ == "__main__":
    unittest.main()
