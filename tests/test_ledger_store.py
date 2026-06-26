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
