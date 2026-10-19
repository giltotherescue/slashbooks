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
    confirm, confirm_group, confirm_split, confirm_transfer, correct,
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


if __name__ == "__main__":
    unittest.main()
