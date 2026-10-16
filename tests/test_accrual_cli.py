"""Local/transport command discovery and authenticated adjustment tests."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.cli import main
from bookkeeping.entity import configure_basis, init_entity, load_entity
from bookkeeping.accrual import journals
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.preparation_runtime import run_preparation_command, supported_preparation_command
from bookkeeping.remote_contract import command_spec, rewrite_paths, parse_command
from bookkeeping.automation_policy import PreparationDenied
from test_accrual_journals import trusted_context


class AccrualCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "company"
        init_entity(self.root, name="Synthetic company")
        configure_basis(self.root, "accrual")
        self.store = LedgerStore(self.root / "ledger.sqlite")
        self.request = {"id": "synthetic-journal", "date": "2026-01-31", "description": "Synthetic adjustment",
            "estimate_basis": "Synthetic estimate", "postings": [{"account": "Expenses:Software", "amount": "10"},
                {"account": "Assets:Bank:Checking", "amount": "-10"}]}
        self.input = self.root / "request.json"
        self.input.write_text(json.dumps(self.request))

    def cli(self, argv: list[str]) -> tuple[int, dict]:
        output = StringIO()
        with redirect_stdout(output):
            status = main(argv)
        return status, json.loads(output.getvalue())

    def test_discovery_needs_no_company_or_financial_permission(self) -> None:
        status, result = self.cli(["accrual", "schema"])
        self.assertEqual(status, 0)
        self.assertEqual(result["evidence"]["properties"]["schema_version"]["const"], 1)
        self.assertTrue({"journal", "schedule", "item", "settlement", "item_adjustment", "support", "migration_supporting"} <= set(result["requests"]))
        self.assertTrue(result["requests"]["schedule"]["examples"])
        opening = result["requests"]["migration_supporting"]["properties"]
        for collection in ("items", "schedules", "external"):
            row = opening[collection]["items"]
            self.assertFalse(row["additionalProperties"])
            self.assertTrue({"counterparty", "reference", "description"} <= set(row["properties"]))
            self.assertFalse({"history", "status", "origin", "generation", "base_remaining_amount"} & set(row["properties"]))
        status, capabilities = self.cli(["accrual", "capabilities"])
        self.assertEqual(status, 0)
        self.assertEqual(capabilities["authority_action"], "adjust")

    def test_preview_apply_and_structured_validation(self) -> None:
        argv = ["accrual", "journal", "preview", "--entity", str(self.root), "--input", str(self.input)]
        status, plan = self.cli(argv)
        self.assertEqual(status, 0)
        self.input.write_text(json.dumps(plan))
        argv[2] = "apply"
        status, receipt = self.cli(argv)
        self.assertEqual(status, 0)
        self.assertEqual(receipt["entry_source_ids"], ["accrual:synthetic-journal"])
        self.input.write_text('{"id":"bad"}')
        argv[2] = "preview"
        status, invalid = self.cli(argv)
        self.assertEqual(status, 1)
        self.assertEqual(invalid["status"], "blocked")
        self.assertTrue(invalid["errors"][0]["code"])

    def test_remote_classification_and_input_rewriting(self) -> None:
        for verb, mutates in (("preview", False), ("apply", True), ("correct-preview", False), ("reverse-apply", True)):
            argv = ["accrual", "journal", verb, "--entity", "@entity", "--input", "@input/journal.json"]
            spec = command_spec(argv)
            self.assertEqual(spec["mutates"], mutates)
            self.assertEqual(spec["input_fields"], ["input"])
            self.assertEqual(spec["entity_fields"], ["entity"])
            rewritten = rewrite_paths(argv, {"@entity": str(self.root), "@input/journal.json": str(self.input)})
            self.assertIn("--input=" + str(self.input), rewritten)
            self.assertEqual(supported_preparation_command(argv).action, "adjust")
        migration = ["accrual", "migration", "preview", "--entity", "@entity", "--folder", "@input/reports",
                     "--cutover", "2026-07-01", "--fiscal-start", "2026-01-01", "--supporting", "@input/support.json"]
        self.assertEqual(command_spec(migration)["input_fields"], ["folder", "supporting"])
        period = ["accrual", "period", "preview", "--entity", "@entity", "--input", "@input/period.json"]
        with self.assertRaises(PreparationDenied):
            supported_preparation_command(period)
        queue = ["queue", "confirm", "--entity", "@entity", "--item", "synthetic-item", "--accrual-treatment", "new_cash_event"]
        parsed, _, _ = parse_command(queue)
        self.assertEqual(parsed.accrual_treatment, "new_cash_event")
        self.assertIn("--accrual-treatment=new_cash_event", rewrite_paths(queue, {"@entity": str(self.root)}))

    def test_trusted_dispatch_requires_adjust_and_binds_verified_evidence(self) -> None:
        context = trusted_context()
        argv = ["accrual", "journal", "preview", "--entity", str(self.root), "--input", str(self.input)]
        output = StringIO()
        with redirect_stdout(output):
            status = run_preparation_command(argv, context, entity_path=self.root,
                expected_ledger_digest=self.store.content_digest(), source_hashes={})
        self.assertEqual(status, 0)
        plan = json.loads(output.getvalue())
        self.input.write_text(json.dumps(plan))
        argv[2] = "apply"
        with redirect_stdout(StringIO()):
            status = run_preparation_command(argv, context, entity_path=self.root,
                expected_ledger_digest=self.store.content_digest(), source_hashes={})
        self.assertEqual(status, 0)
        self.assertEqual(self.store.counts().entries, 1)
        self.input.write_text(json.dumps(self.request))
        argv[2] = "preview"
        denied = trusted_context(("import",))
        with redirect_stdout(StringIO()):
            status = run_preparation_command(argv, denied, entity_path=self.root,
                expected_ledger_digest=self.store.content_digest(), source_hashes={})
        self.assertEqual(status, 1)

    def test_host_fingerprint_must_match_canonical_evidence(self) -> None:
        envelope = {"schema_version": 1, "source": {"namespace": "example.any-provider", "account_id": "synthetic",
            "object_id": "journal", "version": "1"}, "currency": "USD", "dates": {"observed": "2026-01-31"}}
        evidence = journals.intake_evidence(load_entity(self.root), envelope)
        self.request["evidence_ids"] = [evidence["id"]]
        self.input.write_text(json.dumps(self.request))
        argv = ["accrual", "journal", "preview", "--entity", str(self.root), "--input", str(self.input)]
        output = StringIO()
        with redirect_stdout(output):
            status = run_preparation_command(argv, trusted_context(), entity_path=self.root,
                expected_ledger_digest=self.store.content_digest(), source_hashes={evidence["id"]: "a" * 64})
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())["errors"][0]["code"], "source_evidence_changed")
        self.assertEqual(self.store.counts().entries, 0)

    def test_trusted_due_reversal_batch_uses_verified_union_without_stale_self_conflict(self) -> None:
        evidence = {}
        for index in (1, 2):
            envelope = {"schema_version": 1, "source": {"namespace": "example.batch", "account_id": "synthetic",
                "object_id": "journal-" + str(index), "version": "1"}, "currency": "USD", "dates": {"observed": "2026-01-31"}}
            row = journals.intake_evidence(load_entity(self.root), envelope)
            evidence[row["id"]] = row["evidence_hash"]
            request = {**self.request, "id": "due-journal-" + str(index), "evidence_ids": [row["id"]], "reversal_date": "2026-02-01"}
            journals.apply_journal(load_entity(self.root), journals.preview_journal(load_entity(self.root), request))
        argv = ["accrual", "journal", "due-reversals", "--entity", str(self.root), "--as-of", "2026-02-01", "--apply"]
        output = StringIO()
        with redirect_stdout(output):
            status = run_preparation_command(argv, trusted_context(), entity_path=self.root,
                expected_ledger_digest=self.store.content_digest(), source_hashes=evidence)
        self.assertEqual(status, 0, output.getvalue())
        self.assertEqual(len(json.loads(output.getvalue())["results"]), 2)
        self.assertEqual(self.store.counts().entries, 4)


if __name__ == "__main__":
    unittest.main()
