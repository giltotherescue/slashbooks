from __future__ import annotations

"""Tests for src/bookkeeping/ledger/auditlog.py

Test scenarios:
  - Happy path: append records and read them back.
  - Hash chain: each record's ``prev`` matches SHA-256 of the previous line.
  - verify_chain passes on a valid log.
  - verify_chain catches a tampered middle line.
  - last_sealed skips trailing acknowledged/ratified records.
  - has_seal_for_intent: True when seal follows intent, False otherwise.
  - Genesis record: ``prev`` == 64 zeros.
  - Empty log: verify_chain returns [].
"""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.ledger.auditlog import AuditLog, _GENESIS_PREV, verify_chain  # noqa: E402


def _sha256_line(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


class TestAuditLogBasics(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._log_path = Path(self._tmp.name) / "audit-log.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_log(self) -> AuditLog:
        return AuditLog(self._log_path)

    def test_append_and_tail(self) -> None:
        log = self._make_log()
        log.append("intent", ts="2026-01-01T00:00:00Z", session_id="s1", description="test")
        log.append("ledger-sealed", ts="2026-01-01T00:00:01Z", session_id="s1", sha256="abc")
        records = log.tail(10)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["type"], "intent")
        self.assertEqual(records[1]["type"], "ledger-sealed")

    def test_genesis_prev(self) -> None:
        """First record carries prev = 64 zeros."""
        log = self._make_log()
        log.append("intent", ts="2026-01-01T00:00:00Z", session_id="s1", description="first")
        records = log.all_records()
        self.assertEqual(records[0]["prev"], _GENESIS_PREV)

    def test_chain_prev_is_correct(self) -> None:
        """Each record's prev == SHA-256 of the raw line of the preceding record."""
        log = self._make_log()
        log.append("intent", ts="2026-01-01T00:00:00Z", session_id="s1", description="a")
        log.append("ledger-sealed", ts="2026-01-01T00:00:01Z", session_id="s1", sha256="x")
        log.append("entry-written", ts="2026-01-01T00:00:02Z", session_id="s1", source_id="id1")

        raw_lines = []
        with self._log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.rstrip("\n")
                if stripped.strip():
                    raw_lines.append(stripped)

        self.assertEqual(len(raw_lines), 3)

        # Second record's prev should == SHA-256 of first raw line.
        r1 = json.loads(raw_lines[0])
        r2 = json.loads(raw_lines[1])
        r3 = json.loads(raw_lines[2])

        self.assertEqual(r1["prev"], _GENESIS_PREV)
        self.assertEqual(r2["prev"], _sha256_line(raw_lines[0]))
        self.assertEqual(r3["prev"], _sha256_line(raw_lines[1]))

    def test_verify_chain_valid(self) -> None:
        log = self._make_log()
        log.append("intent", ts="2026-01-01T00:00:00Z", session_id="s1", description="a")
        log.append("ledger-sealed", ts="2026-01-01T00:00:01Z", session_id="s1", sha256="x")
        errors = verify_chain(self._log_path)
        self.assertEqual(errors, [])

    def test_verify_chain_empty_log(self) -> None:
        errors = verify_chain(self._log_path)
        self.assertEqual(errors, [])

    def test_verify_chain_nonexistent_file(self) -> None:
        errors = verify_chain(Path(self._tmp.name) / "does-not-exist.jsonl")
        self.assertEqual(errors, [])

    def test_verify_chain_catches_tampered_middle_line(self) -> None:
        """Tamper with the middle line — verify_chain should report an error."""
        log = self._make_log()
        log.append("intent", ts="2026-01-01T00:00:00Z", session_id="s1", description="a")
        log.append("ledger-sealed", ts="2026-01-01T00:00:01Z", session_id="s1", sha256="x")
        log.append("entry-written", ts="2026-01-01T00:00:02Z", session_id="s1", source_id="id1")

        # Read lines, tamper with middle line (index 1).
        lines = self._log_path.read_text(encoding="utf-8").splitlines(keepends=True)
        self.assertEqual(len(lines), 3)

        parsed = json.loads(lines[1].rstrip("\n"))
        parsed["sha256"] = "TAMPERED"
        lines[1] = json.dumps(parsed, separators=(",", ":"), sort_keys=True) + "\n"
        self._log_path.write_text("".join(lines), encoding="utf-8")

        errors = verify_chain(self._log_path)
        # Should report at least one error (broken chain on line 3).
        self.assertGreater(len(errors), 0)
        # The third line's prev should not match the tampered second line.
        self.assertTrue(any("chain broken" in e for e in errors))

    def test_last_sealed_skips_trailing_acknowledged(self) -> None:
        """last_sealed() returns the seal even when acknowledged records follow it."""
        log = self._make_log()
        log.append("intent", ts="2026-01-01T00:00:00Z", session_id="s1", description="a")
        log.append("ledger-sealed", ts="2026-01-01T00:00:01Z", session_id="s1", sha256="deadbeef")
        log.append("acknowledged", ts="2026-01-01T00:00:02Z", session_id="s1", diff_sha256="abc")
        log.append("ratified", ts="2026-01-01T00:00:03Z", session_id="s1", commit_hash="cafecafe", author="a@b.com", diff_sha256="abc")

        sealed = log.last_sealed()
        self.assertIsNotNone(sealed)
        self.assertEqual(sealed["type"], "ledger-sealed")
        self.assertEqual(sealed["sha256"], "deadbeef")

    def test_last_sealed_none_when_no_seal(self) -> None:
        log = self._make_log()
        log.append("intent", ts="2026-01-01T00:00:00Z", session_id="s1", description="a")
        self.assertIsNone(log.last_sealed())

    def test_has_seal_for_intent_true(self) -> None:
        log = self._make_log()
        log.append("intent", ts="2026-01-01T00:00:00Z", session_id="s1", description="a")
        intent = log.last_intent()
        log.append("ledger-sealed", ts="2026-01-01T00:00:01Z", session_id="s1", sha256="x")
        self.assertTrue(log.has_seal_for_intent(intent))

    def test_has_seal_for_intent_false(self) -> None:
        log = self._make_log()
        log.append("intent", ts="2026-01-01T00:00:00Z", session_id="s1", description="a")
        intent = log.last_intent()
        # No seal appended yet.
        self.assertFalse(log.has_seal_for_intent(intent))

    def test_all_record_types_survive_round_trip(self) -> None:
        log = self._make_log()
        types_and_fields = [
            ("intent", {"session_id": "s1", "description": "write"}),
            ("ledger-sealed", {"session_id": "s1", "sha256": "abc123"}),
            ("entry-written", {"session_id": "s1", "source_id": "txn-1"}),
            ("pending-staged", {"session_id": "s1", "source_id": "txn-2"}),
            ("pending-dropped", {"session_id": "s1", "source_id": "txn-2", "reason": "purge"}),
            ("acknowledged", {"session_id": "s1", "diff_sha256": "def456"}),
            ("ratified", {"session_id": "s1", "commit_hash": "abc", "author": "x@y.com", "diff_sha256": "def456"}),
        ]
        ts = "2026-01-01T00:00:00Z"
        for rec_type, fields in types_and_fields:
            log.append(rec_type, ts=ts, **fields)

        records = log.all_records()
        self.assertEqual(len(records), len(types_and_fields))
        for (rec_type, _), record in zip(types_and_fields, records):
            self.assertEqual(record["type"], rec_type)

        errors = verify_chain(self._log_path)
        self.assertEqual(errors, [])

    def test_tail_limits_count(self) -> None:
        log = self._make_log()
        for i in range(5):
            log.append("entry-written", ts="2026-01-01T00:00:00Z", session_id="s", source_id=f"id{i}")
        self.assertEqual(len(log.tail(3)), 3)
        self.assertEqual(len(log.tail(10)), 5)

    def test_reload_continues_chain(self) -> None:
        """Reloading the AuditLog from the same path should continue the chain."""
        log1 = self._make_log()
        log1.append("intent", ts="2026-01-01T00:00:00Z", session_id="s1", description="first")

        log2 = AuditLog(self._log_path)  # Reload
        log2.append("ledger-sealed", ts="2026-01-01T00:00:01Z", session_id="s1", sha256="y")

        errors = verify_chain(self._log_path)
        self.assertEqual(errors, [])

    def test_verify_chain_catches_corrupted_json(self) -> None:
        """A non-JSON line should be reported as an error."""
        log = self._make_log()
        log.append("intent", ts="2026-01-01T00:00:00Z", session_id="s1", description="a")

        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write("NOT VALID JSON\n")

        errors = verify_chain(self._log_path)
        self.assertGreater(len(errors), 0)
        self.assertTrue(any("invalid JSON" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
