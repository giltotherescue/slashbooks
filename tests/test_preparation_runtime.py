from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import os
import select
import subprocess
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

    def authority(self, execution_id="synthetic-execution"):
        return ExecutionContext("synthetic-company", execution_id, self.policy, 1, 4, 4,
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

    def execute(self, item="source-a", command="confirm", session=None, context=None, **overrides):
        sources = json.loads((self.entity.staging_dir / "pending-categorization.json").read_text())
        kwargs = dict(entity_path=self.entity.path, expected_ledger_digest=self.store.content_digest(),
                      source_hashes={source["id"]: evidence_hash(source) for source in sources})
        kwargs.update(overrides)
        argv = ["queue", command, "--entity", str(self.entity.path), "--item", item]
        if session:
            argv.extend(["--session", session])
        with redirect_stdout(StringIO()):
            return run_preparation_command(argv, context or self.authority(), **kwargs)

    def test_normal_confirmation_records_atomic_authority_without_learning_human_trust(self):
        self.prepare()
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.store.counts().entries, 1)
        audit = [row for row in self.store.load_audit_events() if row["type"] == "automation-preparation"]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["payload"]["receipt"]["policy_hash"], self.policy.fingerprint)
        self.assertEqual(audit[0]["payload"]["intent"]["source_ids"], ["source-a"])
        self.assertEqual(queue.load_learned_context(self.entity), {})
        finalization = [row for row in self.store.load_audit_events()
                        if row["type"] == "queue-finalization-intent"]
        self.assertEqual(finalization[0]["payload"]["entity_key"], "synthetic-company")
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

    def test_proposal_rechecks_group_snapshot_and_ledger_under_write_lock(self):
        _add_pending_categorization(self.entity, "proposal-a", "First", txn_date="2026-09-12")
        _add_pending_categorization(self.entity, "proposal-b", "Second", txn_date="2026-09-13")
        pending = self.entity.staging_dir / "pending-categorization.json"
        sources = json.loads(pending.read_text())
        original_run = queue.run

        def mutate_after_preflight(args):
            changed = json.loads(pending.read_text())
            changed[1]["date"] = "2027-01-13"
            pending.write_text(json.dumps(changed))
            return original_run(args)

        argv = ["queue", "propose-group", "--entity", str(self.entity.path),
                "--source-id", "proposal-a", "--source-id", "proposal-b",
                "--category", "Expenses:Software", "--reasoning", "Synthetic evidence"]
        with patch.object(queue, "run", side_effect=mutate_after_preflight), \
                self.assertRaisesRegex(PreparationDenied, "source_evidence_changed"):
            run_preparation_command(
                argv, self.authority(), entity_path=self.entity.path,
                expected_ledger_digest=self.store.content_digest(),
                source_hashes={source["id"]: evidence_hash(source) for source in sources},
            )
        self.assertFalse((self.entity.review_queue_dir / "proposal-a.json").exists())
        self.assertFalse((self.entity.review_queue_dir / "proposal-b.json").exists())

    def test_proposal_rechecks_ledger_revision_at_persistence(self):
        _add_pending_categorization(self.entity, "proposal-ledger", "First", txn_date="2026-09-12")
        source = queue._get_pending_txn(self.entity, "proposal-ledger")
        before = self.store.content_digest()
        original_run = queue.run

        def mutate_ledger_after_preflight(args):
            self.store.set_meta("concurrent-change", "1")
            return original_run(args)

        argv = ["queue", "propose", "--entity", str(self.entity.path),
                "--source-id", "proposal-ledger", "--category", "Expenses:Software",
                "--reasoning", "Synthetic evidence"]
        with patch.object(queue, "run", side_effect=mutate_ledger_after_preflight), \
                self.assertRaisesRegex(PreparationDenied, "ledger_changed"):
            run_preparation_command(
                argv, self.authority(), entity_path=self.entity.path,
                expected_ledger_digest=before,
                source_hashes={"proposal-ledger": evidence_hash(source)},
            )
        self.assertFalse((self.entity.review_queue_dir / "proposal-ledger.json").exists())

    def test_group_proposal_save_failure_is_rolled_forward_on_next_mutation(self):
        for source_id in ("group-save-a", "group-save-b"):
            _add_pending_categorization(self.entity, source_id, source_id, amount="-10.00")
        sources = [queue._get_pending_txn(self.entity, source_id)
                   for source_id in ("group-save-a", "group-save-b")]
        hashes = {source["id"]: evidence_hash(source) for source in sources}
        argv = [
            "queue", "propose-group", "--entity", str(self.entity.path),
            "--source-id", "group-save-a", "--source-id", "group-save-b",
            "--category", "Expenses:Software", "--reasoning", "Evidence",
        ]
        original_replace = queue._replace_group_destination
        saves = 0

        def fail_second_save(path, payload):
            nonlocal saves
            saves += 1
            if saves == 2:
                raise OSError("synthetic grouped write failure")
            return original_replace(path, payload)

        with patch.object(queue, "_replace_group_destination", side_effect=fail_second_save), \
                self.assertRaisesRegex(OSError, "grouped write failure"):
            run_preparation_command(
                argv, self.authority(), entity_path=self.entity.path,
                expected_ledger_digest=self.store.content_digest(), source_hashes=hashes,
            )
        with queue._queue_mutation(self.entity):
            pass
        for source_id in ("group-save-a", "group-save-b"):
            self.assertEqual(queue._load_item(self.entity, source_id)["status"], "open")

    def test_group_proposal_process_death_recovers_a_complete_committed_group(self):
        for source_id in ("crash-group-a", "crash-group-b"):
            _add_pending_categorization(self.entity, source_id, source_id, amount="-10.00")
        sources = [queue._get_pending_txn(self.entity, source_id)
                   for source_id in ("crash-group-a", "crash-group-b")]
        hashes = {source["id"]: evidence_hash(source) for source in sources}
        argv = [
            "queue", "propose-group", "--entity", str(self.entity.path),
            "--source-id", "crash-group-a", "--source-id", "crash-group-b",
            "--category", "Expenses:Software", "--reasoning", "Evidence",
        ]
        pid = os.fork()
        if pid == 0:
            original_replace = queue._replace_group_destination
            replacements = 0

            def exit_after_first(path, payload):
                nonlocal replacements
                original_replace(path, payload)
                replacements += 1
                if replacements == 1:
                    os._exit(73)

            queue._replace_group_destination = exit_after_first
            run_preparation_command(
                argv, self.authority(), entity_path=self.entity.path,
                expected_ledger_digest=self.store.content_digest(), source_hashes=hashes,
            )
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 73)
        journal = self.entity.review_queue_dir / ".transactions"
        before = {str(path.relative_to(self.entity.path)): path.read_bytes()
                  for path in self.entity.path.rglob("*") if path.is_file()}
        read_items = queue.list_queue_items(self.entity)
        self.assertEqual(
            [item["diagnostic"]["code"] for item in read_items if item.get("diagnostic")],
            ["queue_recovery_required"],
        )
        shown = queue.read_queue_item(self.entity, "crash-group-a")
        self.assertEqual(shown["diagnostic"]["code"], "queue_recovery_required")
        after = {str(path.relative_to(self.entity.path)): path.read_bytes()
                 for path in self.entity.path.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertTrue(any(journal.iterdir()))
        with queue._queue_mutation(self.entity):
            pass
        for source_id in ("crash-group-a", "crash-group-b"):
            self.assertEqual(queue._load_item(self.entity, source_id)["status"], "open")
        self.assertEqual(list(journal.iterdir()), [])

    def test_import_append_and_queue_removal_share_one_cross_process_lock(self):
        _add_pending_categorization(self.entity, "remove-me", "Existing", amount="-1.00")
        script = """
import sys
from pathlib import Path
from bookkeeping.entity import load_entity
from bookkeeping.ledger import importer
entity = load_entity(Path(sys.argv[1]))
print('started', flush=True)
importer._add_pending_categorization(entity, {
    'id': 'added-by-importer', 'date': '2026-09-01',
    'description': 'Concurrent', 'amount': '-2.00',
})
print('done', flush=True)
"""
        with importer._entity_write_lock(self.entity.path):
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(self.entity.path)],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "PYTHONPATH": "src:tests"},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(process.stdout.readline().strip(), "started")
            ready, _, _ = select.select([process.stdout], [], [], 0.2)
            self.assertEqual(ready, [])
            queue._remove_from_pending_categorization(self.entity, "remove-me", strict=True)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("done", stdout)
        rows = json.loads((self.entity.staging_dir / "pending-categorization.json").read_text())
        self.assertEqual([row["id"] for row in rows], ["added-by-importer"])

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
        audit = [row for row in self.store.load_audit_events()
                 if row["type"] == "automation-preparation"]
        self.assertEqual(audit[-1]["payload"]["receipt"]["execution_id"], "synthetic-execution")
        self.assertEqual(audit[-1]["payload"]["intent"]["source_ids"], sorted(source_hashes))

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

    def test_confirmation_rejects_extra_verified_sources_outside_the_exact_operation(self):
        self.prepare()
        _add_pending_categorization(self.entity, "unrelated", "Other evidence", amount="-5.00")
        sources = json.loads((self.entity.staging_dir / "pending-categorization.json").read_text())
        hashes = {source["id"]: evidence_hash(source) for source in sources}
        with self.assertRaisesRegex(PreparationDenied, "source_evidence_changed"):
            self.execute(source_hashes=hashes)
        self.assertEqual(self.store.counts().entries, 0)
        self.assertEqual(queue._load_item(self.entity, "source-a")["status"], "open")

    def test_confirmation_rejects_live_source_changed_after_proposal(self):
        self.prepare()
        pending_path = self.entity.staging_dir / "pending-categorization.json"
        pending = json.loads(pending_path.read_text())
        pending[0]["amount"] = "-999.00"
        pending_path.write_text(json.dumps(pending))
        current_hashes = {pending[0]["id"]: evidence_hash(pending[0])}
        with self.assertRaisesRegex(PreparationDenied, "source_evidence_changed"):
            self.execute(source_hashes=current_hashes)
        self.assertEqual(self.store.counts().entries, 0)

    def test_recovery_receipt_rejects_a_different_tenant_identity(self):
        self.prepare()
        source = queue._get_pending_txn(self.entity, "source-a")
        hashes = {"source-a": evidence_hash(source)}
        with patch.object(queue, "_remove_from_pending_categorization", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                self.execute(source_hashes=hashes)
        other_policy = replace(self.policy, entity_key="different-company")
        other_context = ExecutionContext(
            "different-company", "synthetic-execution", other_policy, 1, 4, 4,
            datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        with self.assertRaisesRegex(PreparationDenied, "posted_source_requires_reconciliation"):
            self.execute(source_hashes=hashes, context=other_context)
        self.assertEqual(self.store.counts().entries, 1)

    def test_recovery_receipt_survives_a_same_tenant_workspace_move(self):
        self.prepare()
        source = queue._get_pending_txn(self.entity, "source-a")
        hashes = {"source-a": evidence_hash(source)}
        with patch.object(queue, "_remove_from_pending_categorization", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                self.execute(source_hashes=hashes)
        moved_path = self.entity.path.parent / "moved-entity"
        self.entity.path.rename(moved_path)
        moved_store = LedgerStore(moved_path / "ledger.sqlite")
        with redirect_stdout(StringIO()):
            result = run_preparation_command(
                ["queue", "confirm", "--entity", str(moved_path), "--item", "source-a"],
                self.authority(), entity_path=moved_path,
                expected_ledger_digest=moved_store.content_digest(), source_hashes=hashes,
            )
        self.assertEqual(result, 0)
        self.assertEqual(moved_store.counts().entries, 1)

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

    def test_changed_source_reproposal_cannot_use_standing_confirmation_authority(self):
        self.prepare()
        pending_path = self.entity.staging_dir / "pending-categorization.json"
        pending = json.loads(pending_path.read_text())
        pending[0]["amount"] = "-125.00"
        pending_path.write_text(json.dumps(pending) + "\n")
        queue.reconcile_pending_amount_changes(self.entity)
        item = queue.propose(
            self.entity, "source-a", "Expenses:Software", "Reviewed changed evidence."
        )
        self.assertTrue(item["requires_human_review"])
        with self.assertRaisesRegex(PreparationDenied, "explicit_review_hold"):
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

    def test_exact_receipt_recovers_category_split_and_transfer_cleanup_without_reposting(self):
        self.prepare()
        source_a = queue._get_pending_txn(self.entity, "source-a")
        hashes_a = {"source-a": evidence_hash(source_a)}
        with patch.object(queue, "_remove_from_pending_categorization", side_effect=RuntimeError("after ledger")):
            with self.assertRaisesRegex(RuntimeError, "after ledger"):
                self.execute(source_hashes=hashes_a)
        self.assertEqual(self.store.counts().entries, 1)
        self.assertEqual(self.execute(source_hashes=hashes_a), 0)
        self.assertEqual(self.store.counts().entries, 1)

        _add_pending_categorization(self.entity, "split-recovery", "Allocation", amount="-100.00")
        queue.propose_split(self.entity, "split-recovery", "Evidence", ["Expenses:Software=100.00"])
        split_source = queue._get_pending_txn(self.entity, "split-recovery")
        split_hashes = {"split-recovery": evidence_hash(split_source)}
        with patch.object(queue, "_remove_from_pending_categorization", side_effect=RuntimeError("before pending remove")):
            with self.assertRaisesRegex(RuntimeError, "before pending remove"):
                self.execute(item="split-recovery", command="confirm-split", source_hashes=split_hashes)
        self.assertEqual(self.store.counts().entries, 2)
        self.assertEqual(self.execute(item="split-recovery", command="confirm-split", source_hashes=split_hashes), 0)
        self.assertEqual(self.store.counts().entries, 2)

        _add_pending_categorization(self.entity, "bank-recovery", "Card payment", amount="-125.00", txn_date="2026-02-01")
        _add_pending_categorization(self.entity, "card-recovery", "Payment received", amount="125.00", txn_date="2026-02-03")
        pending = self.entity.staging_dir / "pending-categorization.json"
        rows = json.loads(pending.read_text())
        next(row for row in rows if row["id"] == "card-recovery").update(accountName="Amex", accountType="credit_card")
        pending.write_text(json.dumps(rows))
        transfer = queue.propose_transfer(self.entity, ["bank-recovery", "card-recovery"], "Evidence")
        transfer_sources = [queue._get_pending_txn(self.entity, value) for value in ("bank-recovery", "card-recovery")]
        transfer_hashes = {source["id"]: evidence_hash(source) for source in transfer_sources}
        original_save = queue._save_item

        def fail_final_save(entity, item):
            if item.get("status") == "confirmed":
                raise RuntimeError("before item save")
            return original_save(entity, item)

        with patch.object(queue, "_save_item", side_effect=fail_final_save):
            with self.assertRaisesRegex(RuntimeError, "before item save"):
                self.execute(item=transfer["source_id"], command="confirm-transfer",
                             source_hashes=transfer_hashes)
        self.assertEqual(self.store.counts().entries, 4)
        self.assertEqual(self.execute(item=transfer["source_id"], command="confirm-transfer",
                                     source_hashes=transfer_hashes), 0)
        self.assertEqual(self.store.counts().entries, 4)

    def test_post_commit_recovery_rejects_changed_intent_and_execution_identity(self):
        self.prepare()
        source = queue._get_pending_txn(self.entity, "source-a")
        hashes = {"source-a": evidence_hash(source)}
        with patch.object(queue, "_remove_from_pending_categorization", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                self.execute(session="original-session", source_hashes=hashes)
        with self.assertRaisesRegex(PreparationDenied, "posted_source_requires_reconciliation"):
            self.execute(session="different-session", source_hashes=hashes)
        with self.assertRaisesRegex(PreparationDenied, "posted_source_requires_reconciliation"):
            self.execute(session="original-session", source_hashes=hashes,
                         context=self.authority("different-execution"))
        original_item = queue._load_item(self.entity, "source-a")
        changed_item = dict(original_item, proposed_category="Expenses:Office")
        queue._save_item(self.entity, changed_item)
        with self.assertRaisesRegex(PreparationDenied, "posted_source_requires_reconciliation"):
            self.execute(session="original-session", source_hashes=hashes)
        queue._save_item(self.entity, original_item)
        pending_path = self.entity.staging_dir / "pending-categorization.json"
        pending = json.loads(pending_path.read_text())
        pending[0]["amount"] = "-999.00"
        pending_path.write_text(json.dumps(pending))
        with self.assertRaisesRegex(PreparationDenied, "posted_source_requires_reconciliation"):
            self.execute(session="original-session", source_hashes=hashes)
        pending[0]["amount"] = "-100.00"
        pending_path.write_text(json.dumps(pending))
        self.assertEqual(self.execute(session="original-session", source_hashes=hashes), 0)
        self.assertEqual(self.store.counts().entries, 1)

    def test_receipts_cover_item_save_and_pending_cleanup_gaps_for_every_confirmation_shape(self):
        original_save = queue._save_item

        def fail_confirmed_save(entity, item):
            if item.get("status") == "confirmed":
                raise RuntimeError("item save gap")
            return original_save(entity, item)

        _add_pending_categorization(self.entity, "category-save-gap", "Category", amount="-10.00")
        queue.propose(self.entity, "category-save-gap", "Expenses:Software", "Evidence")
        source = queue._get_pending_txn(self.entity, "category-save-gap")
        hashes = {"category-save-gap": evidence_hash(source)}
        with patch.object(queue, "_save_item", side_effect=fail_confirmed_save), self.assertRaises(RuntimeError):
            self.execute(item="category-save-gap", source_hashes=hashes)
        self.assertEqual(self.execute(item="category-save-gap", source_hashes=hashes), 0)

        _add_pending_categorization(self.entity, "split-save-gap", "Split", amount="-20.00")
        queue.propose_split(self.entity, "split-save-gap", "Evidence", ["Expenses:Software=20.00"])
        source = queue._get_pending_txn(self.entity, "split-save-gap")
        hashes = {"split-save-gap": evidence_hash(source)}
        with patch.object(queue, "_save_item", side_effect=fail_confirmed_save), self.assertRaises(RuntimeError):
            self.execute(item="split-save-gap", command="confirm-split", source_hashes=hashes)
        self.assertEqual(self.execute(item="split-save-gap", command="confirm-split", source_hashes=hashes), 0)

        _add_pending_categorization(self.entity, "bank-pending-gap", "Card payment", amount="-30.00", txn_date="2026-03-01")
        _add_pending_categorization(self.entity, "card-pending-gap", "Payment received", amount="30.00", txn_date="2026-03-02")
        pending = self.entity.staging_dir / "pending-categorization.json"
        rows = json.loads(pending.read_text())
        next(row for row in rows if row["id"] == "card-pending-gap").update(accountName="Amex", accountType="credit_card")
        pending.write_text(json.dumps(rows))
        item = queue.propose_transfer(self.entity, ["bank-pending-gap", "card-pending-gap"], "Evidence")
        sources = [queue._get_pending_txn(self.entity, value) for value in ("bank-pending-gap", "card-pending-gap")]
        hashes = {source["id"]: evidence_hash(source) for source in sources}
        with patch.object(queue, "_remove_many_from_pending_categorization", side_effect=RuntimeError("pending gap")), \
                self.assertRaises(RuntimeError):
            self.execute(item=item["source_id"], command="confirm-transfer", source_hashes=hashes)
        self.assertEqual(self.execute(item=item["source_id"], command="confirm-transfer", source_hashes=hashes), 0)
        self.assertEqual(self.store.counts().entries, 4)

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
