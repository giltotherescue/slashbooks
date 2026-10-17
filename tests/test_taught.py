"""A taught category is a preference for what is suggested. It never records."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from bookkeeping import cli, procedures  # noqa: E402
from bookkeeping.queue import (  # noqa: E402
    check_taught, clear_taught, confirm, correct, eligible_for_autopost, list_taught,
    load_learned_context, propose, read_queue_item, set_taught, taught_for, validate_accrual_hint,
)
from bookkeeping.remote_contract import command_spec  # noqa: E402
from test_queue import _add_pending_categorization, _make_entity  # noqa: E402

KEY = "LUMENFORGE AI"
SOFTWARE = "Expenses:Software"


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TaughtCategoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.entity = _make_entity(Path(self.tmp.name))
        _add_pending_categorization(self.entity, "t1", "LumenForge AI")
        _add_pending_categorization(self.entity, "t2", "LumenForge AI")

    def teach(self, category=SOFTWARE, ts="2026-10-16T10:00:00Z", by="u1"):
        return set_taught(self.entity, KEY, category, by=by, by_name="Gil", ts=ts)

    def test_teach_creates_record_without_touching_the_trust_ramp(self) -> None:
        taught = self.teach()
        self.assertEqual(taught["category"], SOFTWARE)
        entry = load_learned_context(self.entity)[KEY]
        self.assertEqual((entry["canonical_category"], entry["confirmed_count"], entry["reset"]), ("", 0, False))
        self.assertFalse(eligible_for_autopost(self.entity, KEY))
        self.assertEqual(taught_for(self.entity, KEY)["id"], taught["id"])

    def test_same_category_is_idempotent_and_replace_keeps_one_level_previous(self) -> None:
        first = self.teach()
        self.assertEqual(self.teach(ts="2026-10-17T10:00:00Z")["id"], first["id"])
        second = self.teach("Expenses:Office", ts="2026-10-18T10:00:00Z")
        self.assertEqual(second["previous"]["id"], first["id"])
        third = self.teach("Expenses:Meals", ts="2026-10-19T10:00:00Z")
        self.assertNotIn("previous", third["previous"])

    def test_rejects_unknown_bank_cash_and_card_accounts(self) -> None:
        for bad in ("Expenses:Nope", "Assets:Bank:Checking"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.teach(bad)
        self.assertEqual(load_learned_context(self.entity), {})

    def test_clear_restores_previous_or_removes(self) -> None:
        first = self.teach()
        second = self.teach("Expenses:Office", ts="2026-10-18T10:00:00Z")
        with self.assertRaises(ValueError):
            clear_taught(self.entity, KEY, first["id"])
        restored = clear_taught(self.entity, KEY, second["id"])
        self.assertEqual(restored["id"], first["id"])
        self.assertIsNone(clear_taught(self.entity, KEY, first["id"]))
        self.assertIsNone(taught_for(self.entity, KEY))
        with self.assertRaises(ValueError):
            clear_taught(self.entity, KEY, first["id"])

    def test_cli_propose_refuses_a_different_category_and_names_the_taught_one(self) -> None:
        self.teach()
        code, _, err = _run("queue", "propose", "--entity", str(self.entity.path), "--source-id", "t1",
                            "--category", "Expenses:Meals", "--reasoning", "x")
        self.assertEqual(code, 1)
        self.assertIn(SOFTWARE, err)
        self.assertIn("2026-10-16", err)
        code, _, err = _run("queue", "propose-group", "--entity", str(self.entity.path), "--source-id", "t1",
                            "--source-id", "t2", "--category", "Expenses:Meals", "--reasoning", "x")
        self.assertEqual(code, 1)
        self.assertIn(SOFTWARE, err)
        code, out, _ = _run("queue", "propose", "--entity", str(self.entity.path), "--source-id", "t1",
                            "--category", SOFTWARE, "--reasoning", "x")
        self.assertEqual(code, 0, out)

    def test_propose_api_stays_open_and_stamps_only_matching_items(self) -> None:
        taught = self.teach()
        self.assertEqual(propose(self.entity, "t1", "Expenses:Meals", "one-off")["proposed_category"], "Expenses:Meals")
        self.assertNotIn("taught", read_queue_item(self.entity, "t1"))
        item = propose(self.entity, "t2", SOFTWARE, "taught")
        self.assertEqual(item["taught"], {"id": taught["id"], "category": SOFTWARE,
                                          "at": "2026-10-16T10:00:00Z", "by": "u1", "by_name": "Gil"})
        check_taught(self.entity, ["t1"], SOFTWARE)

    def test_confirm_and_correct_keep_taught_and_ramp_is_unchanged(self) -> None:
        self.teach()
        propose(self.entity, "t1", SOFTWARE, "taught")
        confirm(self.entity, "t1", "s1")
        entry = load_learned_context(self.entity)[KEY]
        self.assertEqual(entry["confirmed_count"], 1)
        self.assertEqual(entry["taught"]["category"], SOFTWARE)
        propose(self.entity, "t2", "Expenses:Meals", "one-off")
        correct(self.entity, "t2", SOFTWARE, "note", "s1")
        self.assertEqual(load_learned_context(self.entity)[KEY]["taught"]["category"], SOFTWARE)

    def test_old_record_without_taught_behaves_as_before(self) -> None:
        propose(self.entity, "t1", SOFTWARE, "plain")
        confirm(self.entity, "t1", "s1")
        self.assertIsNone(taught_for(self.entity, KEY))
        self.assertEqual(list_taught(self.entity), [])
        check_taught(self.entity, ["t2"], "Expenses:Meals")

    def test_queue_taught_prints_json_and_is_read_only(self) -> None:
        taught = self.teach()
        code, out, _ = _run("queue", "taught", "--entity", str(self.entity.path))
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["instructions"][0]["id"], taught["id"])
        self.assertEqual(data["instructions"][0]["counterparty"], KEY)
        spec = command_spec(["queue", "taught", "--entity", str(self.entity.path)])
        self.assertFalse(spec["mutates"])

    def test_accrual_hint_shape_is_checked_and_independent_of_taught(self) -> None:
        good = {"version": 1, "treatment": "prepaid", "term_months": 12, "method": "actual_day",
                "accounts": {"account": "Assets:Prepaid", "recognition_account": SOFTWARE},
                "threshold": "1000.00", "note": "annual", "set_by": "u1",
                "set_at": "2026-10-16T10:00:00Z", "source": "person"}
        self.assertEqual(validate_accrual_hint(good), [])
        for bad in ({**good, "treatment": "magic"}, {**good, "version": 2}, {**good, "source": "robot"},
                    {**good, "term_months": 0}, {**good, "method": "daily"}, {**good, "extra": 1},
                    {**good, "accounts": {"account": "x"}}, {**good, "threshold": "lots"}, "prepaid"):
            with self.subTest(bad=bad):
                self.assertNotEqual(validate_accrual_hint(bad), [])
        self.assertEqual(validate_accrual_hint({"version": 1, "treatment": "bill", "set_by": "u1",
                                                "set_at": "2026-10-16T10:00:00Z", "source": "agent"}), [])
        self.teach()
        ctx = load_learned_context(self.entity)
        ctx[KEY]["accrual"] = good
        (self.entity.path / "learned-context" / "counterparties.json").write_text(json.dumps(ctx))
        self.assertEqual(taught_for(self.entity, KEY)["category"], SOFTWARE)
        set_taught(self.entity, KEY, "Expenses:Office", by="u1", by_name="Gil", ts="2026-10-18T10:00:00Z")
        self.assertEqual(load_learned_context(self.entity)[KEY]["accrual"], good)

    def test_a_withdrawn_suggestion_can_be_replaced_by_a_split_proposal(self) -> None:
        from bookkeeping.queue import propose_split, withdraw
        propose(self.entity, "t1", SOFTWARE, "first")
        withdraw(self.entity, "t1", "changed")
        item = propose_split(self.entity, "t1", "replacement", ["Expenses:Meals=-60.00", "Expenses:Fees=-40.00"])
        self.assertEqual(item["status"], "open")

    def test_procedure_carries_the_rule(self) -> None:
        proc = procedures.get_procedure("categorize-record")
        self.assertEqual(proc["version"], 2)
        self.assertTrue(any("queue taught" in rule for rule in proc["rules"]))


if __name__ == "__main__":
    unittest.main()
