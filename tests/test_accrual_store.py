"""Synthetic regression proofs for schema transitions and atomic support writes."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bookkeeping.accrual.store import (
    SupportingMutation, TABLES, apply_mutations, list_records, load_record, record_revision,
)
from bookkeeping.entity import init_entity, load_entity
from bookkeeping.ledger.importer import _atomic_ledger_write, check_store_integrity
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.ledger.store import LedgerStore


def entry(source: str = "synthetic-1") -> Entry:
    return Entry(date=date(2026, 1, 15), narration="Synthetic adjustment",
                 meta=(("source-id", source),), postings=(
                     Posting("Expenses:Software", Decimal("10.00")),
                     Posting("Assets:Bank:Checking", Decimal("-10.00")),
                 ))


class AccrualStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "company"
        init_entity(self.root, name="Synthetic company")
        self.entity = load_entity(self.root)
        self.store = LedgerStore(self.root / "ledger.sqlite")

    def post_cash(self) -> None:
        _atomic_ledger_write(self.entity, [], [entry()], "fixture", None, "Synthetic fixture")

    def accrual(self) -> None:
        from bookkeeping.entity import configure_basis
        configure_basis(self.root, "accrual")
        self.entity = load_entity(self.root)

    def test_populated_cash_upgrade_preserves_financial_state_and_audit_prefix(self) -> None:
        self.post_cash()
        old_entries = self.store.load_entries()
        old_accounts = self.store.load_opens()
        old_history = self.store.load_audit_events()
        old_digest = self.store.content_digest()
        backup = self.store.upgrade()
        self.assertTrue(backup and backup.exists())
        self.assertEqual(LedgerStore(backup).content_digest(), old_digest)
        self.assertEqual(check_store_integrity(LedgerStore(backup)).status, "ok")
        self.assertEqual(self.store.load_entries(), old_entries)
        self.assertEqual(self.store.load_opens(), old_accounts)
        self.assertEqual(self.store.load_audit_events()[:len(old_history)], old_history)
        self.assertEqual(self.store.get_meta("schema_version"), "2")
        self.assertEqual(check_store_integrity(self.store).status, "ok")
        after = self.store.content_digest()
        self.assertIsNone(self.store.upgrade())
        self.store.initialize()
        self.assertEqual(self.store.content_digest(), after)
        self.assertEqual(len(list((self.root / "backups").glob("*.sqlite"))), 1)

    def test_empty_upgrade_and_each_supporting_table_is_sealed(self) -> None:
        self.store.upgrade()
        self.assertEqual(check_store_integrity(self.store).status, "ok")
        initial = self.store.content_digest()
        for table in TABLES:
            with self.subTest(table=table):
                with self.store.transaction() as conn:
                    conn.execute(f"INSERT INTO accrual_{table} VALUES (?,?,?,?)", ("tamper", 1, 1, "{}"))
                self.assertNotEqual(self.store.content_digest(), initial)
                self.assertEqual(check_store_integrity(self.store).status, "halt")
                with self.store.transaction() as conn:
                    conn.execute(f"DELETE FROM accrual_{table}")
        self.assertEqual(check_store_integrity(self.store).status, "ok")

    def test_upgrade_failure_rolls_back_ddl_metadata_and_audit(self) -> None:
        self.post_cash()
        with self.store.connection() as conn:
            before = list(conn.iterdump())
        with patch.object(LedgerStore, "append_audit_event", side_effect=RuntimeError("Synthetic interruption")):
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                self.store.upgrade()
        with self.store.connection() as conn:
            self.assertEqual(list(conn.iterdump()), before)
        self.assertEqual(check_store_integrity(self.store).status, "ok")
        self.assertEqual(len(list((self.root / "backups").glob("*.sqlite"))), 1)

    def test_tampered_or_unsealed_populated_store_cannot_upgrade(self) -> None:
        self.post_cash()
        with self.store.transaction() as conn:
            conn.execute("UPDATE postings SET amount='999.00' WHERE id=1")
        before = self.store.get_meta("schema_version")
        with self.assertRaisesRegex(ValueError, "Cannot upgrade"):
            self.store.upgrade()
        self.assertEqual(self.store.get_meta("schema_version"), before)
        self.assertFalse((self.root / "backups").exists())

    def test_future_schema_rejected_before_initialize_or_writer_changes(self) -> None:
        with sqlite3.connect(self.store.path) as conn:
            conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        before = self.store.path.read_bytes()
        for call in (self.store.initialize, self.store.upgrade, self.store.load_entries,
                     lambda: _atomic_ledger_write(self.entity, [], [entry()], "future", None, "future")):
            with self.assertRaisesRegex(ValueError, "Unsupported ledger schema"):
                call()
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_compatibility_client_with_old_supported_versions_refuses_v2(self) -> None:
        self.store.upgrade()
        before = self.store.path.read_bytes()
        with patch("bookkeeping.ledger.store.SUPPORTED_SCHEMA_VERSIONS", frozenset({1})):
            with self.assertRaisesRegex(ValueError, "Unsupported ledger schema"):
                self.store.initialize()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_future_record_version_is_rejected_before_unrelated_write(self) -> None:
        self.store.upgrade()
        with sqlite3.connect(self.store.path) as conn:
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute("INSERT INTO accrual_items VALUES ('future',2,1,'{}')")
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Unsupported accrual record version"):
            self.store.initialize()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_support_and_journal_publish_together_and_remain_sealed(self) -> None:
        self.accrual()
        digest = self.store.content_digest()
        mutations = [SupportingMutation("items", "item-1", {"id": "item-1", "amount": "10.00"})]
        _atomic_ledger_write(self.entity, [], [entry()], "support", None, "supported journal",
                             supporting_mutations=mutations, expected_digest=digest)
        self.assertEqual(self.store.counts().entries, 1)
        self.assertEqual(load_record(self.store, "items", "item-1"), mutations[0].payload)
        self.assertEqual(record_revision(self.store, "items", "item-1"), 1)
        self.assertEqual(list_records(self.store, "items"), [mutations[0].payload])
        self.assertEqual(check_store_integrity(self.store).status, "ok")

    def test_conflicting_support_rolls_back_journal_and_audit(self) -> None:
        self.accrual()
        before = self.store.content_digest()
        history = self.store.load_audit_events()
        with self.assertRaisesRegex(ValueError, "Stale supporting"):
            _atomic_ledger_write(self.entity, [], [entry()], "conflict", None, "conflict",
                                 supporting_mutations=[SupportingMutation("items", "absent", {}, 1)])
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(self.store.counts().entries, 0)
        self.assertEqual(self.store.load_audit_events(), history)

    def test_stale_preview_and_tampered_support_cannot_be_resealed(self) -> None:
        self.accrual()
        mutation = SupportingMutation("items", "item-1", {"id": "item-1"})
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "Stale accounting preview"):
            _atomic_ledger_write(self.entity, [], [], "stale", None, "stale",
                                 supporting_mutations=[mutation], expected_digest="0" * 64)
        self.assertEqual(self.store.content_digest(), before)
        with self.store.transaction() as conn:
            apply_mutations(self.store, [mutation], conn)
        with self.assertRaisesRegex(ValueError, "Cannot publish accrual state"):
            _atomic_ledger_write(self.entity, [], [], "tampered", None, "tampered",
                                 supporting_mutations=[SupportingMutation("items", "item-2", {})])
        self.assertEqual(record_revision(self.store, "items", "item-2"), 0)

    def test_mutations_are_bounded_versioned_and_evidence_immutable(self) -> None:
        self.store.upgrade()
        bad = [SupportingMutation("entries", "x", {}), SupportingMutation("items", "", {}),
               SupportingMutation("items", "x", {"v": float("nan")}),
               SupportingMutation("items", "x", {}, -1), SupportingMutation("items", "x", {}, version=2)]
        for mutation in bad:
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, TypeError)):
                with self.store.transaction() as conn:
                    apply_mutations(self.store, [mutation], conn)
        with self.store.transaction() as conn:
            apply_mutations(self.store, [SupportingMutation("evidence", "e-1", {"hash": "synthetic"})], conn)
        with self.assertRaisesRegex(ValueError, "immutable"):
            with self.store.transaction() as conn:
                apply_mutations(self.store, [SupportingMutation("evidence", "e-1", {}, 1)], conn)
        self.assertEqual(record_revision(self.store, "evidence", "e-1"), 1)

    def test_cash_and_unupgraded_store_cannot_accept_support(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit accrual"):
            _atomic_ledger_write(self.entity, [], [], "disabled", None, "disabled",
                                 supporting_mutations=[SupportingMutation("items", "x", {})])
        with self.assertRaisesRegex(ValueError, "explicit ledger schema upgrade"):
            with self.store.transaction() as conn:
                apply_mutations(self.store, [SupportingMutation("items", "x", {})], conn)
        self.assertEqual(list_records(self.store, "items"), [])


if __name__ == "__main__":
    unittest.main()
