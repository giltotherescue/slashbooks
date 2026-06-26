from __future__ import annotations

"""Tests for `books ingest` (src/bookkeeping/ingest.py) and the importer's
pending-categorization dedup behavior it relies on.

Covers:
  - A connector's normalized JSON (array form and {"transactions": [...]} form)
    is ingested and routed to the review worklist.
  - Re-ingesting the same file is idempotent (no duplicate worklist items).
  - The importer now counts an already-queued repeat as a duplicate.
  - Malformed input is rejected with a clear error.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping import ingest as ingest_module  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.entity import Entity  # noqa: E402
from bookkeeping.ledger.importer import import_transactions  # noqa: E402


def _make_entity(tmp_dir: Path) -> Path:
    entity_dir = tmp_dir / "co"
    entity_dir.mkdir(parents=True, exist_ok=True)
    (entity_dir / "staging").mkdir(exist_ok=True)
    (entity_dir / "reports").mkdir(exist_ok=True)
    (entity_dir / "entity.json").write_text(
        json.dumps({"name": "Test Co", "business_type": "consulting"}) + "\n",
        encoding="utf-8",
    )
    (entity_dir / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n",
        encoding="utf-8",
    )
    return entity_dir


def _txns() -> list[dict]:
    return [
        {
            "id": "x1", "date": "2026-01-03", "description": "AWS",
            "amount": "-340.12", "accountId": "acct_chk",
            "accountName": "Checking", "accountType": "checking", "pending": False,
        },
        {
            "id": "x2", "date": "2026-01-07", "description": "Stripe Payout",
            "amount": "5120.00", "accountId": "acct_chk",
            "accountName": "Checking", "accountType": "checking", "pending": False,
        },
    ]


def _pending(entity_dir: Path) -> list[dict]:
    path = entity_dir / "staging" / "pending-categorization.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


class IngestCommandTests(unittest.TestCase):
    def test_array_input_routes_to_review_worklist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = _make_entity(Path(tmp))
            infile = Path(tmp) / "conn.json"
            infile.write_text(json.dumps(_txns()), encoding="utf-8")

            rc = main(["ingest", str(infile), "--entity", str(entity_dir), "--source", "test"])

            self.assertEqual(rc, 0)
            self.assertEqual(len(_pending(entity_dir)), 2)

    def test_transactions_object_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = _make_entity(Path(tmp))
            infile = Path(tmp) / "conn.json"
            infile.write_text(json.dumps({"source": "x", "transactions": _txns()}), encoding="utf-8")

            rc = main(["ingest", str(infile), "--entity", str(entity_dir), "--source", "test"])

            self.assertEqual(rc, 0)
            self.assertEqual(len(_pending(entity_dir)), 2)

    def test_reingest_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = _make_entity(Path(tmp))
            infile = Path(tmp) / "conn.json"
            infile.write_text(json.dumps(_txns()), encoding="utf-8")

            self.assertEqual(main(["ingest", str(infile), "--entity", str(entity_dir)]), 0)
            self.assertEqual(main(["ingest", str(infile), "--entity", str(entity_dir)]), 0)

            # No duplicate worklist items after the second run.
            self.assertEqual(len(_pending(entity_dir)), 2)

    def test_bad_input_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"nope": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                ingest_module.load_transactions(bad)


class ImporterPendingDedupTests(unittest.TestCase):
    def test_already_queued_repeat_counts_as_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = _make_entity(Path(tmp))
            entity = Entity(
                path=entity_dir,
                entity_config={"name": "Test Co", "business_type": "consulting"},
                trust_policy={"auto_post_threshold": 3, "queue_all_until_confirmed": True},
            )
            queue_categorizer = lambda txn: ("", "queue")  # noqa: E731 - route all to review

            first = import_transactions(entity, _txns(), session_id="s1", categorizer=queue_categorizer, ts="2026-01-15T00:00:00Z")
            self.assertEqual(first.pending_categorization, 2)
            self.assertEqual(first.skipped_duplicate, 0)

            second = import_transactions(entity, _txns(), session_id="s2", categorizer=queue_categorizer, ts="2026-01-15T00:00:00Z")
            self.assertEqual(second.pending_categorization, 0)
            self.assertEqual(second.skipped_duplicate, 2)
            self.assertEqual(len(_pending(entity_dir)), 2)


if __name__ == "__main__":
    unittest.main()
