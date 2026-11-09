"""Explicit financial kinds on the SQLite account catalog."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bookkeeping.account_remove import preview
from bookkeeping.accrual.accounts import list_accounts
from bookkeeping.business import upgrade_business
from bookkeeping.entity import add_account, configure_basis, init_entity, load_entity
from bookkeeping.hosted_migration import build_bundle
from bookkeeping.migration_bundle import load_bundle
from bookkeeping.ledger.importer import check_integrity
from bookkeeping.ledger.store import LedgerStore


class AccountFinancialKindTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "company"
        init_entity(self.root, name="Example")
        self.store = LedgerStore(self.root / "ledger.sqlite")

    def test_untyped_ledger_remains_unclassified_and_read_only(self) -> None:
        add_account(self.root, "Assets:Cash:Box")
        before = self.store.path.read_bytes()
        self.assertEqual(self.store.load_financial_kinds(), {})
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_explicit_types_use_exact_keys_and_survive_omitted_flag(self) -> None:
        bank = "Assets:Operating:Reserve"
        other = "Assets:Bank:Receivable"
        add_account(self.root, bank, financial_kind="bank")
        add_account(self.root, other, financial_kind="other")
        before = self.store.path.read_bytes()
        add_account(self.root, bank)
        add_account(self.root, bank, financial_kind="bank")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.load_financial_kinds(), {bank: "bank", other: "other"})
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_explicit_classification_of_existing_account_keeps_catalog_fields(self) -> None:
        account = "Assets:Operating:Reserve"
        add_account(self.root, account, open_date="2026-03-01", currency="EUR")
        add_account(self.root, account, financial_kind="bank")
        row = next(item for item in self.store.load_opens() if item.account == account)
        self.assertEqual((row.date.isoformat(), row.currencies), ("2026-03-01", ("EUR",)))
        self.assertEqual(self.store.load_financial_kinds(), {account: "bank"})

    def test_conflict_and_invalid_kind_leave_catalog_and_audit_unchanged(self) -> None:
        account = "Assets:Operating:Reserve"
        add_account(self.root, account, financial_kind="bank")
        before = self.store.path.read_bytes()
        for key, kind in ((account, "cash"), (account, "Bank"), ("Expenses:Rent", "cash")):
            with self.subTest(key=key, kind=kind), self.assertRaises(ValueError):
                add_account(self.root, key, financial_kind=kind)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_unsealed_kind_change_breaks_integrity(self) -> None:
        account = "Assets:Operating:Reserve"
        add_account(self.root, account, financial_kind="bank")
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE accounts SET financial_kind='other' WHERE name=?", (account,))
        self.assertEqual(check_integrity(load_entity(self.root)).status, "halt")

    def test_untyped_category_keeps_removal_provenance_in_typed_ledger(self) -> None:
        add_account(self.root, "Assets:Operating:Reserve", financial_kind="bank")
        add_account(self.root, "Expenses:Example")
        plan = preview(self.root, "Expenses:Example")
        self.assertTrue(plan["changes"])

    def test_classified_starters_are_opt_in_and_survive_accrual_upgrade(self) -> None:
        fresh = self.root.parent / "fresh"
        init_entity(fresh, name="Example", classify_starter_accounts=True)
        store = LedgerStore(fresh / "ledger.sqlite")
        kinds = store.load_financial_kinds()
        self.assertEqual(kinds["Assets:Bank:Checking"], "bank")
        self.assertEqual(kinds["Liabilities:CreditCard"], "credit_card")
        self.assertEqual(store.get_meta("schema_version"), "1+financial-kind")
        self.assertEqual(check_integrity(load_entity(fresh)).status, "ok")
        configure_basis(fresh, "accrual")
        self.assertEqual(store.schema_version(), 2)
        self.assertEqual(store.get_meta("schema_version"), "2+financial-kind")
        self.assertEqual(store.load_financial_kinds()["Assets:Bank:Checking"], "bank")
        self.assertEqual(store.load_financial_kinds()["Assets:Fixed:Equipment"], "other")
        self.assertEqual(check_integrity(load_entity(fresh)).status, "ok")

    def test_direct_accrual_init_and_business_upgrade_preserve_kinds_and_seals(self) -> None:
        fresh = self.root.parent / "accrual"
        init_entity(fresh, name="Example", basis="accrual", classify_starter_accounts=True)
        store = LedgerStore(fresh / "ledger.sqlite")
        self.assertEqual(store.schema_version(), 2)
        self.assertEqual(store.get_meta("schema_version"), "2+financial-kind")
        kinds = store.load_financial_kinds()
        self.assertEqual(kinds["Assets:Bank:Checking"], "bank")
        self.assertEqual(kinds["Liabilities:CreditCard"], "credit_card")
        self.assertEqual(kinds["Assets:Fixed:Equipment"], "other")
        initial_seals = store.load_audit_events()
        self.assertEqual(initial_seals[0]["payload"]["session_id"], "entity-init")
        self.assertEqual(check_integrity(load_entity(fresh)).status, "ok")

        backup = upgrade_business(store)
        reopened = LedgerStore(fresh / "ledger.sqlite")
        self.assertEqual(reopened.schema_version(), 3)
        self.assertEqual(reopened.get_meta("schema_version"), "3+financial-kind")
        self.assertEqual(reopened.load_financial_kinds(), kinds)
        self.assertEqual(reopened.load_audit_events()[:len(initial_seals)], initial_seals)
        self.assertEqual(check_integrity(load_entity(fresh)).status, "ok")
        self.assertIsNotNone(backup)
        self.assertEqual(LedgerStore(backup).load_financial_kinds(), kinds)

    def test_catalog_tag_and_column_mismatch_refuse_read_and_write(self) -> None:
        typed = self.root.parent / "typed"
        init_entity(typed, name="Example", classify_starter_accounts=True)
        for root, replacement in ((self.root, "1+financial-kind"), (typed, "1")):
            with self.subTest(replacement=replacement):
                store = LedgerStore(root / "ledger.sqlite")
                with closing(sqlite3.connect(store.path)) as conn, conn:
                    conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (replacement,))
                before = store.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "layout does not match"):
                    store.load_financial_kinds()
                with self.assertRaisesRegex(ValueError, "layout does not match"):
                    add_account(root, "Assets:Operating:Unexpected")
                self.assertEqual(store.path.read_bytes(), before)

    def test_bundle_retains_catalog_and_seal(self) -> None:
        account = "Assets:Operating:Reserve"
        add_account(self.root, account, financial_kind="bank")
        source = self.store.path.read_bytes()
        archive = self.root.parent / "example.zip"
        result = build_bundle(self.root, archive)
        self.assertEqual(result["status"], "exported", result)
        loaded = load_bundle(archive)
        self.assertEqual(self.store.path.read_bytes(), source)
        restored = self.root.parent / "restored"
        restored.mkdir()
        for name in ("entity.json", "ledger.sqlite"):
            (restored / name).write_bytes(loaded["files"][name])
        restored_store = LedgerStore(restored / "ledger.sqlite")
        self.assertEqual(restored_store.content_digest(), self.store.content_digest())
        self.assertEqual(restored_store.load_audit_events(), self.store.load_audit_events())
        entity = load_entity(restored)
        row = next(a for a in list_accounts(entity)["accounts"] if a["account"] == account)
        self.assertEqual(row["financial_kind"], "bank")
        self.assertEqual(check_integrity(entity).status, "ok")

    def test_unknown_and_malformed_schema_tags_refuse_open(self) -> None:
        for version in ("1+other", "1+financial-kind+unknown", "4+financial-kind", "01"):
            with self.subTest(version=version):
                with closing(sqlite3.connect(self.store.path)) as conn, conn:
                    conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (version,))
                with self.assertRaisesRegex(ValueError, "Unsupported ledger schema"):
                    self.store.load_financial_kinds()
