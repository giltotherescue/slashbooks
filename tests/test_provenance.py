"""Tests for the caller kind (`bookkeeping.provenance`) and its audit stamp."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping import provenance  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402


class CallerKindTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(provenance.ENV_VAR, None)

    def test_default_is_person_so_existing_callers_do_not_change(self):
        self.assertEqual(provenance.caller_kind(), "person")

    def test_environment_sets_the_kind(self):
        for kind in ("agent", "system", "person", "unknown"):
            os.environ[provenance.ENV_VAR] = kind
            self.assertEqual(provenance.caller_kind(), kind)

    def test_an_invalid_value_is_unknown(self):
        os.environ[provenance.ENV_VAR] = "robot"
        self.assertEqual(provenance.caller_kind(), "unknown")
        os.environ.pop(provenance.ENV_VAR)
        with provenance.use_caller_kind("robot"):
            self.assertEqual(provenance.caller_kind(), "unknown")

    def test_an_empty_environment_value_is_the_default(self):
        os.environ[provenance.ENV_VAR] = " "
        self.assertEqual(provenance.caller_kind(), "person")

    def test_context_wins_over_environment_nests_and_resets(self):
        os.environ[provenance.ENV_VAR] = "system"
        with provenance.use_caller_kind("agent"):
            self.assertEqual(provenance.caller_kind(), "agent")
            with provenance.use_caller_kind("person"):
                self.assertEqual(provenance.caller_kind(), "person")
            self.assertEqual(provenance.caller_kind(), "agent")
        self.assertEqual(provenance.caller_kind(), "system")

    def test_context_resets_after_a_failure(self):
        with self.assertRaises(RuntimeError):
            with provenance.use_caller_kind("agent"):
                raise RuntimeError("boom")
        self.assertEqual(provenance.caller_kind(), "person")

    def test_only_a_person_is_a_person(self):
        self.assertTrue(provenance.is_person("person"))
        for kind in ("agent", "system", "unknown"):
            self.assertFalse(provenance.is_person(kind))


class AuditStampTests(unittest.TestCase):
    def test_a_non_person_kind_is_sealed_in_the_chain_and_old_events_still_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp) / "ledger.sqlite")
            store.initialize()
            with store.transaction(immediate=True) as conn:
                store.append_audit_event("note", {"a": 1}, conn)
                with provenance.use_caller_kind("agent"):
                    store.append_audit_event("note", {"a": 2}, conn)
                # An event with no kind, as an older engine wrote it.
                from bookkeeping.ledger import store as store_module
                prev = conn.execute("SELECT record_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()[0]
                digest = store_module._hash_record("note", "2026-01-01T00:00:00Z", store_module._json({"a": 3}), prev)
                conn.execute("INSERT INTO audit_events (type, ts, payload_json, prev_hash, record_hash) "
                             "VALUES (?, ?, ?, ?, ?)",
                             ("note", "2026-01-01T00:00:00Z", store_module._json({"a": 3}), prev, digest))
            self.assertEqual(store.verify_audit_chain(), [])
            with store.connection() as conn:
                rows = [r[0] for r in conn.execute("SELECT payload_json FROM audit_events ORDER BY id")]
            self.assertNotIn("caller_kind", rows[0])  # the default is not stamped: person events stay as before
            self.assertIn('"caller_kind":"agent"', rows[1].replace(" ", ""))
            self.assertNotIn("caller_kind", rows[2])


if __name__ == "__main__":
    unittest.main()
