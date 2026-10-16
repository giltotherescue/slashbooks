from __future__ import annotations

import tempfile
import unittest
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.bookkeeping.ledger.model import Entry, Open, Posting
from src.bookkeeping.ledger.store import LedgerStore


def _entry(source_id: str = "txn-1", amount: str = "10.00") -> Entry:
    amt = Decimal(amount)
    return Entry(
        date=date(2026, 1, 15),
        narration="Store test",
        meta=(("source-id", source_id), ("import-session", "store-test")),
        postings=(
            Posting("Expenses:Software", amt),
            Posting("Assets:Bank:Checking", -amt),
        ),
    )


class LedgerStoreTests(unittest.TestCase):
    def test_initialize_creates_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp) / "ledger.sqlite")
            store.initialize()

            counts = store.counts()
            self.assertEqual(counts.entries, 0)
            self.assertEqual(store.get_meta("schema_version"), "1")

    def test_insert_and_load_entry_preserves_decimal_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp) / "ledger.sqlite")
            store.initialize()
            with store.transaction() as conn:
                store.insert_opens(
                    [
                        Open(date(2000, 1, 1), "Assets:Bank:Checking", ("USD",)),
                        Open(date(2000, 1, 1), "Expenses:Software", ("USD",)),
                    ],
                    conn,
                )
                store.insert_entries([_entry(amount="123.45")], conn)

            loaded = store.load_entries()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].postings[0].amount, Decimal("123.45"))

            with store.connection() as conn:
                row = conn.execute("SELECT amount FROM postings WHERE amount = ?", ("123.45",)).fetchone()
            self.assertIsNotNone(row)

    def test_legacy_cash_columns_are_repaired_with_verified_backup_and_seal(self) -> None:
        from src.bookkeeping.ledger.importer import check_store_integrity
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp) / "ledger.sqlite")
            store.initialize()
            with store.transaction() as conn:
                store.insert_entries([_entry()], conn)
                conn.execute("ALTER TABLE entries DROP COLUMN session")
                conn.execute("ALTER TABLE entries DROP COLUMN late_arrival")
                store.append_audit_event("ledger-store-sealed", {"store_sha256": store.content_digest(conn)}, conn)
            digest = store.content_digest()
            self.assertEqual(check_store_integrity(store).status, "ok")
            history = store.load_audit_events()
            store.initialize()
            self.assertEqual(store.get_meta("schema_version"), "1")
            self.assertEqual(store.load_audit_events()[:len(history)], history)
            self.assertEqual(store.load_entries()[0].postings[0].amount, Decimal("10.00"))
            self.assertEqual(check_store_integrity(store).status, "ok")
            backups = list((Path(tmp) / "backups").glob("*.sqlite"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(LedgerStore(backups[0]).content_digest(), digest)

    def test_legacy_column_repair_rejects_bad_seal_before_schema_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp) / "ledger.sqlite")
            store.initialize()
            with store.transaction() as conn:
                conn.execute("ALTER TABLE entries DROP COLUMN session")
                store.append_audit_event("ledger-store-sealed", {"store_sha256": "0" * 64}, conn)
            with store.connection() as conn:
                before = list(conn.iterdump())
            with self.assertRaisesRegex(ValueError, "Cannot repair legacy ledger"):
                store.initialize()
            with store.connection() as conn:
                self.assertEqual(list(conn.iterdump()), before)

    def test_default_and_immediate_transactions_commit_with_expected_reservation(self) -> None:
        for immediate in (False, True):
            with self.subTest(immediate=immediate), tempfile.TemporaryDirectory() as tmp:
                store = LedgerStore(Path(tmp) / "ledger.sqlite")
                store.initialize()
                store.set_meta("synthetic", "before")
                other = sqlite3.connect(store.path, timeout=0)
                try:
                    with store.transaction(**({"immediate": True} if immediate else {})) as conn:
                        if immediate:
                            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                                other.execute("BEGIN IMMEDIATE")
                        else:
                            other.execute("BEGIN IMMEDIATE")
                            other.rollback()
                        store.set_meta("synthetic", "after", conn)
                        self.assertEqual(other.execute("SELECT value FROM meta WHERE key='synthetic'").fetchone()[0], "before")
                    self.assertEqual(store.get_meta("synthetic"), "after")
                    other.execute("BEGIN IMMEDIATE")
                    other.rollback()
                finally:
                    other.close()

    def test_both_transaction_modes_roll_back_and_release_writer_reservation(self) -> None:
        for immediate in (False, True):
            with self.subTest(immediate=immediate), tempfile.TemporaryDirectory() as tmp:
                store = LedgerStore(Path(tmp) / "ledger.sqlite")
                store.initialize()
                with store.connection() as conn:
                    before = list(conn.iterdump())
                with self.assertRaisesRegex(RuntimeError, "Synthetic rollback"):
                    with store.transaction(immediate=immediate) as conn:
                        store.set_meta("synthetic", "must roll back", conn)
                        store.insert_opens([Open(date(2026, 1, 1), "Expenses:Rollback", ("USD",))], conn)
                        store.append_audit_event("intent", {"session_id": "synthetic"}, conn)
                        raise RuntimeError("Synthetic rollback")
                with store.connection() as conn:
                    self.assertEqual(list(conn.iterdump()), before)
                other = sqlite3.connect(store.path, timeout=0)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    other.rollback()
                finally:
                    other.close()

    def test_duplicate_source_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp) / "ledger.sqlite")
            store.initialize()

            with self.assertRaises(sqlite3.IntegrityError):
                with store.transaction() as conn:
                    store.insert_entries([_entry("dup"), _entry("dup")], conn)

            self.assertEqual(store.counts().entries, 0)

    def test_audit_chain_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp) / "ledger.sqlite")
            store.initialize()
            with store.transaction() as conn:
                store.append_audit_event("intent", {"session_id": "s1"}, conn, ts="2026-01-01T00:00:00Z")
                store.append_audit_event("sealed", {"sha256": "abc"}, conn, ts="2026-01-01T00:00:01Z")

            self.assertEqual(store.verify_audit_chain(), [])
            with store.connection() as conn:
                conn.execute(
                    "UPDATE audit_events SET payload_json = ? WHERE id = 1",
                    ('{"session_id":"changed"}',),
                )
                conn.commit()

            self.assertTrue(store.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
