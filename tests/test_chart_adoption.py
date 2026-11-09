"""Additive chart adoption never changes ledger history or loses source names."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from bookkeeping.chart_adoption import adopt, KEY
from bookkeeping.entity import init_entity, add_account, apply_starter_accounts
from bookkeeping.ledger.importer import check_store_integrity
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.remote_contract import command_spec


class ChartAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "company"
        init_entity(self.root, name="Synthetic")
        self.store = LedgerStore(self.root / "ledger.sqlite")
        self.manifest = {"version": 1, "operation_id": "adopt-1", "source": "synthetic-export",
                         "decision": "preserve", "scope": "partial", "open_date": "2026-01-01", "accounts": [
                             {"source_id": "1", "name": "Design & illustration", "number": "4100", "parent_id": None,
                              "type": "Income", "account": "Income:Design"},
                             {"source_id": "2", "name": "München • retainer", "number": "4100", "parent_id": "1",
                              "type": "Income", "account": "Income:Design:Retainer"}]}

    def test_exact_source_labels_zero_balance_accounts_and_duplicate_numbers(self):
        before = self.store.content_digest()
        plan = adopt(self.root, self.manifest)
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(plan["duplicate_numbers"], ["4100"])
        self.assertEqual([r["action"] for r in plan["accounts"]], ["create", "create"])
        result = adopt(self.root, self.manifest, apply=True, plan_hash=plan["plan_hash"])
        self.assertEqual(result["entries_changed"], 0)
        self.assertEqual(result["existing_accounts_removed"], 0)
        stored = json.loads(self.store.get_meta(KEY))["sources"]["synthetic-export"]
        self.assertEqual(stored["accounts"]["2"]["name"], "München • retainer")
        self.assertTrue({"Income:Design", "Income:Design:Retainer"} <= self.store.load_account_names())
        self.assertEqual(check_store_integrity(self.store).status, "ok")
        after = self.store.content_digest()
        self.assertEqual(adopt(self.root, self.manifest, apply=True, plan_hash=plan["plan_hash"])["status"], "already_applied")
        self.assertEqual(self.store.content_digest(), after)

    def test_stale_preview_changed_replay_and_source_remap_refused(self):
        plan = adopt(self.root, self.manifest)
        add_account(self.root, "Expenses:Samples")
        with self.assertRaisesRegex(ValueError, "Stale"):
            adopt(self.root, self.manifest, apply=True, plan_hash=plan["plan_hash"])
        plan = adopt(self.root, self.manifest)
        adopt(self.root, self.manifest, apply=True, plan_hash=plan["plan_hash"])
        changed = deepcopy(self.manifest)
        changed["accounts"][0]["name"] = "Different"
        with self.assertRaisesRegex(ValueError, "Operation identity"):
            adopt(self.root, changed)
        changed["operation_id"] = "adopt-2"
        with self.assertRaisesRegex(ValueError, "Source account identity"):
            adopt(self.root, changed)

    def test_atomic_failure_rolls_back_accounts_metadata_and_audit(self):
        plan = adopt(self.root, self.manifest)
        before = self.store.content_digest(), self.store.load_audit_events()
        with patch.object(LedgerStore, "append_audit_event", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                adopt(self.root, self.manifest, apply=True, plan_hash=plan["plan_hash"])
        self.assertEqual((self.store.content_digest(), self.store.load_audit_events()), before)

    def test_ambiguities_refused_without_writes(self):
        cases = [("parent_id", "missing"), ("parent_id", "2"), ("account", "Income:Design"), ("type", "Expenses")]
        before = self.store.content_digest()
        for key, value in cases:
            changed = deepcopy(self.manifest)
            changed["accounts"][1][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                adopt(self.root, changed)
            self.assertEqual(self.store.content_digest(), before)

    def test_typed_manifest_binds_kind_without_rewriting_prior_source_identity(self):
        bank = {"source_id": "bank", "name": "Reserve", "number": None, "parent_id": None,
                "type": "Assets", "account": "Assets:Operating:Reserve"}
        self.manifest["accounts"] = [bank]
        first = adopt(self.root, self.manifest)
        adopt(self.root, self.manifest, apply=True, plan_hash=first["plan_hash"])
        typed = {**self.manifest, "version": 2, "operation_id": "adopt-typed",
                 "accounts": [{**bank, "financial_kind": "bank"}]}
        plan = adopt(self.root, typed)
        self.assertEqual(plan["accounts"][0]["financial_kind"], "bank")
        self.assertEqual(self.store.load_financial_kinds(), {})
        altered = deepcopy(typed)
        altered["accounts"][0]["financial_kind"] = "cash"
        with self.assertRaisesRegex(ValueError, "Stale"):
            adopt(self.root, altered, apply=True, plan_hash=plan["plan_hash"])
        adopt(self.root, typed, apply=True, plan_hash=plan["plan_hash"])
        self.assertEqual(self.store.load_financial_kinds(), {bank["account"]: "bank"})
        stored = json.loads(self.store.get_meta(KEY))["sources"][self.manifest["source"]]["accounts"]
        self.assertEqual(stored["bank"], bank)
        self.assertEqual(adopt(self.root, self.manifest)["status"], "already_applied")
        self.assertEqual(adopt(self.root, typed)["status"], "already_applied")
        self.assertEqual(check_store_integrity(self.store).status, "ok")

    def test_typed_manifest_rejects_mapped_other_and_skips_inactive_rows(self):
        from bookkeeping.entity import map_bank_account
        active = {"source_id": "1", "name": "Reserve", "number": None, "parent_id": None, "type": "Assets",
                  "account": "Assets:Bank:Checking", "financial_kind": "other"}
        inactive = {"source_id": "2", "name": "Former account", "number": None, "parent_id": None,
                    "type": "Assets", "account": "Assets:Operating:Former", "active": False}
        typed = {**self.manifest, "version": 2, "accounts": [active, inactive]}
        map_bank_account(self.root, "example-feed", active["account"])
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "mapped financial source"):
            adopt(self.root, typed)
        self.assertEqual(self.store.content_digest(), before)
        active["financial_kind"] = "bank"
        plan = adopt(self.root, typed)
        adopt(self.root, typed, apply=True, plan_hash=plan["plan_hash"])
        self.assertEqual(self.store.load_financial_kinds(), {active["account"]: "bank"})
        self.assertNotIn(inactive["account"], self.store.load_account_names())

    def test_transport_preview_is_read_apply_is_write(self):
        argv = ["entity", "chart-adopt", str(self.root), "--input", "manifest.json"]
        self.assertFalse(command_spec(argv)["mutates"])
        self.assertTrue(command_spec(argv + ["--apply"])["mutates"])
        self.assertEqual(command_spec(argv)["input_fields"], ["input"])

    def test_adopted_existing_default_survives_later_starter_selection(self):
        self.manifest["accounts"] = [{"source_id": "1", "name": "My consulting", "number": None,
                                      "parent_id": None, "type": "Income", "account": "Income:Consulting"}]
        plan = adopt(self.root, self.manifest)
        adopt(self.root, self.manifest, apply=True, plan_hash=plan["plan_hash"])
        apply_starter_accounts(self.root, "retail")
        self.assertIn("Income:Consulting", self.store.load_account_names())

    def test_adopted_unused_added_account_cannot_be_removed(self):
        from bookkeeping.account_remove import preview, AccountRemoveError
        add_account(self.root, "Income:Design")
        plan = adopt(self.root, self.manifest)
        adopt(self.root, self.manifest, apply=True, plan_hash=plan["plan_hash"])
        with self.assertRaises(AccountRemoveError) as error:
            preview(self.root, "Income:Design")
        self.assertEqual(error.exception.code, "account_referenced")

    def test_same_manifest_case_collision_and_parent_mapping_refused(self):
        changed = deepcopy(self.manifest)
        changed["accounts"][1]["account"] = "Income:DESIGN"
        with self.assertRaisesRegex(ValueError, "Duplicate source"):
            adopt(self.root, changed)
        changed["accounts"][1]["account"] = "Income:Retainer"
        with self.assertRaisesRegex(ValueError, "Preserve requires"):
            adopt(self.root, changed)
        changed["decision"] = "rebuild"
        self.assertEqual(adopt(self.root, changed)["status"], "preview")

    def test_metadata_only_adoption_stales_other_preview_and_cross_source_conflict(self):
        for row in self.manifest["accounts"]:
            add_account(self.root, row["account"])
        unrelated = deepcopy(self.manifest)
        unrelated["operation_id"] = "other"
        unrelated["source"] = "other"
        plan_other = adopt(self.root, unrelated)
        plan = adopt(self.root, self.manifest)
        catalog = self.store.load_opens()
        adopt(self.root, self.manifest, apply=True, plan_hash=plan["plan_hash"])
        self.assertEqual(self.store.load_opens(), catalog)
        with self.assertRaisesRegex(ValueError, "Stale"):
            adopt(self.root, unrelated, apply=True, plan_hash=plan_other["plan_hash"])
        unrelated["accounts"][0]["number"] = "9999"
        with self.assertRaisesRegex(ValueError, "conflicting source"):
            adopt(self.root, unrelated)

    def test_public_cli_preview_apply_inspect_and_replay(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from golden_support import run_cli
        path = Path(self.temp.name) / "manifest.json"
        path.write_text(json.dumps(self.manifest))
        argv = ["entity", "chart-adopt", str(self.root), "--input", str(path)]
        code, out, err = run_cli(argv)
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        code, out, err = run_cli(argv + ["--apply", "--plan-hash", plan["plan_hash"]])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "applied")
        code, out, err = run_cli(["entity", "chart-inspect", str(self.root)])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["source_evidence"]["sources"]["synthetic-export"]["accounts"]["1"]["name"], "Design & illustration")
        self.assertFalse(command_spec(["entity", "chart-inspect", str(self.root)])["mutates"])
