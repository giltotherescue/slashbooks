from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bookkeeping import queue
from bookkeeping.automation_policy import ExecutionContext, PreparationDenied, StandingPolicy
from bookkeeping.ledger import importer
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.preparation_runtime import evidence_hash, is_preparation, run_preparation_command
from test_queue import _add_pending_categorization, _make_entity


class PreparationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.entity = _make_entity(Path(self.temp.name))
        self.store = LedgerStore(self.entity.path / "ledger.sqlite")
        self.policy = StandingPolicy.from_document(dict(schema_version=1, policy_id="synthetic-policy", version=1,
            entity_key="synthetic-company", start_period="2026-01", end_period="2026-12",
            actions=["categorize", "split", "match_transfer"], held_source_ids=[], held_entry_ids=[]))

    def authority(self):
        return ExecutionContext("synthetic-company", "synthetic-execution", self.policy, 1, 4, 4,
                                datetime.now(timezone.utc) + timedelta(minutes=5))

    def prepare(self):
        _add_pending_categorization(self.entity, "source-a", "Synthetic software", amount="-100.00")
        queue.propose(self.entity, "source-a", "Expenses:Software", "Synthetic evidence")

    def execute(self, item="source-a", command="confirm", **overrides):
        sources = json.loads((self.entity.staging_dir / "pending-categorization.json").read_text())
        kwargs = dict(entity_path=self.entity.path, expected_ledger_digest=self.store.content_digest(),
                      source_hashes={source["id"]: evidence_hash(source) for source in sources})
        kwargs.update(overrides)
        with redirect_stdout(StringIO()):
            return run_preparation_command(["queue", command, "--entity", str(self.entity.path), "--item", item],
                                           self.authority(), **kwargs)

    def test_normal_confirmation_records_atomic_authority_without_learning_human_trust(self):
        self.prepare()
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.store.counts().entries, 1)
        audit = [row for row in self.store.load_audit_events() if row["type"] == "automation-preparation"]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["payload"]["receipt"]["policy_hash"], self.policy.fingerprint)
        self.assertEqual(audit[0]["payload"]["intent"]["source_ids"], ["source-a"])
        self.assertEqual(queue.load_learned_context(self.entity), {})
        self.assertEqual(self.store.verify_audit_chain(), [])
        self.assertFalse(is_preparation())

    def test_explicit_hold_leaves_ledger_queue_and_source_untouched_then_human_can_confirm(self):
        self.prepare()
        self.policy = replace(self.policy, held_source_ids=("source-a",))
        before = self.store.content_digest()
        with self.assertRaisesRegex(PreparationDenied, "explicit_review_hold"):
            self.execute()
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(queue._load_item(self.entity, "source-a")["status"], "open")
        self.assertFalse(is_preparation())
        queue.confirm(self.entity, "source-a", "human-review")
        self.assertEqual(next(iter(queue.load_learned_context(self.entity).values()))["confirmed_count"], 1)

    def test_changed_evidence_and_ledger_are_rejected_at_commit(self):
        self.prepare()
        for kwargs, reason in [({"source_hashes": {"source-a": "0" * 64}}, "source_evidence_changed"),
                               ({"expected_ledger_digest": "0" * 64}, "ledger_changed")]:
            with self.subTest(reason=reason), self.assertRaisesRegex(PreparationDenied, reason):
                self.execute(**kwargs)
            self.assertEqual(self.store.counts().entries, 0)
            self.assertEqual(self.store.counts().audit_events, 0)

    def test_missing_staged_source_cannot_use_the_queue_minimal_transaction_fallback(self):
        self.prepare()
        source = queue._get_pending_txn(self.entity, "source-a")
        original_run = queue.run
        def remove_source(args):
            (self.entity.staging_dir / "pending-categorization.json").write_text("[]")
            return original_run(args)
        with patch.object(queue, "run", side_effect=remove_source), self.assertRaisesRegex(PreparationDenied, "source_evidence_changed"):
            self.execute(source_hashes={"source-a": evidence_hash(source)})
        self.assertEqual(self.store.counts().entries, 0)

    def test_proposal_change_between_dispatch_and_write_is_rejected(self):
        self.prepare()
        original = importer._atomic_ledger_write
        def concurrent(*args, **kwargs):
            item = queue._load_item(self.entity, "source-a")
            item["requires_human_review"] = True
            queue._save_item(self.entity, item)
            return original(*args, **kwargs)
        with patch.object(importer, "_atomic_ledger_write", side_effect=concurrent), self.assertRaisesRegex(PreparationDenied, "proposal_changed"):
            self.execute()
        self.assertEqual(self.store.counts().entries, 0)

    def test_late_failure_rolls_back_authority_receipt_together_with_entries(self):
        self.prepare()
        with patch.object(LedgerStore, "insert_entries", side_effect=RuntimeError("synthetic write failure")), self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(self.store.counts().entries, 0)
        self.assertEqual(self.store.counts().audit_events, 0)
        self.assertEqual(queue._load_item(self.entity, "source-a")["status"], "open")
        self.assertEqual(self.execute(), 0)

    def test_split_uses_split_authority_and_cannot_replay_a_posted_source(self):
        _add_pending_categorization(self.entity, "source-a", "Synthetic allocation", amount="-100.00")
        queue.propose_split(self.entity, "source-a", "Evidence", ["Expenses:Software=70.00", "Liabilities:CreditCard=30.00"])
        self.assertEqual(self.execute(), 0)
        audit = [row for row in self.store.load_audit_events() if row["type"] == "automation-preparation"]
        self.assertEqual(audit[0]["payload"]["intent"]["action"], "split")
        with self.assertRaisesRegex(PreparationDenied, "posted_source_requires_reconciliation"):
            self.execute(source_hashes={"source-a": "a" * 64})
        self.assertEqual(self.store.counts().entries, 1)

    def test_transfer_checks_both_sources_and_does_not_partially_post_a_held_pair(self):
        _add_pending_categorization(self.entity, "bank-payment", "Card payment", amount="-125.00", txn_date="2026-02-01")
        _add_pending_categorization(self.entity, "card-payment", "Payment received", amount="125.00", txn_date="2026-02-03")
        pending = self.entity.staging_dir / "pending-categorization.json"
        rows = json.loads(pending.read_text())
        rows[1].update(accountName="Amex", accountType="credit_card")
        pending.write_text(json.dumps(rows))
        item = queue.propose_transfer(self.entity, ["bank-payment", "card-payment"], "Matching evidence")
        self.policy = replace(self.policy, held_source_ids=("card-payment",))
        with self.assertRaisesRegex(PreparationDenied, "explicit_review_hold"):
            self.execute(item=item["source_id"])
        self.assertEqual(self.store.counts().entries, 0)
        self.policy = replace(self.policy, held_source_ids=())
        self.assertEqual(self.execute(item=item["source_id"]), 0)
        self.assertEqual(self.store.counts().entries, 2)
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM source_transactions").fetchone()[0], 2)

    def test_unsupported_batch_and_authority_commands_fail_before_writes(self):
        self.prepare()
        for argv in (["queue", "confirm-group", "--entity", str(self.entity.path), "--category", "Expenses:Software"],
                     ["entity", "init", str(self.entity.path)]):
            with self.assertRaises(PreparationDenied):
                run_preparation_command(argv, self.authority(), entity_path=self.entity.path,
                    expected_ledger_digest=self.store.content_digest(), source_hashes={"source-a": "a" * 64})
        self.assertEqual(self.store.counts().entries, 0)


if __name__ == "__main__":
    unittest.main()
