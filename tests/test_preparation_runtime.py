from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bookkeeping import queue, reconcile
from bookkeeping.automation_policy import ExecutionContext, PreparationDenied, StandingPolicy
from bookkeeping.ledger import importer
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.preparation_runtime import (
    evidence_hash, is_preparation, run_preparation_command, supported_preparation_command,
)
from test_queue import _add_pending_categorization, _make_entity


class PreparationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.entity = _make_entity(Path(self.temp.name))
        self.store = LedgerStore(self.entity.path / "ledger.sqlite")
        self.policy = StandingPolicy.from_document(dict(schema_version=1, policy_id="synthetic-policy", version=1,
            entity_key="synthetic-company", start_period="2026-01", end_period="2026-12",
            actions=["categorize", "split", "match_transfer", "correct"], held_source_ids=[], held_entry_ids=[]))

    def authority(self):
        return ExecutionContext("synthetic-company", "synthetic-execution", self.policy, 1, 4, 4,
                                datetime.now(timezone.utc) + timedelta(minutes=5))

    def test_positive_command_classifier_separates_reads_and_effects(self):
        cases = [
            (["readiness", "--entity", str(self.entity.path), "--period", "2026-09", "--as-of", "2026-10-01"], "read", None),
            (["queue", "propose", "--entity", str(self.entity.path), "--source-id", "x", "--category", "Expenses:Software", "--reasoning", "x"], "proposal", "categorize"),
            (["ingest", str(self.entity.path / "input.json"), "--entity", str(self.entity.path)], "ledger", "import"),
            (["queue", "confirm", "--entity", str(self.entity.path), "--item", "x"], "ledger", None),
            (["reconcile", "--entity", str(self.entity.path), "--account", "Assets:Bank:Checking", "--source-balance", "0", "--as-of", "2026-09-30"], "reconciliation", "reconcile"),
        ]
        for argv, effect, action in cases:
            with self.subTest(argv=argv):
                spec = supported_preparation_command(argv)
                self.assertEqual((spec.effect, spec.action), (effect, action))
        for argv in (
            ["queue", "confirm-group", "--entity", str(self.entity.path), "--category", "Expenses:Software"],
            ["queue", "resolve-duplicate", "--entity", str(self.entity.path), "--source-id", "x", "--decision", "duplicate"],
            ["entity", "source-coverage", str(self.entity.path), "--source", "x", "--from", "2026-09-01", "--to", "2026-09-30"],
        ):
            with self.subTest(rejected=argv), self.assertRaisesRegex(PreparationDenied, "command_not_supported"):
                supported_preparation_command(argv)

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

    def test_preparation_can_import_verified_input_through_the_ordinary_dispatcher(self):
        self.policy = replace(self.policy, actions=tuple(sorted((*self.policy.actions, "import"))))
        queue._save_learned_context(self.entity, {
            "SYNTHETIC VENDOR": {
                "canonical_category": "Expenses:Software", "confirmed_count": 3,
                "last_confirmed_date": "2026-09-01", "reset": False, "notes": "",
            }
        })
        source = {
            "id": "import-source", "date": "2026-09-12", "amount": "-25.00",
            "description": "Synthetic vendor", "accountName": "Checking", "pending": False,
        }
        input_path = self.entity.path / "synthetic-input.json"
        input_path.write_text(json.dumps([source]))
        with redirect_stdout(StringIO()):
            result = run_preparation_command(
                ["ingest", str(input_path), "--entity", str(self.entity.path),
                 "--source", "synthetic", "--session-id", "synthetic-import"],
                self.authority(), entity_path=self.entity.path,
                expected_ledger_digest=self.store.content_digest(),
                source_hashes={"import-source": evidence_hash(source)},
            )
        self.assertEqual(result, 0)
        self.assertTrue(self.store.source_exists("import-source"))
        audit = [row for row in self.store.load_audit_events() if row["type"] == "automation-preparation"]
        self.assertEqual(audit[-1]["payload"]["intent"]["action"], "import")

    def test_preparation_can_save_verified_proposals_without_posting(self):
        _add_pending_categorization(self.entity, "proposal-source", "Synthetic proposal", txn_date="2026-09-12")
        source = queue._get_pending_txn(self.entity, "proposal-source")
        before = self.store.content_digest()
        with redirect_stdout(StringIO()):
            result = run_preparation_command(
                ["queue", "propose", "--entity", str(self.entity.path),
                 "--source-id", "proposal-source", "--category", "Expenses:Software",
                 "--reasoning", "Synthetic evidence"],
                self.authority(), entity_path=self.entity.path,
                expected_ledger_digest=before,
                source_hashes={"proposal-source": evidence_hash(source)},
            )
        self.assertEqual(result, 0)
        self.assertEqual(queue._load_item(self.entity, "proposal-source")["status"], "open")
        self.assertEqual(self.store.content_digest(), before)

    def test_preparation_reconciliation_requires_affirmative_verified_evidence(self):
        self.policy = replace(self.policy, actions=tuple(sorted((*self.policy.actions, "reconcile"))))
        evidence = {
            "account": "Assets:Bank:Checking", "as_of": "2026-09-30",
            "source_balance": "0.00", "source_opening_balance": "0.00",
            "source_transaction_total": "0.00", "source_snapshot_at": "2026-10-01T12:00:00Z",
            "source_through": "2026-09-30",
        }
        argv = [
            "reconcile", "--entity", str(self.entity.path), "--account", evidence["account"],
            "--source-balance", evidence["source_balance"], "--as-of", evidence["as_of"],
            "--source-opening-balance", evidence["source_opening_balance"],
            "--source-transaction-total", evidence["source_transaction_total"],
            "--source-snapshot-at", evidence["source_snapshot_at"],
            "--source-through", evidence["source_through"],
            "--source-evidence-ref", "provider-record:statement-2026-09",
            "--source-evidence-hash", "provider-record:statement-2026-09=" + "b" * 64,
        ]
        source_id = "reconciliation:Assets:Bank:Checking:2026-09-30"
        source_hashes = {
            source_id: evidence_hash(evidence),
            "evidence:provider-record:statement-2026-09": evidence_hash({"verified": "statement-2026-09"}),
        }
        with redirect_stdout(StringIO()):
            result = run_preparation_command(
                argv, self.authority(), entity_path=self.entity.path,
                expected_ledger_digest=self.store.content_digest(),
                source_hashes=source_hashes,
            )
        self.assertEqual(result, 0)
        record = reconcile.list_discrepancies(self.entity.path)[0]
        self.assertEqual(record["source_opening_balance"], "0.00")
        self.assertEqual(record["source_transaction_total"], "0.00")
        self.assertEqual(record["source_evidence_refs"], ["provider-record:statement-2026-09"])
        self.assertEqual(record["source_evidence_hashes"], {
            "provider-record:statement-2026-09": source_hashes["evidence:provider-record:statement-2026-09"],
        })

        incomplete = list(argv)
        index = incomplete.index("--source-opening-balance")
        del incomplete[index:index + 2]
        with self.assertRaisesRegex(PreparationDenied, "affirmative_reconciliation_evidence_required"):
            run_preparation_command(
                incomplete, self.authority(), entity_path=self.entity.path,
                expected_ledger_digest=self.store.content_digest(),
                source_hashes=source_hashes,
            )
        changed = list(argv)
        changed[changed.index("0.00")] = "1.00"
        with self.assertRaisesRegex(PreparationDenied, "source_evidence_changed"):
            run_preparation_command(
                changed, self.authority(), entity_path=self.entity.path,
                expected_ledger_digest=self.store.content_digest(), source_hashes=source_hashes,
            )
        with self.assertRaisesRegex(PreparationDenied, "source_evidence_changed"):
            run_preparation_command(
                argv, self.authority(), entity_path=self.entity.path,
                expected_ledger_digest=self.store.content_digest(),
                source_hashes={source_id: evidence_hash(evidence)},
            )

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

    def entry_hashes(self):
        with self.store.connection() as conn:
            ids = {row["source_id"]: f"entry:{row['id']}" for row in conn.execute("SELECT id, source_id FROM entries")}
        return {ids[entry.source_id]: evidence_hash(asdict(entry)) for entry in self.store.load_entries() if entry.source_id}

    def correction(self, original="source-a", replacement="source-a-corrected", correction_date="2026-02-15", **overrides):
        kwargs = dict(entity_path=self.entity.path, expected_ledger_digest=self.store.content_digest(),
                      source_hashes={}, entry_hashes=self.entry_hashes())
        kwargs.update(overrides)
        argv = ["ledger", "correct", "--entity", str(self.entity.path), "--source-id", original,
                "--new-source-id", replacement, "--date", correction_date, "--description", "Synthetic correction",
                "--posting", "Assets:Bank:Checking=-100", "--posting", "Expenses:Software=100"]
        with redirect_stdout(StringIO()):
            return run_preparation_command(argv, self.authority(), **kwargs)

    def posted(self):
        self.prepare()
        self.assertEqual(self.execute(), 0)

    def test_correction_retains_original_and_receipt_with_reversal_and_replacement(self):
        self.posted()
        original = self.store.load_entries()[0]
        self.assertEqual(self.correction(), 0)
        entries = self.store.load_entries()
        self.assertIn(original, entries)
        self.assertEqual(len(entries), 3)
        self.assertEqual(sum(dict(entry.meta).get("reverses") == "source-a" for entry in entries), 1)
        audit = [row for row in self.store.load_audit_events() if row["type"] == "automation-preparation"]
        intent = audit[-1]["payload"]["intent"]
        self.assertEqual(intent["action"], "correct")
        self.assertEqual(intent["entry_ids"], ["entry:1"])
        self.assertEqual(intent["source_ids"], ["source-a", "source-a-corrected"])
        self.assertEqual(self.store.verify_audit_chain(), [])
        self.assertFalse(is_preparation())
        self.assertEqual(self.correction(), 1)  # Ordinary command reports already-used replacement.
        self.assertEqual(len(self.store.load_entries()), 3)

    def test_correction_enforces_original_and_replacement_periods_and_explicit_holds(self):
        self.posted()
        original_period = self.store.load_entries()[0].date.isoformat()[:7]
        before = self.store.content_digest()
        initial = self.policy
        cases = [
            (replace(initial, held_entry_ids=("entry:1",)), "2026-02-15", "explicit_review_hold"),
            (replace(initial, held_source_ids=("source-a",)), "2026-02-15", "explicit_review_hold"),
            (replace(initial, held_source_ids=("source-a-corrected",)), "2026-02-15", "explicit_review_hold"),
            (replace(initial, start_period=original_period, end_period=original_period), "2026-12-15", "period_outside_policy"),
            (replace(initial, start_period="2026-12"), "2026-12-15", "period_outside_policy"),
            (replace(initial, actions=("categorize",)), "2026-02-15", "action_not_authorized"),
        ]
        for policy, correction_date, reason in cases:
            self.policy = policy
            with self.subTest(reason=reason, policy=policy), self.assertRaisesRegex(PreparationDenied, reason):
                self.correction(correction_date=correction_date)
            self.assertEqual(self.store.content_digest(), before)
        self.assertFalse(is_preparation())

    def test_correction_rechecks_entry_evidence_and_ledger_at_write(self):
        self.posted()
        before = self.store.content_digest()
        for kwargs, reason in [({"entry_hashes": {"entry:1": "0" * 64}}, "entry_evidence_changed"),
                               ({"expected_ledger_digest": "0" * 64}, "ledger_changed")]:
            with self.subTest(reason=reason), self.assertRaisesRegex(PreparationDenied, reason):
                self.correction(**kwargs)
            self.assertEqual(self.store.content_digest(), before)

    def test_correction_ancestor_holds_and_evidence_cannot_be_bypassed_with_new_ids(self):
        self.posted()
        self.assertEqual(self.correction(), 0)
        self.policy = replace(self.policy, held_entry_ids=("entry:1",))
        with self.assertRaisesRegex(PreparationDenied, "explicit_review_hold"):
            self.correction("source-a-corrected", "source-a-second")
        self.policy = replace(self.policy, held_entry_ids=(), held_source_ids=("source-a",))
        with self.assertRaisesRegex(PreparationDenied, "explicit_review_hold"):
            self.correction("source-a-corrected", "source-a-second")
        self.policy = replace(self.policy, held_source_ids=())
        hashes = self.entry_hashes()
        del hashes["entry:1"]
        with self.assertRaisesRegex(PreparationDenied, "entry_evidence_changed"):
            self.correction("source-a-corrected", "source-a-second", entry_hashes=hashes)
        self.assertEqual(self.correction("source-a-corrected", "source-a-second"), 0)
        intent = [row for row in self.store.load_audit_events() if row["type"] == "automation-preparation"][-1]["payload"]["intent"]
        self.assertEqual(intent["entry_ids"], ["entry:1", "entry:3"])
        self.assertEqual(intent["source_ids"], ["source-a", "source-a-corrected", "source-a-second"])

    def test_correction_late_failure_rolls_back_both_entries_and_receipt(self):
        self.posted()
        before = self.store.content_digest()
        audit = self.store.load_audit_events()
        with patch.object(LedgerStore, "insert_entries", side_effect=RuntimeError("synthetic failure")), self.assertRaises(RuntimeError):
            self.correction()
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(self.store.load_audit_events(), audit)
        self.assertEqual(self.correction(), 0)

    def test_tampered_reversal_cannot_be_written_under_correction_authority(self):
        self.posted()
        original = importer._atomic_ledger_write
        def tamper(*args, **kwargs):
            kwargs["new_entries"][0] = replace(kwargs["new_entries"][0], date=kwargs["new_entries"][1].date)
            return original(*args, **kwargs)
        with patch.object(importer, "_atomic_ledger_write", side_effect=tamper), self.assertRaisesRegex(PreparationDenied, "correction_lineage_changed"):
            self.correction(correction_date="2026-12-15")
        self.assertEqual(self.store.counts().entries, 1)

    def test_bank_reclassification_uses_same_correction_boundary(self):
        self.posted()
        original = self.store.load_entries()[0]
        bank = next(posting.account for posting in original.postings if posting.account.startswith("Assets:"))
        argv = ["ledger", "reclassify-bank", "--entity", str(self.entity.path), "--source-id", "source-a",
                "--new-source-id", "source-a-reclassified", "--from-account", bank, "--to-account", "Assets:Bank:Second"]
        with redirect_stdout(StringIO()):
            result = run_preparation_command(argv, self.authority(), entity_path=self.entity.path,
                expected_ledger_digest=self.store.content_digest(), source_hashes={}, entry_hashes=self.entry_hashes())
        self.assertEqual(result, 0)
        replacement = next(entry for entry in self.store.load_entries() if entry.source_id == "source-a-reclassified")
        self.assertIn("Assets:Bank:Second", [posting.account for posting in replacement.postings])

    def test_standalone_reversal_remains_unsupported(self):
        self.posted()
        with self.assertRaisesRegex(PreparationDenied, "command_not_supported"):
            run_preparation_command(["ledger", "reverse", "--entity", str(self.entity.path), "--source-id", "source-a"],
                self.authority(), entity_path=self.entity.path, expected_ledger_digest=self.store.content_digest(),
                source_hashes={}, entry_hashes=self.entry_hashes())
        self.assertEqual(self.store.counts().entries, 1)

    def test_equity_split_keeps_bank_leg_and_applies_original_entry_hold(self):
        _add_pending_categorization(self.entity, "source-a", "Synthetic distribution", amount="-100.00")
        queue.propose(self.entity, "source-a", "Equity:Opening-Balances", "Synthetic evidence")
        self.assertEqual(self.execute(), 0)
        argv = ["ledger", "split-equity", "--entity", str(self.entity.path), "--source-id", "source-a",
                "--new-source-id", "source-a-equity", "--equity-posting", "Equity:Partner-A=60",
                "--equity-posting", "Equity:Partner-B=40"]
        kwargs = dict(entity_path=self.entity.path, expected_ledger_digest=self.store.content_digest(),
                      source_hashes={}, entry_hashes=self.entry_hashes())
        self.policy = replace(self.policy, held_entry_ids=("entry:1",))
        with self.assertRaisesRegex(PreparationDenied, "explicit_review_hold"):
            run_preparation_command(argv, self.authority(), **kwargs)
        self.assertEqual(self.store.counts().entries, 1)
        self.policy = replace(self.policy, held_entry_ids=())
        with redirect_stdout(StringIO()):
            self.assertEqual(run_preparation_command(argv, self.authority(), **kwargs), 0)
        replacement = next(entry for entry in self.store.load_entries() if entry.source_id == "source-a-equity")
        self.assertEqual([(p.account, str(p.amount)) for p in replacement.postings],
                         [("Assets:Bank:Checking", "-100.00"), ("Equity:Partner-A", "60.00"), ("Equity:Partner-B", "40.00")])


if __name__ == "__main__":
    unittest.main()
