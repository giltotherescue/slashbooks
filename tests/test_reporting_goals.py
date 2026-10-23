"""Reporting goals in entity.json and report categories in the account catalog.

``books entity set-reporting-goals`` stores and validates what the owner wants the reports to show.
It changes no entry and no report. ``books entity account-add --category`` accepts only the
approved income and expense category shapes.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from golden_support import run_cli  # noqa: E402

from bookkeeping import reporting_goals  # noqa: E402
from bookkeeping.entity import add_account, load_entity  # noqa: E402
from bookkeeping.ledger.importer import _atomic_ledger_write, check_integrity  # noqa: E402
from bookkeeping.ledger.model import Entry, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

BANK = "Assets:Bank:Checking"
CONSULTING = "Income:Consulting"
INSTALLS = "Income:Installs"


def books(*argv: str):
    code, out, err = run_cli(list(argv))
    return code, (json.loads(out) if out.strip().startswith(("{", "[")) else out), err


def audit_types(root: Path) -> list[str]:
    return [e["type"] if isinstance(e, dict) else e.type for e in LedgerStore(root / "ledger.sqlite").load_audit_events()]


def entry(source_id: str, day: str, amount: str, account: str = CONSULTING, *, payee: str = "Client Co",
          tags: tuple[str, ...] = (), meta: tuple[tuple[str, str], ...] = ()) -> Entry:
    value = Decimal(amount)
    return Entry(date=date.fromisoformat(day), narration=f"Payment {source_id}", payee=payee, tags=tags,
                 meta=(("source-id", source_id), *meta),
                 postings=(Posting(BANK, value), Posting(account, -value)))


class CompanyFixture(unittest.TestCase):
    """A cash company with the consulting starter chart and a few income entries."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "company"
        code, out, err = run_cli(["entity", "init", str(self.root), "--name", "Test Co"])
        self.assertEqual(code, 0, (out, err))
        add_account(self.root, INSTALLS, open_date="2026-01-01")
        self.entity = load_entity(self.root)

    def post(self, *entries: Entry) -> None:
        _atomic_ledger_write(self.entity, [], list(entries), "test-seed", None, "seed test entries")

    def entity_json(self) -> dict:
        return json.loads((self.root / "entity.json").read_text(encoding="utf-8"))


class ReportingGoalsTests(CompanyFixture):
    def goals(self, *argv: str):
        return books("entity", "set-reporting-goals", str(self.root), *argv)

    def test_preview_then_apply_records_goals_and_changes_no_entry(self):
        self.post(entry("a", "2026-02-03", "100.00"))
        digest = LedgerStore(self.root / "ledger.sqlite").content_digest()
        before = (self.root / "entity.json").read_bytes()
        argv = ("--goal", "profit_by_line", "--goal", "cash_runway", "--line", "Installs", "--line", "  Service   plans ")
        code, plan, _ = self.goals(*argv)
        self.assertEqual(code, 0, plan)
        self.assertTrue(plan["changes"])
        self.assertEqual(plan["effects"]["to"], {"version": 1, "goals": ["profit_by_line", "cash_runway"],
                                                 "lines": ["Installs", "Service plans"]})
        self.assertEqual(plan["effects"]["entries_changed"], 0)
        self.assertEqual((self.root / "entity.json").read_bytes(), before)  # The preview writes nothing.

        code, stale, _ = self.goals(*argv, "--apply", "--plan-hash", "0" * 64)
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))
        code, done, _ = self.goals(*argv, "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"]), (0, "applied"), done)
        self.assertIsNone(done["from"])
        stored = self.entity_json()["reporting_goals"]
        self.assertEqual(stored, plan["effects"]["to"])
        self.assertEqual(reporting_goals.reporting_goals(load_entity(self.root).entity_config), stored)
        self.assertEqual(LedgerStore(self.root / "ledger.sqlite").content_digest(), digest)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

        code, same, _ = self.goals(*argv, "--apply")
        self.assertEqual((code, same["status"]), (0, "already_applied"))
        self.assertEqual(audit_types(self.root).count("reporting-goals-set"), 1)

    def test_new_goals_replace_the_old_and_clear_removes_them(self):
        self.goals("--goal", "basics", "--apply")
        code, done, _ = self.goals("--goal", "profit_by_job", "--line", "Kitchens", "--apply")
        self.assertEqual((code, done["from"]["goals"]), (0, ["basics"]))
        self.assertEqual(self.entity_json()["reporting_goals"]["goals"], ["profit_by_job"])
        code, plan, _ = self.goals("--clear")
        self.assertEqual((code, plan["effects"]["to"]), (0, None))
        code, cleared, _ = self.goals("--clear", "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, cleared["status"]), (0, "applied"))
        self.assertNotIn("reporting_goals", self.entity_json())
        self.assertIsNone(reporting_goals.reporting_goals(self.entity_json()))
        code, again, _ = self.goals("--clear", "--apply")
        self.assertEqual((code, again["status"]), (0, "already_applied"))
        self.assertEqual(audit_types(self.root).count("reporting-goals-set"), 3)

    def test_exact_retry_with_the_plan_hash_is_already_applied(self):
        argv = ("--goal", "profit_by_line", "--line", "Installs")
        code, plan, _ = self.goals(*argv)
        self.assertEqual(self.goals(*argv, "--apply", "--plan-hash", plan["plan_hash"])[1]["status"], "applied")
        code, retry, _ = self.goals(*argv, "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, retry["status"]), (0, "already_applied"), retry)
        code, clear_plan, _ = self.goals("--clear")
        self.assertEqual(self.goals("--clear", "--apply", "--plan-hash", clear_plan["plan_hash"])[1]["status"], "applied")
        code, retry, _ = self.goals("--clear", "--apply", "--plan-hash", clear_plan["plan_hash"])
        self.assertEqual((code, retry["status"]), (0, "already_applied"), retry)
        self.assertEqual(audit_types(self.root).count("reporting-goals-set"), 2)
        # A stale hash still refuses a write of a new value.
        code, stale, _ = self.goals("--goal", "basics", "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))

    def goal_events(self) -> list[dict]:
        return [e["payload"] for e in LedgerStore(self.root / "ledger.sqlite").load_audit_events()
                if e["type"] == "reporting-goals-set"]

    def stop_transaction(self, *, after_commit: bool, before_raise=None):
        """Patch LedgerStore.transaction to stop the apply after the file replace, before or after the commit.

        ``before_raise`` runs after the transaction body (the file is replaced) and before the stop.
        """
        import contextlib
        import sqlite3
        from unittest.mock import patch
        real = LedgerStore.transaction

        @contextlib.contextmanager
        def stopping(store, *args, **kwargs):
            if after_commit:
                with real(store, *args, **kwargs) as conn:
                    yield conn
                raise KeyboardInterrupt  # The commit is done; the stop comes right after it.
            with real(store, *args, **kwargs) as conn:
                yield conn
                if before_raise:
                    before_raise()
                raise sqlite3.OperationalError("disk I/O error")  # Rolls the ledger back after the file write.

        return patch.object(LedgerStore, "transaction", stopping)

    def write_raw_entity_json(self) -> bytes:
        """Write entity.json in a layout this command never writes, so an exact restore is visible."""
        config = self.entity_json()
        raw = (json.dumps(config, separators=(",", ":")) + "\n\n").encode("utf-8")
        (self.root / "entity.json").write_bytes(raw)
        return raw

    def test_failure_before_commit_restores_the_exact_original_bytes(self):
        import sqlite3
        self.goals("--goal", "basics", "--apply")
        before, events = self.write_raw_entity_json(), self.goal_events()
        with self.stop_transaction(after_commit=False), self.assertRaises(sqlite3.OperationalError):
            reporting_goals.apply_set(load_entity(self.root), ["cash_runway"])
        self.assertEqual((self.root / "entity.json").read_bytes(), before)
        self.assertEqual(self.goal_events(), events)
        self.assertFalse((self.root / "entity.json.reporting-goals.tmp").exists())

    def test_failed_lookup_keeps_the_new_file(self):
        import sqlite3
        from unittest.mock import patch
        self.goals("--goal", "basics", "--apply")
        events = self.goal_events()
        patcher = patch.object(LedgerStore, "load_audit_events", side_effect=sqlite3.DatabaseError("database is locked"))
        self.addCleanup(patcher.stop)
        with self.stop_transaction(after_commit=False, before_raise=patcher.start), self.assertRaises(sqlite3.OperationalError):
            reporting_goals.apply_set(load_entity(self.root), ["cash_runway"])
        patcher.stop()
        self.assertEqual(reporting_goals.reporting_goals(self.entity_json())["goals"], ["cash_runway"])
        self.assertEqual(self.goal_events(), events)  # The ledger rolled back.
        code, plan, _ = self.goals("--goal", "cash_runway")
        self.assertEqual((code, plan["changes"], [w["code"] for w in plan["warnings"]]), (0, False, ["unrecorded_change"]))

    def test_file_changed_by_another_writer_is_not_overwritten(self):
        import sqlite3
        self.goals("--goal", "basics", "--apply")
        other = b'{"name": "Changed by another writer"}\n'
        with self.stop_transaction(after_commit=False, before_raise=lambda: (self.root / "entity.json").write_bytes(other)), \
                self.assertRaises(sqlite3.OperationalError):
            reporting_goals.apply_set(load_entity(self.root), ["cash_runway"])
        self.assertEqual((self.root / "entity.json").read_bytes(), other)

    def test_edit_during_the_rollback_sync_is_not_overwritten(self):
        import sqlite3
        from unittest.mock import patch
        self.goals("--goal", "basics", "--apply")
        original = self.write_raw_entity_json()
        other = b'{"name": "Edited during the rollback sync"}\n'
        real = reporting_goals._write_durable

        def staged(path, data):
            real(path, data)
            if data == original:  # The rollback bytes are staged and synced; another writer edits now.
                (self.root / "entity.json").write_bytes(other)

        with patch.object(reporting_goals, "_write_durable", side_effect=staged), \
                self.stop_transaction(after_commit=False), self.assertRaises(sqlite3.OperationalError):
            reporting_goals.apply_set(load_entity(self.root), ["cash_runway"])
        self.assertEqual((self.root / "entity.json").read_bytes(), other)
        self.assertFalse((self.root / "entity.json.reporting-goals.tmp").exists())

    def test_edit_during_the_staging_sync_aborts_with_stale_preview(self):
        from unittest.mock import patch
        self.goals("--goal", "basics", "--apply")
        events = self.goal_events()
        other = b'{"name": "Edited during the staging sync"}\n'
        real = reporting_goals._write_durable

        def staged(path, data):
            real(path, data)
            (self.root / "entity.json").write_bytes(other)  # An editor that bypasses the company lock.

        with patch.object(reporting_goals, "_write_durable", side_effect=staged), \
                self.assertRaises(reporting_goals.GoalsError) as caught:
            reporting_goals.apply_set(load_entity(self.root), ["cash_runway"])
        self.assertEqual(caught.exception.code, "stale_preview")
        self.assertEqual((self.root / "entity.json").read_bytes(), other)
        self.assertEqual(self.goal_events(), events)
        self.assertFalse((self.root / "entity.json.reporting-goals.tmp").exists())

    def test_unchanged_file_at_the_rename_is_applied(self):
        from unittest.mock import patch
        self.goals("--goal", "basics", "--apply")
        with patch.object(reporting_goals, "_write_durable", wraps=reporting_goals._write_durable) as staged:
            result = reporting_goals.apply_set(load_entity(self.root), ["cash_runway"])
        self.assertEqual((result["status"], staged.call_count), ("applied", 1))
        self.assertEqual(reporting_goals.reporting_goals(self.entity_json())["goals"], ["cash_runway"])

    def test_failure_while_staging_leaves_no_temporary_file(self):
        from unittest.mock import patch

        def write_then_fail(stop):
            def staged(path, data):
                with open(path, "wb") as handle:
                    handle.write(data[:5])  # A partial write, then the failure.
                raise stop
            return staged

        self.goals("--goal", "basics", "--apply")
        before, events = (self.root / "entity.json").read_bytes(), self.goal_events()
        for stop in (OSError("write failed"), OSError("fsync failed"), KeyboardInterrupt()):
            with self.subTest(stop=repr(stop)):
                with patch.object(reporting_goals, "_write_durable", side_effect=write_then_fail(stop)), \
                        self.assertRaises(type(stop)):
                    reporting_goals.apply_set(load_entity(self.root), ["cash_runway"])
                self.assertFalse((self.root / "entity.json.reporting-goals.tmp").exists())
                self.assertEqual((self.root / "entity.json").read_bytes(), before)
                self.assertEqual(self.goal_events(), events)

    def test_interrupt_after_commit_keeps_the_new_entity_json(self):
        self.goals("--goal", "basics", "--apply")
        with self.stop_transaction(after_commit=True), self.assertRaises(KeyboardInterrupt):
            reporting_goals.apply_set(load_entity(self.root), ["cash_runway"])
        self.assertEqual(reporting_goals.reporting_goals(self.entity_json())["goals"], ["cash_runway"])
        events = self.goal_events()
        self.assertEqual([e["to"]["goals"] for e in events], [["basics"], ["cash_runway"]])
        self.assertEqual(len({e["operation_id"] for e in events}), 2)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        code, plan, _ = self.goals("--goal", "cash_runway")
        self.assertEqual((code, plan["changes"], plan.get("warnings")), (0, False, None))

    def test_unrecorded_value_needs_a_fresh_plan_hash_to_write_its_event(self):
        """The state after a kill between the file replace and the commit, or after a hand edit."""
        self.goals("--goal", "basics", "--apply")
        config = self.entity_json()
        config["reporting_goals"] = {"version": 1, "goals": ["cash_runway"], "lines": []}
        (self.root / "entity.json").write_text(json.dumps(config), encoding="utf-8")
        code, plan, _ = self.goals("--goal", "cash_runway")
        self.assertEqual((code, plan["changes"]), (0, False))
        self.assertEqual([w["code"] for w in plan["warnings"]], ["unrecorded_change"])
        # Without a plan hash nothing is written; a wrong or all-zero hash is refused.
        code, same, _ = self.goals("--goal", "cash_runway", "--apply")
        self.assertEqual((code, same["status"], same["warnings"][0]["code"]), (0, "already_applied", "unrecorded_change"))
        for wrong in ("0" * 64, "f" * 64):
            code, refused, _ = self.goals("--goal", "cash_runway", "--apply", "--plan-hash", wrong)
            self.assertEqual((code, refused["errors"][0]["code"]), (1, "stale_preview"))
        self.assertEqual(len(self.goal_events()), 1)
        # The person's own plan hash writes the event once.
        code, done, _ = self.goals("--goal", "cash_runway", "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"], done["confirmed_by_apply"]), (0, "applied", True), done)
        events = self.goal_events()
        self.assertEqual(len(events), 2)
        self.assertEqual((events[-1]["from"]["goals"], events[-1]["to"]["goals"], events[-1]["confirmed_by_apply"]),
                         (["basics"], ["cash_runway"], True))
        self.assertNotIn("recovered", events[-1])
        code, again, _ = self.goals("--goal", "cash_runway", "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, again["status"]), (0, "already_applied"))
        self.assertEqual(len(self.goal_events()), 2)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_new_value_after_an_unrecorded_value_writes_one_normal_event(self):
        self.goals("--goal", "basics", "--apply")
        config = self.entity_json()
        config["reporting_goals"] = {"version": 1, "goals": ["cash_runway"], "lines": []}
        (self.root / "entity.json").write_text(json.dumps(config), encoding="utf-8")
        code, stale, _ = self.goals("--goal", "owner_pay", "--apply", "--plan-hash", "0" * 64)
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))
        code, plan, _ = self.goals("--goal", "owner_pay")
        code, done, _ = self.goals("--goal", "owner_pay", "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"]), (0, "applied"), done)
        last = self.goal_events()[-1]
        self.assertEqual((last["from"]["goals"], last["to"]["goals"], "confirmed_by_apply" in last), (["cash_runway"], ["owner_pay"], False))
        self.assertEqual(len(self.goal_events()), 2)

    def test_stop_right_after_the_rename_restores_the_original_bytes(self):
        from unittest.mock import patch
        self.goals("--goal", "basics", "--apply")
        for stop in (OSError("directory sync failed"), KeyboardInterrupt()):
            with self.subTest(stop=type(stop).__name__):
                before, events = self.write_raw_entity_json(), self.goal_events()
                calls = []

                def fail_first(path, stop=stop):
                    calls.append(path)
                    if len(calls) == 1:  # The rename is done; the directory sync of the apply stops it.
                        raise stop

                with patch.object(reporting_goals, "_fsync_directory", side_effect=fail_first), \
                        self.assertRaises(type(stop)):
                    reporting_goals.apply_set(load_entity(self.root), ["cash_runway"])
                self.assertEqual((self.root / "entity.json").read_bytes(), before)
                self.assertEqual(self.goal_events(), events)
                self.assertFalse((self.root / "entity.json.reporting-goals.tmp").exists())

    def test_a_used_confirmation_hash_cannot_confirm_a_later_hand_edit(self):
        self.goals("--goal", "basics", "--apply")

        def hand_edit(goal):
            config = self.entity_json()
            config["reporting_goals"] = {"version": 1, "goals": [goal], "lines": []}
            (self.root / "entity.json").write_text(json.dumps(config), encoding="utf-8")

        hand_edit("cash_runway")
        code, plan, _ = self.goals("--goal", "cash_runway")
        code, done, _ = self.goals("--goal", "cash_runway", "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done.get("confirmed_by_apply")), (0, True), done)
        code, retry, _ = self.goals("--goal", "cash_runway", "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, retry["status"]), (0, "already_applied"))  # An exact retry of the confirmation.
        self.goals("--goal", "owner_pay", "--apply")
        hand_edit("cash_runway")
        events = self.goal_events()
        code, reused, _ = self.goals("--goal", "cash_runway", "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, reused["errors"][0]["code"]), (1, "stale_preview"), reused)
        self.assertEqual(self.goal_events(), events)

    def test_validation_refuses_bad_values_before_any_write(self):
        before = (self.root / "entity.json").read_bytes()
        cases = {
            "unknown_goal": ("--goal", "mrr"),
            "repeated_goal": ("--goal", "basics", "--goal", "basics"),
            "lines_need_line_goal": ("--goal", "cash_runway", "--line", "Installs"),
            "too_many_lines": ("--goal", "profit_by_line", *[arg for n in range(13) for arg in ("--line", f"Line {n}")]),
            "invalid_line": ("--goal", "profit_by_line", "--line", "x" * 41),
            "repeated_line": ("--goal", "profit_by_line", "--line", "Installs", "--line", "INSTALLS"),
            "nothing_to_set": (),
            "no_goals": ("--line", "Installs"),
            "clear_with_values": ("--clear", "--goal", "basics"),
        }
        for code_name, argv in cases.items():
            with self.subTest(code_name):
                for apply in ((), ("--apply",)):
                    code, result, _ = self.goals(*argv, *apply)
                    self.assertEqual((code, result["errors"][0]["code"]), (1, code_name), result)
        for bad in ("Installs:Big", "Repairs\x00", "!!!", "", "<b>"):
            with self.subTest(bad=bad):
                with self.assertRaises(reporting_goals.GoalsError) as caught:
                    reporting_goals.validate(["profit_by_line"], [bad])
                self.assertEqual(caught.exception.code, "invalid_line")
        self.assertEqual(reporting_goals.validate(list(reporting_goals.GOAL_CODES), [f"L{n}" for n in range(12)])["goals"],
                         list(reporting_goals.GOAL_CODES))
        self.assertEqual(reporting_goals.validate(["profit_by_line"], ["Café & Bar (A/B), 2nd + 3rd - Ltd."])["lines"],
                         ["Café & Bar (A/B), 2nd + 3rd - Ltd."])
        self.assertEqual((self.root / "entity.json").read_bytes(), before)
        self.assertNotIn("reporting-goals-set", audit_types(self.root))

    def test_getter_refuses_a_bad_stored_value_and_the_command_repairs_it(self):
        for bad in ({"version": 2, "goals": ["basics"], "lines": []}, ["basics"],
                    {"version": 1, "goals": ["basics", "basics"], "lines": []},
                    {"version": 1, "goals": ["basics"], "lines": [" Installs"]},
                    {"version": 1, "goals": ["basics"], "lines": [], "extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                reporting_goals.reporting_goals({"reporting_goals": bad})
        for bad in ({"version": 999, "goals": ["basics"], "lines": []}, {"version": 1, "goals": ["nope"], "lines": []}):
            with self.subTest(stored=bad):
                config = self.entity_json()
                config["reporting_goals"] = bad
                (self.root / "entity.json").write_text(json.dumps(config), encoding="utf-8")
                events = self.goal_events()
                with self.assertRaises(ValueError):
                    reporting_goals.reporting_goals(self.entity_json())
                code, plan, _ = self.goals("--goal", "owner_pay")
                self.assertEqual(code, 0, plan)
                self.assertEqual([w["code"] for w in plan["warnings"]], ["invalid_stored_value", "unrecorded_change"])
                # The command needs an explicit new value or --clear; without one nothing is written.
                code, refused, _ = self.goals("--apply")
                self.assertEqual((code, refused["errors"][0]["code"]), (1, "nothing_to_set"))
                self.assertEqual(self.goal_events(), events)
                code, done, _ = self.goals("--goal", "owner_pay", "--apply", "--plan-hash", plan["plan_hash"])
                self.assertEqual(code, 0, done)
                self.assertEqual(reporting_goals.reporting_goals(self.entity_json())["goals"], ["owner_pay"])
                new = self.goal_events()[len(events):]
                self.assertEqual([(e["from"], "recovered" in e) for e in new], [(bad, False)])

    def test_preview_is_read_only_for_hosts_and_apply_changes_the_company(self):
        base = ["entity", "set-reporting-goals", "E", "--goal", "basics"]
        self.assertFalse(command_spec(base)["mutates"])
        self.assertTrue(command_spec(base + ["--apply"])["mutates"])

    def test_goals_travel_in_the_migration_bundle_and_keep_report_fingerprints(self):
        from bookkeeping import migration_bundle
        self.post(entry("a", "2026-02-03", "100.00"), entry("b", "2026-03-04", "40.00", INSTALLS))
        first = migration_bundle.build_bundle(self.root, self.root.parent / "before.zip")
        self.assertEqual(first["status"], "exported", first)
        self.goals("--goal", "profit_by_line", "--line", "Installs", "--apply")
        second = migration_bundle.build_bundle(self.root, self.root.parent / "after.zip")
        self.assertEqual(second["status"], "exported", second)
        self.assertEqual(second["report_fingerprints"], first["report_fingerprints"])
        self.assertEqual(second["ledger_content_sha256"], first["ledger_content_sha256"])
        files = migration_bundle.load_bundle(self.root.parent / "after.zip")["files"]
        self.assertEqual(json.loads(files["entity.json"])["reporting_goals"]["lines"], ["Installs"])


class CategoryAccountTests(CompanyFixture):
    def add(self, account: str, *extra: str):
        return run_cli(["entity", "account-add", str(self.root), "--account", account, *extra])

    def test_category_shapes_are_accepted(self):
        for account in ("Income:Repairs", "Income:Services:Installs", "Expenses:Fuel",
                        "Expenses:Cost-of-Sales", "Expenses:Cost-of-Sales:Installs", "Expenses:COGS:Repairs"):
            with self.subTest(account):
                code, out, err = self.add(account, "--category", "--open-date", "2025-01-01")
                self.assertEqual(code, 0, (out, err))
                self.assertIn(f"Added: {account}", out)
        opened = {o.account for o in LedgerStore(self.root / "ledger.sqlite").load_opens()}
        self.assertIn("Expenses:Cost-of-Sales:Installs", opened)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_category_refuses_other_shapes_before_any_write(self):
        digest = LedgerStore(self.root / "ledger.sqlite").content_digest()
        for account in ("Income:Services:Installs:Big", "Expenses:Office:Chairs", "Expenses:Cost-of-Sales:Installs:Big",
                        "Assets:Bank:Savings", "Liabilities:Sales-Tax-Payable", "Equity:Owner-Draws", "Income"):
            with self.subTest(account):
                code, out, err = self.add(account, "--category")
                self.assertEqual(code, 1, (out, err))
                self.assertIn("A category must have one of these shapes", err)
        self.assertEqual(LedgerStore(self.root / "ledger.sqlite").content_digest(), digest)

    def test_category_keeps_the_usual_account_checks(self):
        code, _, err = self.add("Income:services", "--category")
        self.assertEqual(code, 1)
        self.assertIn("A category name may use letters", err)
        code, out, _ = self.add(CONSULTING, "--category")
        self.assertEqual(code, 0)
        self.assertIn("Already present", out)

    def test_category_refuses_control_characters_and_keeps_catalog_and_audit(self):
        store = LedgerStore(self.root / "ledger.sqlite")
        digest, audit = store.content_digest(), len(audit_types(self.root))
        for account in ("Income:Consulting\n", "Income:Consulting\r", "Income:Install\ts", "Expenses:Cost-of-Sales:X\x00",
                        "Income:Consulting\n:Big"):
            with self.subTest(account=account):
                with self.assertRaises(ValueError) as caught:
                    add_account(self.root, account, category=True)
                self.assertIn("A category name may use letters", str(caught.exception))
                code, _, err = self.add(account, "--category")
                self.assertEqual(code, 1, err)
        self.assertEqual((store.content_digest(), len(audit_types(self.root))), (digest, audit))
        self.assertEqual([o.account for o in store.load_opens() if o.account.startswith(CONSULTING)], [CONSULTING])

    def test_without_category_any_valid_account_is_still_accepted(self):
        for account in ("Income:Services:Installs:Big", "Liabilities:Sales-Tax-Payable"):
            with self.subTest(account):
                code, out, err = self.add(account)
                self.assertEqual(code, 0, (out, err))


if __name__ == "__main__":
    unittest.main()
