"""Readiness with confirmed outside-books decisions (``disposition_context``). All data is synthetic."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping import source_disposition as sd
from bookkeeping.ledger.books_policy_check import workspace_digest
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.readiness import DependencyVersions, ReadinessDispositionContext, assess_review_readiness
from bookkeeping.reconcile import reconcile
from test_queue import _make_entity

CHECKING = "Assets:Bank:Checking"
LEGACY_KEYS = {"schema_version", "basis", "state", "period", "period_status", "books_revision", "source_revision",
               "reply_revision", "policy_version", "evidence_fingerprint", "blockers", "warnings", "sources",
               "coverage", "reconciliations", "reports", "package_id"}


def feed_row(source_id: str = "banksync:r1", day: str = "2026-09-10", amount: str = "-25.00") -> dict:
    return {"id": source_id, "date": day, "amount": amount, "description": "NORTHWIND COFFEE", "currency": "USD",
            "accountId": "feed-chk", "pending": False}


def policy(state: str = "authorized", version: int = 2, epoch: int = 4) -> dict:
    return {"schema_version": 1, "mode": "enforced", "epoch": epoch,
            "accounts": {CHECKING: {"state": state, "start_day": "2026-10-01", "version": version}}}


class ReadinessDispositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.entity = _make_entity(Path(self.temp.name)).path
        config_path = self.entity / "entity.json"
        config = json.loads(config_path.read_text())
        config["declared_sources"] = [{"name": "Synthetic checking", "account": CHECKING,
                                       "coverage_from": "2026-09-01", "coverage_to": "2026-09-30"}]
        config["bank_account_mappings"] = {"feed-chk": CHECKING}
        config_path.write_text(json.dumps(config, sort_keys=True) + "\n")
        self.store = LedgerStore(self.entity / "ledger.sqlite")
        with self.store.transaction() as conn:
            self.store.insert_entries([Entry(
                date=date(2026, 9, 15), narration="Synthetic receipt", flag="*",
                meta=(("source-id", "synthetic-receipt"),),
                postings=(Posting(CHECKING, Decimal("100.00"), "USD"),
                          Posting("Income:Consulting", Decimal("-100.00"), "USD")))], conn)
        reconcile(self.entity, CHECKING, Decimal("100.00"), date(2026, 9, 30),
                  source_opening_balance=Decimal("0.00"), source_transaction_total=Decimal("100.00"),
                  source_snapshot_at="2026-10-01T12:00:00Z", source_through="2026-09-30",
                  source_evidence_refs=["provider-record:statement-2026-09"],
                  source_evidence_hashes={"provider-record:statement-2026-09": "a" * 64})
        self.stage([feed_row()])
        self.policy = policy()
        self.claim = self.preview("banksync:r1")

    # -- helpers ----------------------------------------------------------------------------------------------

    def stage(self, rows):
        (self.entity / "staging" / "pending-categorization.json").write_text(json.dumps(rows) + "\n")

    def preview(self, source_id, policy_document=None):
        result = sd.preview(sd.load_context(self.entity, policy_document or self.policy), source_id, "outside_books")
        self.assertEqual(result["status"], "ok", result)
        return result["claim"]

    def context(self, claims=None, *, policy_document=None, transitions=None, generation=1):
        document = policy_document or self.policy
        return ReadinessDispositionContext(
            document, tuple(claims if claims is not None else [self.claim]), transitions or {},
            {"base_sha256": "b" * 64, "policy_epoch": document["epoch"], "generation": generation,
             "captured_disposition_sha256": "c" * 64})

    def assess(self, disposition_context=None, **kwargs):
        if disposition_context is not None:
            kwargs["disposition_context"] = disposition_context
        return assess_review_readiness(self.entity, "2026-09", DependencyVersions(17, 9, 4, 3),
                                       as_of=date(2026, 10, 13), **kwargs).to_document()

    def staged_refs(self, document):
        work = next((b for b in document["blockers"] if b["code"] == "unresolved_period_work"), None)
        return [] if work is None else work["refs"]

    def book_entry(self, source_id, day=date(2026, 9, 10), account=CHECKING, import_source=None):
        meta = (("source-id", source_id),) + ((("import-source", import_source),) if import_source else ())
        with self.store.transaction() as conn:
            self.store.insert_entries([Entry(
                date=day, narration="Synthetic", flag="*", meta=meta,
                postings=(Posting(account, Decimal("-25.00"), "USD"),
                          Posting("Expenses:Meals" if import_source is None else "Equity:Opening-Balances",
                                  Decimal("25.00"), "USD")))], conn)

    # -- legacy parity ----------------------------------------------------------------------------------------

    def test_no_context_keeps_the_legacy_document(self):
        legacy = self.assess()
        explicit = self.assess(disposition_context=None, )
        self.assertEqual(json.dumps(legacy, sort_keys=True), json.dumps(explicit, sort_keys=True))
        self.assertEqual(set(legacy), LEGACY_KEYS)
        self.assertEqual(self.staged_refs(legacy), ["banksync:r1"])
        self.assertEqual(legacy["state"], "blocked")

    def test_portable_actions_keep_the_complete_package_stable_across_roots(self):
        self.stage([feed_row(), feed_row("banksync:r2", "2026-09-11")])
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "same-books"
            shutil.copytree(self.entity, copied)
            self.assertEqual(workspace_digest(self.entity), workspace_digest(copied))
            context = self.context()
            local = self.assess(context)
            copied_local = assess_review_readiness(copied, "2026-09", DependencyVersions(17, 9, 4, 3),
                                                   as_of=date(2026, 10, 13), disposition_context=context).to_document()
            self.assertNotEqual(local["package_id"], copied_local["package_id"])
            original = self.assess(context, portable_actions=True)
            relocated = assess_review_readiness(copied, "2026-09", DependencyVersions(17, 9, 4, 3),
                                                as_of=date(2026, 10, 13), disposition_context=context,
                                                portable_actions=True).to_document()
            self.assertEqual(relocated, original)
        blocker = next(b for b in original["blockers"] if b["code"] == "unresolved_period_work")
        self.assertEqual(blocker["action"], "Run: books queue list --entity @entity and confirm, correct, or withdraw each item.")
        self.assertEqual(self.staged_refs(original), ["banksync:r2"])
        self.assertNotIn(str(self.entity.resolve()), json.dumps(original))

        legacy = self.assess(context)
        self.assertEqual(legacy, self.assess(context, portable_actions=False))
        self.assertNotEqual(legacy["package_id"], original["package_id"])
        self.assertEqual(next(b["action"] for b in legacy["blockers"] if b["code"] == "unresolved_period_work"),
                         blocker["action"].replace("@entity", str(self.entity.resolve())))

    def test_a_context_that_resolves_nothing_still_binds_the_package(self):
        stale = self.context(policy_document=policy(version=3))
        document = self.assess(stale)
        legacy = self.assess()
        self.assertEqual(self.staged_refs(document), ["banksync:r1"])
        self.assertEqual(document["disposition_basis"]["excluded"], [])
        self.assertEqual({k: v for k, v in document.items() if k not in ("disposition_basis", "package_id")},
                         {k: v for k, v in legacy.items() if k != "package_id"})
        self.assertNotEqual(document["package_id"], legacy["package_id"])

    # -- suppression ------------------------------------------------------------------------------------------

    def test_a_current_resolved_decision_leaves_out_only_its_row(self):
        self.stage([feed_row(), feed_row("banksync:r2", "2026-09-11")])
        before = workspace_digest(self.entity)
        document = self.assess(self.context())
        self.assertEqual(self.staged_refs(document), ["banksync:r2"])
        identity = sd.row_identity(feed_row())
        self.assertEqual(document["disposition_basis"], {
            "base_sha256": "b" * 64, "policy_epoch": 4, "generation": 1, "captured_disposition_sha256": "c" * 64,
            "excluded": [{"source_id": "banksync:r1", "row_digest": identity["row_digest"],
                          "preview_digest": self.claim["preview_digest"]}]})
        self.assertEqual(workspace_digest(self.entity), before)

    def test_the_only_open_row_resolved_makes_the_period_ready(self):
        self.assertEqual(self.assess()["state"], "blocked")
        document = self.assess(self.context())
        self.assertEqual((document["state"], document["blockers"]), ("ready_for_review", []))

    def test_invalidated_stale_and_ambiguous_decisions_stay_visible(self):
        cases = {
            "row changed": ([feed_row(amount="-26.00")], self.context()),
            "policy changed": ([feed_row()], self.context(policy_document=policy(version=3))),
            "two spellings": ([feed_row(), feed_row("r1")], self.context()),
            "two decisions": ([feed_row()], self.context([self.claim, dict(self.claim)])),
        }
        for name, (rows, context) in cases.items():
            with self.subTest(name):
                self.stage(rows)
                document = self.assess(context)
                self.assertIn("banksync:r1", self.staged_refs(document))
                self.assertEqual(document["disposition_basis"]["excluded"], [])

    def test_a_pending_transition_stays_visible_until_its_evidence_is_recorded(self):
        pending = policy("authorized_pending_opening", version=2)
        claim = self.preview("banksync:r1", pending)
        self.book_entry("statement-opening-s2", day=date(2026, 10, 1), import_source="statement-opening")
        authorized = policy("authorized", version=3, epoch=5)
        result = sd.evaluate(sd.load_context(self.entity, authorized), [claim])[0]
        self.assertEqual(result["status"], "transition_pending")
        document = self.assess(self.context([claim], policy_document=authorized))
        self.assertEqual(self.staged_refs(document), ["banksync:r1"])
        transitions = {claim["preview_digest"]: result["transition_evidence"]}
        document = self.assess(self.context([claim], policy_document=authorized, transitions=transitions))
        self.assertEqual(self.staged_refs(document), [])

    def test_unknown_or_borrowed_ids_cannot_leave_a_row_out(self):
        self.stage([feed_row(), feed_row("banksync:r2", "2026-09-11")])
        unknown = {**self.claim, "source_id": "banksync:nope"}
        borrowed = {**self.claim, "source_id": "banksync:r2"}
        for claim in (unknown, borrowed):
            with self.subTest(claim["source_id"]):
                document = self.assess(self.context([claim]))
                self.assertEqual(self.staged_refs(document), ["banksync:r1", "banksync:r2"])
        with self.assertRaises(ValueError):
            ReadinessDispositionContext.from_document({
                "policy": self.policy, "claims": [], "transitions": {}, "suppress": ["banksync:r1"],
                "binding": self.context().binding})

    def test_a_booked_or_recorded_row_is_never_left_out(self):
        self.book_entry("r1")
        self.assertEqual(self.staged_refs(self.assess(self.context())), ["banksync:r1"])

    def test_a_row_sealed_with_no_financial_effect_is_never_left_out(self):
        with self.store.transaction() as conn:
            self.store.insert_source_transactions([feed_row("r1")], conn, status="no_financial_effect")
        result = sd.evaluate(sd.load_context(self.entity, self.policy), [self.claim])[0]
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(self.staged_refs(self.assess(self.context())), ["banksync:r1"])

    # -- other blockers ---------------------------------------------------------------------------------------

    def test_proposals_duplicates_and_questions_still_block(self):
        (self.entity / "review-queue" / "p1.json").write_text(json.dumps(
            {"status": "open", "source_id": "proposal-1", "date": "2026-09-12"}))
        (self.entity / "staging" / "duplicate-candidates.json").write_text(json.dumps([
            {"status": "duplicate-candidate", "source_id": "dup-1", "transaction": {"date": "2026-09-13"}}]))
        (self.entity / "reports" / "open-questions.json").write_text(json.dumps([
            {"status": "open", "source_id": "question-1", "date": "2026-09-14"}]))
        document = self.assess(self.context())
        self.assertEqual(self.staged_refs(document), ["dup-1", "proposal-1"])
        self.assertIn("unresolved_questions", {b["code"] for b in document["blockers"]})
        self.assertEqual(len(document["disposition_basis"]["excluded"]), 1)

    # -- binding ----------------------------------------------------------------------------------------------

    def test_another_epoch_or_generation_gives_another_package(self):
        base = self.assess(self.context())
        later_generation = self.assess(self.context(generation=2))
        later_epoch = self.assess(self.context(policy_document=policy(epoch=5)))
        visible = lambda doc: {k: v for k, v in doc.items() if k not in ("disposition_basis", "package_id")}
        for other in (later_generation, later_epoch):
            self.assertEqual(visible(other), visible(base))
            self.assertEqual(other["disposition_basis"]["excluded"], base["disposition_basis"]["excluded"])
            self.assertNotEqual(other["package_id"], base["package_id"])
        self.assertEqual(self.assess(self.context())["package_id"], base["package_id"])

    def test_a_malformed_context_is_refused(self):
        good = self.context().binding
        bad = [
            {**good, "policy_epoch": 5},
            {**good, "generation": -1},
            {**good, "generation": True},
            {**good, "base_sha256": "B" * 64},
            {k: v for k, v in good.items() if k != "captured_disposition_sha256"},
            {**good, "extra": 1},
        ]
        for binding in bad:
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                ReadinessDispositionContext(self.policy, (self.claim,), {}, binding)
        with self.assertRaises(ValueError):
            ReadinessDispositionContext(self.policy, ({"source_id": "banksync:r1"},), {}, good)
        with self.assertRaises(TypeError):
            self.assess({"policy": self.policy})

    def test_the_context_is_a_private_copy(self):
        claim = dict(self.claim)
        context = self.context([claim])
        claim["row_digest"] = hashlib.sha256(b"other").hexdigest()
        self.assertEqual(self.staged_refs(self.assess(context)), [])


if __name__ == "__main__":
    unittest.main()
