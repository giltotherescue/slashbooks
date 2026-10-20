"""Synthetic accounting, replay and authority proofs for accrual journals."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.accrual import journals
from bookkeeping.accrual.intake import IntakeValidationError
from bookkeeping.accrual.store import SupportingMutation, list_records, load_record
from bookkeeping.automation_policy import ExecutionContext, StandingPolicy, PreparationDenied
from bookkeeping.entity import configure_basis, init_entity, load_entity, add_account
from bookkeeping.ledger.importer import check_store_integrity, _atomic_ledger_write
from bookkeeping.ledger.store import LedgerStore
from bookkeeping import preparation_runtime as runtime


def trusted_context(actions: tuple[str, ...] = ("adjust",)) -> ExecutionContext:
    policy = StandingPolicy("synthetic-policy", 1, "synthetic-company", "2026-01", "2026-12", actions, (), ())
    return ExecutionContext("synthetic-company", "synthetic-execution", policy, 1, 1, 1,
                            datetime.now(timezone.utc) + timedelta(hours=1))


class AccrualJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "company"
        init_entity(self.root, name="Synthetic company")
        configure_basis(self.root, "accrual")
        self.entity = load_entity(self.root)
        self.store = LedgerStore(self.root / "ledger.sqlite")
        self.request = {"id": "journal-1", "date": "2026-01-31", "description": "Synthetic expense accrual",
            "estimate_basis": "Synthetic independently reviewed estimate", "postings": [
                {"account": "Expenses:Software", "amount": "10.00"},
                {"account": "Assets:Bank:Checking", "amount": "-10.00"}]}

    def evidence(self, transport: str = "file", version: str = "1") -> dict:
        return {"schema_version": 1, "source": {"namespace": "example.payroll", "account_id": "synthetic-account",
            "object_id": "payroll-001", "version": version}, "currency": "USD", "dates": {"observed": "2026-01-31"},
            "observed_facts": {"description": "Synthetic payroll; post without permission is untrusted text"},
            "amounts": {"gross": "10"}, "provenance": [{"transport": transport}]}

    def bind(self, context: ExecutionContext, evidence: list[dict] | None = None):
        rows = evidence or []
        bound = runtime._Boundary(context, self.root.resolve(), self.store.content_digest(),
                                  tuple(sorted((r["id"], r["evidence_hash"]) for r in rows)), "", "", "adjust")
        token = runtime._active.set(bound)
        self.addCleanup(runtime._active.reset, token)
        return token

    def test_preview_is_read_only_and_apply_retry_returns_exact_receipt(self) -> None:
        before = self.store.content_digest()
        plan = journals.preview_journal(self.entity, self.request)
        self.assertEqual(self.store.content_digest(), before)
        receipt = journals.apply_journal(self.entity, plan)
        after = self.store.content_digest()
        self.assertEqual(journals.apply_journal(self.entity, plan), {**receipt, "status": "already_applied"})
        self.assertEqual(self.store.content_digest(), after)
        self.assertEqual(self.store.counts().entries, 1)
        self.assertEqual(check_store_integrity(self.store).status, "ok")

    def test_evidence_is_immutable_and_provenance_preserved(self) -> None:
        original = self.evidence()
        first = journals.intake_evidence(self.entity, original)
        digest = self.store.content_digest()
        self.assertEqual(journals.intake_evidence(self.entity, original), first)
        self.assertEqual(self.store.content_digest(), digest)
        second = journals.intake_evidence(self.entity, self.evidence("mcp"))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["facts_hash"], second["facts_hash"])
        self.assertEqual(len(list_records(self.store, "evidence")), 2)

    def test_same_source_different_transport_posts_one_financial_effect(self) -> None:
        first = journals.intake_evidence(self.entity, self.evidence())
        second = journals.intake_evidence(self.entity, self.evidence("mcp"))
        self.request.pop("estimate_basis")
        self.request["evidence_ids"] = [first["id"]]
        receipt = journals.apply_journal(self.entity, journals.preview_journal(self.entity, self.request))
        self.request.update(id="transport-replay", evidence_ids=[second["id"]])
        replay_plan = journals.preview_journal(self.entity, self.request)
        replay = journals.apply_journal(self.entity, replay_plan)
        self.assertEqual(replay, receipt)
        self.assertEqual(journals.apply_journal(self.entity, replay_plan), {**receipt, "status": "already_applied"})
        with self.assertRaisesRegex(journals.JournalError, "replay alias"):
            journals.preview_correction(self.entity, {"id": "reverse-alias", "original_id": "transport-replay",
                "date": "2026-02-01", "description": "Aliases cannot extinguish an origin twice"}, reverse_only=True)
        self.assertEqual(self.store.counts().entries, 1)
        self.assertEqual(len(list_records(self.store, "evidence")), 2)
        self.request["description"] = "Alias id cannot be reused with new facts"
        with self.assertRaises(journals.JournalError):
            journals.apply_journal(self.entity, journals.preview_journal(self.entity, self.request))

    def test_two_transports_one_request_deduplicates_source_mutations(self) -> None:
        first = journals.intake_evidence(self.entity, self.evidence())
        second = journals.intake_evidence(self.entity, self.evidence("mcp"))
        self.request["evidence_ids"] = [first["id"], second["id"]]
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, self.request))
        claims = [r for r in list_records(self.store, "intents") if r.get("kind") == "source_claim"]
        self.assertEqual(len(claims), 1)

    def test_explicit_evidence_alias_links_other_identifier_without_financial_effect(self) -> None:
        first = journals.intake_evidence(self.entity, self.evidence())
        self.request["evidence_ids"] = [first["id"]]
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, self.request))
        other = self.evidence("document")
        other["source"].update(namespace="example.documents", object_id="statement-line-001")
        second = journals.intake_evidence(self.entity, other)
        config = json.loads((self.root / "entity.json").read_text())
        config["closed_periods"] = ["2026-01"]
        (self.root / "entity.json").write_text(json.dumps(config))
        self.entity = load_entity(self.root)
        request = {"operation_id": "duplicate-link", "target_operation_id": "journal-1",
                   "evidence_ids": [second["id"]], "reason": "Synthetic document explicitly identifies the original payroll event."}
        plan = journals.preview_evidence_link(self.entity, request)
        self.assertEqual(plan["entries"], [])
        receipt = journals.apply_evidence_link(self.entity, plan)
        self.assertEqual(journals.apply_evidence_link(self.entity, plan), {**receipt, "status": "already_applied"})
        fresh = journals.preview_evidence_link(self.entity, request)
        self.assertEqual(journals.apply_evidence_link(self.entity, fresh), {**receipt, "status": "already_applied"})
        self.assertEqual(self.store.counts().entries, 1)
        config["closed_periods"] = []
        (self.root / "entity.json").write_text(json.dumps(config))
        self.entity = load_entity(self.root)
        duplicate = copy.deepcopy(self.request)
        duplicate.update(id="would-duplicate", evidence_ids=[second["id"]])
        with self.assertRaisesRegex(journals.JournalError, "different financial treatment"):
            journals.preview_journal(self.entity, duplicate)

    def test_evidence_alias_cannot_reassign_an_owned_source(self) -> None:
        evidence = journals.intake_evidence(self.entity, self.evidence())
        self.request["evidence_ids"] = [evidence["id"]]
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, self.request))
        other = copy.deepcopy(self.request)
        other.update(id="other-origin", evidence_ids=[])
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, other))
        request = {"operation_id": "invalid-link", "target_operation_id": "other-origin",
                   "evidence_ids": [evidence["id"]], "reason": "A conflicting claim is not authority."}
        with self.assertRaisesRegex(journals.JournalError, "already owned"):
            journals.preview_evidence_link(self.entity, request)

    def test_conflicting_facts_same_source_in_request_are_rejected(self) -> None:
        first = journals.intake_evidence(self.entity, self.evidence())
        second = journals.intake_evidence(self.entity, self.evidence("mcp", "2"))
        self.request["evidence_ids"] = [first["id"], second["id"]]
        with self.assertRaisesRegex(journals.JournalError, "conflicting observed facts"):
            journals.preview_journal(self.entity, self.request)

    def test_source_version_change_requires_correction(self) -> None:
        first = journals.intake_evidence(self.entity, self.evidence())
        self.request["evidence_ids"] = [first["id"]]
        plan = journals.preview_journal(self.entity, self.request)
        original = journals.apply_journal(self.entity, plan)
        second = journals.intake_evidence(self.entity, self.evidence("mcp", "2"))
        self.request.update(id="new-version", evidence_ids=[second["id"]])
        with self.assertRaisesRegex(journals.JournalError, "different financial treatment"):
            journals.preview_journal(self.entity, self.request)
        self.assertEqual(journals.apply_journal(self.entity, plan), {**original, "status": "already_applied"})

    def test_reused_id_different_facts_is_a_conflict(self) -> None:
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, self.request))
        self.request["description"] = "changed"
        with self.assertRaisesRegex(journals.JournalError, "different committed facts"):
            journals.preview_journal(self.entity, self.request)

    def test_wrong_balance_currency_account_precision_and_missing_evidence(self) -> None:
        mutations = [
            lambda r: r["postings"][0].update(amount="10.01"),
            lambda r: r["postings"][0].update(currency="EUR"),
            lambda r: r["postings"][0].update(account="Expenses:NotMapped"),
            lambda r: r["postings"][0].update(amount="10.001"),
            lambda r: r["postings"][0].update(amount="NaN"),
            lambda r: r["postings"][0].update(amount=10.0),
            lambda r: r.pop("estimate_basis"),
            lambda r: r.update(authorized=True),
        ]
        digest = self.store.content_digest()
        for mutate in mutations:
            request = copy.deepcopy(self.request)
            mutate(request)
            with self.assertRaises((journals.JournalError, IntakeValidationError)):
                journals.preview_journal(self.entity, request)
            self.assertEqual(self.store.content_digest(), digest)

    def test_account_must_be_open_on_entry_date(self) -> None:
        add_account(self.root, "Expenses:Future", open_date="2026-02-01")
        self.request["postings"][0]["account"] = "Expenses:Future"
        with self.assertRaisesRegex(journals.JournalError, "not open"):
            journals.preview_journal(load_entity(self.root), self.request)

    def test_stale_ledger_and_configuration_are_rejected(self) -> None:
        plan = journals.preview_journal(self.entity, self.request)
        other = copy.deepcopy(self.request)
        other["id"] = "other"
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, other))
        with self.assertRaisesRegex(journals.JournalError, "preview changed"):
            journals.apply_journal(self.entity, plan)
        current = journals.preview_journal(self.entity, self.request)
        config = json.loads((self.root / "entity.json").read_text())
        config["synthetic_mapping_revision"] = 2
        (self.root / "entity.json").write_text(json.dumps(config))
        with self.assertRaisesRegex(journals.JournalError, "preview changed"):
            journals.apply_journal(self.entity, current)

    def test_supplied_displayed_plan_cannot_be_edited_while_retaining_hash(self) -> None:
        plan = journals.preview_journal(self.entity, self.request)
        changes = [lambda p: p["entries"][0]["postings"][0].update(amount="999.00"),
            lambda p: p.update(authority={"mode": "invented"}),
            lambda p: p.update(affected_dates=["2026-02-01"]),
            lambda p: p.update(supporting_hash="a" * 64),
            lambda p: p.update(opens=[{"account": "Expenses:Invented", "date": "2026-01-01", "currencies": ["USD"]}])]
        digest = self.store.content_digest()
        for change in changes:
            altered = copy.deepcopy(plan)
            change(altered)
            with self.assertRaisesRegex(journals.JournalError, "preview has been changed"):
                journals.apply_journal(self.entity, altered)
            self.assertEqual(self.store.content_digest(), digest)

    def test_closed_period_blocks_financial_writes_but_allows_historical_evidence(self) -> None:
        config = json.loads((self.root / "entity.json").read_text())
        config["closed_periods"] = ["2026-01"]
        (self.root / "entity.json").write_text(json.dumps(config))
        entity = load_entity(self.root)
        receipt = journals.intake_evidence(entity, self.evidence())
        self.assertTrue(load_record(self.store, "evidence", receipt["id"]))
        with self.assertRaisesRegex(journals.JournalError, "closed"):
            journals.preview_journal(entity, self.request)

    def test_canonical_period_lock_and_cutover_block_journal(self) -> None:
        _atomic_ledger_write(self.entity, [], [], "fixture-period", None, "Synthetic fixture",
            supporting_mutations=[SupportingMutation("periods", "2026-01", {"id": "2026-01", "status": "closed"})])
        with self.assertRaisesRegex(journals.JournalError, "closed"):
            journals.preview_journal(self.entity, self.request)

    def test_failed_atomic_apply_leaves_no_partial_receipt_or_entries(self) -> None:
        plan = journals.preview_journal(self.entity, self.request)
        with patch.object(LedgerStore, "append_audit_event", side_effect=RuntimeError("Synthetic interruption")):
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                journals.apply_journal(self.entity, plan)
        self.assertEqual(self.store.counts().entries, 0)
        self.assertIsNone(load_record(self.store, "intents", "operation:journal-1"))
        receipt = journals.apply_journal(self.entity, plan)
        self.assertEqual(journals.apply_journal(self.entity, plan), {**receipt, "status": "already_applied"})

    def test_correction_is_linked_atomic_reversal_and_replacement(self) -> None:
        original_plan = journals.preview_journal(self.entity, self.request)
        journals.apply_journal(self.entity, original_plan)
        replacement = copy.deepcopy(self.request)
        replacement.update(id="replacement-1", date="2026-02-01")
        replacement["postings"][0]["amount"] = "20.00"
        replacement["postings"][1]["amount"] = "-20.00"
        request = {"id": "correction-1", "original_id": "journal-1", "date": "2026-02-01",
                   "description": "Correct synthetic estimate", "replacement": replacement}
        plan = journals.preview_correction(self.entity, request)
        receipt = journals.apply_correction(self.entity, plan)
        self.assertEqual(journals.apply_correction(self.entity, plan), {**receipt, "status": "already_applied"})
        entries = self.store.load_entries()
        self.assertEqual(len(entries), 3)
        self.assertEqual(dict(entries[1].meta)["reverses"], "accrual:journal-1")
        self.assertEqual(dict(entries[2].meta)["correction-of"], "accrual:journal-1")
        self.assertEqual(check_store_integrity(self.store).status, "ok")

    def test_reversal_and_correction_cannot_precede_original_entry(self) -> None:
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, self.request))
        before = self.store.content_digest()
        for reverse_only in (True, False):
            request = {"id": "pre-origin-" + str(reverse_only), "original_id": "journal-1", "date": "2026-01-01",
                "description": "Synthetic impossible reversal"}
            if not reverse_only:
                request["replacement"] = {**self.request, "id": "replacement"}
            with self.subTest(reverse_only=reverse_only), self.assertRaisesRegex(ValueError, "cannot precede"):
                journals.preview_correction(self.entity, request, reverse_only=reverse_only)
        self.assertEqual(self.store.content_digest(), before)

    def test_correction_deduplicates_transport_sources_and_rejects_conflicting_versions(self) -> None:
        first = journals.intake_evidence(self.entity, self.evidence())
        second = journals.intake_evidence(self.entity, self.evidence("mcp"))
        request = {**self.request, "evidence_ids": [first["id"]]}
        request.pop("estimate_basis")
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, request))
        replacement = {**request, "id": "corrected-transport", "date": "2026-02-01", "evidence_ids": [first["id"], second["id"]]}
        correction = {"id": "transport-correction", "original_id": "journal-1", "date": "2026-02-01",
            "description": "Correct once across both transports", "replacement": replacement}
        plan = journals.preview_correction(self.entity, correction)
        receipt = journals.apply_correction(self.entity, plan)
        self.assertEqual(journals.apply_correction(self.entity, plan), {**receipt, "status": "already_applied"})
        self.assertEqual(self.store.counts().entries, 3)
        conflict = journals.intake_evidence(self.entity, self.evidence("api", version="2"))
        changed = {**correction, "id": "conflicting-correction", "original_id": "corrected-transport",
            "replacement": {**replacement, "id": "conflicting-replacement", "evidence_ids": [second["id"], conflict["id"]]}}
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "conflicting observed facts"):
            journals.preview_correction(self.entity, changed)
        self.assertEqual(self.store.content_digest(), before)

    def test_legacy_correction_cli_and_direct_apis_cannot_bypass_managed_lineage(self) -> None:
        from bookkeeping.cli import main
        from bookkeeping.ledger.importer import reverse_entry, reverse_and_correct
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, self.request))
        original = self.store.load_entries()[0]
        corrected = replace(original, meta=(("source-id", "legacy-replacement"),))
        before = self.store.content_digest()
        for operation in (lambda: reverse_entry(self.entity, original.source_id, "legacy"),
                lambda: reverse_and_correct(self.entity, original.source_id, corrected, "legacy")):
            with self.assertRaisesRegex(ValueError, "managed accrual"):
                operation()
        common = ["--entity", str(self.root), "--source-id", original.source_id]
        for arguments in (["ledger", "reverse", *common], ["ledger", "correct", *common, "--new-source-id", "legacy-replacement",
                "--date", "2026-02-01", "--description", "Legacy", "--posting", "Expenses:Software=10", "--posting", "Assets:Bank:Checking=-10"],
                ["ledger", "reclassify-bank", *common, "--new-source-id", "legacy-bank", "--from-account", "Assets:Bank:Checking", "--to-account", "Assets:Bank:Savings"],
                ["ledger", "split-equity", *common, "--new-source-id", "legacy-equity", "--equity-posting", "Equity:Owner-Draws=10"]):
            with self.subTest(command=arguments[1]), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(arguments), 1)
                self.assertIn("managed accrual", output.getvalue())
        self.assertEqual(self.store.content_digest(), before)
        request = {"id": "canonical-reversal", "original_id": "journal-1", "date": "2026-02-01", "description": "Reviewed reversal"}
        journals.apply_correction(self.entity, journals.preview_correction(self.entity, request, reverse_only=True))
        self.assertEqual(self.store.counts().entries, 2)

    def test_legacy_reverse_cannot_mutate_opening_without_managed_items_or_schedules(self) -> None:
        from datetime import date
        from decimal import Decimal
        from bookkeeping.ledger.importer import reverse_entry
        from bookkeeping.ledger.model import Entry, Posting
        opening = Entry(date(2026, 1, 1), "Synthetic bank-only opening", meta=(("source-id", "opening-only"),),
            postings=(Posting("Assets:Bank:Checking", Decimal("100")), Posting("Equity:Opening-Balances", Decimal("-100"))))
        coverage = {"id": "opening", "basis": "accrual", "currency": "USD", "cutover_date": "2026-01-02",
            "source_as_of": "2026-01-01", "fiscal_start": "2026-01-01", "summary_from": None, "summary_to": None,
            "opening_source_id": "opening-only", "verified": True, "support_status": "reconciled"}
        _atomic_ledger_write(self.entity, [], [opening], "synthetic-opening", None, "Synthetic coverage fixture",
            supporting_mutations=[SupportingMutation("coverage", "opening", coverage)])
        self.assertEqual(list_records(self.store, "items"), [])
        self.assertEqual(list_records(self.store, "schedules"), [])
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "managed accrual"):
            reverse_entry(self.entity, "opening-only", "legacy-opening-reversal")
        self.assertEqual(self.store.content_digest(), before)

    def test_successive_corrections_reverse_only_live_replacement(self) -> None:
        source = journals.intake_evidence(self.entity, self.evidence())
        self.request["evidence_ids"] = [source["id"]]
        original_plan = journals.preview_journal(self.entity, self.request)
        original_receipt = journals.apply_journal(self.entity, original_plan)
        original_id = "journal-1"
        for number, amount in ((1, "20.00"), (2, "30.00")):
            replacement = copy.deepcopy(self.request)
            replacement.update(id=f"replacement-{number}", date=f"2026-02-0{number}")
            replacement["postings"][0]["amount"] = amount
            replacement["postings"][1]["amount"] = "-" + amount
            request = {"id": f"correction-{number}", "original_id": original_id,
                       "date": f"2026-02-0{number}", "description": "Synthetic successive correction", "replacement": replacement}
            journals.apply_correction(self.entity, journals.preview_correction(self.entity, request))
            original_id = replacement["id"]
        duplicate = copy.deepcopy(replacement)
        duplicate["id"] = "second-transport-corrected"
        replay = journals.apply_journal(self.entity, journals.preview_journal(self.entity, duplicate))
        self.assertEqual(replay["entry_source_ids"], ["accrual:replacement-2"])
        child_preview = journals.preview_journal(self.entity, replacement)
        self.assertEqual(journals.apply_journal(self.entity, child_preview), {**replay, "status": "already_applied"})
        request = {"id": "final-reversal", "original_id": "replacement-2", "date": "2026-02-03", "description": "Final synthetic reversal"}
        journals.apply_correction(self.entity, journals.preview_correction(self.entity, request, reverse_only=True))
        self.assertEqual(self.store.counts().entries, 6)
        software = sum(p.amount for e in self.store.load_entries() for p in e.postings if p.account == "Expenses:Software")
        self.assertEqual(software, 0)
        self.assertEqual(journals.apply_journal(self.entity, original_plan), {**original_receipt, "status": "already_applied"})
        self.assertEqual(check_store_integrity(self.store).status, "ok")

    def test_forward_reversal_after_original_period_closed_is_allowed(self) -> None:
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, self.request))
        config = json.loads((self.root / "entity.json").read_text())
        config["closed_periods"] = ["2026-01"]
        (self.root / "entity.json").write_text(json.dumps(config))
        request = {"id": "reverse-1", "original_id": "journal-1", "date": "2026-02-01", "description": "Synthetic reversal"}
        plan = journals.preview_correction(load_entity(self.root), request, reverse_only=True)
        journals.apply_correction(load_entity(self.root), plan)
        self.assertEqual(self.store.counts().entries, 2)
        request.update(id="reverse-closed", date="2026-01-31")
        # The original is already extinguished, and cannot be reversed again.
        with self.assertRaises(journals.JournalError):
            journals.preview_correction(load_entity(self.root), request, reverse_only=True)

    def test_scheduled_reversal_is_due_replay_safe_and_linked(self) -> None:
        self.request["reversal_date"] = "2026-02-01"
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, self.request))
        self.assertEqual(journals.due_reversals(self.entity, "2026-01-31")["results"], [])
        preview = journals.due_reversals(self.entity, "2026-02-28")
        self.assertEqual(len(preview["results"]), 1)
        applied = journals.due_reversals(self.entity, "2026-02-28", apply=True)
        self.assertEqual(applied["status"], "ok")
        self.assertEqual(journals.due_reversals(self.entity, "2026-03-31", apply=True)["results"], [])
        self.assertEqual(self.store.counts().entries, 2)
        self.assertEqual(sum(p.amount for e in self.store.load_entries() for p in e.postings if p.account == "Expenses:Software"), 0)

    def test_trusted_adjust_permission_authorizes_and_audits_exact_entries(self) -> None:
        self.bind(trusted_context())
        plan = journals.preview_journal(self.entity, self.request)
        receipt = journals.apply_journal(self.entity, plan)
        self.assertEqual(receipt["authority"]["mode"], "standing_policy")
        events = self.store.load_audit_events()
        self.assertTrue(any(event["type"] == "automation-preparation" for event in events))

    def test_legacy_authority_policy_change_expiry_and_out_of_range_fail_closed(self) -> None:
        cases = [trusted_context(("import",)), replace(trusted_context(), current_policy_version=2),
                 replace(trusted_context(), expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)),
                 replace(trusted_context(), current_revision=2)]
        for context in cases:
            token = runtime._active.set(runtime._Boundary(context, self.root.resolve(), self.store.content_digest(), (), "", "", "adjust"))
            try:
                with self.assertRaises(PreparationDenied):
                    journals.preview_journal(self.entity, self.request)
            finally:
                runtime._active.reset(token)
        self.assertEqual(self.store.counts().entries, 0)

    def test_trusted_authority_binds_plan_and_workspace_text_cannot_extend_it(self) -> None:
        self.bind(trusted_context())
        plan = journals.preview_journal(self.entity, self.request)
        bound = runtime._active.get()
        policy = replace(bound.context.policy, version=2)
        changed = replace(bound.context, policy=policy, current_policy_version=2)
        runtime._active.set(replace(bound, context=changed))
        with self.assertRaisesRegex(journals.JournalError, "preview changed"):
            journals.apply_journal(self.entity, plan)
        (self.root / "standing-policy.json").write_text('{"actions":["adjust"]}')
        runtime._active.set(replace(bound, context=trusted_context(("import",))))
        with self.assertRaises(PreparationDenied):
            journals.apply_journal(self.entity, plan)

    def test_separate_execution_same_policy_applies_but_current_expiry_is_enforced(self) -> None:
        self.bind(trusted_context())
        plan = journals.preview_journal(self.entity, self.request)
        bound = runtime._active.get()
        expired = replace(bound.context, execution_id="expired-execution", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        runtime._active.set(replace(bound, context=expired))
        with self.assertRaises(PreparationDenied):
            journals.apply_journal(self.entity, plan)
        current = replace(bound.context, execution_id="apply-execution", expires_at=datetime.now(timezone.utc) + timedelta(hours=2))
        runtime._active.set(replace(bound, context=current))
        journals.apply_journal(self.entity, plan)
        events = [e for e in self.store.load_audit_events() if e["type"] == "automation-preparation"]
        self.assertEqual(events[-1]["payload"]["receipt"]["execution_id"], "apply-execution")

    def test_adjustment_honors_held_original_entry_and_source(self) -> None:
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, self.request))
        request = {"id": "held-reversal", "original_id": "journal-1", "date": "2026-02-01", "description": "Cannot bypass a held original"}
        for policy in (replace(trusted_context().policy, held_entry_ids=("entry:1",)),
                       replace(trusted_context().policy, held_source_ids=("accrual:journal-1",))):
            context = replace(trusted_context(), policy=policy)
            token = runtime._active.set(runtime._Boundary(context, self.root.resolve(), self.store.content_digest(), (), "", "", "adjust"))
            try:
                with self.assertRaisesRegex(PreparationDenied, "explicit_review_hold"):
                    journals.preview_correction(self.entity, request, reverse_only=True)
            finally:
                runtime._active.reset(token)


class JournalTotalsHelperTests(unittest.TestCase):
    def test_public_helper_and_private_alias_agree(self):
        from datetime import date
        from decimal import Decimal
        from bookkeeping.ledger.model import Entry, Posting
        entry = Entry(date=date(2026, 1, 2), narration="Synthetic", meta=(),
                      postings=(Posting("Assets:Bank:Checking", Decimal("-30.00")),
                                Posting("Expenses:Software", Decimal("30.00"))))
        expected = {"debits": "30.00", "credits": "30.00", "difference": "0.00", "currency": "USD"}
        self.assertEqual(journals.journal_totals([entry]), expected)
        self.assertIs(journals._journal_totals, journals.journal_totals)


if __name__ == "__main__":
    unittest.main()
