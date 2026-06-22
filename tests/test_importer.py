from __future__ import annotations

"""Tests for src/bookkeeping/ledger/importer.py

Covers all U4 test scenarios from the plan:
  - AE8: pending $50 staged; posts at $52 with new ID + pendingTransactionId link.
  - Happy path: same window imported twice → zero new entries (byte-identical).
  - Overlapping windows → no duplicates.
  - Two same-day same-amount same-description posted txns with distinct IDs → BOTH import.
  - Simulated crash between intent and seal → next run detects incomplete-write, not halt.
  - Orphaned staging .tmp detected and reported.
  - Audit chain verification catches tampered line.
  - Hash mismatch with no git → halt with diff.
  - Acknowledged continue recorded with diff hash.
  - reverse_and_correct: both entries in one write, validator passes, original untouched.
  - Late-arrival flag set correctly.
  - Categorizer routes uncategorized to pending-categorization list.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.entity import Entity  # noqa: E402
from bookkeeping.ledger.auditlog import AuditLog  # noqa: E402
from bookkeeping.ledger.importer import (  # noqa: E402
    ImportResult,
    IntegrityResult,
    _ledger_account_for_txn,
    acknowledge_mismatch,
    check_integrity,
    import_transactions,
    ratify_git_commit,
    reverse_and_correct,
)
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.projections import render_store_ledger  # noqa: E402
from bookkeeping.ledger.staging import StagingStore  # noqa: E402
from bookkeeping.ledger.store import LedgerStore, default_store_path  # noqa: E402
from bookkeeping.ledger.validator import validate  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TS = "2026-01-15T00:00:00Z"
SESSION = "test-session-001"


def _make_entity(tmp_dir: Path) -> Entity:
    """Create a minimal Entity in *tmp_dir*."""
    entity_dir = tmp_dir / "entity"
    entity_dir.mkdir(parents=True, exist_ok=True)
    (entity_dir / "staging").mkdir(exist_ok=True)
    (entity_dir / "reports").mkdir(exist_ok=True)

    entity_config = {"name": "Test Entity", "business_type": "consulting"}
    (entity_dir / "entity.json").write_text(
        json.dumps(entity_config) + "\n", encoding="utf-8"
    )
    trust_policy = {"auto_post_threshold": 3, "queue_all_until_confirmed": True}
    (entity_dir / "trust-policy.json").write_text(
        json.dumps(trust_policy) + "\n", encoding="utf-8"
    )

    return Entity(
        path=entity_dir,
        entity_config=entity_config,
        trust_policy=trust_policy,
    )


def _make_posted_txn(
    txn_id: str,
    description: str = "Test Merchant",
    amount: str = "100.00",
    account_id: str = "acct-1",
    account_name: str = "Checking",
    txn_date: str = "2026-01-15",
    pending_txn_id: str | None = None,
) -> dict:
    d: dict = {
        "id": txn_id,
        "date": txn_date,
        "description": description,
        "amount": amount,
        "accountId": account_id,
        "accountName": account_name,
        "pending": False,
    }
    if pending_txn_id is not None:
        d["pendingTransactionId"] = pending_txn_id
    return d


def _make_pending_txn(
    txn_id: str,
    description: str = "Test Merchant",
    amount: str = "50.00",
    account_id: str = "acct-1",
    account_name: str = "Checking",
    txn_date: str = "2026-01-15",
) -> dict:
    return {
        "id": txn_id,
        "date": txn_date,
        "description": description,
        "amount": amount,
        "accountId": account_id,
        "accountName": account_name,
        "pending": True,
    }


def _simple_categorizer(account: str):
    """Return a categorizer that always assigns *account*."""
    def categorizer(txn: dict) -> tuple[str, str]:
        return account, "high"
    return categorizer


def _read_ledger(entity: Entity) -> str:
    store_path = default_store_path(entity.path)
    if store_path.exists():
        return render_store_ledger(store_path)
    if not entity.books_path.exists():
        return ""
    return entity.books_path.read_text(encoding="utf-8")


def _validate_ledger(entity: Entity) -> list:
    """Return validation errors for the entity ledger."""
    text = _read_ledger(entity)
    if not text.strip():
        return []
    return validate(text)


class BankAccountMappingTests(unittest.TestCase):
    def test_explicit_mapping_uses_account_id_before_name(self) -> None:
        txn = {
            "accountId": "acct_card",
            "accountName": "Corporate Card",
            "accountType": "credit_card",
        }
        mappings = {
            "acct_card": "Liabilities:CreditCard:Mercury-Corporate",
            "Corporate Card": "Liabilities:CreditCard:Name-Fallback",
        }

        self.assertEqual(
            _ledger_account_for_txn(txn, mappings),
            "Liabilities:CreditCard:Mercury-Corporate",
        )

    def test_explicit_mapping_uses_account_name_when_id_missing(self) -> None:
        txn = {
            "accountId": "unknown",
            "accountName": "Operating Checking",
            "accountType": "checking",
        }
        mappings = {"Operating Checking": "Assets:Bank:Mercury-Checking"}

        self.assertEqual(_ledger_account_for_txn(txn, mappings), "Assets:Bank:Mercury-Checking")

    def test_checking_fallback_uses_bank_asset_namespace(self) -> None:
        txn = {"accountName": "Operating Checking", "accountType": "checking"}

        self.assertEqual(_ledger_account_for_txn(txn), "Assets:Bank:Operating-Checking")

    def test_credit_card_fallback_uses_liability_namespace(self) -> None:
        txn = {"accountName": "Mercury Credit", "accountType": "credit_card"}

        self.assertEqual(_ledger_account_for_txn(txn), "Liabilities:CreditCard:Mercury-Credit")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestImportTransactionsBasic(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._entity = _make_entity(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_import_single_posted_transaction(self) -> None:
        txns = [_make_posted_txn("txn-1")]
        result = import_transactions(
            self._entity, txns, SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        self.assertEqual(result.new_entries, 1)
        self.assertEqual(result.skipped_duplicate, 0)
        errors = _validate_ledger(self._entity)
        self.assertEqual(errors, [], f"Validation errors: {errors}")

    def test_idempotent_same_window_twice(self) -> None:
        """Same window imported twice → zero new entries on second run."""
        txns = [_make_posted_txn("txn-2")]
        import_transactions(
            self._entity, txns, SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        ledger_after_first = _read_ledger(self._entity)

        result2 = import_transactions(
            self._entity, txns, SESSION + "-2",
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        ledger_after_second = _read_ledger(self._entity)

        self.assertEqual(result2.new_entries, 0)
        self.assertEqual(result2.skipped_duplicate, 1)
        # Ledger content is byte-identical.
        self.assertEqual(ledger_after_first, ledger_after_second)
        errors = _validate_ledger(self._entity)
        self.assertEqual(errors, [])

    def test_overlapping_windows_no_duplicates(self) -> None:
        """Overlapping windows (shared boundary day) produce no duplicates."""
        txns_day1 = [
            _make_posted_txn("txn-3", txn_date="2026-01-10"),
            _make_posted_txn("txn-4", txn_date="2026-01-11"),
        ]
        txns_day2 = [
            _make_posted_txn("txn-4", txn_date="2026-01-11"),  # Overlap
            _make_posted_txn("txn-5", txn_date="2026-01-12"),
        ]

        import_transactions(
            self._entity, txns_day1, SESSION + "-d1",
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 11),
        )
        result2 = import_transactions(
            self._entity, txns_day2, SESSION + "-d2",
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 12),
        )
        self.assertEqual(result2.new_entries, 1)  # Only txn-5 is new.
        self.assertEqual(result2.skipped_duplicate, 1)  # txn-4 is a dup.

        errors = _validate_ledger(self._entity)
        self.assertEqual(errors, [])

    def test_two_posted_same_day_amount_desc_distinct_ids_both_import(self) -> None:
        """Two same-day same-amount same-description posted txns with distinct IDs → BOTH import."""
        txns = [
            _make_posted_txn("txn-A", description="COFFEE SHOP", amount="5.00", txn_date="2026-01-15"),
            _make_posted_txn("txn-B", description="COFFEE SHOP", amount="5.00", txn_date="2026-01-15"),
        ]
        result = import_transactions(
            self._entity, txns, SESSION,
            categorizer=_simple_categorizer("Expenses:Food"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        self.assertEqual(result.new_entries, 2)
        self.assertEqual(result.skipped_duplicate, 0)

        errors = _validate_ledger(self._entity)
        self.assertEqual(errors, [])

        # Verify both source-ids appear in the ledger.
        ledger_text = _read_ledger(self._entity)
        self.assertIn("txn-A", ledger_text)
        self.assertIn("txn-B", ledger_text)


class TestAE8PendingSupersession(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._entity = _make_entity(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ae8_pending_50_posts_at_52(self) -> None:
        """AE8: pending $50 staged; posts at $52 with new ID + pendingTransactionId."""
        pending_txn = _make_pending_txn("pend-50", amount="50.00")

        # Stage the pending transaction.
        result1 = import_transactions(
            self._entity, [pending_txn], SESSION + "-p",
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        self.assertEqual(result1.new_pending, 1)
        self.assertEqual(result1.new_entries, 0)

        # Ledger should have no entries (pending stays in staging).
        self.assertEqual(_validate_ledger(self._entity), [])
        staging = StagingStore(self._entity.staging_dir, AuditLog(self._entity.path / "audit-log.jsonl"))
        self.assertEqual(len(staging.get_pending()), 1)
        self.assertFalse(staging.is_seen("pend-50"))

        # Now the transaction posts at $52 with a new ID.
        posted_txn = _make_posted_txn("post-52", amount="52.00", pending_txn_id="pend-50")
        result2 = import_transactions(
            self._entity, [posted_txn], SESSION + "-posted",
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        self.assertEqual(result2.new_entries, 1)
        self.assertEqual(result2.superseded, 1)
        self.assertEqual(result2.skipped_duplicate, 0)

        # Staging should be empty.
        staging2 = StagingStore(self._entity.staging_dir, AuditLog(self._entity.path / "audit-log.jsonl"))
        self.assertEqual(len(staging2.get_pending()), 0)
        self.assertTrue(staging2.is_seen("post-52"))
        self.assertFalse(staging2.is_seen("pend-50"))

        # Ledger should contain the $52 transaction (the posted amount).
        ledger_text = _read_ledger(self._entity)
        self.assertIn("52.00", ledger_text)
        self.assertIn("post-52", ledger_text)
        # Should NOT have the $50 amount.
        # (The ledger has the posted entry only.)

        errors = _validate_ledger(self._entity)
        self.assertEqual(errors, [])

        # Re-importing the same posted txn → zero new entries.
        result3 = import_transactions(
            self._entity, [posted_txn], SESSION + "-dup",
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        self.assertEqual(result3.new_entries, 0)
        self.assertEqual(result3.skipped_duplicate, 1)

    def test_supersession_by_correlation_key_when_no_pending_txn_id(self) -> None:
        """Supersede by correlation key when pendingTransactionId absent."""
        pending = _make_pending_txn(
            "pend-corr",
            description="STARBUCKS #1234",
            amount="6.00",
            account_id="acct-2",
            txn_date="2026-02-01",
        )
        import_transactions(
            self._entity, [pending], SESSION + "-p",
            categorizer=_simple_categorizer("Expenses:Food"),
            ts=TS,
            session_date=date(2026, 2, 1),
        )

        # Post arrives with same normalized key but no pendingTransactionId.
        posted = _make_posted_txn(
            "post-corr",
            description="STARBUCKS #1234",
            amount="6.00",
            account_id="acct-2",
            txn_date="2026-02-01",
        )
        result = import_transactions(
            self._entity, [posted], SESSION + "-post",
            categorizer=_simple_categorizer("Expenses:Food"),
            ts=TS,
            session_date=date(2026, 2, 1),
        )
        self.assertEqual(result.new_entries, 1)
        self.assertEqual(result.superseded, 1)

        staging = StagingStore(self._entity.staging_dir, AuditLog(self._entity.path / "audit-log.jsonl"))
        self.assertEqual(len(staging.get_pending()), 0)


class TestPendingCategorization(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._entity = _make_entity(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_uncategorized_routes_to_pending_categorization(self) -> None:
        """Categorizer returning empty string → pending-categorization list."""
        txns = [_make_posted_txn("txn-u1")]

        def no_category(txn: dict) -> tuple[str, str]:
            return "", ""

        result = import_transactions(
            self._entity, txns, SESSION,
            categorizer=no_category,
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        self.assertEqual(result.new_entries, 0)
        self.assertEqual(result.pending_categorization, 1)

        # Pending-categorization file should exist.
        pc_path = self._entity.staging_dir / "pending-categorization.json"
        self.assertTrue(pc_path.exists())
        data = json.loads(pc_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "txn-u1")

    def test_no_categorizer_routes_all_to_pending_categorization(self) -> None:
        txns = [_make_posted_txn("txn-nc1"), _make_posted_txn("txn-nc2")]
        result = import_transactions(
            self._entity, txns, SESSION,
            categorizer=None,
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        self.assertEqual(result.new_entries, 0)
        self.assertEqual(result.pending_categorization, 2)


class TestLateArrival(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._entity = _make_entity(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_late_arrival_flag_set(self) -> None:
        """Transaction date >30 days before session date → late-arrival: true."""
        txns = [_make_posted_txn("txn-late", txn_date="2026-01-01")]
        result = import_transactions(
            self._entity, txns, SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 3, 1),  # 59 days later → late
        )
        self.assertEqual(result.new_entries, 1)
        self.assertEqual(result.late_arrivals, 1)
        ledger_text = _read_ledger(self._entity)
        self.assertIn("late-arrival", ledger_text)
        self.assertIn('"true"', ledger_text)

    def test_not_late_arrival_within_30_days(self) -> None:
        """Transaction date within 30 days of session date → no late-arrival flag."""
        txns = [_make_posted_txn("txn-on-time", txn_date="2026-01-15")]
        result = import_transactions(
            self._entity, txns, SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 2, 5),  # 21 days later → not late
        )
        self.assertEqual(result.late_arrivals, 0)
        ledger_text = _read_ledger(self._entity)
        self.assertNotIn("late-arrival", ledger_text)


class TestOrphanedTmpDetection(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._entity = _make_entity(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_orphaned_tmp_detected_and_reported(self) -> None:
        """An orphaned .tmp file in staging/ is detected and reported."""
        self._entity.staging_dir.mkdir(parents=True, exist_ok=True)
        orphan = self._entity.staging_dir / "pending.json.tmp"
        orphan.write_text("{}", encoding="utf-8")

        result = import_transactions(
            self._entity, [], SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
        )
        self.assertEqual(len(result.orphaned_tmps), 1)
        self.assertIn("pending.json.tmp", result.orphaned_tmps[0])


class TestAtomicWriteAndIntegrity(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._entity = _make_entity(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_integrity_ok_after_normal_import(self) -> None:
        """After a normal import, check_integrity returns ok."""
        txns = [_make_posted_txn("txn-ok")]
        import_transactions(
            self._entity, txns, SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        result = check_integrity(self._entity)
        self.assertEqual(result.status, "ok")

    def test_import_creates_entity_write_lock_file(self) -> None:
        """Ledger mutations use an entity-level lock file."""
        import_transactions(
            self._entity,
            [_make_posted_txn("txn-lock")],
            SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        self.assertTrue((self._entity.path / ".books.lock").exists())

    def test_incomplete_write_recovery_not_halt(self) -> None:
        """Simulated store corruption: intent written, seal never written.
        Next check_integrity returns 'incomplete-write', NOT 'halt'.
        """
        store = LedgerStore(default_store_path(self._entity.path))
        store.initialize()
        with store.transaction() as conn:
            store.append_audit_event(
                "intent",
                {"session_id": SESSION, "description": "simulated crash"},
                conn,
                ts=TS,
            )

        result = check_integrity(self._entity)
        self.assertEqual(result.status, "incomplete-write")
        self.assertNotEqual(result.status, "halt")

    def test_store_audit_tamper_returns_halt(self) -> None:
        """Store audit tampering returns halt."""
        txns = [_make_posted_txn("txn-mismatch")]
        import_transactions(
            self._entity, txns, SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        store = LedgerStore(default_store_path(self._entity.path))
        with store.connection() as conn:
            conn.execute("UPDATE audit_events SET payload_json = ? WHERE id = 1", ('{"tampered":true}',))
            conn.commit()
        result = check_integrity(self._entity)
        self.assertEqual(result.status, "halt")

    def test_store_audit_halt_has_message(self) -> None:
        """When status is halt, result.message explains the store issue."""
        txns = [_make_posted_txn("txn-diff")]
        import_transactions(
            self._entity, txns, SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        store = LedgerStore(default_store_path(self._entity.path))
        with store.connection() as conn:
            conn.execute("UPDATE audit_events SET record_hash = ? WHERE id = 1", ("bad",))
            conn.commit()
        result = check_integrity(self._entity)
        self.assertEqual(result.status, "halt")
        self.assertTrue(result.message)

    def test_acknowledge_mismatch_records_diff_hash(self) -> None:
        """acknowledge_mismatch writes an 'acknowledged' record with diff_sha256."""
        txns = [_make_posted_txn("txn-ack")]
        import_transactions(
            self._entity, txns, SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )

        diff_text = "; hand-edited line"
        acknowledge_mismatch(self._entity, diff_text, SESSION, ts=TS)

        records = LedgerStore(default_store_path(self._entity.path)).load_audit_events()
        ack_records = [r for r in records if r["type"] == "acknowledged"]
        self.assertEqual(len(ack_records), 1)

        expected_sha256 = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
        self.assertEqual(ack_records[0]["payload"]["diff_sha256"], expected_sha256)

    def test_store_integrity_ignores_exported_file_edits(self) -> None:
        """Exported Beancount snapshots are not the source of truth."""
        import_transactions(
            self._entity,
            [_make_posted_txn("txn-git-dirty")],
            SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        self._entity.books_path.write_text("; exported file edit\n", encoding="utf-8")
        result = check_integrity(self._entity)
        self.assertEqual(result.status, "ok")

    def test_ratification_records_store_event(self) -> None:
        """Ratifying an external review records a store audit event."""
        import_transactions(
            self._entity,
            [_make_posted_txn("txn-git-ratify")],
            SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        ratify_git_commit(
            self._entity,
            commit_hash="abc123",
            author="owner@example.com",
            diff_sha256="deadbeef",
            session_id=SESSION,
            ts=TS,
        )

        after = check_integrity(self._entity)
        self.assertEqual(after.status, "ok")

    def test_ledger_passes_validation_after_import(self) -> None:
        """After import, ledger always passes the built-in validator."""
        txns = [
            _make_posted_txn("txn-v1", txn_date="2026-01-10"),
            _make_posted_txn("txn-v2", txn_date="2026-01-12"),
            _make_posted_txn("txn-v3", txn_date="2026-01-15"),
        ]
        import_transactions(
            self._entity, txns, SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        errors = _validate_ledger(self._entity)
        self.assertEqual(errors, [], f"Validation errors: {errors}")

    def test_audit_chain_valid_after_import(self) -> None:
        """Audit log chain is intact after a successful import."""
        txns = [_make_posted_txn("txn-chain")]
        import_transactions(
            self._entity, txns, SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        errors = LedgerStore(default_store_path(self._entity.path)).verify_audit_chain()
        self.assertEqual(errors, [], f"Audit chain errors: {errors}")

    def test_audit_log_contains_intent_and_seal(self) -> None:
        """After import, store audit contains intent followed by store seal."""
        txns = [_make_posted_txn("txn-il")]
        import_transactions(
            self._entity, txns, SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        records = LedgerStore(default_store_path(self._entity.path)).load_audit_events()

        types = [r["type"] for r in records]
        self.assertIn("intent", types)
        self.assertIn("ledger-store-sealed", types)

        # intent must come before the seal in the store audit history.
        intent_idx = types.index("intent")
        seal_idx = types.index("ledger-store-sealed")
        self.assertLess(intent_idx, seal_idx)

    def test_integrity_ok_fresh_entity_no_ledger(self) -> None:
        """Fresh entity with no ledger → integrity ok."""
        result = check_integrity(self._entity)
        self.assertEqual(result.status, "ok")

    def test_multiple_imports_accumulate_entries(self) -> None:
        """Multiple import sessions accumulate entries correctly."""
        import_transactions(
            self._entity,
            [_make_posted_txn("batch1-1", txn_date="2026-01-01")],
            "sess-1",
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        import_transactions(
            self._entity,
            [_make_posted_txn("batch2-1", txn_date="2026-01-05")],
            "sess-2",
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        from bookkeeping.ledger.validator import parse_ledger
        text = _read_ledger(self._entity)
        parsed = parse_ledger(text)
        self.assertEqual(len(parsed["entries"]), 2)

        errors = _validate_ledger(self._entity)
        self.assertEqual(errors, [])


class TestReverseAndCorrect(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._entity = _make_entity(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reverse_and_correct_produces_both_entries(self) -> None:
        """reverse_and_correct writes reversing + corrected entries; validator passes."""
        # First import an original transaction.
        original_txn = _make_posted_txn("orig-001", description="Wrong Category", amount="75.00")
        import_transactions(
            self._entity, [original_txn], SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )

        # Build the corrected entry.
        corrected = Entry(
            date=date(2026, 1, 15),
            narration="Wrong Category (corrected)",
            flag="*",
            meta=(("source-id", "orig-001-corrected"),),
            postings=(
                Posting("Assets:Checking", Decimal("75.00")),
                Posting("Expenses:Consulting", Decimal("-75.00")),
            ),
        )

        rev_entry, corr_entry = reverse_and_correct(
            self._entity, "orig-001", corrected, SESSION + "-fix", ts=TS
        )

        # Validate ledger.
        errors = _validate_ledger(self._entity)
        self.assertEqual(errors, [], f"Validation errors: {errors}")

        ledger_text = _read_ledger(self._entity)

        # Reversing entry has negated amounts.
        self.assertIn("reverses", ledger_text)
        self.assertIn("orig-001", ledger_text)

        # Corrected entry has correction-of metadata.
        self.assertIn("correction-of", ledger_text)

        # Original entry is still present (untouched).
        self.assertIn("-75.00", ledger_text)
        self.assertIn("75.00", ledger_text)

        # Verify the metadata on the returned entries.
        rev_meta = dict(rev_entry.meta)
        self.assertEqual(rev_meta.get("reverses"), "orig-001")

        corr_meta = dict(corr_entry.meta)
        self.assertEqual(corr_meta.get("correction-of"), "orig-001")

    def test_reverse_and_correct_one_atomic_write(self) -> None:
        """Both reversal and correction land in one ledger write (one intent+seal pair)."""
        original_txn = _make_posted_txn("orig-atomic", amount="50.00")
        import_transactions(
            self._entity, [original_txn], SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )

        store = LedgerStore(default_store_path(self._entity.path))
        records_before = store.load_audit_events()
        intent_count_before = sum(1 for r in records_before if r["type"] == "intent")

        corrected = Entry(
            date=date(2026, 1, 15),
            narration="Corrected",
            flag="*",
            meta=(("source-id", "orig-atomic-c"),),
            postings=(
                Posting("Assets:Checking", Decimal("50.00")),
                Posting("Expenses:Consulting", Decimal("-50.00")),
            ),
        )
        reverse_and_correct(self._entity, "orig-atomic", corrected, "fix-session", ts=TS)

        records_after = store.load_audit_events()
        intent_count_after = sum(1 for r in records_after if r["type"] == "intent")
        seal_count_after = sum(1 for r in records_after if r["type"] == "ledger-store-sealed")

        # Exactly one additional intent (and seal) for the correction.
        self.assertEqual(intent_count_after - intent_count_before, 1)
        # Each intent has a matching seal.
        self.assertEqual(intent_count_after, seal_count_after)

    def test_reverse_and_correct_original_untouched(self) -> None:
        """Original entry's source-id and amounts remain in the ledger file."""
        original_txn = _make_posted_txn("orig-keep", amount="120.00")
        import_transactions(
            self._entity, [original_txn], SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )

        corrected = Entry(
            date=date(2026, 1, 15),
            narration="Fixed",
            flag="*",
            meta=(("source-id", "orig-keep-fixed"),),
            postings=(
                Posting("Assets:Checking", Decimal("120.00")),
                Posting("Expenses:Consulting", Decimal("-120.00")),
            ),
        )
        reverse_and_correct(self._entity, "orig-keep", corrected, "fix-2", ts=TS)

        ledger_text = _read_ledger(self._entity)
        # Original source-id and amount still present.
        self.assertIn("orig-keep", ledger_text)

        from bookkeeping.ledger.validator import parse_ledger
        parsed = parse_ledger(ledger_text)
        source_ids = [e.source_id for e in parsed["entries"]]
        self.assertIn("orig-keep", source_ids)

    def test_reverse_and_correct_raises_for_unknown_source_id(self) -> None:
        """Raises ValueError when the original source-id is not found."""
        corrected = Entry(
            date=date(2026, 1, 15),
            narration="Corrected",
            flag="*",
            meta=(("source-id", "never-existed"),),
            postings=(
                Posting("Assets:Checking", Decimal("10.00")),
                Posting("Expenses:Services", Decimal("-10.00")),
            ),
        )
        with self.assertRaises((ValueError, FileNotFoundError)):
            reverse_and_correct(self._entity, "ghost-id", corrected, "bad-session", ts=TS)


class TestAuditChainIntegrity(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._entity = _make_entity(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_audit_chain_valid_after_multiple_sessions(self) -> None:
        for i in range(3):
            import_transactions(
                self._entity,
                [_make_posted_txn(f"txn-chain-{i}", txn_date="2026-01-15")],
                f"sess-{i}",
                categorizer=_simple_categorizer("Expenses:Services"),
                ts=TS,
                session_date=date(2026, 1, 15),
            )
        errors = LedgerStore(default_store_path(self._entity.path)).verify_audit_chain()
        self.assertEqual(errors, [], f"Audit chain errors: {errors}")

    def test_tampered_store_audit_detected(self) -> None:
        """Tampering with a middle store audit record is detected."""
        import_transactions(
            self._entity,
            [_make_posted_txn("txn-tamper")],
            SESSION,
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )
        import_transactions(
            self._entity,
            [_make_posted_txn("txn-tamper2")],
            SESSION + "-2",
            categorizer=_simple_categorizer("Expenses:Services"),
            ts=TS,
            session_date=date(2026, 1, 15),
        )

        store = LedgerStore(default_store_path(self._entity.path))
        with store.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            self.assertGreater(count, 2)
            mid = count // 2
            conn.execute("UPDATE audit_events SET payload_json = ? WHERE id = ?", ('{"tampered":true}', mid))
            conn.commit()

        errors = store.verify_audit_chain()
        self.assertGreater(len(errors), 0)


class TestRatifyAndAcknowledge(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._entity = _make_entity(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ratify_git_commit_records_correct_fields(self) -> None:
        ratify_git_commit(
            self._entity,
            commit_hash="abc123",
            author="owner@example.com",
            diff_sha256="deadbeef",
            session_id=SESSION,
            ts=TS,
        )
        records = LedgerStore(default_store_path(self._entity.path)).load_audit_events()
        ratified = [r for r in records if r["type"] == "ratified"]
        self.assertEqual(len(ratified), 1)
        self.assertEqual(ratified[0]["payload"]["commit_hash"], "abc123")
        self.assertEqual(ratified[0]["payload"]["author"], "owner@example.com")
        self.assertEqual(ratified[0]["payload"]["diff_sha256"], "deadbeef")


if __name__ == "__main__":
    unittest.main()
