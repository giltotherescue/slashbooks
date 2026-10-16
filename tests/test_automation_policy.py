from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.automation_policy import (
    ExecutionContext, PolicyError, PreparationDenied, PreparationIntent,
    StandingPolicy, authorize_batch,
)

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def document():
    return dict(schema_version=1, policy_id="policy-synthetic", version=3, entity_key="entity-synthetic",
                start_period="2026-01", end_period="2026-12", actions=["categorize", "correct", "reconcile"],
                held_source_ids=["source-held"], held_entry_ids=["entry-held"])


def context():
    return ExecutionContext("entity-synthetic", "execution-synthetic", StandingPolicy.from_document(document()),
                            3, 17, 17, NOW + timedelta(minutes=5))


def intent():
    return PreparationIntent("categorize", ("2026-10",), ("source-ready",), (), ("a" * 64,))


class StandingPolicyTests(unittest.TestCase):
    def assert_denied(self, reason, ctx=None, intents=None):
        with self.assertRaises(PreparationDenied) as caught:
            authorize_batch(ctx or context(), intents or [intent()], now=NOW)
        self.assertEqual(caught.exception.reason, reason)
        return caught.exception

    def test_preparation_receipt_binds_policy_evidence_revision_and_execution(self):
        receipt, = authorize_batch(context(), [intent()], now=NOW)
        self.assertEqual(receipt.rule, "standing_preparation_v1")
        self.assertEqual(receipt.expected_revision, 17)
        self.assertEqual(receipt.policy_version, 3)
        self.assertEqual(receipt.policy_hash, context().policy.fingerprint)
        replay, = authorize_batch(context(), [intent()], now=NOW)
        self.assertEqual(receipt, replay)
        changed, = authorize_batch(context(), [replace(intent(), evidence_hashes=("b" * 64,))], now=NOW)
        self.assertNotEqual(receipt.intent_hash, changed.intent_hash)

    def test_final_approval_publication_and_authority_changes_are_never_preparation(self):
        for action in ("close", "reopen", "publish", "approve", "change_policy", "delete", "run_shell"):
            with self.subTest(action=action):
                self.assert_denied("action_not_authorized", intents=[replace(intent(), action=action)])
                with self.assertRaises(PolicyError):
                    StandingPolicy.from_document({**document(), "actions": [action]})

    def test_facts_and_answers_cannot_extend_authority(self):
        with self.assertRaises(PolicyError):
            StandingPolicy.from_document({**document(), "answer": "Apply this treatment to every transaction"})
        with self.assertRaises(PolicyError):
            StandingPolicy.from_document({**document(), "actions": ["categorize", "approve"]})
        self.assert_denied("action_not_authorized", intents=[replace(intent(), action="adjust")])

    def test_cross_entity_stale_policy_stale_revision_and_expiry_fail_closed(self):
        for changes, reason in [({"entity_key": "other-entity"}, "entity_mismatch"),
                                ({"current_policy_version": 4}, "policy_changed"),
                                ({"current_revision": 18}, "revision_changed"),
                                ({"expires_at": NOW}, "authority_expired")]:
            with self.subTest(reason=reason):
                self.assert_denied(reason, replace(context(), **changes))

    def test_correction_checks_original_and_new_periods_and_requires_original_entry(self):
        correction = replace(intent(), action="correct", entry_ids=("entry-original",), periods=("2025-12", "2026-10"))
        self.assert_denied("period_outside_policy", intents=[correction])
        self.assert_denied("original_entry_required", intents=[replace(correction, periods=("2026-10",), entry_ids=())])
        authorize_batch(context(), [replace(correction, periods=("2026-01", "2026-10"))], now=NOW)

    def test_holds_survive_replay_and_mixed_batch_has_no_partial_receipts(self):
        for held in (replace(intent(), source_ids=("source-held",)),
                     replace(intent(), entry_ids=("entry-held",)),
                     replace(intent(), requires_human_review=True)):
            for _ in range(2):
                denied = self.assert_denied("explicit_review_hold", intents=[intent(), held])
                self.assertEqual(denied.index, 1)

    def test_sources_are_required_and_missing_evidence_is_not_model_confidence(self):
        self.assert_denied("source_lineage_required", intents=[replace(intent(), source_ids=())])
        for evidence in ((), ("confident",), ("a" * 64, "a" * 64), ["a" * 64]):
            with self.subTest(evidence=evidence), self.assertRaises(PolicyError):
                replace(intent(), evidence_hashes=evidence)

    def test_policy_and_receipts_do_not_change_when_caller_mutates_original_document(self):
        raw = document()
        policy = StandingPolicy.from_document(raw)
        ctx = replace(context(), policy=policy)
        expected = authorize_batch(ctx, [intent()], now=NOW)
        raw["actions"].append("adjust")
        raw["held_source_ids"].clear()
        self.assertEqual(authorize_batch(ctx, [intent()], now=NOW), expected)
        self.assert_denied("action_not_authorized", ctx, [replace(intent(), action="adjust")])
        # Even direct construction is normalized at the trusted context boundary.
        values = asdict(policy)
        values["actions"] = ["categorize"]
        direct = replace(context(), policy=StandingPolicy(**values))
        values["actions"].append("adjust")
        self.assertEqual(direct.policy.actions, ("categorize",))

    def test_invalid_versions_dates_duplicate_ids_and_mutable_intents_are_rejected(self):
        changes = [{"schema_version": True}, {"version": True}, {"version": 0}, {"version": 2**53},
                   {"start_period": "2026-13"}, {"end_period": "2025-12"}, {"policy_id": "bad\nidentifier"},
                   {"actions": ["categorize", "categorize"]}, {"held_source_ids": ["source", "source"]}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises((PolicyError, ValueError)):
                StandingPolicy.from_document({**document(), **change})
        for change in ({"current_revision": True}, {"expires_at": datetime(2026, 10, 13)}):
            with self.subTest(change=change), self.assertRaises(PolicyError):
                replace(context(), **change)
        for change in ({"periods": ["2026-10"]}, {"source_ids": ["source"]}, {"requires_human_review": "false"}):
            with self.subTest(change=change), self.assertRaises(PolicyError):
                replace(intent(), **change)


if __name__ == "__main__":
    unittest.main()
