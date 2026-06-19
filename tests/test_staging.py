from __future__ import annotations

"""Tests for src/bookkeeping/ledger/staging.py

Test scenarios:
  - add_pending is idempotent (same id not staged twice).
  - get_pending returns all staged pending txns.
  - supersede_pending by pendingTransactionId (primary path).
  - supersede_pending by correlation key when no pendingTransactionId.
  - supersede returns None when no match.
  - drop_pending removes the record and emits audit-log entry.
  - purge_stale drops old records, keeps recent, audit-logs each drop.
  - mark_seen / is_seen / bulk_mark_seen round-trip.
  - check_orphaned_tmps detects .tmp files.
  - Atomic write: pending.json is a well-formed JSON file after each op.
  - Correlation key: two same-day same-amount same-desc txns with distinct
    IDs → only the first is superseded (no spurious second match).
"""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.ledger.auditlog import AuditLog  # noqa: E402
from bookkeeping.ledger.staging import StagingStore  # noqa: E402


def _make_pending_txn(
    txn_id: str = "txn-001",
    description: str = "Coffee Shop",
    amount: str = "50.00",
    account_id: str = "acct-1",
    txn_date: str = "2026-01-10",
    pending_txn_id: str | None = None,
) -> dict:
    d: dict = {
        "id": txn_id,
        "date": txn_date,
        "description": description,
        "amount": amount,
        "accountId": account_id,
        "pending": True,
    }
    if pending_txn_id is not None:
        d["pendingTransactionId"] = pending_txn_id
    return d


def _make_posted_txn(
    txn_id: str = "txn-002",
    description: str = "Coffee Shop",
    amount: str = "50.00",
    account_id: str = "acct-1",
    txn_date: str = "2026-01-10",
    pending_txn_id: str | None = None,
) -> dict:
    d: dict = {
        "id": txn_id,
        "date": txn_date,
        "description": description,
        "amount": amount,
        "accountId": account_id,
        "pending": False,
    }
    if pending_txn_id is not None:
        d["pendingTransactionId"] = pending_txn_id
    return d


class TestStagingStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._staging_dir = Path(self._tmp.name) / "staging"
        self._log_path = Path(self._tmp.name) / "audit-log.jsonl"
        self._audit_log = AuditLog(self._log_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_store(self) -> StagingStore:
        return StagingStore(self._staging_dir, self._audit_log)

    def test_add_and_get_pending(self) -> None:
        store = self._make_store()
        txn = _make_pending_txn("p1")
        store.add_pending(txn)
        pending = store.get_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], "p1")

    def test_add_pending_idempotent(self) -> None:
        """Same id staged twice → only one record."""
        store = self._make_store()
        txn = _make_pending_txn("p1")
        store.add_pending(txn)
        store.add_pending(txn)
        self.assertEqual(len(store.get_pending()), 1)

    def test_add_pending_persists_to_file(self) -> None:
        store = self._make_store()
        store.add_pending(_make_pending_txn("p1"))
        # Read the raw file.
        data = json.loads((self._staging_dir / "pending.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "p1")

    def test_supersede_by_pending_transaction_id(self) -> None:
        """supersede_pending matches by pendingTransactionId (primary path)."""
        store = self._make_store()
        pending = _make_pending_txn("p-orig", amount="50.00")
        store.add_pending(pending)

        posted = _make_posted_txn("posted-new", amount="52.00", pending_txn_id="p-orig")
        superseded = store.supersede_pending(posted)

        self.assertIsNotNone(superseded)
        self.assertEqual(superseded["id"], "p-orig")
        self.assertEqual(len(store.get_pending()), 0)

    def test_supersede_by_correlation_key(self) -> None:
        """supersede_pending falls back to correlation key when no pendingTransactionId."""
        store = self._make_store()
        pending = _make_pending_txn(
            "p-corr",
            description="STARBUCKS #1234",
            amount="5.00",
            account_id="acct-1",
            txn_date="2026-01-10",
        )
        store.add_pending(pending)

        # Posted with same normalized key but no pendingTransactionId.
        posted = _make_posted_txn(
            "posted-corr",
            description="STARBUCKS #1234",
            amount="5.00",
            account_id="acct-1",
            txn_date="2026-01-10",
        )
        superseded = store.supersede_pending(posted)

        self.assertIsNotNone(superseded)
        self.assertEqual(superseded["id"], "p-corr")
        self.assertEqual(len(store.get_pending()), 0)

    def test_supersede_returns_none_when_no_match(self) -> None:
        store = self._make_store()
        store.add_pending(_make_pending_txn("p1", description="Grocery", amount="20.00"))

        posted = _make_posted_txn("post-x", description="Totally Different", amount="999.00")
        superseded = store.supersede_pending(posted)
        self.assertIsNone(superseded)
        self.assertEqual(len(store.get_pending()), 1)

    def test_ae8_pending_50_posts_at_52_via_pending_txn_id(self) -> None:
        """AE8: pending $50 staged; posts at $52 with new ID + pendingTransactionId link."""
        store = self._make_store()
        pending = _make_pending_txn("pend-50", amount="50.00")
        store.add_pending(pending)

        posted = _make_posted_txn("post-52", amount="52.00", pending_txn_id="pend-50")
        superseded = store.supersede_pending(posted)

        self.assertIsNotNone(superseded)
        self.assertEqual(superseded["id"], "pend-50")
        # Posted transaction carries its new ID.
        self.assertEqual(posted["id"], "post-52")
        self.assertEqual(len(store.get_pending()), 0)

    def test_correlation_key_does_not_dedup_posted_vs_posted(self) -> None:
        """Two posted txns with distinct IDs but identical correlation keys → distinct."""
        store = self._make_store()
        # No pending to supersede.  Two posted with same day/amount/desc/account.
        self.assertFalse(store.is_seen("post-A"))
        self.assertFalse(store.is_seen("post-B"))
        store.mark_seen("post-A")
        self.assertTrue(store.is_seen("post-A"))
        self.assertFalse(store.is_seen("post-B"))
        store.mark_seen("post-B")
        self.assertTrue(store.is_seen("post-B"))

    def test_drop_pending(self) -> None:
        store = self._make_store()
        store.add_pending(_make_pending_txn("p1"))
        result = store.drop_pending("p1", reason="test", session_id="s1", ts="2026-01-01T00:00:00Z")
        self.assertTrue(result)
        self.assertEqual(len(store.get_pending()), 0)

    def test_drop_pending_emits_audit_record(self) -> None:
        store = self._make_store()
        store.add_pending(_make_pending_txn("p2"))
        store.drop_pending("p2", reason="canceled", session_id="s1", ts="2026-01-01T00:00:00Z")

        records = self._audit_log.all_records()
        drop_records = [r for r in records if r["type"] == "pending-dropped"]
        self.assertEqual(len(drop_records), 1)
        self.assertEqual(drop_records[0]["source_id"], "p2")
        self.assertEqual(drop_records[0]["reason"], "canceled")

    def test_drop_pending_returns_false_when_not_found(self) -> None:
        store = self._make_store()
        result = store.drop_pending("nonexistent", reason="test", session_id="s1")
        self.assertFalse(result)

    def test_purge_stale_drops_old_records(self) -> None:
        store = self._make_store()
        old_txn = _make_pending_txn("old", txn_date="2026-01-01")
        recent_txn = _make_pending_txn("recent", txn_date="2026-06-01")
        store.add_pending(old_txn)
        store.add_pending(recent_txn)

        # Reference date is 2026-06-12; cutoff = 30 days back = 2026-05-13.
        count = store.purge_stale(
            30,
            session_id="s1",
            ts="2026-06-12T00:00:00Z",
            reference_date=date(2026, 6, 12),
        )
        self.assertEqual(count, 1)
        remaining = store.get_pending()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], "recent")

    def test_purge_stale_emits_audit_records(self) -> None:
        store = self._make_store()
        store.add_pending(_make_pending_txn("stale1", txn_date="2026-01-01"))
        store.add_pending(_make_pending_txn("stale2", txn_date="2026-01-05"))
        store.purge_stale(
            30,
            session_id="s1",
            ts="2026-06-12T00:00:00Z",
            reference_date=date(2026, 6, 12),
        )
        records = self._audit_log.all_records()
        drops = [r for r in records if r["type"] == "pending-dropped"]
        self.assertEqual(len(drops), 2)

    def test_mark_seen_and_is_seen(self) -> None:
        store = self._make_store()
        self.assertFalse(store.is_seen("txn-1"))
        store.mark_seen("txn-1")
        self.assertTrue(store.is_seen("txn-1"))

    def test_bulk_mark_seen(self) -> None:
        store = self._make_store()
        store.bulk_mark_seen(["a", "b", "c"])
        for sid in ["a", "b", "c"]:
            self.assertTrue(store.is_seen(sid))

    def test_seen_ids_persisted_to_file(self) -> None:
        store = self._make_store()
        store.mark_seen("txn-x")
        data = json.loads((self._staging_dir / "seen-ids.json").read_text(encoding="utf-8"))
        self.assertIn("txn-x", data)

    def test_seen_ids_survive_reload(self) -> None:
        store1 = self._make_store()
        store1.mark_seen("persist-1")

        store2 = StagingStore(self._staging_dir, self._audit_log)
        self.assertTrue(store2.is_seen("persist-1"))

    def test_check_orphaned_tmps_none(self) -> None:
        store = self._make_store()
        self.assertEqual(store.check_orphaned_tmps(), [])

    def test_check_orphaned_tmps_detected(self) -> None:
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        (self._staging_dir / "pending.json.tmp").write_text("{}", encoding="utf-8")
        store = self._make_store()
        orphans = store.check_orphaned_tmps()
        self.assertEqual(len(orphans), 1)
        self.assertIn("pending.json.tmp", orphans[0])

    def test_pending_file_is_valid_json_after_multiple_ops(self) -> None:
        store = self._make_store()
        for i in range(5):
            store.add_pending(_make_pending_txn(f"p{i}"))
        store.drop_pending("p2", reason="test", session_id="s1")
        raw = (self._staging_dir / "pending.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertIsInstance(data, list)
        ids = {d["id"] for d in data}
        self.assertNotIn("p2", ids)
        self.assertIn("p0", ids)

    def test_supersede_only_matches_first_pending_with_same_correlation(self) -> None:
        """Two staged pendings with the same correlation key: only first is superseded."""
        store = self._make_store()
        p1 = _make_pending_txn("p1", description="FOOD MART", amount="10.00")
        p2 = _make_pending_txn("p2", description="FOOD MART", amount="10.00")
        store.add_pending(p1)
        store.add_pending(p2)

        posted = _make_posted_txn("post-x", description="FOOD MART", amount="10.00")
        superseded = store.supersede_pending(posted)
        self.assertIsNotNone(superseded)
        # Only one should have been removed.
        self.assertEqual(len(store.get_pending()), 1)


if __name__ == "__main__":
    unittest.main()
