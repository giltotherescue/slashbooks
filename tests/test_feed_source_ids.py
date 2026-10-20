"""Namespaced feed source IDs and the legacy BankSync alias."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.cli import main  # noqa: E402
from bookkeeping.entity import add_account, init_entity, load_entity, map_bank_account  # noqa: E402
from bookkeeping.ledger.importer import _load_pending_categorization, import_transactions  # noqa: E402
from bookkeeping.ledger.store import LedgerStore, default_store_path  # noqa: E402
from bookkeeping.source_ids import namespaced_feed_id, source_id_variants  # noqa: E402

CHECKING = "Assets:Bank:Operating-Checking"


def _company(tmp: str) -> Path:
    root = Path(tmp).resolve() / "company"
    init_entity(root, name="Harbor Studio", business_type="consulting", legal_structure="llc")
    add_account(root, CHECKING, open_date="2025-01-01")
    map_bank_account(root, "acct_1", CHECKING)
    return root


def _row(source_id: str, **changes) -> dict:
    row = {"id": source_id, "date": "2026-01-15", "description": "Synthetic vendor", "amount": "-25.00",
           "accountId": "acct_1", "accountName": "Checking", "accountType": "checking", "pending": False}
    row.update(changes)
    return row


def _ingest(root: Path, rows: list[dict], *extra: str, source: str = "banksync") -> int:
    path = root.parent / "feed.json"
    path.write_text(json.dumps({"transactions": rows}), encoding="utf-8")
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return main(["ingest", str(path), "--entity", str(root), "--source", source, "--no-feed-window", *extra])


def _ledger_ids(root: Path) -> list[str]:
    return sorted(e.source_id for e in LedgerStore(default_store_path(root)).load_entries() if e.source_id)


def _queued_ids(root: Path) -> list[str]:
    return sorted(item["id"] for item in _load_pending_categorization(load_entity(root)))


class VariantTests(unittest.TestCase):
    def test_banksync_spellings_name_one_row(self) -> None:
        self.assertEqual(source_id_variants("abc"), {"abc", "banksync:abc"})
        self.assertEqual(source_id_variants("banksync:abc"), {"abc", "banksync:abc"})

    def test_other_ids_have_no_alias(self) -> None:
        for value in ("plaid:abc", "stmt:abc", "csv:abc", "stripe:acct:xyz", "accrual:one", ""):
            self.assertEqual(source_id_variants(value), {value})

    def test_namespacing_is_idempotent(self) -> None:
        self.assertEqual(namespaced_feed_id("plaid", "abc"), "plaid:abc")
        self.assertEqual(namespaced_feed_id("plaid", "plaid:abc"), "plaid:abc")
        self.assertEqual(namespaced_feed_id("plaid", ""), "")


class IngestNamespaceTests(unittest.TestCase):
    def test_new_banksync_rows_get_the_banksync_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self.assertEqual(_ingest(root, [_row("tx1")]), 0)
            self.assertEqual(_queued_ids(root), ["banksync:tx1"])

    def test_other_provider_prefix_comes_from_source_or_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self.assertEqual(_ingest(root, [_row("p1")], source="plaid"), 0)
            self.assertEqual(_ingest(root, [_row("c1", description="Other vendor")], "--feed-provider", "custom-bank",
                                     source="my-sync"), 0)
            self.assertEqual(_queued_ids(root), ["custom-bank:c1", "plaid:p1"])

    def test_generic_ingest_keeps_raw_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self.assertEqual(_ingest(root, [_row("raw1")], source="ingest"), 0)
            self.assertEqual(_queued_ids(root), ["raw1"])

    def test_reingest_is_still_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            _ingest(root, [_row("tx1")])
            _ingest(root, [_row("tx1")])
            self.assertEqual(_queued_ids(root), ["banksync:tx1"])

    def test_old_unprefixed_banksync_id_still_matches_a_prefixed_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            entity = load_entity(root)
            import_transactions(entity, [_row("legacy1")], "old-session", lambda t: ("Expenses:Services", "high"))
            self.assertEqual(_ledger_ids(root), ["legacy1"])
            _ingest(root, [_row("legacy1")])
            self.assertEqual(_ledger_ids(root), ["legacy1"])
            self.assertEqual(_queued_ids(root), [])

    def test_prefixed_row_in_ledger_matches_an_unprefixed_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            entity = load_entity(root)
            import_transactions(entity, [_row("banksync:new1")], "s", lambda t: ("Expenses:Services", "high"))
            result = import_transactions(entity, [_row("new1")], "s2", lambda t: ("Expenses:Services", "high"))
            self.assertEqual((result.new_entries, result.skipped_duplicate), (0, 1))
            self.assertEqual(_ledger_ids(root), ["banksync:new1"])

    def test_queued_old_id_blocks_the_prefixed_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            import_transactions(load_entity(root), [_row("q1")], "s")  # no categorizer: queued
            self.assertEqual(_queued_ids(root), ["q1"])
            _ingest(root, [_row("q1")])
            self.assertEqual(_queued_ids(root), ["q1"])

    def test_two_rows_with_both_spellings_in_one_batch_post_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            result = import_transactions(load_entity(root), [_row("b1"), _row("banksync:b1")], "s",
                                         lambda t: ("Expenses:Services", "high"))
            self.assertEqual((result.new_entries, result.skipped_duplicate), (1, 1))

    def test_other_providers_are_not_aliased_to_raw_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            import_transactions(load_entity(root), [_row("same")], "s", lambda t: ("Expenses:Services", "high"))
            _ingest(root, [_row("same", description="Different merchant")], source="plaid")
            self.assertEqual(_queued_ids(root), ["plaid:same"])

    def test_pending_link_survives_the_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            _ingest(root, [_row("pend1", pending=True, amount="-20.00")])
            self.assertEqual(len(json.loads((root / "staging" / "pending.json").read_text())), 1)
            # A posted row names the pending row by the provider's raw ID.
            _ingest(root, [_row("post1", pendingTransactionId="pend1", amount="-21.00")])
            self.assertEqual(json.loads((root / "staging" / "pending.json").read_text()), [])
            self.assertEqual(_queued_ids(root), ["banksync:post1"])

    def test_old_staged_pending_is_superseded_by_a_prefixed_posted_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            import_transactions(load_entity(root), [_row("pend0", pending=True, amount="-20.00")], "old")
            _ingest(root, [_row("post0", pendingTransactionId="pend0", amount="-21.00")])
            self.assertEqual(json.loads((root / "staging" / "pending.json").read_text()), [])

    def test_a_host_prepared_snapshot_keeps_its_own_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            path = root.parent / "feed.json"
            path.write_text(json.dumps({"transactions": [_row("snap1")]}), encoding="utf-8")
            with mock.patch("bookkeeping.preparation_runtime.prepared_ingest_transactions",
                            return_value=([_row("snap1")], True)):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["ingest", str(path), "--entity", str(root),
                                           "--source", "banksync", "--no-feed-window"]), 0)
            self.assertEqual(_queued_ids(root), ["snap1"])


if __name__ == "__main__":
    unittest.main()
