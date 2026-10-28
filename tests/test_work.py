"""Tests for work events (bookkeeping.work) and the ``--work-out`` output."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bookkeeping import cli, work  # noqa: E402
from bookkeeping.queue import _update_learned_context  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "work"
SCHEMA_PATH = ROOT / "docs" / "work-event.schema.json"
CHECKING = "Assets:Bank:Operating-Checking"


def _txn(source_id: str, day: str, amount: str, description: str, account: str = "Operating Checking") -> dict:
    return {"id": source_id, "date": day, "amount": amount, "description": description,
            "accountName": account, "accountType": "checking"}


FIRST_IMPORT = [
    _txn("syn-001", "2026-10-01", "-54.99", "DESIGN TOOL MONTHLY"),
    _txn("syn-002", "2026-10-02", "1840.00", "CARD PROCESSOR PAYOUT"),
    _txn("syn-003", "2026-10-03", "-180.00", "HARBOR CAFE"),
    _txn("syn-004", "2026-10-04", "-42.00", "CLOUD HOSTING"),
    _txn("syn-005", "2026-10-05", "-75.00", "OFFICE SUPPLY STORE"),
    _txn("syn-006", "2026-10-06", "-500.00", "TRANSFER TO SAVINGS"),
    _txn("syn-007", "2026-10-06", "500.00", "TRANSFER FROM CHECKING", account="Reserve Savings"),
]


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def ledger_entry_count(entity: Path) -> int:
    path = entity / "ledger.sqlite"
    if not path.exists():
        return 0
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
    finally:
        conn.close()


class Books:
    """A synthetic company directory driven through the public CLI."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.entity = root / "co"
        self.counter = 0

    def write_input(self, name: str, payload: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run(self, *argv: str, expect: int = 0) -> list[dict]:
        self.counter += 1
        out = self.root / f"work-{self.counter}.json"
        before = ledger_entry_count(self.entity)
        code, stdout, stderr = run_cli(["--work-out", str(out), *argv])
        if code != expect:
            raise AssertionError(f"{argv} exited {code}: {stdout}{stderr}")
        document = json.loads(out.read_text(encoding="utf-8"))
        events = document["work"]
        assert_ledger_delta(events, ledger_entry_count(self.entity) - before)
        for event in events:
            errors = work.validate_event(event, engine=True)
            if errors:
                raise AssertionError(f"{argv}: invalid event {errors}: {event}")
        return events

    def init(self) -> list[dict]:
        return self.run("entity", "init", str(self.entity), "--name", "Synthetic Studio")

    def ingest(self, name: str, txns: list[dict], *extra: str) -> list[dict]:
        path = self.write_input(name, txns)
        return self.run("ingest", str(path), "--entity", str(self.entity), "--session-id", "syn-session", *extra)


def assert_ledger_delta(events: list[dict], delta: int) -> None:
    """Every new ledger row is counted exactly once: recorded + corrected."""
    total = 0
    for event in events:
        if event["kind"] == "recorded":
            total += event["counts"]["entries"]
        if event["kind"] == "corrected":
            total += event["counts"]["entries"] + event["counts"]["reversals"]
    if total != delta:
        raise AssertionError(f"work counts {total} entries but the ledger gained {delta}")


def kinds(events: list[dict]) -> list[str]:
    return [event["kind"] for event in events]


def by_kind(events: list[dict], kind: str) -> dict:
    matches = [event for event in events if event["kind"] == kind]
    assert len(matches) == 1, events
    return matches[0]


def run_scenario(root: Path) -> dict[str, list[dict]]:
    """Run one command of each producing type and return its work events."""
    books = Books(root)
    steps: dict[str, list[dict]] = {}
    steps["entity_init"] = books.init()
    entity = str(books.entity)
    steps["ingest"] = books.ingest("first.json", FIRST_IMPORT)
    steps["propose"] = books.run("queue", "propose", "--entity", entity, "--source-id", "syn-001",
                                 "--category", "Expenses:Software", "--reasoning", "Monthly design tool.")
    steps["propose_group"] = books.run("queue", "propose-group", "--entity", entity, "--source-id", "syn-003",
                                       "--source-id", "syn-005", "--category", "Expenses:Office",
                                       "--reasoning", "Office purchases.")
    steps["propose_split"] = books.run("queue", "propose-split", "--entity", entity, "--source-id", "syn-004",
                                       "--posting", "Expenses:Software=30.00", "--posting", "Expenses:Fees=12.00",
                                       "--reasoning", "Shared hosting.")
    steps["propose_transfer"] = books.run("queue", "propose-transfer", "--entity", entity,
                                          "--source-id", "syn-006", "--source-id", "syn-007",
                                          "--reasoning", "Same amount between owned accounts.")
    steps["propose_income"] = books.run("queue", "propose", "--entity", entity, "--source-id", "syn-002",
                                        "--category", "Income:Consulting", "--reasoning", "Client payout.")
    steps["confirm"] = books.run("queue", "confirm", "--entity", entity, "--item", "syn-001")
    steps["confirm_split"] = books.run("queue", "confirm-split", "--entity", entity, "--item", "syn-004")
    transfer_id = next(event for event in steps["propose_transfer"])["subjects"][0]["id"]
    steps["confirm_transfer"] = books.run("queue", "confirm-transfer", "--entity", entity, "--item", transfer_id)
    steps["correct"] = books.run("queue", "correct", "--entity", entity, "--item", "syn-003",
                                 "--category", "Expenses:Meals")
    steps["withdraw"] = books.run("queue", "withdraw", "--entity", entity, "--item", "syn-005")
    steps["confirm_group"] = books.run("queue", "confirm-group", "--entity", entity,
                                       "--category", "Income:Consulting")
    steps["ledger_correct"] = books.run("ledger", "correct", "--entity", entity, "--source-id", "syn-001",
                                        "--new-source-id", "syn-001-fix", "--date", "2026-10-01",
                                        "--description", "Design tool", "--posting", f"{CHECKING}=-54.99",
                                        "--posting", "Expenses:Office=54.99")
    steps["ledger_reverse"] = books.run("ledger", "reverse", "--entity", entity, "--source-id", "syn-002")
    steps["reconcile"] = books.run("reconcile", "--entity", entity, "--account", CHECKING,
                                   "--source-balance", "100.00", "--as-of", "2026-10-12")
    steps["reconcile_resolve"] = books.run("reconcile-resolve", "--entity", entity, "--account", CHECKING,
                                           "--as-of", "2026-10-12", "--note", "Timing difference reviewed.")
    for _ in range(3):
        _update_learned_context(load_entity(books.entity), "SOFTWARE SUBSCRIPTION", "Expenses:Software",
                                corrected=False)
    steps["ingest_auto_post"] = books.ingest(
        "second.json", FIRST_IMPORT + [_txn("syn-008", "2026-10-08", "-20.00", "SOFTWARE SUBSCRIPTION")]
    )
    config_path = books.entity / "entity.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["bank_account_mappings"] = {"feed-checking": CHECKING}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    steps["ingest_duplicate_candidate"] = books.ingest("feed.json", [{
        "id": "feed-legacy-001", "accountId": "feed-checking", "date": "2026-10-08",
        "amount": "-20.00", "description": "SOFTWARE SUBSCRIPTION",
    }], "--source", "banksync")
    return steps


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.steps = run_scenario(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_golden_output_per_command(self) -> None:
        golden = json.loads((FIXTURES / "golden-events.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(golden), sorted(self.steps))
        for name, events in self.steps.items():
            with self.subTest(step=name):
                self.assertEqual(events, golden[name])

    def test_every_engine_kind_is_produced(self) -> None:
        produced = {event["kind"] for events in self.steps.values() for event in events}
        self.assertEqual(produced, set(work.ENGINE_KINDS))

    def test_entity_init_reports_no_work(self) -> None:
        self.assertEqual(self.steps["entity_init"], [])

    def test_imported(self) -> None:
        event = by_kind(self.steps["ingest"], "imported")
        self.assertEqual(event["counts"], {"transactions": 7, "duplicates_skipped": 0})
        self.assertEqual(event["period"], {"from": "2026-10-01", "to": "2026-10-06"})
        self.assertEqual(event["accounts"], [CHECKING, "Assets:Bank:Reserve-Savings"])
        self.assertEqual([s["id"] for s in event["subjects"]], [t["id"] for t in FIRST_IMPORT])
        self.assertEqual(kinds(self.steps["ingest"]), ["imported"])

    def test_imported_counts_skipped_duplicates_and_auto_posting_is_direct(self) -> None:
        events = self.steps["ingest_auto_post"]
        self.assertEqual(kinds(events), ["imported", "recorded"])
        self.assertEqual(by_kind(events, "imported")["counts"], {"transactions": 1, "duplicates_skipped": 7})
        recorded = by_kind(events, "recorded")
        self.assertEqual(recorded["counts"], {"entries": 1, "from_suggestions": 0, "direct": 1})
        self.assertEqual(recorded["breakdown"], [{"label": "Expenses:Software", "count": 1}])

    def test_categorized(self) -> None:
        self.assertEqual(by_kind(self.steps["propose"], "categorized")["counts"], {"suggestions": 1})
        group = by_kind(self.steps["propose_group"], "categorized")
        self.assertEqual(group["counts"], {"suggestions": 2})
        self.assertEqual(group["breakdown"], [{"label": "Expenses:Office", "count": 2}])
        self.assertEqual(by_kind(self.steps["propose_split"], "categorized")["counts"], {"suggestions": 1})
        transfer = by_kind(self.steps["propose_transfer"], "categorized")
        self.assertTrue(transfer["subjects"][0]["id"].startswith("transfer-pair-"))

    def test_recorded_from_suggestions(self) -> None:
        recorded = by_kind(self.steps["confirm"], "recorded")
        self.assertEqual(recorded["counts"], {"entries": 1, "from_suggestions": 1, "direct": 0})
        self.assertEqual(recorded["subjects"], [{"type": "entry", "id": "1", "source_id": "syn-001"}])
        self.assertEqual(recorded["accounts"], [CHECKING])
        split = by_kind(self.steps["confirm_split"], "recorded")
        self.assertEqual(split["breakdown"], [{"label": "Split transaction", "count": 1}])
        transfer = by_kind(self.steps["confirm_transfer"], "recorded")
        self.assertEqual(transfer["breakdown"], [{"label": "Internal transfer", "count": 1}])
        self.assertEqual(by_kind(self.steps["confirm_group"], "recorded")["counts"]["from_suggestions"], 1)

    def test_corrected(self) -> None:
        self.assertEqual(by_kind(self.steps["correct"], "corrected")["counts"],
                         {"entries": 1, "reversals": 0, "withdrawn": 0})
        self.assertEqual(by_kind(self.steps["withdraw"], "corrected")["counts"],
                         {"entries": 0, "reversals": 0, "withdrawn": 1})
        self.assertEqual(by_kind(self.steps["ledger_correct"], "corrected")["counts"],
                         {"entries": 1, "reversals": 1, "withdrawn": 0})
        self.assertEqual(kinds(self.steps["ledger_correct"]), ["corrected"])
        self.assertEqual(by_kind(self.steps["ledger_reverse"], "corrected")["counts"],
                         {"entries": 0, "reversals": 1, "withdrawn": 0})

    def test_reconciled(self) -> None:
        opened = by_kind(self.steps["reconcile"], "reconciled")
        self.assertEqual(opened["counts"], {"accounts": 1, "clean": 0, "open": 1})
        self.assertEqual(opened["subjects"], [{"type": "reconciliation", "id": f"{CHECKING}@2026-10-12"}])
        self.assertEqual(opened["period"], {"from": "2026-10-12", "to": "2026-10-12"})
        resolved = by_kind(self.steps["reconcile_resolve"], "reconciled")
        self.assertEqual(resolved["counts"], {"accounts": 1, "clean": 1, "open": 0})

    def test_flagged(self) -> None:
        events = self.steps["ingest_duplicate_candidate"]
        self.assertEqual(kinds(events), ["flagged"])
        self.assertEqual(events[0]["counts"], {"duplicates": 1})
        self.assertEqual(events[0]["subjects"], [{"type": "source", "id": "banksync:feed-legacy-001"}])

    def test_events_never_contain_host_fields_and_money_stays_in_amounts(self) -> None:
        for events in self.steps.values():
            for event in events:
                for field in work.HOST_FIELDS:
                    self.assertNotIn(field, event)
                text = json.dumps({key: value for key, value in event.items()
                                   if key not in ("amounts", "amounts_truncated")})
                for amount in ("54.99", "1840", "180.00", "500.00"):
                    self.assertNotIn(amount, text)

    def test_amounts_name_the_money_and_accounts_that_were_recorded(self) -> None:
        def rows(step: str, kind: str) -> list[dict]:
            return next(event for event in self.steps[step] if event["kind"] == kind)["amounts"]

        transfer = rows("propose_transfer", "categorized")
        self.assertEqual(transfer[0]["amount"], "500.00")
        self.assertEqual(transfer[0]["accounts"], [CHECKING, "Assets:Bank:Reserve-Savings"])
        entry = rows("confirm_transfer", "recorded")[0]
        self.assertEqual((entry["amount"], entry["accounts"]), ("500.00", [CHECKING, "Assets:Bank:Reserve-Savings"]))
        self.assertEqual(rows("propose", "categorized")[0]["amount"], "54.99")
        self.assertEqual(rows("propose_split", "categorized")[0]["accounts"], ["Expenses:Software", "Expenses:Fees"])
        self.assertEqual(rows("confirm_split", "recorded")[0]["amount"], "42.00")
        self.assertEqual(sorted(row["id"] for row in rows("propose_group", "categorized")), ["syn-003", "syn-005"])
        self.assertEqual(rows("withdraw", "corrected")[0]["amount"], "75.00")
        self.assertEqual([row["amount"] for row in rows("ledger_correct", "corrected")], ["54.99", "54.99"])
        reconciled = rows("reconcile", "reconciled")[0]
        self.assertEqual((reconciled["source_balance"], reconciled["ledger_balance"], reconciled["discrepancy"]),
                         ("100.00", "-776.99", "876.99"))

    def test_amounts_are_additive_and_leave_the_fingerprint_alone(self) -> None:
        for events in self.steps.values():
            for event in events:
                if "amounts" in event:
                    self.assertEqual(work.validate_event(event, engine=True), [])
                    bare = {key: value for key, value in event.items() if key != "amounts"}
                    self.assertEqual(event["fingerprint"], work._fingerprint(bare))

    def test_derived_events_carry_no_amounts(self) -> None:
        events = work.derived_events(Path(self.tmp.name) / "co")
        self.assertTrue(events)
        for event in events:
            self.assertNotIn("amounts", event)
            self.assertEqual(work.validate_event(event, engine=True), [])

    def test_summary_still_has_no_money(self) -> None:
        events = [event for step in self.steps.values() for event in step]
        self.assertNotIn("amount", json.dumps(work.summarize(events)))


class OneCategorySplitTests(unittest.TestCase):
    """A split proposal with one category line is a plain categorization, not a split."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.books = Books(Path(self.tmp.name))
        self.books.init()
        self.books.ingest("first.json", FIRST_IMPORT)
        self.entity = str(self.books.entity)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def propose(self, source_id: str, *postings: str) -> list[dict]:
        argv = ["queue", "propose-split", "--entity", self.entity, "--source-id", source_id,
                "--reasoning", "Categorized by a person."]
        for posting in postings:
            argv += ["--posting", posting]
        return self.books.run(*argv)

    def test_one_category_split_is_labeled_with_its_category(self) -> None:
        proposed = self.propose("syn-004", "Expenses:Software=42.00")
        self.assertEqual(by_kind(proposed, "categorized")["breakdown"],
                         [{"label": "Expenses:Software", "count": 1}])
        recorded = by_kind(self.books.run("queue", "confirm-split", "--entity", self.entity,
                                          "--item", "syn-004"), "recorded")
        self.assertEqual(recorded["breakdown"], [{"label": "Expenses:Software", "count": 1}])

    def test_one_category_split_lines_on_the_same_category_are_not_a_split(self) -> None:
        proposed = self.propose("syn-004", "Expenses:Software=30.00", "Expenses:Software=12.00")
        self.assertEqual(by_kind(proposed, "categorized")["breakdown"],
                         [{"label": "Expenses:Software", "count": 1}])
        recorded = by_kind(self.books.run("queue", "confirm-split", "--entity", self.entity,
                                          "--item", "syn-004"), "recorded")
        self.assertEqual(recorded["breakdown"], [{"label": "Expenses:Software", "count": 1}])

    def test_many_one_category_splits_count_by_category(self) -> None:
        self.propose("syn-001", "Expenses:Software=54.99")
        self.propose("syn-004", "Expenses:Software=42.00")
        self.propose("syn-003", "Expenses:Meals=180.00")
        recorded = []
        for item in ("syn-001", "syn-004", "syn-003"):
            recorded += self.books.run("queue", "confirm-split", "--entity", self.entity, "--item", item)
        labels: dict[str, int] = {}
        for event in recorded:
            for row in event["breakdown"]:
                labels[row["label"]] = labels.get(row["label"], 0) + row["count"]
        self.assertEqual(labels, {"Expenses:Software": 2, "Expenses:Meals": 1})

    def test_two_category_split_is_still_a_split(self) -> None:
        proposed = self.propose("syn-004", "Expenses:Software=30.00", "Expenses:Fees=12.00")
        self.assertEqual(by_kind(proposed, "categorized")["breakdown"],
                         [{"label": "Split transaction", "count": 1}])
        recorded = by_kind(self.books.run("queue", "confirm-split", "--entity", self.entity,
                                          "--item", "syn-004"), "recorded")
        self.assertEqual(recorded["breakdown"], [{"label": "Split transaction", "count": 1}])


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.books = Books(self.root / "a")
        self.books.root.mkdir()
        self.books.init()
        self.books.ingest("first.json", FIRST_IMPORT)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def clone(self) -> Books:
        other = Books(self.root / "b")
        shutil.copytree(self.books.root, other.root)
        return other

    def test_same_change_gives_same_events(self) -> None:
        other = self.clone()
        commands = [
            ("queue", "propose", "--entity", "{e}", "--source-id", "syn-001", "--category",
             "Expenses:Software", "--reasoning", "Design tool."),
            ("queue", "confirm", "--entity", "{e}", "--item", "syn-001"),
            ("reconcile", "--entity", "{e}", "--account", CHECKING, "--source-balance", "-54.99",
             "--as-of", "2026-10-12"),
        ]
        for command in commands:
            left = self.books.run(*(part.format(e=self.books.entity) for part in command))
            right = other.run(*(part.format(e=other.entity) for part in command))
            self.assertTrue(left)
            self.assertEqual(left, right)

    def test_replayed_command_gives_no_events(self) -> None:
        e = str(self.books.entity)
        self.assertEqual(self.books.ingest("again.json", FIRST_IMPORT), [])
        propose = ("queue", "propose", "--entity", e, "--source-id", "syn-001", "--category",
                   "Expenses:Software", "--reasoning", "Design tool.")
        self.assertEqual(kinds(self.books.run(*propose)), ["categorized"])
        self.assertEqual(self.books.run(*propose), [])
        reconcile = ("reconcile", "--entity", e, "--account", CHECKING, "--source-balance", "1.00",
                     "--as-of", "2026-10-12")
        self.assertEqual(kinds(self.books.run(*reconcile)), ["reconciled"])
        self.assertEqual(self.books.run(*reconcile), [])

    def test_changed_suggestion_is_categorized_again(self) -> None:
        e = str(self.books.entity)
        self.books.run("queue", "propose", "--entity", e, "--source-id", "syn-001", "--category",
                       "Expenses:Software", "--reasoning", "Design tool.")
        events = self.books.run("queue", "propose", "--entity", e, "--source-id", "syn-001", "--category",
                                "Expenses:Office", "--reasoning", "Design tool.")
        self.assertEqual(by_kind(events, "categorized")["breakdown"], [{"label": "Expenses:Office", "count": 1}])

    def test_failed_and_read_commands_give_no_events(self) -> None:
        e = str(self.books.entity)
        self.assertEqual(self.books.run("queue", "confirm", "--entity", e, "--item", "missing", expect=1), [])
        self.assertEqual(self.books.run("queue", "list", "--entity", e), [])
        self.assertEqual(self.books.run("report", "pnl", "--entity", e, "--from", "2026-01-01",
                                        "--to", "2026-12-31"), [])

    def test_existing_output_does_not_change(self) -> None:
        other = self.clone()
        commands = [
            ("queue", "propose", "--entity", "{e}", "--source-id", "syn-001", "--category",
             "Expenses:Software", "--reasoning", "Design tool."),
            ("queue", "confirm", "--entity", "{e}", "--item", "syn-001"),
            ("queue", "list", "--entity", "{e}"),
        ]
        for command in commands:
            plain = run_cli([part.format(e=self.books.entity) for part in command])
            with_work = run_cli(["--work-out", str(self.root / "w.json")]
                                + [part.format(e=other.entity) for part in command])
            self.assertEqual(plain[0], with_work[0])
            self.assertEqual(plain[1].replace(str(self.books.entity), "@"),
                             with_work[1].replace(str(other.entity), "@"))
            self.assertEqual(plain[2], with_work[2])
        self.assertEqual(ledger_entry_count(self.books.entity), ledger_entry_count(other.entity))

    def test_snapshot_writes_nothing(self) -> None:
        missing = self.root / "absent"
        with work.capture(missing) as receipt:
            pass
        self.assertEqual(receipt.events, [])
        self.assertFalse(missing.exists())
        with work.capture(None) as receipt:
            pass
        self.assertEqual(receipt.events, [])

    def test_python_capture_matches_cli(self) -> None:
        other = self.clone()
        command = ["queue", "propose", "--entity", "{e}", "--source-id", "syn-002", "--category",
                   "Income:Consulting", "--reasoning", "Payout."]
        with work.capture(self.books.entity) as receipt:
            run_cli([part.format(e=self.books.entity) for part in command])
        self.assertEqual(receipt.events, other.run(*(part.format(e=other.entity) for part in command)))

    def test_work_out_is_local_only(self) -> None:
        (self.books.entity / ".slashbooks-remote.json").write_text("{}", encoding="utf-8")
        code, _, stderr = run_cli(["--work-out", str(self.root / "w.json"), "queue", "list",
                                   "--entity", str(self.books.entity)])
        self.assertEqual(code, 1)
        self.assertIn("WORK_OUTPUT_LOCAL_ONLY", stderr)
        self.assertFalse((self.root / "w.json").exists())


class WorkCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.books = Books(self.root)
        self.books.init()
        self.books.ingest("first.json", FIRST_IMPORT)
        e = str(self.books.entity)
        self.books.run("queue", "propose", "--entity", e, "--source-id", "syn-001", "--category",
                       "Expenses:Software", "--reasoning", "Design tool.")
        self.books.run("queue", "confirm", "--entity", e, "--item", "syn-001")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_text_summary(self) -> None:
        code, stdout, _ = run_cli(["work", "--entity", str(self.books.entity)])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.splitlines(), [
            "Imported 7 transactions.",
            "Recorded 1 entry. 1 came from approved suggestions.",
        ])

    def test_json_summary_is_derived_and_windowed(self) -> None:
        code, stdout, _ = run_cli(["work", "--entity", str(self.books.entity), "--from", "2026-10-02",
                                   "--to", "2026-10-03", "--format", "json"])
        self.assertEqual(code, 0)
        document = json.loads(stdout)
        self.assertTrue(document["derived"])
        self.assertEqual([line["kind"] for line in document["summary"]], ["imported"])
        self.assertEqual(document["summary"][0]["counts"]["transactions"], 2)
        for event in document["work"]:
            self.assertTrue(event["derived"])
            self.assertEqual(work.validate_event(event, engine=True), [])

    def test_empty_window_and_bad_dates(self) -> None:
        code, stdout, _ = run_cli(["work", "--entity", str(self.books.entity), "--from", "2020-01-01",
                                   "--to", "2020-01-31"])
        self.assertEqual((code, stdout.strip()), (0, "No books work found for this period."))
        self.assertEqual(run_cli(["work", "--entity", str(self.books.entity), "--from", "10/01/2026"])[0], 1)

    def test_work_is_a_read_command(self) -> None:
        spec = command_spec(["work", "--entity", str(self.books.entity)])
        self.assertFalse(spec["mutates"])
        self.assertEqual(spec["entity_fields"], ["entity"])


class SummaryTests(unittest.TestCase):
    def test_summary_adds_counts_and_merges_breakdowns(self) -> None:
        events = [
            {"kind": "recorded", "counts": {"entries": 2, "from_suggestions": 2, "direct": 0},
             "breakdown": [{"label": "Expenses:Meals", "count": 2}], "accounts": ["A"],
             "period": {"from": "2026-10-02", "to": "2026-10-03"}},
            {"kind": "imported", "counts": {"transactions": 4, "duplicates_skipped": 1},
             "breakdown": [], "accounts": ["B"], "period": {"from": "2026-10-01", "to": "2026-10-01"}},
            {"kind": "recorded", "counts": {"entries": 1, "from_suggestions": 0, "direct": 1},
             "breakdown": [{"label": "Expenses:Meals", "count": 1}], "accounts": ["C"],
             "period": {"from": "2026-10-05", "to": "2026-10-05"}},
        ]
        lines = work.summarize(events)
        self.assertEqual([line["kind"] for line in lines], ["imported", "recorded"])
        self.assertEqual(lines[1]["counts"], {"direct": 1, "entries": 3, "from_suggestions": 2})
        self.assertEqual(lines[1]["breakdown"], [{"label": "Expenses:Meals", "count": 3}])
        self.assertEqual(lines[1]["period"], {"from": "2026-10-02", "to": "2026-10-05"})
        self.assertEqual(lines[0]["text"], "Imported 4 transactions. Skipped 1 duplicate.")


class SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_schema_matches_validator_constants(self) -> None:
        properties = self.schema["properties"]
        self.assertEqual(properties["schema_version"], {"const": work.SCHEMA_VERSION})
        self.assertEqual(properties["kind"]["enum"], list(work.KINDS))
        self.assertEqual(properties["subjects"]["items"]["properties"]["type"]["enum"], list(work.SUBJECT_TYPES))
        self.assertEqual(properties["actor"]["properties"]["type"]["enum"], list(work.ACTOR_TYPES))
        self.assertEqual(properties["via"]["enum"], list(work.VIA_VALUES))
        rules = {rule["if"]["properties"]["kind"]["const"]: rule["then"]["properties"]["counts"]
                 for rule in self.schema["allOf"]}
        self.assertEqual(set(rules), set(work.KINDS))
        for kind, (required, optional) in work.COUNT_KEYS.items():
            self.assertEqual(rules[kind].get("required", []), list(required))
            self.assertEqual(set(rules[kind]["properties"]), set(required) | set(optional))
        self.assertTrue(set(work.HOST_FIELDS) <= set(properties))

    def test_valid_fixtures(self) -> None:
        fixtures = json.loads((FIXTURES / "valid-events.json").read_text(encoding="utf-8"))
        self.assertEqual({event["kind"] for event in fixtures}, set(work.KINDS))
        for event in fixtures:
            with self.subTest(kind=event["kind"]):
                self.assertEqual(work.validate_event(event), [])
                self._jsonschema(event, valid=True)

    def test_invalid_fixtures(self) -> None:
        fixtures = json.loads((FIXTURES / "invalid-events.json").read_text(encoding="utf-8"))
        for case in fixtures:
            with self.subTest(case=case["name"]):
                self.assertNotEqual(work.validate_event(case["event"]), [])
                if not case.get("validator_only"):
                    # JSON Schema cannot compare two dates; the validator does.
                    self._jsonschema(case["event"], valid=False)

    def test_golden_events_validate_against_schema(self) -> None:
        golden = json.loads((FIXTURES / "golden-events.json").read_text(encoding="utf-8"))
        for events in golden.values():
            for event in events:
                self._jsonschema(event, valid=True)

    def test_engine_mode_rejects_host_fields_and_bad_fingerprints(self) -> None:
        golden = json.loads((FIXTURES / "golden-events.json").read_text(encoding="utf-8"))
        event = dict(golden["confirm"][0])
        self.assertEqual(work.validate_event(event, engine=True), [])
        self.assertNotEqual(work.validate_event({**event, "visibility": "staff"}, engine=True), [])
        self.assertNotEqual(work.validate_event({**event, "counts": {**event["counts"], "entries": 9}},
                                                engine=True), [])

    def test_amounts_validation(self) -> None:
        golden = json.loads((FIXTURES / "golden-events.json").read_text(encoding="utf-8"))
        event = dict(golden["confirm"][0])
        self.assertIn("amounts", event)
        bad_rows = [
            [],
            [{"type": "entry", "id": "1", "accounts": [], "amount": 5.0}],
            [{"type": "entry", "id": "1", "accounts": [], "amount": "5,00"}],
            [{"type": "entry", "id": "1", "accounts": [], "amount": "1e3"}],
            [{"type": "money", "id": "1", "accounts": []}],
            [{"type": "entry", "id": "1", "accounts": [], "memo": "x"}],
            [{"type": "entry", "id": "1", "accounts": "Assets"}],
        ]
        for rows in bad_rows:
            with self.subTest(rows=rows):
                bad = {**event, "amounts": rows}
                self.assertNotEqual(work.validate_event(bad), [])
                self._jsonschema(bad, valid=False)
        self.assertNotEqual(work.validate_event({**event, "amounts_truncated": False}), [])
        self.assertNotEqual(work.validate_event({k: v for k, v in event.items() if k != "amounts"} | {"amounts_truncated": True}), [])
        self._jsonschema(event, valid=True)

    def test_amounts_are_capped(self) -> None:
        rows = [{"type": "suggestion", "id": f"s{i:04d}", "accounts": ["Expenses:Fees"], "amount": "1.00"}
                for i in range(work.MAX_AMOUNT_ROWS + 5)]
        event = work._event("categorized", {"suggestions": len(rows)}, dates=[], accounts=[], labels=[],
                            subjects=[], derived=False, amounts=rows)
        self.assertEqual(len(event["amounts"]), work.MAX_AMOUNT_ROWS)
        self.assertIs(event["amounts_truncated"], True)
        self.assertEqual(work.validate_event(event, engine=True), [])

    def test_money_formatting(self) -> None:
        self.assertEqual(work._money("-54.9"), "-54.90")
        self.assertEqual(work._money("100"), "100.00")
        self.assertEqual(work._money("-54.99", absolute=True), "54.99")
        self.assertEqual(work._money("0.001"), "0.001")
        for bad in ("abc", "NaN", "Infinity", ""):
            self.assertIsNone(work._money(bad))

    def _jsonschema(self, event: dict, *, valid: bool) -> None:
        try:
            import jsonschema
        except ImportError:
            return
        errors = list(jsonschema.Draft202012Validator(self.schema).iter_errors(event))
        self.assertEqual(not errors, valid, errors)


if __name__ == "__main__":
    unittest.main()
