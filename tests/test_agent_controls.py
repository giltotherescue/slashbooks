from __future__ import annotations

"""Caller controls on the review queue: creator, self-approval, ask-first amount, amounts in results."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bookkeeping import cli  # noqa: E402
from bookkeeping import queue as queue_module  # noqa: E402
from bookkeeping.queue import (  # noqa: E402
    make_categorizer, confirm, confirm_group, confirm_split, confirm_transfer, correct,
    propose, propose_group, propose_split, propose_transfer, read_queue_item,
)
from test_queue import TS, _add_pending_categorization, _make_entity  # noqa: E402


def _run_cli(*argv: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ControlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.entity = _make_entity(Path(self.tmp.name))
        # Never inherit a control from the developer shell.
        patcher = patch.dict(os.environ, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("SLASHBOOKS_ACTOR", "SLASHBOOKS_FORBID_SELF_APPROVAL", "SLASHBOOKS_ENFORCE_ASK_FIRST"):
            os.environ.pop(name, None)

    def pending(self, source_id: str, amount: str = "100.00", **kwargs) -> None:
        _add_pending_categorization(self.entity, source_id, kwargs.pop("description", "Vendor " + source_id),
                                    amount=amount, **kwargs)

    def ledger_count(self) -> int:
        from bookkeeping.ledger.store import LedgerStore, default_store_path
        store = LedgerStore(default_store_path(self.entity.path))
        with store.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]


class CreatorTests(ControlTestCase):
    def test_no_actor_leaves_item_without_creator(self) -> None:
        self.pending("a1")
        item = propose(self.entity, "a1", "Expenses:Software", "r")
        self.assertNotIn("created_by", item)
        self.assertNotIn("created_by", read_queue_item(self.entity, "a1"))

    def test_actor_is_recorded_on_every_proposal_kind(self) -> None:
        os.environ["SLASHBOOKS_ACTOR"] = "agent:claude-7"
        self.pending("s1")
        self.pending("g1")
        self.pending("g2")
        self.pending("sp1", amount="-100.00")
        self.pending("t1", amount="-50.00", txn_date="2026-02-01")
        self.pending("t2", amount="50.00", txn_date="2026-02-01")
        pending_path = self.entity.staging_dir / "pending-categorization.json"
        rows = json.loads(pending_path.read_text(encoding="utf-8"))
        rows[-1]["accountName"] = "Savings"
        pending_path.write_text(json.dumps(rows), encoding="utf-8")
        single = propose(self.entity, "s1", "Expenses:Software", "r")
        group = propose_group(self.entity, ["g1", "g2"], "Expenses:Fees", "r")
        split = propose_split(self.entity, "sp1", "r", ["Expenses:Software=100.00"])
        transfer = propose_transfer(self.entity, ["t1", "t2"], "r")
        for item in [single, *group, split, transfer]:
            self.assertEqual(item["created_by"], "agent:claude-7")
            self.assertEqual(read_queue_item(self.entity, item["source_id"])["created_by"], "agent:claude-7")

    def test_actor_is_validated(self) -> None:
        self.pending("a1")
        for bad in ("x" * 201, "line\nbreak", "tab\there"):
            os.environ["SLASHBOOKS_ACTOR"] = bad
            with self.assertRaises(queue_module.InvalidActorError):
                propose(self.entity, "a1", "Expenses:Software", "r")
        os.environ["SLASHBOOKS_ACTOR"] = "x" * 200
        self.assertEqual(propose(self.entity, "a1", "Expenses:Software", "r")["created_by"], "x" * 200)

    def test_empty_actor_counts_as_unset(self) -> None:
        os.environ["SLASHBOOKS_ACTOR"] = ""
        self.pending("a1")
        self.assertNotIn("created_by", propose(self.entity, "a1", "Expenses:Software", "r"))

    def test_creator_appears_in_list_and_show_json(self) -> None:
        self.pending("a1")
        code, _, _ = _run_cli("queue", "propose", "--entity", str(self.entity.path), "--source-id", "a1",
                              "--category", "Expenses:Software", "--reasoning", "r",
                              env={"SLASHBOOKS_ACTOR": "agent:one"})
        self.assertEqual(code, 0)
        code, out, _ = _run_cli("queue", "show", "--entity", str(self.entity.path), "--item", "a1")
        self.assertEqual(json.loads(out)["created_by"], "agent:one")
        code, out, _ = _run_cli("queue", "list", "--entity", str(self.entity.path), "--json")
        document = json.loads(out)
        self.assertEqual(document["count"], 1)
        self.assertEqual(document["items"][0]["created_by"], "agent:one")

    def test_list_text_output_is_unchanged(self) -> None:
        self.pending("a1")
        propose(self.entity, "a1", "Expenses:Software", "r")
        code, out, _ = _run_cli("queue", "list", "--entity", str(self.entity.path))
        self.assertEqual(out, "  [open] a1  2026-01-10  Expenses:Software\n")


class SelfApprovalTests(ControlTestCase):
    def propose_as(self, source_id: str, actor: str | None, **kwargs) -> dict:
        if actor:
            os.environ["SLASHBOOKS_ACTOR"] = actor
        else:
            os.environ.pop("SLASHBOOKS_ACTOR", None)
        return propose(self.entity, source_id, "Expenses:Software", "r")

    def test_refused_without_write_when_actor_is_creator(self) -> None:
        self.pending("a1")
        self.propose_as("a1", "agent:one")
        os.environ["SLASHBOOKS_FORBID_SELF_APPROVAL"] = "1"
        before = self.ledger_count()
        with self.assertRaises(queue_module.SelfApprovalError) as raised:
            confirm(self.entity, "a1", "s", ts=TS)
        self.assertEqual(raised.exception.code, "self_approval")
        self.assertEqual(self.ledger_count(), before)
        self.assertEqual(read_queue_item(self.entity, "a1")["status"], "open")
        self.assertIsNotNone(queue_module._get_pending_txn(self.entity, "a1"))

    def test_other_actor_and_no_actor_may_confirm(self) -> None:
        self.pending("a1")
        self.pending("a2")
        self.propose_as("a1", "agent:one")
        self.propose_as("a2", "agent:one")
        os.environ["SLASHBOOKS_FORBID_SELF_APPROVAL"] = "1"
        os.environ["SLASHBOOKS_ACTOR"] = "person:two"
        self.assertEqual(confirm(self.entity, "a1", "s", ts=TS)["status"], "confirmed")
        os.environ.pop("SLASHBOOKS_ACTOR")
        with self.assertRaises(queue_module.InvalidActorError):
            confirm(self.entity, "a2", "s", ts=TS)
        os.environ.pop("SLASHBOOKS_FORBID_SELF_APPROVAL")
        self.assertEqual(confirm(self.entity, "a2", "s", ts=TS)["status"], "confirmed")

    def test_item_without_creator_is_unaffected(self) -> None:
        self.pending("a1")
        self.propose_as("a1", None)
        os.environ["SLASHBOOKS_FORBID_SELF_APPROVAL"] = "1"
        os.environ["SLASHBOOKS_ACTOR"] = "agent:one"
        self.assertEqual(confirm(self.entity, "a1", "s", ts=TS)["status"], "confirmed")

    def test_without_the_flag_self_approval_is_allowed(self) -> None:
        self.pending("a1")
        self.propose_as("a1", "agent:one")
        self.assertEqual(confirm(self.entity, "a1", "s", ts=TS)["status"], "confirmed")

    def test_flag_value_other_than_one_does_not_enable(self) -> None:
        self.pending("a1")
        self.propose_as("a1", "agent:one")
        os.environ["SLASHBOOKS_FORBID_SELF_APPROVAL"] = "true"
        self.assertEqual(confirm(self.entity, "a1", "s", ts=TS)["status"], "confirmed")

    def test_split_transfer_group_and_correct_are_covered(self) -> None:
        os.environ["SLASHBOOKS_ACTOR"] = "agent:one"
        self.pending("sp1", amount="-100.00")
        propose_split(self.entity, "sp1", "r", ["Expenses:Software=100.00"])
        self.pending("t1", amount="-50.00", txn_date="2026-02-01")
        self.pending("t2", amount="50.00", txn_date="2026-02-01")
        rows = json.loads((self.entity.staging_dir / "pending-categorization.json").read_text(encoding="utf-8"))
        rows[-1]["accountName"] = "Savings"
        (self.entity.staging_dir / "pending-categorization.json").write_text(json.dumps(rows), encoding="utf-8")
        transfer = propose_transfer(self.entity, ["t1", "t2"], "r")
        self.pending("g1")
        self.pending("g2")
        propose_group(self.entity, ["g1", "g2"], "Expenses:Fees", "r")
        self.pending("c1")
        propose(self.entity, "c1", "Expenses:Software", "r")
        os.environ["SLASHBOOKS_FORBID_SELF_APPROVAL"] = "1"
        before = self.ledger_count()
        for call in (
            lambda: confirm_split(self.entity, "sp1", "s", ts=TS),
            lambda: confirm_transfer(self.entity, transfer["source_id"], "s", ts=TS),
            lambda: confirm_group(self.entity, "Expenses:Fees", "s", ts=TS),
            lambda: correct(self.entity, "c1", "Expenses:Fees", "n", "s", ts=TS),
        ):
            with self.assertRaises(queue_module.SelfApprovalError):
                call()
        self.assertEqual(self.ledger_count(), before)

    def test_group_refusal_leaves_no_partial_write(self) -> None:
        self.pending("g1")
        self.pending("g2")
        os.environ["SLASHBOOKS_ACTOR"] = "person:two"
        propose(self.entity, "g1", "Expenses:Fees", "r")
        os.environ["SLASHBOOKS_ACTOR"] = "agent:one"
        propose(self.entity, "g2", "Expenses:Fees", "r")
        os.environ["SLASHBOOKS_FORBID_SELF_APPROVAL"] = "1"
        with self.assertRaises(queue_module.SelfApprovalError):
            confirm_group(self.entity, "Expenses:Fees", "s", ts=TS)
        self.assertEqual(self.ledger_count(), 0)
        self.assertEqual(read_queue_item(self.entity, "g1")["status"], "open")

    def test_cli_prints_code_and_exits_non_zero(self) -> None:
        self.pending("a1")
        self.propose_as("a1", "agent:one")
        code, out, err = _run_cli("queue", "confirm", "--entity", str(self.entity.path), "--item", "a1",
                                  env={"SLASHBOOKS_ACTOR": "agent:one", "SLASHBOOKS_FORBID_SELF_APPROVAL": "1"})
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["error"]["code"], "self_approval")
        self.assertEqual(read_queue_item(self.entity, "a1")["status"], "open")


def _set_ask_first(entity, value) -> None:
    entity.trust_policy["ask_first_above"] = value
    (entity.path / "trust-policy.json").write_text(json.dumps(entity.trust_policy) + "\n", encoding="utf-8")


def _learn(entity, key: str = "VENDOR A", category: str = "Expenses:Software") -> None:
    for _ in range(3):
        queue_module._update_learned_context(entity, key, category, corrected=False)


class AskFirstAutopostTests(ControlTestCase):
    def test_no_setting_changes_nothing(self) -> None:
        _learn(self.entity)
        self.assertTrue(queue_module.eligible_for_autopost(self.entity, "VENDOR A", {"id": "x", "amount": "999999.00"}))
        self.assertIsNone(queue_module.ask_first_above(self.entity))

    def test_amount_above_holds_even_when_a_learned_rule_matches(self) -> None:
        _learn(self.entity)
        _set_ask_first(self.entity, "500.00")
        check = lambda row: False  # noqa: E731
        eligible = lambda amount: queue_module.eligible_for_autopost(  # noqa: E731
            self.entity, "VENDOR A", {"id": "x", "amount": amount}, treatment_check=check)
        self.assertTrue(eligible("500.00"))
        self.assertTrue(eligible("-499.99"))
        self.assertFalse(eligible("500.01"))
        self.assertFalse(eligible("-500.01"))

    def test_unreadable_amount_and_missing_transaction_are_held(self) -> None:
        _learn(self.entity)
        _set_ask_first(self.entity, "500")
        check = lambda row: False  # noqa: E731
        for amount in ("abc", "NaN", "Infinity", None):
            row = {"id": "x"} if amount is None else {"id": "x", "amount": amount}
            self.assertFalse(queue_module.eligible_for_autopost(self.entity, "VENDOR A", row, treatment_check=check), amount)
        self.assertFalse(queue_module.eligible_for_autopost(self.entity, "VENDOR A"))

    def test_unreadable_setting_holds_every_nonzero_amount(self) -> None:
        _learn(self.entity)
        check = lambda row: False  # noqa: E731
        for bad in ("lots", -5, True, "NaN"):
            _set_ask_first(self.entity, bad)
            self.assertFalse(queue_module.eligible_for_autopost(
                self.entity, "VENDOR A", {"id": "x", "amount": "0.01"}, treatment_check=check), bad)

    def test_import_holds_rule_match_above_amount_with_reason(self) -> None:
        _learn(self.entity)
        _set_ask_first(self.entity, "500.00")
        rows = [
            {"id": "small", "date": "2026-01-10", "amount": "-100.00", "description": "VENDOR A", "accountName": "Checking"},
            {"id": "large", "date": "2026-01-10", "amount": "-900.00", "description": "VENDOR A", "accountName": "Checking"},
        ]
        from bookkeeping.ledger.importer import import_transactions
        result = import_transactions(self.entity, rows, "imp-1", make_categorizer(self.entity), ts=TS)
        self.assertEqual(result.new_entries, 1)
        self.assertEqual(result.pending_categorization, 1)
        pending = queue_module._load_pending_categorization(self.entity)
        self.assertEqual([row["id"] for row in pending], ["large"])
        self.assertEqual(pending[0]["review_hold_reason"], "above_ask_first_amount")

    def test_intake_only_import_marks_the_reason_without_a_categorizer(self) -> None:
        _set_ask_first(self.entity, "50")
        rows = [{"id": "r1", "date": "2026-01-10", "amount": "75.00", "description": "VENDOR B", "accountName": "Checking"},
                {"id": "r2", "date": "2026-01-10", "amount": "5.00", "description": "VENDOR B", "accountName": "Checking"}]
        from bookkeeping.ledger.importer import import_transactions
        import_transactions(self.entity, rows, "imp-2", None, ts=TS)
        pending = {row["id"]: row for row in queue_module._load_pending_categorization(self.entity)}
        self.assertEqual(pending["r1"]["review_hold_reason"], "above_ask_first_amount")
        self.assertNotIn("review_hold_reason", pending["r2"])

    def test_no_setting_leaves_pending_rows_without_a_reason(self) -> None:
        rows = [{"id": "r1", "date": "2026-01-10", "amount": "75.00", "description": "VENDOR B", "accountName": "Checking"}]
        from bookkeeping.ledger.importer import import_transactions
        import_transactions(self.entity, rows, "imp-3", None, ts=TS)
        self.assertNotIn("review_hold_reason", queue_module._load_pending_categorization(self.entity)[0])

    def test_routine_rule_match_above_amount_is_unresolved(self) -> None:
        from bookkeeping.entity import load_entity
        from bookkeeping.routine import routine_plan
        config = json.loads((self.entity.path / "entity.json").read_text(encoding="utf-8"))
        config["bank_account_mappings"] = {"checking": "Assets:Bank:Checking"}
        (self.entity.path / "entity.json").write_text(json.dumps(config), encoding="utf-8")
        rows = [{"id": f"r{i}", "date": "2026-01-10", "amount": amount, "description": "VENDOR A", "counterparty": "VENDOR A",
                 "accountId": "checking", "accountName": "Checking", "accountType": "checking", "currency": "USD"}
                for i, amount in enumerate(("-100.00", "-900.00"))]
        from bookkeeping.ledger.importer import import_transactions
        import_transactions(load_entity(self.entity.path), rows, "imp", None, ts=TS)
        policy = {"version": 1, "enabled": True, "rules": [{"id": "rule", "source_account_id": "checking",
                  "counterparty_key": "VENDOR A", "category": "Expenses:Software",
                  "approved_ledger_account": "Assets:Bank:Checking"}],
                  "review_categories": ["Expenses:Software"], "answer_roles": ["client"]}
        before = routine_plan(load_entity(self.entity.path), policy)
        self.assertEqual(len(before["actions"]), 2)
        _set_ask_first(self.entity, "500.00")
        plan = routine_plan(load_entity(self.entity.path), policy)
        self.assertEqual([action["source_id"] for action in plan["actions"]], ["r0"])
        self.assertEqual([(row["source_id"], row["reason"]) for row in plan["unresolved"]],
                         [("r1", "above_ask_first_amount")])

    def test_lower_of_company_setting_and_trusted_policy_applies(self) -> None:
        _set_ask_first(self.entity, "500")
        with patch.object(queue_module, "_boundary_ask_first", return_value=Decimal("100")):
            self.assertEqual(queue_module.ask_first_above(self.entity), Decimal("100"))
        with patch.object(queue_module, "_boundary_ask_first", return_value=Decimal("900")):
            self.assertEqual(queue_module.ask_first_above(self.entity), Decimal("500"))
        with patch.object(queue_module, "_boundary_ask_first", return_value=Decimal("100")):
            self.assertEqual(queue_module.ask_first_above(_make_entity(Path(self.tmp.name) / "other")), Decimal("100"))


class AskFirstEnforcementTests(ControlTestCase):
    def setUp(self) -> None:
        super().setUp()
        _set_ask_first(self.entity, "500.00")

    def test_not_enforced_without_the_flag(self) -> None:
        self.pending("big", amount="900.00")
        self.assertEqual(propose(self.entity, "big", "Expenses:Software", "r")["status"], "open")
        self.assertEqual(confirm(self.entity, "big", "s", ts=TS)["status"], "confirmed")

    def test_propose_and_confirm_refused_above_the_amount(self) -> None:
        self.pending("big", amount="900.00")
        self.pending("ok", amount="500.00")
        os.environ["SLASHBOOKS_ENFORCE_ASK_FIRST"] = "1"
        with self.assertRaises(queue_module.AskFirstError) as raised:
            propose(self.entity, "big", "Expenses:Software", "r")
        self.assertEqual(raised.exception.code, "above_ask_first_amount")
        self.assertFalse((self.entity.review_queue_dir / "big.json").exists())
        propose(self.entity, "ok", "Expenses:Software", "r")
        # A suggestion made before the limit existed still waits for a person.
        os.environ.pop("SLASHBOOKS_ENFORCE_ASK_FIRST")
        propose(self.entity, "big", "Expenses:Software", "r")
        os.environ["SLASHBOOKS_ENFORCE_ASK_FIRST"] = "1"
        before = self.ledger_count()
        with self.assertRaises(queue_module.AskFirstError):
            confirm(self.entity, "big", "s", ts=TS)
        with self.assertRaises(queue_module.AskFirstError):
            correct(self.entity, "big", "Expenses:Fees", "", "s", ts=TS)
        self.assertEqual(self.ledger_count(), before)
        self.assertEqual(confirm(self.entity, "ok", "s", ts=TS)["status"], "confirmed")

    def test_group_is_refused_as_a_whole(self) -> None:
        self.pending("g1", amount="10.00")
        self.pending("g2", amount="-900.00")
        os.environ["SLASHBOOKS_ENFORCE_ASK_FIRST"] = "1"
        with self.assertRaises(queue_module.AskFirstError):
            propose_group(self.entity, ["g1", "g2"], "Expenses:Fees", "r")
        self.assertEqual(list(self.entity.review_queue_dir.glob("*.json")), [])

    def test_confirm_group_is_checked_before_any_write(self) -> None:
        self.pending("g1", amount="10.00")
        self.pending("g2", amount="900.00")
        propose_group(self.entity, ["g1", "g2"], "Expenses:Fees", "r")
        os.environ["SLASHBOOKS_ENFORCE_ASK_FIRST"] = "1"
        with self.assertRaises(queue_module.AskFirstError):
            confirm_group(self.entity, "Expenses:Fees", "s", ts=TS)
        self.assertEqual(self.ledger_count(), 0)

    def test_split_compares_the_source_amount(self) -> None:
        self.pending("sp1", amount="-900.00")
        os.environ["SLASHBOOKS_ENFORCE_ASK_FIRST"] = "1"
        with self.assertRaises(queue_module.AskFirstError):
            propose_split(self.entity, "sp1", "r", ["Expenses:Software=900.00"])
        self.pending("sp2", amount="-400.00")
        item = propose_split(self.entity, "sp2", "r", ["Expenses:Software=100.00", "Expenses:Fees=300.00"])
        self.assertEqual(queue_module.amount_for_item(self.entity, item["source_id"]), Decimal("400.00"))

    def test_transfer_compares_the_absolute_amount(self) -> None:
        self.pending("t1", amount="-900.00", txn_date="2026-02-01")
        self.pending("t2", amount="900.00", txn_date="2026-02-01")
        rows = json.loads((self.entity.staging_dir / "pending-categorization.json").read_text(encoding="utf-8"))
        rows[-1]["accountName"] = "Savings"
        (self.entity.staging_dir / "pending-categorization.json").write_text(json.dumps(rows), encoding="utf-8")
        os.environ["SLASHBOOKS_ENFORCE_ASK_FIRST"] = "1"
        with self.assertRaises(queue_module.AskFirstError):
            propose_transfer(self.entity, ["t1", "t2"], "r")
        os.environ.pop("SLASHBOOKS_ENFORCE_ASK_FIRST")
        item = propose_transfer(self.entity, ["t1", "t2"], "r")
        self.assertEqual(queue_module.amount_for_item(self.entity, item["source_id"]), Decimal("900.00"))
        os.environ["SLASHBOOKS_ENFORCE_ASK_FIRST"] = "1"
        with self.assertRaises(queue_module.AskFirstError):
            confirm_transfer(self.entity, item["source_id"], "s", ts=TS)

    def test_unreadable_amount_is_refused(self) -> None:
        self.pending("bad", amount="abc")
        os.environ["SLASHBOOKS_ENFORCE_ASK_FIRST"] = "1"
        with self.assertRaises(queue_module.AskFirstError):
            propose(self.entity, "bad", "Expenses:Software", "r")

    def test_trusted_policy_enforces_without_the_flag(self) -> None:
        self.pending("big", amount="900.00")
        with patch.object(queue_module, "_boundary_ask_first", return_value=Decimal("500.00")), \
                patch("bookkeeping.preparation_runtime.is_preparation", return_value=True):
            with self.assertRaises(queue_module.AskFirstError):
                propose(self.entity, "big", "Expenses:Software", "r")

    def test_cli_prints_the_code(self) -> None:
        self.pending("big", amount="900.00")
        code, out, err = _run_cli("queue", "propose", "--entity", str(self.entity.path), "--source-id", "big",
                                  "--category", "Expenses:Software", "--reasoning", "r",
                                  env={"SLASHBOOKS_ENFORCE_ASK_FIRST": "1"})
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)["error"]["code"], "above_ask_first_amount")


class AmountForItemTests(ControlTestCase):
    def test_category_item_uses_the_live_source_then_the_recorded_amount(self) -> None:
        self.pending("a1", amount="-42.50")
        propose(self.entity, "a1", "Expenses:Software", "r")
        self.assertEqual(queue_module.amount_for_item(self.entity, "a1"), Decimal("42.50"))
        confirm(self.entity, "a1", "s", ts=TS)
        self.assertEqual(queue_module.amount_for_item(self.entity, "a1"), Decimal("42.50"))

    def test_unreadable_amount_gives_none_and_missing_item_raises(self) -> None:
        self.pending("a1", amount="-42.50")
        propose(self.entity, "a1", "Expenses:Software", "r")
        path = self.entity.review_queue_dir / "a1.json"
        item = json.loads(path.read_text(encoding="utf-8"))
        item["amount"] = "unknown"
        path.write_text(json.dumps(item), encoding="utf-8")
        queue_module._remove_from_pending_categorization(self.entity, "a1")
        self.assertIsNone(queue_module.amount_for_item(self.entity, "a1"))
        with self.assertRaises(FileNotFoundError):
            queue_module.amount_for_item(self.entity, "nope")


class StandingPolicyAskFirstTests(unittest.TestCase):
    BASE = {"schema_version": 1, "policy_id": "p1", "version": 1, "entity_key": "e1",
            "start_period": "2026-01", "end_period": "2026-12", "actions": ["categorize"],
            "held_source_ids": [], "held_entry_ids": []}

    def test_field_is_optional_and_keeps_the_old_fingerprint(self) -> None:
        from bookkeeping.automation_policy import StandingPolicy
        import hashlib
        policy = StandingPolicy.from_document(self.BASE)
        self.assertIsNone(policy.ask_first_above)
        legacy = {"schema_version": 1, "policy_id": "p1", "version": 1, "entity_key": "e1", "start_period": "2026-01",
                  "end_period": "2026-12", "actions": ["categorize"], "held_source_ids": [], "held_entry_ids": []}
        expected = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(policy.fingerprint, expected)

    def test_amount_is_parsed_and_changes_the_fingerprint(self) -> None:
        from bookkeeping.automation_policy import StandingPolicy
        base = StandingPolicy.from_document(self.BASE)
        policy = StandingPolicy.from_document({**self.BASE, "ask_first_above": "500"})
        self.assertEqual(policy.ask_first_above, Decimal("500.00"))
        self.assertNotEqual(policy.fingerprint, base.fingerprint)
        self.assertEqual(StandingPolicy.from_document({**self.BASE, "ask_first_above": 500}).fingerprint, policy.fingerprint)
        self.assertEqual(StandingPolicy.from_document({**self.BASE, "ask_first_above": "0"}).ask_first_above, Decimal("0.00"))

    def test_bad_amounts_are_refused(self) -> None:
        from bookkeeping.automation_policy import PolicyError, StandingPolicy
        for bad in (-1, "-1", "abc", "1.005", 1.5, True, "NaN", "Infinity", [], {}):
            with self.assertRaises(PolicyError, msg=repr(bad)):
                StandingPolicy.from_document({**self.BASE, "ask_first_above": bad})

    def test_unknown_fields_are_still_refused(self) -> None:
        from bookkeeping.automation_policy import PolicyError, StandingPolicy
        with self.assertRaises(PolicyError):
            StandingPolicy.from_document({**self.BASE, "extra": 1})

    def test_execution_context_round_trips_the_amount(self) -> None:
        from datetime import datetime, timezone
        from bookkeeping.automation_policy import ExecutionContext, StandingPolicy
        policy = StandingPolicy.from_document({**self.BASE, "ask_first_above": "250.50"})
        context = ExecutionContext("e1", "x1", policy, 1, 3, 3, datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(context.policy.ask_first_above, Decimal("250.50"))


class PreparationBoundaryAskFirstTests(ControlTestCase):
    def test_standing_policy_limit_applies_inside_the_trusted_boundary(self) -> None:
        from datetime import datetime, timedelta, timezone
        from bookkeeping.automation_policy import ExecutionContext, StandingPolicy
        from bookkeeping.ledger.store import LedgerStore
        from bookkeeping.preparation_runtime import evidence_hash, run_preparation_command
        policy = StandingPolicy.from_document({
            "schema_version": 1, "policy_id": "p", "version": 1, "entity_key": "co", "start_period": "2026-01",
            "end_period": "2026-12", "actions": ["categorize"], "held_source_ids": [], "held_entry_ids": [],
            "ask_first_above": "500.00"})
        context = ExecutionContext("co", "run-1", policy, 1, 1, 1, datetime.now(timezone.utc) + timedelta(minutes=5))
        store = LedgerStore(self.entity.path / "ledger.sqlite")
        results = {}
        for source_id, amount in (("small", "100.00"), ("large", "900.00")):
            self.pending(source_id, amount=amount)
            argv = ["queue", "propose", "--entity", str(self.entity.path), "--source-id", source_id,
                    "--category", "Expenses:Software", "--reasoning", "Looks right."]
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                results[source_id] = (run_preparation_command(
                    argv, context, entity_path=self.entity.path, expected_ledger_digest=store.content_digest(),
                    source_hashes={source_id: evidence_hash(queue_module._get_pending_txn(self.entity, source_id))}),
                    err.getvalue())
        self.assertEqual(results["small"][0], 0)
        self.assertNotEqual(results["large"][0], 0)
        self.assertEqual(json.loads(results["large"][1])["error"]["code"], "above_ask_first_amount")
        self.assertEqual(sorted(path.stem for path in self.entity.review_queue_dir.glob("*.json")), ["small"])


class ReviewFixTests(ControlTestCase):
    def test_boundary_enforces_the_lower_of_company_and_policy_limits(self) -> None:
        self.pending("mid", amount="300.00")
        self.pending("big", amount="900.00")
        in_boundary = patch("bookkeeping.preparation_runtime.is_preparation", return_value=True)
        # Company limit lower than the policy limit.
        _set_ask_first(self.entity, "200.00")
        with patch.object(queue_module, "_boundary_ask_first", return_value=Decimal("500.00")), in_boundary:
            with self.assertRaises(queue_module.AskFirstError):
                propose(self.entity, "mid", "Expenses:Software", "r")
        # Policy limit lower than the company limit.
        _set_ask_first(self.entity, "800.00")
        with patch.object(queue_module, "_boundary_ask_first", return_value=Decimal("250.00")), in_boundary:
            with self.assertRaises(queue_module.AskFirstError):
                propose(self.entity, "mid", "Expenses:Software", "r")
        # Only the company limit is set; the policy has none.
        _set_ask_first(self.entity, "200.00")
        with patch.object(queue_module, "_boundary_ask_first", return_value=None), in_boundary:
            with self.assertRaises(queue_module.AskFirstError):
                propose(self.entity, "mid", "Expenses:Software", "r")
        # Confirm enforces both limits as well.
        _set_ask_first(self.entity, "1000.00")
        propose(self.entity, "mid", "Expenses:Software", "r")
        _set_ask_first(self.entity, "200.00")
        with patch.object(queue_module, "_boundary_ask_first", return_value=None), in_boundary:
            with self.assertRaises(queue_module.AskFirstError):
                confirm(self.entity, "mid", "s", ts=TS)
        self.assertEqual(self.ledger_count(), 0)

    def test_null_company_limit_means_no_limit(self) -> None:
        _set_ask_first(self.entity, None)
        self.assertIsNone(queue_module.ask_first_above(self.entity))
        _learn(self.entity)
        self.assertTrue(queue_module.eligible_for_autopost(
            self.entity, "VENDOR A", {"id": "x", "amount": "999999.00"}, treatment_check=lambda row: False))

    def test_self_approval_control_fails_closed_without_an_actor(self) -> None:
        self.pending("a1")
        os.environ["SLASHBOOKS_ACTOR"] = "agent:one"
        propose(self.entity, "a1", "Expenses:Software", "r")
        os.environ.pop("SLASHBOOKS_ACTOR")
        os.environ["SLASHBOOKS_FORBID_SELF_APPROVAL"] = "1"
        with self.assertRaises(queue_module.InvalidActorError):
            confirm(self.entity, "a1", "s", ts=TS)
        self.assertEqual(self.ledger_count(), 0)

    def test_actor_with_surrounding_whitespace_is_invalid(self) -> None:
        self.pending("a1")
        for bad in (" agent", "agent ", "  "):
            os.environ["SLASHBOOKS_ACTOR"] = bad
            with self.assertRaises(queue_module.InvalidActorError):
                propose(self.entity, "a1", "Expenses:Software", "r")

    def test_a_new_proposal_replaces_the_creator(self) -> None:
        self.pending("a1")
        os.environ["SLASHBOOKS_ACTOR"] = "agent:one"
        propose(self.entity, "a1", "Expenses:Software", "r")
        os.environ["SLASHBOOKS_ACTOR"] = "person:two"
        self.assertEqual(propose(self.entity, "a1", "Expenses:Fees", "r")["created_by"], "person:two")
        os.environ.pop("SLASHBOOKS_ACTOR")
        self.assertNotIn("created_by", propose(self.entity, "a1", "Expenses:Meals", "r"))

    def test_env_file_cannot_supply_a_caller_control(self) -> None:
        env_file = Path(self.tmp.name) / ".env"
        env_file.write_text("SLASHBOOKS_ACTOR=x\nSLASHBOOKS_FORBID_SELF_APPROVAL=1\n"
                            "SLASHBOOKS_ENFORCE_ASK_FIRST=1\nOTHER_KEY_FOR_TEST=kept\n", encoding="utf-8")
        os.environ.pop("OTHER_KEY_FOR_TEST", None)
        self.addCleanup(os.environ.pop, "OTHER_KEY_FOR_TEST", None)
        cli.load_dotenv(env_file)
        for name in ("SLASHBOOKS_ACTOR", "SLASHBOOKS_FORBID_SELF_APPROVAL", "SLASHBOOKS_ENFORCE_ASK_FIRST"):
            self.assertNotIn(name, os.environ)
        self.assertEqual(os.environ["OTHER_KEY_FOR_TEST"], "kept")

    def test_huge_amount_in_a_queue_item_does_not_break_work_capture(self) -> None:
        from bookkeeping import work
        self.assertIsNone(work._money("1e30"))
        self.assertIsNone(work._money("-9e999999", absolute=True))
        self.pending("big", amount="1e30")
        propose(self.entity, "big", "Expenses:Software", "r")
        with work.capture(self.entity.path) as receipt:
            pass
        self.assertEqual(receipt.events, [])

    def test_accrual_and_multi_line_entries_keep_accounts_without_an_amount(self) -> None:
        from bookkeeping.work import EntryRow, _entry_amount_row
        def row(accounts, amounts, **meta):
            return EntryRow(1, "2026-01-01", "s", "", meta, tuple(accounts), tuple(amounts))
        bank = _entry_amount_row(row(["Assets:Bank:Checking", "Expenses:Fees"], ["-5.00", "5.00"], **{"source-id": "s"}))
        self.assertEqual(bank["amount"], "5.00")
        journal = _entry_amount_row(row(["Assets:Bank:Checking", "Expenses:Fees"], ["-5.00", "5.00"],
                                        **{"accrual-intent": "j1"}))
        self.assertNotIn("amount", journal)
        self.assertEqual(journal["accounts"], ["Assets:Bank:Checking", "Expenses:Fees"])
        multi = _entry_amount_row(row(["Assets:AR", "Income:A", "Income:B"], ["10.00", "-6.00", "-4.00"]))
        self.assertNotIn("amount", multi)
        self.assertEqual(multi["accounts"], ["Assets:AR", "Income:A", "Income:B"])


if __name__ == "__main__":
    unittest.main()


class CorrectionAmountControlTests(ControlTestCase):
    def posted(self, amount):
        self.pending("correction-original", amount=amount)
        propose(self.entity, "correction-original", "Expenses:Software", "Synthetic original")
        confirm(self.entity, "correction-original", "synthetic-person", ts=TS)
        from bookkeeping.ledger.store import LedgerStore
        store = LedgerStore(self.entity.path / "ledger.sqlite")
        return store, store.load_entries()[0]

    def test_correction_refuses_original_or_replacement_over_limit_without_writes(self):
        from dataclasses import replace
        from bookkeeping.ledger.importer import reverse_and_correct, reverse_entry
        from bookkeeping.ledger.model import Posting
        store, original = self.posted("100.00")
        _set_ask_first(self.entity, "100.00")
        os.environ["SLASHBOOKS_ENFORCE_ASK_FIRST"] = "1"
        replacement = replace(original, meta=(("source-id", "replacement"),), postings=(
            Posting("Assets:Bank:Checking", Decimal("50000.00")),
            Posting("Expenses:Software", Decimal("-50000.00"))))
        before = store.content_digest()
        with self.assertRaises(queue_module.AskFirstError):
            reverse_and_correct(self.entity, original.source_id, replacement, "synthetic-agent")
        self.assertEqual(store.content_digest(), before)
        _set_ask_first(self.entity, "99.99")
        with self.assertRaises(queue_module.AskFirstError):
            reverse_entry(self.entity, original.source_id, "synthetic-agent")
        self.assertEqual(store.content_digest(), before)

    def test_split_postings_do_not_bypass_limit_and_exact_limit_is_allowed(self):
        from dataclasses import replace
        from bookkeeping.ledger.importer import reverse_and_correct
        from bookkeeping.ledger.model import Posting
        store, original = self.posted("100.00")
        _set_ask_first(self.entity, "100.00")
        os.environ["SLASHBOOKS_ENFORCE_ASK_FIRST"] = "1"
        replacement = replace(original, meta=(("source-id", "replacement"),), postings=(
            Posting("Assets:Bank:Checking", Decimal("60.00")),
            Posting("Expenses:Software", Decimal("60.00")),
            Posting("Income:Consulting", Decimal("-120.00"))))
        before = store.content_digest()
        with self.assertRaises(queue_module.AskFirstError):
            reverse_and_correct(self.entity, original.source_id, replacement, "synthetic-agent")
        self.assertEqual(store.content_digest(), before)
        allowed = replace(original, meta=(("source-id", "replacement-allowed"),))
        reverse_and_correct(self.entity, original.source_id, allowed, "synthetic-agent")
        self.assertEqual(len(store.load_entries()), 3)
