"""Saved accrual rules only change what is proposed. A person still approves; nothing posts from a rule."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bookkeeping.accrual import rules
from bookkeeping.accrual.cli import request_schemas
from bookkeeping.accrual.journals import JournalError
from bookkeeping.accrual.schedules import apply_schedule, preview_schedule
from bookkeeping.automation_policy import PreparationDenied
from bookkeeping.cli import main
from bookkeeping.entity import add_account, init_entity, load_entity
from bookkeeping.preparation_runtime import supported_preparation_command
from bookkeeping.queue import (clear_taught, eligible_for_autopost, load_learned_context, propose, read_queue_item,
                               set_taught, taught_for)
from bookkeeping.remote_contract import command_spec
from test_accrual_schedules import AR, AP, BANK, DEFERRED, EXPENSE, INCOME, PREPAID, AccrualFixture
from test_queue import _add_pending_categorization

EQUIPMENT, CONTRA, DEPRECIATION = "Assets:Fixed:Equipment", "Assets:Fixed:Accumulated-Depreciation", "Expenses:Depreciation"
ACCRUED = "Liabilities:Other:Accrued-Liabilities"
HOSTING = "NORTHWIND HOSTING"


class RulesFixture(AccrualFixture):
    def setUp(self):
        super().setUp()
        for account in (EQUIPMENT, CONTRA, DEPRECIATION, ACCRUED, "Expenses:Software:Tools", "Expenses:Equipment", "Expenses:Hosting"):
            add_account(self.root, account, open_date="2000-01-01")
        self.entity = load_entity(self.root)
        self.counter = 0

    def op(self) -> str:
        self.counter += 1
        return f"rule-op-{self.counter}"

    def supplier(self, **changes):
        request = {"operation_id": self.op(), "counterparty": "Northwind Hosting", "treatment": "prepaid", "term_months": 12,
                   "method": "equal_month", "note": "Annual plan, paid up front", "set_by": "gil", "source": "person",
                   "set_at": "2026-10-16T10:00:00Z"}
        request.update(changes)
        return request

    def company(self, **changes):
        request = {"operation_id": self.op(), "id": "software-prepaid", "match": {"accounts": [EXPENSE]}, "treatment": "prepaid",
                   "threshold": "1000.00", "note": "Software at 1,000 or more is prepaid", "set_by": "gil", "source": "person",
                   "set_at": "2026-10-16T10:00:00Z"}
        request.update(changes)
        return request

    def save(self, request, kind="set"):
        preview = (rules.set_preview if kind == "set" else rules.clear_preview)(self.entity, request)
        return (rules.set_apply if kind == "set" else rules.clear_apply)(self.entity, preview)

    def facts(self, **changes):
        facts = {"counterparty": "Northwind Hosting", "amount": "-1200.00", "account": EXPENSE, "description": "NORTHWIND HOSTING 5001",
                 "date": "2026-02-01", "service_start": "2026-02-01", "source_id": "bank-nw-0201", "funding_account": BANK,
                 "evidence_ids": [self.eid]}
        facts.update(changes)
        return facts

    def blocked(self, code, call, *args):
        with self.assertRaises(JournalError) as caught:
            call(self.entity, *args)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception


class SetClearListTests(RulesFixture):
    def test_preview_writes_nothing_and_apply_stores_the_rule_on_the_counterparty(self):
        before = self.store.content_digest()
        preview = rules.set_preview(self.entity, self.supplier())
        self.assertFalse((self.root / "learned-context" / "counterparties.json").exists())
        self.assertEqual((preview["action"], preview["effect"], preview["target"]["counterparty"]), ("create", "proposals_only", HOSTING))
        receipt = rules.set_apply(self.entity, preview)
        self.assertEqual((receipt["status"], receipt["after"]["treatment"]), ("applied", "prepaid"))
        stored = load_learned_context(self.entity)[HOSTING]["accrual"]
        self.assertEqual((stored["version"], stored["term_months"], stored["source"], stored["set_at"]), (1, 12, "person", "2026-10-16T10:00:00Z"))
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(rules.list_rules(self.entity)["counterparty_rules"][0]["counterparty"], HOSTING)

    def test_stored_rule_passes_the_counterparty_shape_check(self):
        from bookkeeping.queue import validate_accrual_hint
        self.save(self.supplier(threshold="500.00", accounts={"account": PREPAID, "recognition_account": EXPENSE}))
        self.assertEqual(validate_accrual_hint(load_learned_context(self.entity)[HOSTING]["accrual"]), [])

    def test_replaying_the_same_preview_or_a_fresh_preview_returns_the_receipt_and_writes_nothing(self):
        request = self.supplier()
        preview = rules.set_preview(self.entity, request)
        first = rules.set_apply(self.entity, preview)
        files = {p: p.read_text() for p in (self.root / "learned-context").glob("*.json")}
        again = rules.set_apply(self.entity, preview)
        fresh = rules.set_apply(self.entity, rules.set_preview(self.entity, request))
        self.assertEqual(again, {**first, "status": "already_applied"})
        self.assertEqual(fresh, {**first, "status": "already_applied"})
        self.assertEqual(files, {p: p.read_text() for p in (self.root / "learned-context").glob("*.json")})

    def test_default_time_replays_with_a_fresh_preview(self):
        request = self.supplier()
        request.pop("set_at")
        first = rules.set_apply(self.entity, rules.set_preview(self.entity, request))
        self.assertEqual(rules.set_apply(self.entity, rules.set_preview(self.entity, request))["status"], "already_applied")
        self.assertEqual(first["after"]["set_at"][-1], "Z")

    def test_same_operation_id_with_different_facts_is_an_identity_conflict(self):
        request = self.supplier()
        self.save(request)
        self.blocked("identity_conflict", rules.set_apply, rules.set_preview(self.entity, {**request, "term_months": 24}))
        self.assertEqual(load_learned_context(self.entity)[HOSTING]["accrual"]["term_months"], 12)

    def test_a_preview_goes_stale_when_the_rules_change_first(self):
        stale = rules.set_preview(self.entity, self.supplier())
        self.save(self.supplier(term_months=6))
        self.blocked("stale_preview", rules.set_apply, stale)
        self.assertEqual(load_learned_context(self.entity)[HOSTING]["accrual"]["term_months"], 6)

    def test_an_edited_preview_is_refused(self):
        preview = rules.set_preview(self.entity, self.supplier())
        preview["request"]["term_months"] = 1
        self.blocked("plan_changed", rules.set_apply, preview)
        preview = rules.set_preview(self.entity, self.supplier())
        preview["after"]["treatment"] = "deferred"
        self.blocked("plan_changed", rules.set_apply, preview)
        self.assertNotIn(HOSTING, load_learned_context(self.entity))

    def test_a_preview_made_for_set_cannot_be_applied_as_clear(self):
        self.blocked("invalid_plan", rules.clear_apply, rules.set_preview(self.entity, self.supplier()))

    def test_clear_removes_only_the_accrual_object_and_keeps_taught(self):
        taught = set_taught(self.entity, HOSTING, EXPENSE, by="u1", by_name="Gil", ts="2026-10-16T09:00:00Z")
        self.save(self.supplier())
        self.assertEqual(set(load_learned_context(self.entity)[HOSTING]) & {"taught", "accrual"}, {"taught", "accrual"})
        receipt = self.save({"operation_id": self.op(), "counterparty": "northwind hosting", "cleared_by": "gil", "source": "person"}, "clear")
        self.assertEqual((receipt["status"], receipt["before"]["treatment"], receipt["after"]), ("applied", "prepaid", None))
        entry = load_learned_context(self.entity)[HOSTING]
        self.assertNotIn("accrual", entry)
        self.assertEqual(taught_for(self.entity, HOSTING)["id"], taught["id"])
        self.assertEqual(rules.list_rules(self.entity)["count"], 0)

    def test_clearing_a_rule_leaves_no_empty_counterparty_record(self):
        self.save(self.supplier())
        self.save({"operation_id": self.op(), "counterparty": HOSTING, "cleared_by": "gil", "source": "person"}, "clear")
        self.assertNotIn(HOSTING, load_learned_context(self.entity))

    def test_clear_replay_and_missing_rule(self):
        self.blocked("rule_not_found", rules.clear_preview, {"operation_id": "nothing", "counterparty": HOSTING, "cleared_by": "gil", "source": "person"})
        self.save(self.supplier())
        clear = {"operation_id": "clear-once", "counterparty": HOSTING, "cleared_by": "gil", "source": "person"}
        preview = rules.clear_preview(self.entity, clear)
        receipt = rules.clear_apply(self.entity, preview)
        self.assertEqual(rules.clear_apply(self.entity, preview), {**receipt, "status": "already_applied"})

    def test_an_apply_that_stopped_before_its_receipt_finishes_on_retry(self):
        preview = rules.set_preview(self.entity, self.supplier())
        with patch.object(rules, "_save_company", side_effect=OSError("disk full")), self.assertRaises(OSError):
            rules.set_apply(self.entity, preview)
        self.assertEqual(load_learned_context(self.entity)[HOSTING]["accrual"]["treatment"], "prepaid")
        receipt = rules.set_apply(self.entity, preview)
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual(rules.set_apply(self.entity, preview)["status"], "already_applied")
        clear = rules.clear_preview(self.entity, {"operation_id": "clear-crash", "counterparty": HOSTING, "cleared_by": "gil", "source": "person"})
        with patch.object(rules, "_save_company", side_effect=OSError("disk full")), self.assertRaises(OSError):
            rules.clear_apply(self.entity, clear)
        self.assertNotIn(HOSTING, load_learned_context(self.entity))
        self.assertEqual(rules.clear_apply(self.entity, clear)["status"], "applied")

    def test_taught_is_preserved_when_a_rule_is_set_and_taught_changes_leave_the_rule(self):
        taught = set_taught(self.entity, HOSTING, EXPENSE, by="u1", by_name="Gil", ts="2026-10-16T09:00:00Z")
        self.save(self.supplier())
        entry = load_learned_context(self.entity)[HOSTING]
        self.assertEqual(entry["taught"], taught)
        self.assertEqual((entry["canonical_category"], entry["confirmed_count"], entry["reset"]), ("", 0, False))
        second = set_taught(self.entity, HOSTING, "Expenses:Hosting", by="u1", by_name="Gil", ts="2026-10-17T09:00:00Z")
        self.assertEqual(load_learned_context(self.entity)[HOSTING]["accrual"]["treatment"], "prepaid")
        clear_taught(self.entity, HOSTING, second["id"])
        self.assertEqual(taught_for(self.entity, HOSTING)["id"], taught["id"])
        self.assertEqual(load_learned_context(self.entity)[HOSTING]["accrual"]["treatment"], "prepaid")

    def test_a_rule_keeps_the_trust_ramp_and_other_counterparty_fields(self):
        path = self.root / "learned-context"
        path.mkdir(exist_ok=True)
        (path / "counterparties.json").write_text(json.dumps({HOSTING: {"canonical_category": EXPENSE, "confirmed_count": 2,
            "last_confirmed_date": "2026-09-01", "reset": False, "notes": "n"}}))
        self.save(self.supplier())
        entry = load_learned_context(self.entity)[HOSTING]
        self.assertEqual((entry["canonical_category"], entry["confirmed_count"], entry["notes"]), (EXPENSE, 2, "n"))
        self.assertFalse(eligible_for_autopost(self.entity, HOSTING))

    def test_an_unreadable_counterparty_file_is_never_overwritten(self):
        path = self.root / "learned-context"
        path.mkdir(exist_ok=True)
        (path / "counterparties.json").write_text("{not json")
        self.blocked("learned_context_unreadable", rules.set_preview, self.supplier())
        self.assertEqual((path / "counterparties.json").read_text(), "{not json")

    def test_company_rule_defaults_its_id_from_the_match_and_updates_in_place(self):
        request = self.company()
        request.pop("id")
        first = self.save(request)
        rule_id = first["target"]["id"]
        self.assertTrue(rule_id.startswith("rule-"))
        second = self.save({**request, "operation_id": self.op(), "note": "Reworded"})
        self.assertEqual((second["target"]["id"], second["action"]), (rule_id, "replace"))
        listed = rules.list_rules(self.entity)["company_rules"]
        self.assertEqual([r["note"] for r in listed], ["Reworded"])
        self.assertEqual(json.loads((self.root / rules.RULES_FILE).read_text())["version"], 1)

    def test_unchanged_decision_keeps_the_original_time(self):
        self.save(self.company())
        receipt = self.save(self.company(set_at="2026-11-01T00:00:00Z"))
        self.assertEqual(receipt["action"], "unchanged")
        self.assertEqual(rules.list_rules(self.entity)["company_rules"][0]["set_at"], "2026-10-16T10:00:00Z")

    def test_same_match_under_two_ids_warns(self):
        self.save(self.company())
        preview = rules.set_preview(self.entity, self.company(id="software-other"))
        self.assertEqual([w["code"] for w in preview["warnings"]], ["duplicate_match"])

    def test_agent_rule_is_flagged_and_cannot_replace_or_clear_a_person_rule(self):
        preview = rules.set_preview(self.entity, self.supplier(source="agent", set_by="books-agent"))
        self.assertIn("agent_rule", [w["code"] for w in preview["warnings"]])
        rules.set_apply(self.entity, preview)
        self.save(self.supplier(treatment="deferred", accounts={"account": DEFERRED, "recognition_account": INCOME}, term_months=12))
        self.blocked("person_rule_protected", rules.set_preview, self.supplier(source="agent", term_months=3))
        self.blocked("person_rule_protected", rules.clear_preview,
                     {"operation_id": "agent-clear", "counterparty": HOSTING, "cleared_by": "books-agent", "source": "agent"})
        self.assertEqual(rules.set_preview(self.entity, self.supplier(source="person", term_months=3))["action"], "replace")

    def test_receipt_command_finds_a_saved_rule_receipt(self):
        request = self.supplier(operation_id="receipt-probe")
        receipt = self.save(request)
        out = StringIO()
        with redirect_stdout(out):
            status = main(["accrual", "receipt", "--entity", str(self.root), "--id", "receipt-probe"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())["receipt"], receipt)

    def test_cash_company_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cash"
            init_entity(root, name="Cash co")
            with self.assertRaises(JournalError) as caught:
                rules.list_rules(load_entity(root))
            self.assertEqual(caught.exception.code, "accrual_basis_required")


class ValidationTests(RulesFixture):
    def test_unknown_fields_are_rejected_naming_the_field(self):
        error = self.blocked("invalid_request", rules.set_preview, self.supplier(approve=True, auto_post=True))
        self.assertIn("approve", str(error))
        self.assertIn("auto_post", str(error))
        self.blocked("invalid_request", rules.match, {"amount": "10.00", "account": EXPENSE, "post": True})
        self.blocked("invalid_request", rules.clear_preview, {"operation_id": "x", "counterparty": "a", "cleared_by": "g", "source": "person", "extra": 1})

    def test_scope_must_be_exactly_one(self):
        self.blocked("invalid_scope", rules.set_preview, self.supplier(match={"keywords": ["aws"]}))
        request = self.supplier()
        request.pop("counterparty")
        self.blocked("invalid_scope", rules.set_preview, request)
        self.blocked("id_not_allowed", rules.set_preview, self.supplier(id="rule-1"))
        self.blocked("invalid_scope", rules.clear_preview, {"operation_id": "x", "cleared_by": "g", "source": "person"})

    def test_treatment_term_method_and_threshold_checks(self):
        self.blocked("invalid_treatment", rules.set_preview, self.supplier(treatment="amortize"))
        for bad in (0, 601, -1, True, 1.5, "12"):
            with self.subTest(term=bad):
                self.blocked("invalid_term", rules.set_preview, self.supplier(term_months=bad))
        for good in (1, 600):
            rules.set_preview(self.entity, self.supplier(term_months=good))
        self.blocked("term_not_allowed", rules.set_preview, self.supplier(treatment="bill"))
        request = self.supplier(treatment="bill")
        request.pop("term_months")
        self.blocked("method_not_allowed", rules.set_preview, request)
        self.blocked("invalid_method", rules.set_preview, self.supplier(method="custom"))
        request = self.supplier(treatment="depreciation")
        request.pop("term_months")
        self.blocked("term_required", rules.set_preview, request)
        for bad in ("1e3", "12.345", "-5.00", "1,000", "", 1000, 10.5, None, {"minor_units": 100}):
            with self.subTest(threshold=bad):
                self.blocked("invalid_threshold", rules.set_preview, self.supplier(threshold=bad))
        self.assertEqual(rules.set_preview(self.entity, self.supplier(threshold="1000"))["after"]["threshold"], "1000.00")

    def test_note_who_source_and_time(self):
        self.blocked("missing_fact", rules.set_preview, self.supplier(note=" "))
        self.blocked("missing_fact", rules.set_preview, self.supplier(set_by=""))
        self.blocked("invalid_source", rules.set_preview, self.supplier(source="system"))
        self.blocked("invalid_timestamp", rules.set_preview, self.supplier(set_at="2026-10-16"))
        self.blocked("missing_fact", rules.set_preview, self.supplier(counterparty="  "))

    def test_accounts_must_fit_the_treatment(self):
        wrong_direction = self.blocked("wrong_account_direction", rules.set_preview, self.supplier(accounts={"account": EXPENSE, "recognition_account": EXPENSE}))
        self.assertIn("accounts.account", str(wrong_direction))
        self.blocked("unmapped_account", rules.set_preview, self.supplier(accounts={"account": "Assets:Prepaid:Nope", "recognition_account": EXPENSE}))
        subtype = self.blocked("account_subtype_mismatch", rules.set_preview, self.supplier(accounts={"account": AR, "recognition_account": EXPENSE}))
        self.assertIn("receivable", str(subtype))
        self.blocked("account_subtype_mismatch", rules.set_preview, self.supplier(accounts={"account": BANK, "recognition_account": EXPENSE}))
        self.blocked("invalid_accounts", rules.set_preview, self.supplier(accounts={"account": PREPAID}))
        self.blocked("invalid_accounts", rules.set_preview, self.supplier(accounts={"account": PREPAID, "recognition_account": EXPENSE, "contra_account": CONTRA}))
        self.blocked("wrong_account_direction", rules.set_preview, self.supplier(accounts={"account": PREPAID, "recognition_account": PREPAID}))
        self.blocked("wrong_account_direction", rules.set_preview, self.supplier(treatment="deferred", accounts={"account": DEFERRED, "recognition_account": EXPENSE}))
        self.blocked("invalid_accounts", rules.set_preview, self.supplier(treatment="depreciation", term_months=36, accounts={"account": EQUIPMENT, "recognition_account": DEPRECIATION}))
        bill = {k: v for k, v in self.supplier(treatment="bill").items() if k not in ("term_months", "method")}
        self.blocked("wrong_account_direction", rules.set_preview, {**bill, "accounts": {"account": AR, "recognition_account": EXPENSE}})
        for treatment, accounts, extra in (
                ("prepaid", {"account": PREPAID, "recognition_account": EXPENSE}, {"term_months": 12}),
                ("deferred", {"account": DEFERRED, "recognition_account": INCOME}, {"term_months": 12}),
                ("depreciation", {"account": EQUIPMENT, "recognition_account": DEPRECIATION, "contra_account": CONTRA}, {"term_months": 36}),
                ("bill", {"account": AP, "recognition_account": EXPENSE}, {}),
                ("invoice", {"account": AR, "recognition_account": INCOME}, {}),
                ("estimate", {"account": ACCRUED, "recognition_account": EXPENSE}, {})):
            with self.subTest(treatment=treatment):
                request = {k: v for k, v in self.supplier(treatment=treatment, accounts=accounts).items() if k not in ("term_months", "method")}
                request.update(extra)
                self.assertEqual(rules.set_preview(self.entity, request)["action"], "create")

    def test_company_match_checks(self):
        self.blocked("invalid_match", rules.set_preview, self.company(match={}))
        self.blocked("invalid_match", rules.set_preview, self.company(match={"threshold": "5"}))
        self.blocked("invalid_match", rules.set_preview, self.company(match={"accounts": []}))
        self.blocked("invalid_match", rules.set_preview, self.company(match={"accounts": [BANK]}))
        self.blocked("invalid_match", rules.set_preview, self.company(match={"accounts": [AP]}))
        self.blocked("unmapped_account", rules.set_preview, self.company(match={"accounts": ["Expenses:Typo"]}))
        self.blocked("invalid_match", rules.set_preview, self.company(match={"keywords": []}))
        self.blocked("invalid_match", rules.set_preview, self.company(match={"keywords": ["x" * 61]}))
        self.blocked("invalid_id", rules.set_preview, self.company(id="bad id"))
        self.assertEqual(rules.set_preview(self.entity, self.company(match={"accounts": ["Expenses:Software"]}))["after"]["match"]["accounts"], ["Expenses:Software"])
        self.assertEqual(rules.set_preview(self.entity, self.company(match={"keywords": [" AWS ", "aws", "Cloud  Hosting"]}))["after"]["match"]["keywords"],
                         ["aws", "cloud hosting"])

    def test_match_facts_checks(self):
        self.blocked("missing_fact", rules.match, {"account": EXPENSE})
        self.blocked("missing_fact", rules.match, {"amount": "10.00"})
        self.blocked("invalid_amount", rules.match, {"amount": 10.5, "account": EXPENSE})
        self.blocked("invalid_date", rules.match, {"amount": "10.00", "account": EXPENSE, "date": "02/01/2026"})
        self.blocked("invalid_service_period", rules.match, {"amount": "10.00", "account": EXPENSE, "service_start": "2026-02-01", "service_end": "2026-01-31"})
        self.blocked("invalid_evidence", rules.match, {"amount": "10.00", "account": EXPENSE, "evidence_ids": ["a", "a"]})

    def test_schemas_and_capabilities_list_the_rules_commands(self):
        schemas = request_schemas()
        for name in ("rule_set", "rule_clear", "rule_match"):
            self.assertIn(name, schemas)
        self.assertEqual(set(schemas["rule_set"]["required"]), {"operation_id", "treatment", "note", "set_by", "source"})
        out = StringIO()
        with redirect_stdout(out):
            main(["accrual", "capabilities"])
        caps = json.loads(out.getvalue())
        self.assertEqual(caps["verbs"]["rules"], ["set-preview", "set-apply", "clear-preview", "clear-apply", "match", "list"])
        out = StringIO()
        with redirect_stdout(out):
            main(["accrual", "schema"])
        self.assertTrue(any(line.startswith("accrual rules ") for line in json.loads(out.getvalue())["commands"]))


class MatchTests(RulesFixture):
    def test_supplier_rule_wins_over_a_company_rule(self):
        self.save(self.company(threshold="100.00"))
        self.save(self.supplier(treatment="deferred", accounts={"account": DEFERRED, "recognition_account": INCOME}, term_months=12))
        result = rules.match(self.entity, self.facts())
        self.assertEqual((result["status"], result["rule"]["scope"], result["rule"]["treatment"]), ("matched", "counterparty", "deferred"))
        self.assertEqual([c["scope"] for c in result["considered"]], ["counterparty", "company"])
        self.assertTrue(any("software-prepaid" in line and "not used" in line for line in result["why"]))

    def test_company_rule_applies_with_no_supplier_rule(self):
        self.save(self.company())
        result = rules.match(self.entity, self.facts(counterparty="Other Vendor", description="OTHER VENDOR"))
        self.assertEqual((result["status"], result["rule"]["id"]), ("matched", "software-prepaid"))
        self.assertIn("amount 1200.00 is at or above the threshold 1000.00", result["why"][0])
        self.assertIn("company rule software-prepaid", result["explanation"])
        self.assertIn("a person still approves", result["explanation"])

    def test_threshold_is_inclusive_and_uses_the_size_not_the_sign(self):
        self.save(self.company())
        for amount, expected in (("1000.00", "matched"), ("-1000.00", "matched"), ("999.99", "no_match")):
            with self.subTest(amount=amount):
                self.assertEqual(rules.match(self.entity, self.facts(amount=amount, counterparty="Other", description="OTHER"))["status"], expected)

    def test_supplier_rule_below_its_threshold_stops_company_rules(self):
        self.save(self.company(threshold="100.00"))
        self.save(self.supplier(threshold="500.00"))
        result = rules.match(self.entity, self.facts(amount="300.00"))
        self.assertEqual((result["status"], result["reason"], result["draft"]), ("no_match", "below_supplier_threshold", None))
        self.assertIn("ordinary expense", " ".join(result["why"]))
        self.assertEqual(rules.match(self.entity, self.facts(amount="500.00"))["status"], "matched")

    def test_most_specific_company_rule_wins(self):
        self.save(self.company(id="a-parent", match={"accounts": ["Expenses:Software"]}, threshold="100.00"))
        self.save(self.company(id="b-child", match={"accounts": ["Expenses:Software:Tools"]}, threshold="100.00", term_months=6))
        self.save(self.company(id="c-keyword", match={"accounts": ["Expenses:Software:Tools"], "keywords": ["figma"]}, threshold="100.00", term_months=3))
        facts = dict(counterparty="Vendor", description="VENDOR", account="Expenses:Software:Tools", amount="200.00")
        plain = rules.match(self.entity, self.facts(**facts))
        self.assertEqual(plain["rule"]["id"], "b-child")
        self.assertIn("a-parent", " ".join(plain["why"]))
        keyword = rules.match(self.entity, self.facts(**{**facts, "description": "FIGMA seats"}))
        self.assertEqual((keyword["rule"]["id"], keyword["rule"]["term_months"]), ("c-keyword", 3))
        parent_only = rules.match(self.entity, self.facts(**{**facts, "account": EXPENSE}))
        self.assertEqual(parent_only["rule"]["id"], "a-parent")

    def test_higher_threshold_is_more_specific_and_equal_rules_break_ties_by_id(self):
        self.save(self.company(id="low", threshold="100.00"))
        self.save(self.company(id="high", threshold="1000.00", term_months=24))
        facts = self.facts(counterparty="Vendor", description="VENDOR", amount="2000.00")
        self.assertEqual(rules.match(self.entity, facts)["rule"]["id"], "high")
        self.assertEqual(rules.match(self.entity, {**facts, "amount": "500.00"})["rule"]["id"], "low")
        self.save(self.company(id="zz-twin", threshold="1000.00", term_months=36))
        self.assertEqual(rules.match(self.entity, facts)["rule"]["id"], "high")

    def test_keyword_rule_matches_description_and_counterparty_without_case(self):
        self.save({k: v for k, v in self.company(id="cloud", match={"keywords": ["aws"]}).items() if k != "threshold"})
        hit = rules.match(self.entity, {"amount": "5.00", "description": "Amazon Web Services AWS invoice"})
        self.assertEqual(hit["rule"]["id"], "cloud")
        self.assertEqual(rules.match(self.entity, {"amount": "5.00", "counterparty": "Aws Inc"})["rule"]["id"], "cloud")
        self.assertEqual(rules.match(self.entity, {"amount": "5.00", "description": "Coffee"})["status"], "no_match")

    def test_a_rule_that_needs_an_account_does_not_apply_without_one(self):
        self.save(self.company())
        result = rules.match(self.entity, {"amount": "5000.00", "counterparty": "Vendor"})
        self.assertEqual(result["status"], "no_match")
        self.assertIn("name no account", result["considered"][0]["reason"])

    def test_category_is_accepted_as_the_account_fact(self):
        self.save(self.company())
        facts = {"amount": "2000.00", "category": EXPENSE, "counterparty": "V"}
        self.assertEqual(rules.match(self.entity, facts)["rule"]["id"], "software-prepaid")
        self.blocked("invalid_request", rules.match, {**facts, "account": "Expenses:Other"})

    def test_supplier_key_is_normalized_like_the_queue(self):
        self.save(self.supplier())
        for facts in ({"counterparty": "northwind hosting"}, {"counterparty": "Northwind Hosting 20260201"}, {"description": "NORTHWIND HOSTING 12345"}):
            with self.subTest(facts=facts):
                self.assertEqual(rules.match(self.entity, {"amount": "1200.00", **facts})["rule"]["counterparty"], HOSTING)

    def test_agent_rule_is_marked_in_the_explanation(self):
        self.save(self.supplier(source="agent", set_by="books-agent"))
        result = rules.match(self.entity, self.facts())
        self.assertIn("proposed by an agent, not yet confirmed by a person", result["explanation"])

    def test_malformed_stored_rule_is_ignored_and_listed_as_skipped(self):
        path = self.root / "learned-context"
        path.mkdir(exist_ok=True)
        (path / "counterparties.json").write_text(json.dumps({HOSTING: {"accrual": {"version": 1, "treatment": "magic"}}}))
        self.assertEqual(rules.match(self.entity, self.facts())["status"], "no_match")
        listing = rules.list_rules(self.entity)
        self.assertEqual((listing["count"], len(listing["skipped"])), (0, 1))

    def test_match_changes_nothing(self):
        self.save(self.supplier())
        self.save(self.company())
        files = {p: p.read_text() for p in (self.root / "learned-context").glob("*.json")}
        digest = self.store.content_digest()
        rules.match(self.entity, self.facts())
        rules.list_rules(self.entity)
        self.assertEqual(files, {p: p.read_text() for p in (self.root / "learned-context").glob("*.json")})
        self.assertEqual(digest, self.store.content_digest())


class DraftTests(RulesFixture):
    def test_schedule_draft_is_ready_and_previews_unchanged(self):
        self.save(self.supplier(accounts={"account": PREPAID, "recognition_account": EXPENSE}))
        draft = rules.match(self.entity, self.facts(account=EXPENSE))["draft"]
        self.assertEqual((draft["status"], draft["ready_for_preview"], draft["missing"]), ("draft", True, []))
        request = draft["request"]
        self.assertEqual((request["kind"], request["service_end"], request["method"], request["amount"]), ("prepaid", "2027-01-31", "equal_month", "1200.00"))
        self.assertEqual(request["origin"], {"date": "2026-02-01", "funding_account": BANK, "mode": "new", "source_id": "bank-nw-0201"})
        preview = preview_schedule(self.entity, request)
        self.assertEqual(self.store.counts().entries, 0)
        apply_schedule(self.entity, preview)
        self.assertEqual(self.store.counts().entries, 1)

    def test_draft_names_what_is_missing_and_offers_candidates(self):
        self.save(self.supplier())
        facts = {"counterparty": "Northwind Hosting", "amount": "1200.00", "account": EXPENSE}
        draft = rules.match(self.entity, facts)["draft"]
        fields = {row["field"]: row for row in draft["missing"]}
        self.assertFalse(draft["ready_for_preview"])
        self.assertEqual(set(fields), {"account", "service_start", "service_end", "origin.source_id", "evidence_ids"})
        self.assertEqual(fields["account"]["candidates"], ["Assets:Other:Prepaid-Expenses", PREPAID])

    def test_two_candidate_accounts_are_listed_not_guessed(self):
        add_account(self.root, "Assets:Prepaid:Insurance", open_date="2000-01-01")
        self.save(self.supplier())
        draft = rules.match(self.entity, self.facts())["draft"]
        missing = {row["field"]: row for row in draft["missing"]}
        self.assertNotIn("account", draft["request"])
        self.assertEqual(missing["account"]["candidates"], ["Assets:Other:Prepaid-Expenses", "Assets:Prepaid:Insurance", PREPAID])

    def test_document_service_dates_override_the_saved_term(self):
        self.save(self.supplier(accounts={"account": PREPAID, "recognition_account": EXPENSE}))
        draft = rules.match(self.entity, self.facts(service_end="2026-07-31"))["draft"]
        self.assertEqual(draft["request"]["service_end"], "2026-07-31")
        self.assertTrue(any("instead of the saved term" in note for note in draft["notes"]))

    def test_equal_month_on_partial_months_warns_instead_of_guessing(self):
        self.save(self.supplier(accounts={"account": PREPAID, "recognition_account": EXPENSE}))
        draft = rules.match(self.entity, self.facts(service_start="2026-02-15"))["draft"]
        self.assertEqual((draft["request"]["method"], draft["request"]["service_end"]), ("equal_month", "2027-02-14"))
        self.assertTrue(any("whole calendar months" in w for w in draft["warnings"]))

    def test_default_method_follows_the_dates(self):
        request = {k: v for k, v in self.supplier(accounts={"account": PREPAID, "recognition_account": EXPENSE}).items() if k != "method"}
        self.save(request)
        self.assertEqual(rules.match(self.entity, self.facts())["draft"]["request"]["method"], "equal_month")
        self.assertEqual(rules.match(self.entity, self.facts(service_start="2026-02-15"))["draft"]["request"]["method"], "actual_day")

    def test_depreciation_draft_uses_the_purchase_date_and_the_chart(self):
        self.save(self.company(id="equipment", match={"accounts": ["Expenses:Equipment", EQUIPMENT]}, treatment="depreciation",
                               term_months=36, threshold="2500.00", note="Equipment over 2,500 is depreciated over 36 months"))
        facts = {k: v for k, v in self.facts(counterparty="Laptop Store", description="LAPTOP", account=EQUIPMENT, amount="3000.00",
                                             date="2026-03-01").items() if k != "service_start"}
        draft = rules.match(self.entity, facts)["draft"]
        request = draft["request"]
        self.assertEqual((request["kind"], request["account"], request["contra_account"], request["recognition_account"]),
                         ("depreciation", EQUIPMENT, CONTRA, DEPRECIATION))
        self.assertEqual((request["service_start"], request["service_end"], request["method"]), ("2026-03-01", "2029-02-28", "equal_month"))
        self.assertTrue(any("purchase date" in w for w in draft["warnings"]))
        below = rules.match(self.entity, self.facts(counterparty="Laptop Store", description="LAPTOP", account=EQUIPMENT, amount="2400.00"))
        self.assertEqual(below["status"], "no_match")

    def test_deferred_draft_releases_into_income(self):
        self.save(self.supplier(counterparty="Big Customer", treatment="deferred", term_months=12, accounts={"account": DEFERRED, "recognition_account": INCOME}))
        draft = rules.match(self.entity, self.facts(counterparty="Big Customer", account=INCOME, amount="12000.00"))["draft"]
        self.assertEqual((draft["request"]["kind"], draft["request"]["account"], draft["request"]["recognition_account"]), ("deferred", DEFERRED, INCOME))

    def test_existing_payment_drafts_a_reclass(self):
        self.post("bank-nw-0201", "2026-02-01", [(EXPENSE, "1200.00"), (BANK, "-1200.00")])
        self.save(self.supplier(accounts={"account": PREPAID, "recognition_account": EXPENSE}))
        draft = rules.match(self.entity, {k: v for k, v in self.facts().items() if k != "funding_account"})["draft"]
        self.assertEqual(draft["request"]["origin"]["mode"], "existing")
        self.assertEqual(draft["request"]["origin"]["reclass_account"], EXPENSE)
        self.assertEqual(draft["missing"], [])

    def test_open_balance_drafts(self):
        for treatment, kind, account, counter, extra in (("bill", "payable", AP, EXPENSE, {}), ("invoice", "receivable", AR, INCOME, {}),
                                                         ("estimate", "accrual", ACCRUED, EXPENSE, {"estimate_basis": "Quoted at $450"})):
            with self.subTest(treatment=treatment):
                self.save({k: v for k, v in self.supplier(counterparty=f"{treatment} party", treatment=treatment,
                           accounts={"account": account, "recognition_account": counter}).items() if k not in ("term_months", "method")})
                facts = {"counterparty": f"{treatment} party", "amount": "450.00", "account": counter, "date": "2026-03-31",
                         "source_id": f"doc-{treatment}", "due_date": "2026-04-30", **extra}
                if treatment != "estimate":
                    facts["evidence_ids"] = [self.eid]
                draft = rules.match(self.entity, facts)["draft"]
                self.assertEqual((draft["ready_for_preview"], draft["request"]["kind"], draft["request"]["account"], draft["request"]["original_amount"]),
                                 (True, kind, account, "450.00"), draft["missing"])
                self.assertIn("item preview", draft["command"])
                self.assertEqual(draft["request"]["origin"]["counter_account"], counter)
                from bookkeeping.accrual.items import preview_item
                self.assertEqual(preview_item(self.entity, draft["request"])["kind"], "item_create")

    def test_estimate_without_a_basis_asks_for_one(self):
        self.save({k: v for k, v in self.supplier(treatment="estimate", accounts={"account": ACCRUED, "recognition_account": EXPENSE}).items() if k not in ("term_months", "method")})
        draft = rules.match(self.entity, {"counterparty": "Northwind Hosting", "amount": "450.00", "account": EXPENSE, "date": "2026-03-31", "source_id": "doc-e"})["draft"]
        self.assertEqual([row["field"] for row in draft["missing"]], ["estimate_basis"])

    def test_the_draft_has_stable_ids_for_the_same_facts(self):
        self.save(self.supplier(accounts={"account": PREPAID, "recognition_account": EXPENSE}))
        facts = {k: v for k, v in self.facts().items() if k != "source_id"}
        first, second = (rules.match(self.entity, facts)["draft"]["request"] for _ in range(2))
        self.assertEqual((first["id"], first["operation_id"]), (second["id"], second["operation_id"]))


class NothingPostsFromARuleTests(RulesFixture):
    def test_saving_clearing_and_matching_never_touch_the_ledger_or_the_queue(self):
        digest, counts = self.store.content_digest(), self.store.counts()
        self.save(self.supplier())
        self.save(self.company())
        rules.match(self.entity, self.facts())
        self.save({"operation_id": self.op(), "counterparty": HOSTING, "cleared_by": "gil", "source": "person"}, "clear")
        self.assertEqual((self.store.content_digest(), self.store.counts()), (digest, counts))
        self.assertEqual(list((self.root / "review-queue").glob("*.json")) if (self.root / "review-queue").exists() else [], [])

    def test_a_matching_proposal_names_the_rule_but_stays_open_and_unposted(self):
        self.save(self.supplier(accounts={"account": PREPAID, "recognition_account": EXPENSE}))
        (self.root / "staging").mkdir(exist_ok=True)
        _add_pending_categorization(self.entity, "nw-1", "Northwind Hosting", amount="-1200.00", txn_date="2026-02-01")
        digest = self.store.content_digest()
        item = propose(self.entity, "nw-1", EXPENSE, "Hosting invoice", "Annual plan")
        self.assertEqual(item["status"], "open")
        self.assertEqual((item["accrual_rule"]["scope"], item["accrual_rule"]["treatment"], item["accrual_rule"]["term_months"]),
                         ("counterparty", "prepaid", 12))
        self.assertTrue(item["context"].startswith("Saved accrual rule (supplier rule for NORTHWIND HOSTING"))
        self.assertTrue(item["context"].endswith("Annual plan"))
        self.assertEqual(read_queue_item(self.entity, "nw-1")["accrual_rule"], item["accrual_rule"])
        self.assertEqual(self.store.content_digest(), digest)
        self.assertFalse(eligible_for_autopost(self.entity, HOSTING))
        self.assertEqual(load_learned_context(self.entity)[HOSTING]["confirmed_count"], 0)

    def test_proposals_without_a_matching_rule_are_unchanged(self):
        (self.root / "staging").mkdir(exist_ok=True)
        _add_pending_categorization(self.entity, "other-1", "Coffee Shop", amount="-5.00")
        item = propose(self.entity, "other-1", "Expenses:Software", "Coffee", "ctx")
        self.assertNotIn("accrual_rule", item)
        self.assertEqual(item["context"], "ctx")

    def test_a_failing_rule_lookup_never_blocks_a_proposal(self):
        (self.root / "staging").mkdir(exist_ok=True)
        _add_pending_categorization(self.entity, "nw-2", "Northwind Hosting", amount="-1200.00")
        with patch.object(rules, "match", side_effect=RuntimeError("boom")):
            item = propose(self.entity, "nw-2", EXPENSE, "r", "c")
        self.assertEqual((item["status"], item["context"]), ("open", "c"))


class CommandSurfaceTests(RulesFixture):
    def cli(self, *argv):
        out = StringIO()
        with redirect_stdout(out):
            status = main(["accrual", *argv])
        return status, json.loads(out.getvalue()) if out.getvalue().lstrip().startswith("{") else out.getvalue()

    def test_json_round_trip_through_the_cli(self):
        request, preview = self.root / "request.json", self.root / "preview.json"
        request.write_text(json.dumps(self.supplier()))
        status, plan = self.cli("rules", "set-preview", "--entity", str(self.root), "--input", str(request))
        self.assertEqual(status, 0)
        preview.write_text(json.dumps(plan))
        status, receipt = self.cli("rules", "set-apply", "--entity", str(self.root), "--input", str(preview))
        self.assertEqual((status, receipt["status"]), (0, "applied"))
        status, listing = self.cli("rules", "list", "--entity", str(self.root))
        self.assertEqual((status, listing["count"]), (0, 1))
        status, text = self.cli("rules", "list", "--entity", str(self.root), "--format", "text")
        self.assertIn("a prepaid expense over 12 months", text)
        request.write_text(json.dumps(self.facts()))
        status, found = self.cli("rules", "match", "--entity", str(self.root), "--input", str(request))
        self.assertEqual((status, found["status"]), (0, "matched"))
        request.write_text(json.dumps({"operation_id": "bad", "counterparty": "x", "treatment": "nope"}))
        status, blocked = self.cli("rules", "set-preview", "--entity", str(self.root), "--input", str(request))
        self.assertEqual((status, blocked["status"]), (1, "blocked"))

    def test_remote_contract_classifies_reads_and_writes(self):
        base = ["--entity", "@entity", "--input", "@input/r.json"]
        for verb, mutates in (("set-preview", False), ("clear-preview", False), ("match", False), ("set-apply", True), ("clear-apply", True)):
            with self.subTest(verb=verb):
                spec = command_spec(["accrual", "rules", verb, *base])
                self.assertEqual((spec["mutates"], spec["input_fields"], spec["entity_fields"]), (mutates, ["input"], ["entity"]))
        spec = command_spec(["accrual", "rules", "list", "--entity", "@entity"])
        self.assertEqual((spec["mutates"], spec["input_fields"]), (False, []))

    def test_preparation_runtime_allows_reads_and_never_saves_a_rule(self):
        base = ["--entity", "@entity", "--input", "@input/r.json"]
        for verb in ("set-preview", "clear-preview", "match"):
            self.assertEqual(supported_preparation_command(["accrual", "rules", verb, *base]).effect, "read")
        self.assertEqual(supported_preparation_command(["accrual", "rules", "list", "--entity", "@entity"]).effect, "read")
        for verb in ("set-apply", "clear-apply"):
            with self.subTest(verb=verb), self.assertRaises(PreparationDenied):
                supported_preparation_command(["accrual", "rules", verb, *base])


if __name__ == "__main__":
    unittest.main()
