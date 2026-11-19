"""Tests for change sets (bookkeeping.changeset) and the ``--change-set-out`` output.

All data is synthetic. The main test runs a sequence of writes. It applies each change set to a host view that
started empty, and it compares the view with a full read of the books after every write.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bookkeeping import changeset, cli, queue  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.remote_contract import command_spec, parse_command, rewrite_paths  # noqa: E402

CHECKING = "Assets:Bank:Operating-Checking"


def _txn(source_id: str, day: str, amount: str, description: str, account: str = "Operating Checking") -> dict:
    return {"id": source_id, "date": day, "amount": amount, "description": description,
            "accountName": account, "accountType": "checking"}


IMPORT = [
    _txn("cs-001", "2026-10-01", "-54.99", "DESIGN TOOL MONTHLY"),
    _txn("cs-002", "2026-10-02", "1840.00", "CARD PROCESSOR PAYOUT"),
    _txn("cs-003", "2026-10-03", "-180.00", "HARBOR CAFE"),
    _txn("cs-004", "2026-10-04", "-42.00", "CLOUD HOSTING"),
    _txn("cs-005", "2026-10-05", "-75.00", "OFFICE SUPPLY STORE"),
]


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def independent_view(entity: Path) -> dict:
    """The books through the engine's own readers, not through the change set module."""
    store = LedgerStore(entity / "ledger.sqlite")
    entries = store.load_entries()
    company = load_entity(entity)
    return {
        "entries": sorted((e.date.isoformat(), e.narration, e.source_id,
                           tuple((p.account, str(p.amount), p.currency) for p in e.postings)) for e in entries),
        "accounts": sorted(o.account for o in store.load_opens()),
        "audit": [(e["id"], e["type"], e["record_hash"]) for e in store.load_audit_events()],
        "queue": sorted((str(i["source_id"]), str(i["status"]))
                        for i in queue.list_queue_items(company) if i.get("status") != "staged"
                        and i.get("status") != "duplicate-candidate"),
        "taught": {row["counterparty"]: row["category"] for row in queue.list_taught(company)},
        # Read with plain json, not with the change set module.
        "staging": {name: _plain_json(entity / "staging" / name) for name in
                    ("pending-categorization.json", "pending.json", "seen-ids.json", "duplicate-candidates.json")},
    }


def _plain_json(path: Path):
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if isinstance(data, list):
        data = sorted(data, key=lambda row: json.dumps(row, sort_keys=True)) or None
    return data


def view_summary(view: dict) -> dict:
    """The same summary, taken from a host view."""
    entries = sorted(
        (e["date"], e["narration"], e["source_id"],
         tuple((p["account"], p["amount"], p["currency"]) for p in e["postings"]))
        for e in view["entries"].values())
    taught = {key: value["taught"]["category"] for key, value in view["learned_context"].items()
              if isinstance(value, dict) and isinstance(value.get("taught"), dict) and value["taught"].get("category")}
    return {
        "entries": entries,
        "accounts": sorted(view["accounts"]),
        "audit": [(e["id"], e["type"], e["record_hash"]) for _, e in sorted(view["audit_events"].items())],
        "queue": sorted((k, str(v["status"])) for k, v in view["review_queue"].items()),
        "taught": taught,
        "staging": {
            name: sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)) or None for name, rows in (
                ("pending-categorization.json", view["staging"]["pending"].values()),
                ("pending.json", view["staging"]["store_pending"].values()),
                ("seen-ids.json", view["staging"]["seen_ids"]),
                ("duplicate-candidates.json", view["staging"]["duplicates"].values()))
        },
    }


class Sequence_:
    """Run writes one at a time and keep a host view that only applies change sets."""

    def __init__(self, test: unittest.TestCase, root: Path) -> None:
        self.test, self.root = test, root
        self.entity = root / "co"
        self.view = changeset.empty_view()
        self.sets: dict[str, dict] = {}
        self.n = 0

    def cap(self):
        """A capture with the revisions that a host gives: the base is the revision of the view, and the next is one more."""
        revision = self.view["revision"]
        return changeset.capture(self.entity, books_revision=revision + 1, base_revision=revision)

    def _apply(self, name: str, document: dict) -> dict:
        self.test.assertEqual(changeset.validate(document), [], name)
        self.sets[name] = document
        self.view = changeset.apply_change_set(self.view, json.loads(json.dumps(document)))
        full = changeset.read_view(self.entity, revision=self.view["revision"])
        self.test.assertEqual(self.view, full, f"{name}: the applied view differs from a full read")
        if self.entity.is_dir() and (self.entity / "ledger.sqlite").exists():
            self.test.assertEqual(view_summary(self.view), independent_view(self.entity), name)
        return document

    def cli(self, name: str, *argv: str, expect: int = 0) -> dict:
        self.n += 1
        out = self.root / f"cs-{self.n}.json"
        revision = self.view["revision"]
        extra = ["--change-set-base-revision", str(revision), "--change-set-revision", str(revision + 1)]
        code, stdout, stderr = run_cli(["--change-set-out", str(out), *extra, *argv])
        self.test.assertEqual(code, expect, f"{argv}: {stdout}{stderr}")
        return self._apply(name, json.loads(out.read_text(encoding="utf-8")))

    def api(self, name: str, call) -> dict:
        with self.cap() as receipt:
            call()
        return self._apply(name, receipt.change_set)

    def ingest(self, name: str, txns: list[dict], *extra: str) -> dict:
        path = self.root / f"in-{self.n}.json"
        path.write_text(json.dumps(txns), encoding="utf-8")
        return self.cli(name, "ingest", str(path), "--entity", str(self.entity), "--session-id", "cs-session", *extra)


def scenario(test: unittest.TestCase, root: Path) -> Sequence_:
    seq = Sequence_(test, root)
    entity = str(seq.entity)
    seq.cli("init", "entity", "init", entity, "--name", "Synthetic Studio")
    seq.ingest("import", IMPORT)
    seq.cli("propose_1", "queue", "propose", "--entity", entity, "--source-id", "cs-001",
            "--category", "Expenses:Software", "--reasoning", "Monthly design tool.")
    seq.cli("approve", "queue", "confirm", "--entity", entity, "--item", "cs-001")
    seq.cli("propose_3", "queue", "propose", "--entity", entity, "--source-id", "cs-003",
            "--category", "Expenses:Meals", "--reasoning", "Lunch.")
    seq.cli("decline", "queue", "withdraw", "--entity", entity, "--item", "cs-003")
    seq.cli("propose_4", "queue", "propose", "--entity", entity, "--source-id", "cs-004",
            "--category", "Expenses:Software", "--reasoning", "Hosting.")
    seq.cli("categorize", "queue", "correct", "--entity", entity, "--item", "cs-004", "--category", "Expenses:Fees")
    seq.cli("propose_2", "queue", "propose", "--entity", entity, "--source-id", "cs-002",
            "--category", "Income:Consulting", "--reasoning", "Client payout.")
    seq.cli("approve_2", "queue", "confirm", "--entity", entity, "--item", "cs-002")
    seq.cli("reverse", "ledger", "reverse", "--entity", entity, "--source-id", "cs-002")
    seq.cli("correct", "ledger", "correct", "--entity", entity, "--source-id", "cs-001",
            "--new-source-id", "cs-001-fix", "--date", "2026-10-01", "--description", "Design tool",
            "--posting", f"{CHECKING}=-54.99", "--posting", "Expenses:Office=54.99")
    code, out, err = run_cli(["entity", "account-rename", entity, "--account", "Expenses:Office",
                              "--to", "Expenses:Workspace", "--json"])
    test.assertEqual(code, 0, err)
    plan = json.loads(out)
    seq.cli("rename", "entity", "account-rename", entity, "--account", "Expenses:Office", "--to", "Expenses:Workspace",
            "--apply", "--plan-hash", plan["plan_hash"], "--json")
    company = load_entity(seq.entity)
    seq.api("teach", lambda: queue.set_taught(company, "HARBOR CAFE", "Expenses:Meals", by="u1", by_name="Pat",
                                              ts="2026-10-16T10:00:00Z"))
    taught = queue.taught_for(company, "HARBOR CAFE")
    seq.api("undo_teach", lambda: queue.clear_taught(company, "HARBOR CAFE", taught["id"]))
    return seq


class SequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.seq = scenario(unittest.TestCase(), Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_every_write_gave_a_change_set_that_equals_a_full_read(self) -> None:
        # The assertions run inside ``Sequence_._apply`` after each write. Here we check that all steps ran.
        self.assertEqual(list(self.seq.sets), [
            "init", "import", "propose_1", "approve", "propose_3", "decline", "propose_4", "categorize",
            "propose_2", "approve_2", "reverse", "correct", "rename", "teach", "undo_teach"])

    def test_import_adds_bank_rows_and_pending_rows_and_no_entries(self) -> None:
        document = self.seq.sets["import"]
        self.assertEqual(document["entries_added"], [])
        self.assertEqual(document["bank_rows"], [])  # A bank row is stored in the ledger when it is posted.
        pending = [row["id"] for row in document["staging"]["pending"]["added"]]
        self.assertEqual(sorted(pending), [t["id"] for t in IMPORT])
        self.assertTrue(document["changed"])

    def test_approve_adds_the_entry_closes_the_item_and_removes_the_pending_row(self) -> None:
        document = self.seq.sets["approve"]
        self.assertEqual(document["books_revision"], document["base_revision"] + 1)
        [entry] = document["entries_added"]
        self.assertEqual(entry["source_id"], "cs-001")
        self.assertEqual({p["account"] for p in entry["postings"]}, {CHECKING, "Expenses:Software"})
        self.assertEqual(sum(Decimal(p["amount"]) for p in entry["postings"]), Decimal("0"))
        [item] = document["review_queue"]
        self.assertEqual((item["id"], item["change"], item["status_before"], item["status_after"], item["closed"]),
                         ("cs-001", "updated", "open", "confirmed", True))
        self.assertEqual(document["staging"]["pending"]["removed"], ["cs-001"])
        self.assertEqual(document["audit"]["count"], len(document["audit"]["events"]))
        self.assertGreaterEqual(document["audit"]["count"], 1)
        self.assertEqual(document["ledger"]["after"]["audit_event_id"], document["audit"]["last_event_id"])
        self.assertEqual(document["ledger"]["before"]["max_entry_id"] + 1, document["ledger"]["after"]["max_entry_id"])
        self.assertTrue(any(row["counterparty"] for row in document["learned_context"]))

    def test_proposal_creates_an_open_item(self) -> None:
        [item] = self.seq.sets["propose_1"]["review_queue"]
        self.assertEqual((item["change"], item["status_before"], item["status_after"], item["closed"]),
                         ("created", None, "open", False))
        self.assertEqual(self.seq.sets["propose_1"]["entries_added"], [])

    def test_decline_closes_the_item_without_an_entry(self) -> None:
        document = self.seq.sets["decline"]
        self.assertEqual(document["entries_added"], [])
        [item] = document["review_queue"]
        self.assertEqual((item["id"], item["status_after"], item["closed"]), ("cs-003", "withdrawn", True))

    def test_categorize_a_proposed_item_posts_the_new_category(self) -> None:
        document = self.seq.sets["categorize"]
        [entry] = document["entries_added"]
        self.assertIn("Expenses:Fees", {p["account"] for p in entry["postings"]})
        [item] = document["review_queue"]
        self.assertEqual(item["status_after"], "corrected")

    def test_reverse_lists_the_reversal_with_the_original_entry(self) -> None:
        document = self.seq.sets["reverse"]
        [entry] = document["entries_added"]
        [reversal] = document["entries_reversed"]
        self.assertEqual(reversal["reversal_entry_id"], entry["id"])
        self.assertEqual(reversal["reverses_source_id"], "cs-002")
        self.assertIsNotNone(reversal["reverses_entry_id"])
        self.assertLess(reversal["reverses_entry_id"], entry["id"])
        self.assertEqual(reversal["replacement_entry_ids"], [])

    def test_correct_lists_the_reversal_and_the_replacement(self) -> None:
        document = self.seq.sets["correct"]
        [reversal] = document["entries_reversed"]
        self.assertEqual(len(document["entries_added"]), 2)
        self.assertEqual(reversal["replacement_entry_ids"], [document["entries_added"][1]["id"]])
        self.assertIn("Expenses:Office", [p["account"] for e in document["entries_added"] for p in e["postings"]])

    def test_rename_lists_the_new_name_and_adds_no_entry(self) -> None:
        document = self.seq.sets["rename"]
        self.assertEqual(document["entries_added"], [])
        [renamed] = document["accounts"]["renamed"]
        self.assertEqual((renamed["from"], renamed["to"]), ("Expenses:Office", "Expenses:Workspace"))
        self.assertIsNotNone(renamed["audit_event_id"])
        self.assertNotIn("Expenses:Office", self.seq.view["accounts"])
        self.assertIn("Expenses:Workspace", self.seq.view["accounts"])
        names = {p["account"] for e in self.seq.view["entries"].values() for p in e["postings"]}
        self.assertNotIn("Expenses:Office", names)

    def test_teach_and_undo_change_only_the_learned_context(self) -> None:
        teach, undo = self.seq.sets["teach"], self.seq.sets["undo_teach"]
        for document in (teach, undo):
            self.assertEqual(document["entries_added"], [])
            self.assertEqual(document["ledger"]["before"], document["ledger"]["after"])
            self.assertTrue(document["changed"])
        [row] = teach["learned_context"]
        self.assertEqual(row["entry"]["taught"]["category"], "Expenses:Meals")
        [row] = undo["learned_context"]
        self.assertNotIn("taught", row["entry"])

    def test_documents_are_stable_json(self) -> None:
        for name, document in self.seq.sets.items():
            with self.subTest(step=name):
                self.assertEqual(json.loads(json.dumps(document, sort_keys=True)), document)
                self.assertEqual(document["schema_version"], 1)
                self.assertEqual(document["kind"], "books-change-set")


class OtherBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.seq = Sequence_(self, self.root)
        entity = str(self.seq.entity)
        self.seq.cli("init", "entity", "init", entity, "--name", "Synthetic Studio")
        self.seq.ingest("import", IMPORT)

    def test_a_read_only_command_gives_an_empty_change_set(self) -> None:
        document = self.seq.cli("list", "queue", "list", "--entity", str(self.seq.entity))
        self.assertFalse(document["changed"])
        self.assertEqual(document["ledger"]["before"], document["ledger"]["after"])
        for name in ("entries_added", "entries_reversed", "bank_rows", "review_queue", "learned_context"):
            self.assertEqual(document[name], [], name)
        self.assertEqual(document["audit"]["count"], 0)

    def test_the_command_output_does_not_change(self) -> None:
        argv = ["queue", "list", "--entity", str(self.seq.entity)]
        plain = run_cli(argv)
        with_set = run_cli(["--change-set-out", str(self.root / "x.json"), *argv])
        self.assertEqual(plain, with_set)

    def test_a_failed_command_is_incomplete_also_when_nothing_changed(self) -> None:
        out = self.root / "fail.json"
        code, _, _ = run_cli(["--change-set-out", str(out), "queue", "confirm", "--entity", str(self.seq.entity),
                              "--item", "missing"])
        self.assertEqual(code, 1)
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertFalse(document["complete"])
        self.assertEqual(document["reason"], "command_failed")
        self.assertFalse(document["changed"])
        with self.assertRaises(changeset.ChangeSetRefused):
            changeset.apply_change_set(self.seq.view, document)

    def test_a_work_events_failure_in_a_command_that_writes_nothing_is_a_finalize_failure(self) -> None:
        out = self.root / "fin.json"
        with mock.patch("bookkeeping.work.work_events", side_effect=RuntimeError("finalize")):
            with self.assertRaises(RuntimeError):
                run_cli(["--work-out", str(self.root / "w.json"), "--change-set-out", str(out), "queue", "list",
                         "--entity", str(self.seq.entity)])
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertFalse(document["complete"])
        self.assertEqual(document["reason"], "finalize_failed")
        self.assertFalse(document["changed"])

    def test_a_command_that_fails_after_a_write_is_incomplete_and_the_reducer_refuses_it(self) -> None:
        entity = self.seq.entity
        self.seq.cli("propose", "queue", "propose", "--entity", str(entity), "--source-id", "cs-001",
                     "--category", "Expenses:Software", "--reasoning", "Tool.")
        out = self.root / "partial.json"
        # The entry is written, then the step after it fails: the command stops with a partial commit.
        with mock.patch.object(queue, "_update_learned_context", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                run_cli(["--change-set-out", str(out), "queue", "confirm", "--entity", str(entity), "--item", "cs-001"])
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(changeset.validate(document), [])
        self.assertFalse(document["complete"])
        self.assertEqual(document["reason"], "command_failed")
        self.assertTrue(document["entries_added"])  # The partial change is visible, but it never claims to be complete.
        with self.assertRaises(changeset.ChangeSetRefused):
            changeset.apply_change_set(self.seq.view, document)
        self.seq.view = changeset.read_view(entity, revision=self.seq.view["revision"])  # The host does a full read.
        self.assertEqual(self.seq.view["position"], document["ledger"]["after"])

    def test_a_nonzero_exit_after_a_write_is_incomplete(self) -> None:
        entity = self.seq.entity
        with self.seq.cap() as receipt:
            (entity / "staging" / "seen-ids.json").write_text('["x"]', encoding="utf-8")
            receipt.failed = True
        self.assertFalse(receipt.change_set["complete"])
        self.assertTrue(receipt.change_set["changed"])

    def test_a_command_with_no_company_gives_an_empty_change_set(self) -> None:
        out = self.root / "none.json"
        run_cli(["--change-set-out", str(out), "procedures", "list"])
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(changeset.validate(document), [])
        self.assertFalse(document["changed"])

    def test_work_out_and_change_set_out_work_together(self) -> None:
        work_out, set_out = self.root / "w.json", self.root / "c.json"
        code, _, err = run_cli(["--work-out", str(work_out), "--change-set-out", str(set_out), "queue", "propose",
                                "--entity", str(self.seq.entity), "--source-id", "cs-001", "--category",
                                "Expenses:Software", "--reasoning", "Tool."])
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(work_out.read_text())["work"])
        self.assertEqual(json.loads(set_out.read_text())["review_queue"][0]["status_after"], "open")

    def test_a_bank_row_status_change_is_listed(self) -> None:
        entity = self.seq.entity
        with self.seq.cap() as receipt:
            conn = sqlite3.connect(entity / "ledger.sqlite")
            try:
                with conn:
                    conn.execute("INSERT INTO source_transactions (id, payload_json, status, imported_at) "
                                 "VALUES ('cs-x', '{\"id\": \"cs-x\"}', 'posted', '2026-10-01T00:00:00Z')")
            finally:
                conn.close()
        self.assertEqual(receipt.change_set["bank_rows"][0]["change"], "added")
        self.seq._apply("add_row", receipt.change_set)
        with self.seq.cap() as receipt:
            conn = sqlite3.connect(entity / "ledger.sqlite")
            try:
                with conn:
                    conn.execute("UPDATE source_transactions SET status='transfer-match' WHERE id='cs-x'")
            finally:
                conn.close()
        [row] = receipt.change_set["bank_rows"]
        self.assertEqual((row["change"], row["status_before"], row["status_after"]), ("status-changed", "posted", "transfer-match"))
        self.seq._apply("status", receipt.change_set)

    def test_a_payload_only_change_of_a_bank_row_is_listed(self) -> None:
        entity = self.seq.entity

        def write(sql: str, *params: str) -> None:
            conn = sqlite3.connect(entity / "ledger.sqlite")
            try:
                with conn:
                    conn.execute(sql, params)
            finally:
                conn.close()

        with self.seq.cap() as receipt:
            write("INSERT INTO source_transactions (id, payload_json, status, imported_at) VALUES "
                  "('cs-p', '{\"memo\": \"one\"}', 'posted', '2026-10-01T00:00:00Z')")
        self.seq._apply("payload_added", receipt.change_set)
        with self.seq.cap() as receipt:  # The same status and the same time. Only the payload differs.
            write("UPDATE source_transactions SET payload_json = '{\"memo\": \"two\"}' WHERE id = 'cs-p'")
        [row] = receipt.change_set["bank_rows"]
        self.assertEqual((row["change"], row["status_before"], row["status_after"]), ("updated", "posted", "posted"))
        self.assertEqual(row["row"], {"memo": "two"})
        self.assertEqual(row["payload_sha256"], changeset.payload_digest({"memo": "two"}))
        self.seq._apply("payload_changed", receipt.change_set)

    def test_stale_skipped_and_out_of_order_change_sets_are_refused(self) -> None:
        entity = str(self.seq.entity)
        view0 = copy.deepcopy(self.seq.view)
        a = self.seq.cli("a", "queue", "propose", "--entity", entity, "--source-id", "cs-001",
                         "--category", "Expenses:Software", "--reasoning", "Tool.")
        b = self.seq.cli("b", "queue", "confirm", "--entity", entity, "--item", "cs-001")
        c = self.seq.cli("c", "ledger", "reverse", "--entity", entity, "--source-id", "cs-001")
        view_c = self.seq.view
        for label, view, document in (
                ("stale replay of b on the newest view", view_c, b),
                ("replay of the same change set", view_c, c),
                ("gap: a is missing before b", view0, b),
                ("out of order: c before a", view0, c)):
            with self.subTest(label):
                with self.assertRaises(changeset.ChangeSetRefused):
                    changeset.apply_change_set(view, document)
        # The right order works, and a refused change set leaves the view as it was.
        view = changeset.apply_change_set(changeset.apply_change_set(changeset.apply_change_set(view0, a), b), c)
        self.assertEqual(view, view_c)

    def test_a_queue_only_change_set_cannot_be_replayed(self) -> None:
        company = load_entity(self.seq.entity)
        first = self.seq.api("teach", lambda: queue.set_taught(company, "HARBOR CAFE", "Expenses:Meals", by="u1",
                                                               by_name="Pat", ts="2026-10-16T10:00:00Z"))
        self.assertEqual(first["ledger"]["before"], first["ledger"]["after"])  # The ledger position did not move.
        with self.assertRaises(changeset.ChangeSetRefused):
            changeset.apply_change_set(self.seq.view, first)

    def test_a_bad_digest_after_the_apply_is_refused(self) -> None:
        document = copy.deepcopy(self.seq.sets["import"])
        document["state"]["after"] = "0" * 64
        document["fingerprint"] = changeset._fingerprint(document)
        with self.assertRaises(changeset.ChangeSetRefused):
            changeset.apply_change_set(changeset.empty_view(), document)

    def test_the_store_pending_rows_and_seen_ids_are_covered(self) -> None:
        entity = self.seq.entity
        with self.seq.cap() as receipt:
            (entity / "staging" / "pending.json").write_text(
                json.dumps([{"id": "sp-1", "date": "2026-10-01", "amount": "-1.00"}]), encoding="utf-8")
            (entity / "staging" / "seen-ids.json").write_text(json.dumps(["cs-001", "sp-1"]), encoding="utf-8")
        staging = receipt.change_set["staging"]
        self.assertEqual([r["id"] for r in staging["store_pending"]["added"]], ["sp-1"])
        self.assertEqual(staging["seen_ids"]["added"], ["cs-001", "sp-1"])
        self.seq._apply("staging", receipt.change_set)

    def test_every_file_in_the_covered_folders_is_covered(self) -> None:
        # A new file under staging/ or learned-context/ must be added to the change set (and COVERED_FILES).
        seen = {str(p.relative_to(self.seq.entity)) for folder in changeset.COVERED_FOLDERS
                for p in (self.seq.entity / folder).rglob("*") if p.is_file()}
        self.assertLessEqual(seen, changeset.COVERED_FILES)

    def test_a_stale_change_set_is_refused_after_a_cycle_that_returns_to_the_same_state(self) -> None:
        company = load_entity(self.seq.entity)
        self.seq.api("teach_a", lambda: queue.set_taught(company, "HARBOR CAFE", "Expenses:Meals", by="u1",
                                                         by_name="Pat", ts="2026-10-16T10:00:00Z"))
        view_a = copy.deepcopy(self.seq.view)
        replace = self.seq.api("teach_b", lambda: queue.set_taught(company, "HARBOR CAFE", "Expenses:Fees", by="u1",
                                                                   by_name="Pat", ts="2026-10-17T10:00:00Z"))
        taught = queue.taught_for(company, "HARBOR CAFE")
        self.seq.api("undo", lambda: queue.clear_taught(company, "HARBOR CAFE", taught["id"]))
        # The undo restored the first instruction: the position and the digest equal those after the first write.
        self.assertEqual(self.seq.view["position"], view_a["position"])
        self.assertEqual(changeset.view_digest(self.seq.view), changeset.view_digest(view_a))
        self.assertEqual(replace["state"]["before"], changeset.view_digest(self.seq.view))
        with self.assertRaises(changeset.ChangeSetRefused):  # Only the revision tells them apart.
            changeset.apply_change_set(self.seq.view, replace)
        view_a["revision"] = 99
        with self.assertRaises(changeset.ChangeSetRefused):
            changeset.apply_change_set(view_a, replace)

    def test_a_change_set_without_revisions_or_with_a_lower_new_revision_is_refused(self) -> None:
        document = self.seq.sets["import"]
        view = changeset.empty_view()
        for base, new in ((None, 1), (0, None), (0, 0), (0, -1)):
            doc = {**copy.deepcopy(document), "base_revision": base, "books_revision": new}
            doc["fingerprint"] = changeset._fingerprint(doc)
            with self.subTest(base=base, new=new):
                with self.assertRaises(changeset.ChangeSetRefused):
                    changeset.apply_change_set(view, doc)

    def test_replica_content_that_differs_with_matching_hashes_is_refused(self) -> None:
        entity = str(self.seq.entity)
        self.seq.cli("propose", "queue", "propose", "--entity", entity, "--source-id", "cs-001",
                     "--category", "Expenses:Software", "--reasoning", "Tool.")
        self.seq.cli("approve", "queue", "confirm", "--entity", entity, "--item", "cs-001")
        with self.seq.cap() as receipt:
            conn = sqlite3.connect(self.seq.entity / "ledger.sqlite")
            try:
                with conn:
                    conn.execute("INSERT INTO source_transactions (id, payload_json, status, imported_at) VALUES "
                                 "('cs-r', '{\"memo\": \"one\"}', 'posted', '2026-10-01T00:00:00Z')")
            finally:
                conn.close()
        self.seq._apply("bank", receipt.change_set)
        with self.seq.cap() as receipt:
            self.seq.cli  # The next change set is an ordinary one.
            (self.seq.entity / "staging" / "seen-ids.json").write_text('["cs-001", "z"]', encoding="utf-8")
        following = receipt.change_set
        position = copy.deepcopy(self.seq.view["position"])
        def corrupt_entry(view): next(iter(view["entries"].values()))["narration"] = "changed"
        def corrupt_posting(view): next(iter(view["entries"].values()))["postings"][0]["amount"] = "9.99"
        def corrupt_audit(view): next(iter(view["audit_events"].values()))["payload"] = {"forged": True}
        def corrupt_bank_payload(view): view["bank_rows"]["cs-r"]["row"] = {"memo": "forged"}  # The stored hash stays.
        def corrupt_queue(view): next(iter(view["review_queue"].values()))["status"] = "open"
        for label, damage in (("entry", corrupt_entry), ("posting", corrupt_posting), ("audit", corrupt_audit),
                              ("bank payload", corrupt_bank_payload), ("queue", corrupt_queue)):
            view = copy.deepcopy(self.seq.view)
            damage(view)
            self.assertEqual(view["position"], position)
            with self.subTest(label):
                with self.assertRaises(changeset.ChangeSetRefused):
                    changeset.apply_change_set(view, following)
        self.seq._apply("seen", following)  # The undamaged view accepts it.

    def test_a_bank_row_payload_hash_that_does_not_match_its_row_is_refused(self) -> None:
        with self.seq.cap() as receipt:
            conn = sqlite3.connect(self.seq.entity / "ledger.sqlite")
            try:
                with conn:
                    conn.execute("INSERT INTO source_transactions (id, payload_json, status, imported_at) VALUES "
                                 "('cs-h', '{\"a\": 1}', 'posted', '2026-10-01T00:00:00Z')")
            finally:
                conn.close()
        document = copy.deepcopy(receipt.change_set)
        document["bank_rows"][0]["row"] = {"a": 2}
        document["fingerprint"] = changeset._fingerprint(document)
        with self.assertRaises(changeset.ChangeSetRefused):
            changeset.apply_change_set(self.seq.view, document)

    def test_an_error_after_the_command_body_gives_an_incomplete_change_set(self) -> None:
        entity = self.seq.entity
        for label, target in (("build", mock.patch.object(changeset, "_build", side_effect=RuntimeError("x"))),
                              ("digest", mock.patch.object(changeset._Side, "digest", side_effect=RuntimeError("x")))):
            with self.subTest(label):
                with target:
                    with self.seq.cap() as receipt:
                        (entity / "staging" / "seen-ids.json").write_text('["q"]', encoding="utf-8")
                self.assertFalse(receipt.change_set["complete"])
                self.assertEqual(receipt.change_set["reason"], "finalize_failed")
                with self.assertRaises(changeset.ChangeSetRefused):
                    changeset.apply_change_set(self.seq.view, receipt.change_set)

    def test_a_failed_write_of_the_document_never_leaves_a_complete_receipt(self) -> None:
        out = self.root / "w.json"
        real = changeset._atomic_write
        calls = []

        def first_fails(path, text):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk full")
            real(path, text)

        argv = ["--change-set-out", str(out), "queue", "propose", "--entity", str(self.seq.entity), "--source-id",
                "cs-001", "--category", "Expenses:Software", "--reasoning", "Tool."]
        with mock.patch.object(changeset, "_atomic_write", first_fails):
            with self.assertRaises(OSError):
                run_cli(argv)
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertFalse(document["complete"])
        self.assertEqual(document["reason"], "write_failed")
        out.unlink()
        with mock.patch.object(changeset, "_atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                run_cli(["--change-set-out", str(out), "queue", "list", "--entity", str(self.seq.entity)])
        self.assertFalse(out.exists())  # No file: the host sees no receipt and does a full read.

    def test_a_document_of_an_earlier_run_is_removed_before_the_command(self) -> None:
        out = self.root / "old.json"
        out.write_text("{}", encoding="utf-8")
        with mock.patch.object(cli, "_dispatch", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_cli(["--change-set-out", str(out), "queue", "list", "--entity", str(self.seq.entity)])
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(document["kind"], "books-change-set")  # The new document, never the old one

    def test_a_corrupt_staging_or_learned_context_file_is_not_read_as_empty(self) -> None:
        entity = self.seq.entity
        targets = ("staging/pending-categorization.json", "staging/seen-ids.json", "staging/duplicate-candidates.json",
                   "staging/pending.json", "staging/context-history.json", "staging/split-templates.json",
                   "staging/source-id-aliases.json", "learned-context/counterparties.json",
                   "learned-context/accrual-rules.json")
        for name in targets:
            path = entity / name
            original = path.read_text(encoding="utf-8") if path.exists() else None
            for bad in ("{broken", '"a string"'):
                with self.subTest(name=name, bad=bad):
                    with self.seq.cap() as receipt:
                        path.parent.mkdir(exist_ok=True)
                        path.write_text(bad, encoding="utf-8")
                    self.assertFalse(receipt.change_set["complete"])
                    self.assertEqual(receipt.change_set["reason"], "state_unreadable")
                    with self.assertRaises(changeset.ChangeSetError):
                        changeset.read_view(entity)
            if original is None:
                path.unlink()
            else:
                path.write_text(original, encoding="utf-8")

    def test_split_templates_accrual_rules_aliases_and_context_history_are_covered(self) -> None:
        entity = self.seq.entity
        company = load_entity(entity)
        with self.seq.cap() as receipt:
            queue.save_split_template(company, "halves", ["Expenses:Software=10.00", "Expenses:Meals=-10.00"])
            (entity / "staging" / "source-id-aliases.json").write_text('[{"from": "a", "to": "b"}]', encoding="utf-8")
            (entity / "learned-context").mkdir(exist_ok=True)
            (entity / "learned-context" / "accrual-rules.json").write_text('{"version": 1, "rules": []}', encoding="utf-8")
            (entity / "staging" / "context-history.json").write_text(json.dumps(
                {"schema_version": 1, "rows": [{"source_id": "ctx-1", "status": "context-only"}]}), encoding="utf-8")
        change = receipt.change_set
        self.assertEqual({row["path"] for row in change["documents"]},
                         {"staging/split-templates.json", "staging/source-id-aliases.json",
                          "learned-context/accrual-rules.json"})
        self.assertEqual([row["source_id"] for row in change["staging"]["context_history"]["added"]], ["ctx-1"])
        self.seq._apply("files", change)
        with self.seq.cap() as receipt:
            (entity / "staging" / "source-id-aliases.json").unlink()
        self.assertEqual([(r["path"], r["change"]) for r in receipt.change_set["documents"]],
                         [("staging/source-id-aliases.json", "removed")])
        self.seq._apply("files_removed", receipt.change_set)

    def test_a_null_or_missing_bank_payload_never_matches_a_stored_hash(self) -> None:
        with self.seq.cap() as receipt:
            conn = sqlite3.connect(self.seq.entity / "ledger.sqlite")
            try:
                with conn:
                    conn.execute("INSERT INTO source_transactions (id, payload_json, status, imported_at) VALUES "
                                 "('cs-n', '{\"a\": 1}', 'posted', '2026-10-01T00:00:00Z')")
            finally:
                conn.close()
        for row_value in (None, [], "text", {}):
            document = copy.deepcopy(receipt.change_set)
            document["bank_rows"][0]["row"] = row_value  # The stored hash stays the hash of the real payload.
            document["fingerprint"] = changeset._fingerprint(document)
            with self.subTest(row=row_value):
                with self.assertRaises(changeset.ChangeSetRefused):
                    changeset.apply_change_set(self.seq.view, document)
        # A damaged replica: the row is null in the view but the stored hash is kept.
        self.seq._apply("good", receipt.change_set)
        damaged = copy.deepcopy(self.seq.view)
        damaged["bank_rows"]["cs-n"]["row"] = None
        self.assertNotEqual(changeset.view_digest(damaged), changeset.view_digest(self.seq.view))
        # A stored payload that is not a JSON object is unreadable state. It never collides with a real object.
        for text in ("null", "[]", "not json", '{"unreadable_payload_text": "null"'):
            with self.subTest(payload=text):
                with self.seq.cap() as receipt:
                    conn = sqlite3.connect(self.seq.entity / "ledger.sqlite")
                    try:
                        with conn:
                            conn.execute("INSERT OR REPLACE INTO source_transactions (id, payload_json, status, "
                                         "imported_at) VALUES ('cs-bad', ?, 'posted', '2026-10-01T00:00:00Z')", (text,))
                    finally:
                        conn.close()
                self.assertFalse(receipt.change_set["complete"])
                self.assertEqual(receipt.change_set["reason"], "state_unreadable")
                with self.assertRaises(changeset.ChangeSetError):
                    changeset.read_view(self.seq.entity)
        conn = sqlite3.connect(self.seq.entity / "ledger.sqlite")
        try:
            with conn:
                conn.execute("DELETE FROM source_transactions WHERE id = 'cs-bad'")
        finally:
            conn.close()

    def test_an_ingest_that_fails_to_finalize_never_leaves_a_complete_receipt(self) -> None:
        path = self.root / "second.json"
        path.write_text(json.dumps([_txn("cs-new", "2026-10-09", "-5.00", "NEW VENDOR")]), encoding="utf-8")
        for label, argv_extra, patch in (
                ("work events", ["--work-out", str(self.root / "work.json")],
                 mock.patch("bookkeeping.work.work_events", side_effect=RuntimeError("finalize"))),
                ("work document write", ["--work-out", str(self.root / "work2.json")],
                 mock.patch("bookkeeping.work.write_document", side_effect=OSError("disk"))),
                ("after the ledger write", [],
                 mock.patch("bookkeeping.ingest.print", side_effect=RuntimeError("late"), create=True))):
            out = self.root / "ingest.json"
            sid = f"cs-new-{len(label)}"
            path.write_text(json.dumps([_txn(sid, "2026-10-09", "-5.00", "NEW VENDOR " + label)]), encoding="utf-8")
            with self.subTest(label), patch:
                with self.assertRaises((RuntimeError, OSError)):
                    run_cli([*argv_extra, "--change-set-out", str(out), "ingest", str(path), "--entity",
                             str(self.seq.entity), "--session-id", "s2"])
            document = json.loads(out.read_text(encoding="utf-8"))
            self.assertFalse(document["complete"])
            self.assertTrue(document["changed"])  # The ingest wrote its rows before the failure.

    def test_malformed_global_options_never_leave_an_earlier_receipt(self) -> None:
        for argv in (["--change-set-revision", "bad"], ["--change-set-base-revision", "x"], ["--product-name"],
                     ["--answer-reply-watermark", "z"], ["--change-set-revision"], ["--work-out"]):
            for form in (["--change-set-out", "{}"], ["--change-set-out={}"]):
                out = self.root / "old2.json"
                out.write_text(json.dumps({"complete": True}), encoding="utf-8")
                with self.subTest(argv=argv, form=form[0]):
                    flag = [part.format(out) for part in form]
                    with self.assertRaises(SystemExit):
                        run_cli([*flag, *argv, "queue", "list", "--entity", str(self.seq.entity)])
                    self.assertFalse(out.exists())

    def test_an_invalid_command_never_leaves_an_earlier_receipt(self) -> None:
        out = self.root / "old.json"
        out.write_text(json.dumps({"complete": True}), encoding="utf-8")
        with self.assertRaises(SystemExit):
            run_cli(["--change-set-out", str(out), "queue", "no-such-command"])
        self.assertFalse(out.exists())

    VALID_FILES = {
        "staging/pending-categorization.json": [{"id": "p1", "date": "2026-10-01", "amount": "-1.00"}],
        "staging/duplicate-candidates.json": [{"source_id": "d1", "status": "duplicate-candidate"}],
        "staging/pending.json": [{"id": "sp1", "date": "2026-10-01"}],
        "staging/seen-ids.json": ["cs-001", "cs-002"],
        "staging/context-history.json": {"schema_version": 1, "rows": [{"source_id": "c1", "status": "context-only"}]},
        "staging/split-templates.json": {"halves": [{"account": "Expenses:Meals", "amount": "1.00"}]},
        "staging/source-id-aliases.json": [{"source_id": "a1", "status": "confirmed-distinct"}],
        "learned-context/counterparties.json": {"K": {"canonical_category": "", "confirmed_count": 0}},
        "learned-context/accrual-rules.json": {"version": 1, "rules": [], "receipts": []},
    }

    def _write_valid_files(self) -> None:
        with self.seq.cap() as receipt:
            for name, value in self.VALID_FILES.items():
                path = self.seq.entity / name
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps(value), encoding="utf-8")
        self.assertTrue(receipt.change_set["complete"])
        self.seq._apply("valid_files", receipt.change_set)

    def test_every_strict_file_refuses_null_a_wrong_version_and_a_duplicate_from_a_valid_state(self) -> None:
        self._write_valid_files()
        bad = {
            "staging/pending-categorization.json": ["null", "[null]", "{}", '[{"id": "p1"}, {"id": "p1"}]'],
            "staging/duplicate-candidates.json": ["null", "[null]", '[{"source_id": "d1"}, {"source_id": "d1"}]'],
            "staging/pending.json": ["null", "[[]]", '[{"id": "sp1"}, {"id": "sp1"}]'],
            "staging/seen-ids.json": ["null", "[null]", '["a", "a"]'],
            "staging/context-history.json": [
                "null", '{"schema_version": 999, "rows": []}', '{"schema_version": 1}',
                '{"schema_version": 1, "rows": [{"source_id": "c1"}, {"source_id": "c1"}]}',
                '{"schema_version": 1, "rows": [], "extra": 1}'],
            "staging/split-templates.json": ["null", '{"a": null}', '{"a": {}}', '{"a": [], "a": []}'],
            "staging/source-id-aliases.json": ["null", "[null]", "{}"],
            "learned-context/counterparties.json": ["null", '{"k": null}', '{"k": {}, "k": {}}'],
            "learned-context/accrual-rules.json": ["null", '{"rules": null}', '{"rules": [], "rules": []}'],
        }
        self.assertEqual(set(bad), set(self.VALID_FILES))
        for name, variants in bad.items():
            path = self.seq.entity / name
            valid = path.read_text(encoding="utf-8")
            for text in variants:
                with self.subTest(name=name, bad=text):
                    with self.seq.cap() as receipt:
                        path.write_text(text, encoding="utf-8")
                    self.assertFalse(receipt.change_set["complete"], (name, text))
                    self.assertEqual(receipt.change_set["reason"], "state_unreadable")
                    with self.assertRaises(changeset.ChangeSetError):
                        changeset.read_view(self.seq.entity)
                    path.write_text(valid, encoding="utf-8")
        # A missing file is still an empty state, not an error.
        with self.seq.cap() as receipt:
            (self.seq.entity / "staging" / "pending.json").unlink()
        self.assertTrue(receipt.change_set["complete"])
        self.assertEqual(receipt.change_set["staging"]["store_pending"]["removed"], ["sp1"])
        self.seq._apply("file_removed", receipt.change_set)

    def test_a_replica_key_that_is_not_the_id_of_its_row_is_refused(self) -> None:
        entity = str(self.seq.entity)
        self.seq.cli("propose", "queue", "propose", "--entity", entity, "--source-id", "cs-001",
                     "--category", "Expenses:Software", "--reasoning", "Tool.")
        self.seq.cli("approve", "queue", "confirm", "--entity", entity, "--item", "cs-001")
        self.seq.cli("reverse", "ledger", "reverse", "--entity", entity, "--source-id", "cs-001")
        self._write_valid_files()
        view = self.seq.view
        for label, path in (("entries", ("entries",)), ("audit", ("audit_events",)),
                            ("queue", ("review_queue",)), ("pending", ("staging", "pending")),
                            ("duplicates", ("staging", "duplicates")), ("store_pending", ("staging", "store_pending")),
                            ("accounts", ("accounts",)), ("context", ("staging", "context_history"))):
            damaged = copy.deepcopy(view)
            table = damaged
            for part in path:
                table = table[part]
            self.assertTrue(table, label)
            key = sorted(table, key=str)[0]
            table[f"moved-{key}"] = table.pop(key)  # The same row under another key
            with self.subTest(label):
                with self.assertRaises(changeset.ChangeSetRefused):
                    changeset.view_digest(damaged)
        self.assertEqual(changeset.view_digest(view), self.seq.sets["valid_files"]["state"]["after"])
        # Swapping the rows of two keys is refused too.
        damaged = copy.deepcopy(view)
        ids = sorted(damaged["entries"])
        damaged["entries"][ids[0]], damaged["entries"][ids[1]] = damaged["entries"][ids[1]], damaged["entries"][ids[0]]
        with self.assertRaises(changeset.ChangeSetRefused):
            changeset.view_digest(damaged)

    def test_a_missing_ledger_is_not_a_complete_receipt(self) -> None:
        entity = self.seq.entity
        with self.seq.cap() as receipt:
            (entity / "ledger.sqlite").unlink()
        self.assertFalse(receipt.change_set["complete"])
        self.assertEqual(receipt.change_set["reason"], "state_unreadable")
        with self.assertRaises(changeset.ChangeSetRefused):
            changeset.apply_change_set(self.seq.view, receipt.change_set)

    def test_a_ledger_that_is_absent_before_and_after_is_an_empty_state(self) -> None:
        empty = self.root / "none"
        empty.mkdir()
        with changeset.capture(empty, books_revision=1, base_revision=0) as receipt:
            pass
        self.assertTrue(receipt.change_set["complete"])
        self.assertFalse(receipt.change_set["changed"])

    def test_a_failed_command_with_no_company_directory_is_incomplete(self) -> None:
        out = self.root / "nocompany.json"
        code, _, _ = run_cli(["--change-set-out", str(out), "procedures", "show", "no-such-procedure"])
        self.assertNotEqual(code, 0)
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(changeset.validate(document), [])
        self.assertFalse(document["complete"])
        self.assertEqual(document["reason"], "command_failed")
        with self.assertRaises(RuntimeError):
            with changeset.capture(None) as receipt:
                raise RuntimeError("x")
        self.assertFalse(receipt.change_set["complete"])
        with changeset.capture(None) as receipt:
            pass
        self.assertTrue(receipt.change_set["complete"])

    def test_duplicate_candidates_are_listed(self) -> None:
        entity = self.seq.entity
        path = entity / "staging" / "duplicate-candidates.json"
        with self.seq.cap() as receipt:
            path.write_text(json.dumps([{"source_id": "d-1", "status": "duplicate-candidate", "date": "2026-10-01"}]),
                            encoding="utf-8")
        self.assertEqual([r["source_id"] for r in receipt.change_set["staging"]["duplicates"]["added"]], ["d-1"])
        self.seq._apply("dup_added", receipt.change_set)
        with self.seq.cap() as receipt:
            path.write_text("[]", encoding="utf-8")
        self.assertEqual(receipt.change_set["staging"]["duplicates"]["removed"], ["d-1"])
        self.seq._apply("dup_removed", receipt.change_set)

    def _archive_one(self) -> tuple[Path, Path, dict]:
        entity = self.seq.entity
        self.seq.cli("propose", "queue", "propose", "--entity", str(entity), "--source-id", "cs-001",
                     "--category", "Expenses:Software", "--reasoning", "Tool.")
        self.seq.cli("approve", "queue", "confirm", "--entity", str(entity), "--item", "cs-001")
        path = entity / "review-queue" / "cs-001.json"
        archive = entity / "review-queue" / "archive"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:  # The queue module moved the decided item into an archive already.
            raw = next(json.loads(a.read_text(encoding="utf-8"))["items"]["cs-001.json"] for a in archive.glob("*.json"))
        return path, archive, raw

    @unittest.skipUnless(hasattr(queue, "load_all_items"), "needs the strict queue archive reader of the queue module")
    def test_archived_queue_items_are_read_and_a_move_is_not_a_change(self) -> None:
        path, archive, raw = self._archive_one()
        with self.seq.cap() as receipt:
            archive.mkdir(exist_ok=True)
            (archive / "2026-10.json").write_text(
                json.dumps({"version": 1, "items": {"cs-001.json": raw}}), encoding="utf-8")
            if path.exists():
                path.unlink()
        self.assertTrue(receipt.change_set["complete"])
        self.assertFalse(receipt.change_set["changed"])
        self.assertEqual(changeset.read_view(self.seq.entity)["review_queue"]["cs-001"]["status"], "confirmed")

    @unittest.skipUnless(hasattr(queue, "load_all_items"), "needs the strict queue archive reader of the queue module")
    def test_a_corrupt_or_unknown_archive_is_never_read_as_empty_or_as_a_deletion(self) -> None:
        path, archive, raw = self._archive_one()
        archive.mkdir(exist_ok=True)
        good = json.dumps({"version": 1, "items": {"cs-001.json": raw}})
        (archive / "2026-10.json").write_text(good, encoding="utf-8")
        if path.exists():
            path.unlink()
        for bad in ("{not json", json.dumps({"version": 2, "items": {}}), json.dumps({"version": 1, "items": []})):
            with self.subTest(bad=bad[:20]):
                (archive / "2026-10.json").write_text(good, encoding="utf-8")
                with self.seq.cap() as receipt:
                    (archive / "2026-10.json").write_text(bad, encoding="utf-8")
                self.assertFalse(receipt.change_set["complete"])
                self.assertEqual(receipt.change_set["reason"], "state_unreadable")
                with self.assertRaises(changeset.ChangeSetError):
                    changeset.read_view(self.seq.entity)
                with self.assertRaises(changeset.ChangeSetRefused):
                    changeset.apply_change_set(self.seq.view, receipt.change_set)
        (archive / "2026-10.json").write_text(good, encoding="utf-8")

    def test_an_archive_is_refused_when_the_queue_module_has_no_strict_reader(self) -> None:
        path, archive, raw = self._archive_one()
        archive.mkdir(exist_ok=True)
        (archive / "2026-10.json").write_text(json.dumps({"version": 1, "items": {}}), encoding="utf-8")
        with mock.patch.object(queue, "load_all_items", None, create=True):
            with self.assertRaises(changeset.ChangeSetError):
                changeset.load_queue(self.seq.entity)

    def test_an_unreadable_item_file_is_not_a_deletion(self) -> None:
        path, archive, raw = self._archive_one()
        with self.seq.cap() as receipt:
            path.write_text("{broken", encoding="utf-8")
        self.assertFalse(receipt.change_set["complete"])
        self.assertEqual(receipt.change_set["reason"], "state_unreadable")

    @unittest.skipUnless(hasattr(queue, "compact_closed_items"), "needs the queue archive of the queue module")
    def test_a_compaction_during_the_read_does_not_drop_items(self) -> None:
        path, archive, raw = self._archive_one()
        entity = load_entity(self.seq.entity)
        real = queue.load_all_items
        calls = []

        def racing(entity_arg):
            if not calls:  # A compaction runs while the queue is read.
                calls.append(1)
                queue.compact_closed_items(entity)
            return real(entity_arg)

        with mock.patch.object(queue, "load_all_items", racing):
            items = changeset.load_queue(self.seq.entity)
        self.assertEqual(items["cs-001"]["status"], "confirmed")

    def test_a_replaced_ledger_sets_reset(self) -> None:
        entity = self.seq.entity
        self.seq.cli("propose", "queue", "propose", "--entity", str(entity), "--source-id", "cs-001",
                     "--category", "Expenses:Software", "--reasoning", "Tool.")
        self.seq.cli("approve", "queue", "confirm", "--entity", str(entity), "--item", "cs-001")
        other = self.root / "other"
        run_cli(["entity", "init", str(other), "--name", "Other Studio"])
        with self.seq.cap() as receipt:
            (entity / "ledger.sqlite").write_bytes((other / "ledger.sqlite").read_bytes())
        self.assertTrue(receipt.change_set["reset"])
        self.seq._apply("reset", receipt.change_set)

    def test_validate_refuses_a_wrong_version_and_a_wrong_fingerprint(self) -> None:
        document = copy.deepcopy(self.seq.sets["import"])
        self.assertEqual(changeset.validate(document), [])
        self.assertTrue(changeset.validate({**document, "schema_version": 2}))
        self.assertTrue(changeset.validate({**document, "changed": not document["changed"]}))
        with self.assertRaises(ValueError):
            changeset.apply_change_set(changeset.empty_view(), {**document, "schema_version": 2})

    def test_change_sets_do_not_change_the_books(self) -> None:
        # The same writes with and without capture give the same ledger content.
        a, b = self.root / "a", self.root / "b"
        for entity, flag in ((a, True), (b, False)):
            argv = ["entity", "init", str(entity), "--name", "Synthetic Studio"]
            run_cli(["--change-set-out", str(self.root / "ignored.json"), *argv] if flag else argv)
            path = self.root / "same.json"
            path.write_text(json.dumps(IMPORT), encoding="utf-8")
            argv = ["ingest", str(path), "--entity", str(entity), "--session-id", "same"]
            run_cli(["--change-set-out", str(self.root / "ignored.json"), *argv] if flag else argv)
        self.assertEqual(independent_view(a)["entries"], independent_view(b)["entries"])


class ContractTests(unittest.TestCase):
    def test_change_set_out_is_an_output_file_that_a_host_can_rewrite(self) -> None:
        argv = ["--change-set-out", "@output/change.json", "--change-set-revision", "41", "queue", "confirm",
                "--entity", "@entity", "--item", "x"]
        spec = command_spec(argv)
        self.assertIn("change_set_out", spec["output_fields"])
        self.assertTrue(spec["mutates"])
        rewritten = rewrite_paths(argv, {"@output/change.json": "/tmp/out/change.json", "@entity": "/tmp/co"})
        self.assertEqual(rewritten[:2], ["--change-set-out=/tmp/out/change.json", "--change-set-revision=41"])
        args, _, path = parse_command(rewritten)
        self.assertEqual((args.change_set_out, args.change_set_revision, path),
                         (Path("/tmp/out/change.json"), 41, ("queue", "confirm")))
        self.assertNotIn("change_set_out", command_spec(["queue", "list", "--entity", "@entity"])["output_fields"])


if __name__ == "__main__":
    unittest.main()
