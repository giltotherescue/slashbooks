"""``books entity account-rename``: give an account a new name and keep its history.

The rename moves every posting, balance assertion, child account, mapping and rule to the new name in one ledger
transaction with the audit event ``account-renamed`` and a new seal. Amounts do not change. All data is synthetic.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from golden_support import run_cli  # noqa: E402

from bookkeeping import account_rename  # noqa: E402
from bookkeeping.automation_policy import PreparationDenied  # noqa: E402
from bookkeeping.coverage import load_statements, record_feed_window, record_statement  # noqa: E402
from bookkeeping.entity import add_account, load_entity  # noqa: E402
from bookkeeping.feed_proof import prove  # noqa: E402
from bookkeeping.ledger.importer import _atomic_ledger_write, _ledger_account_for_txn, check_integrity  # noqa: E402
from bookkeeping.ledger.model import Balance, Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.preparation_runtime import supported_preparation_command  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

BANK = "Assets:Bank:Checking"
OFFICE = "Expenses:Office"
COSTS = "Expenses:Office-Costs"
SUPPLIES = "Expenses:Office:Supplies"
OPERATING = "Assets:Bank:Operating"


def books(*argv: str):
    code, out, err = run_cli(list(argv))
    return code, (json.loads(out) if out.strip().startswith(("{", "[")) else out), err


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class AccountRenameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "company"
        code, out, err = run_cli(["entity", "init", str(self.root), "--name", "Test Co"])
        self.assertEqual(code, 0, (out, err))
        self.store = LedgerStore(self.root / "ledger.sqlite")

    # -- helpers ---------------------------------------------------------------------------------------------

    def rename(self, old: str = OFFICE, new: str = COSTS, *extra: str):
        return books("entity", "account-rename", str(self.root), "--account", old, "--to", new, "--json", *extra)

    def rename_applied(self, old: str = OFFICE, new: str = COSTS):
        code, plan, _ = self.rename(old, new)
        self.assertEqual((code, plan["status"]), (0, "preview"), plan)
        code, done, _ = self.rename(old, new, "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"]), (0, "applied"), done)
        return plan, done

    def post(self, account: str, amount: str, source_id: str, day: date = date(2026, 2, 3), other: str = BANK) -> None:
        _atomic_ledger_write(load_entity(self.root), [], [Entry(
            date=day, narration="Synthetic", payee="Oak Co", meta=(("source-id", source_id),),
            postings=(Posting(account, Decimal(amount)), Posting(other, -Decimal(amount))))], "test-seed", None, "seed")

    def catalog(self) -> set[str]:
        return self.store.load_account_names()

    def balances(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        with self.store.connection() as conn:
            for account, amount in conn.execute("SELECT account, amount FROM postings"):
                totals[account] = totals.get(account, Decimal("0")) + Decimal(amount)
        return totals

    def state(self):
        files = sorted((path.relative_to(self.root).as_posix(), path.read_bytes())
                       for path in self.root.rglob("*") if path.is_file() and path.name != ".books.lock")
        return self.store.content_digest(), len(self.store.load_audit_events()), files

    def assert_refused(self, code_name: str, old: str = OFFICE, new: str = COSTS):
        before = self.state()
        for extra in ((), ("--apply",)):
            code, result, _ = self.rename(old, new, *extra)
            self.assertEqual((code, result["status"], result["errors"][0]["code"]), (1, "blocked", code_name), result)
        self.assertEqual(self.state(), before, "a refusal writes nothing")
        return result["errors"][0]

    def assert_valid(self) -> None:
        self.assertEqual(self.store.verify_audit_chain(), [])
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        last = self.store.load_audit_events()[-1]
        self.assertEqual((last["type"], last["payload"]["store_sha256"]), ("ledger-store-sealed", self.store.content_digest()))

    # -- the rename ------------------------------------------------------------------------------------------

    def test_leaf_rename_moves_every_posting_and_keeps_every_amount(self):
        self.post(OFFICE, "120.00", "s1")
        self.post(OFFICE, "30.50", "s2", date(2026, 3, 9))
        before = self.balances()
        with self.store.connection() as conn:
            row = dict(conn.execute("SELECT id, currency, open_date FROM accounts WHERE name=?", (OFFICE,)).fetchone())
            marker = self.store.load_open_date_provenance(conn)["accounts"][OFFICE]
        digest = self.store.content_digest()
        code, plan, _ = self.rename()
        self.assertEqual(self.store.content_digest(), digest, "the preview writes nothing")
        self.assertEqual(plan["effects"]["accounts"], [{"from": OFFICE, "to": COSTS, "postings": 2, "balance_assertions": 0}])
        self.assertEqual((plan["effects"]["entries_changed"], plan["effects"]["amounts_changed"]), (0, 0))
        code, done, _ = self.rename(OFFICE, COSTS, "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"], done["postings_relabelled"]), (0, "applied", 2), done)
        self.assertNotIn(OFFICE, self.catalog())
        self.assertIn(COSTS, self.catalog())
        after = self.balances()
        self.assertEqual(after.pop(COSTS), before.pop(OFFICE))
        self.assertEqual(after, before, "every other balance is the same")
        with self.store.connection() as conn:
            moved = dict(conn.execute("SELECT id, currency, open_date FROM accounts WHERE name=?", (COSTS,)).fetchone())
            record = self.store.load_open_date_provenance(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 2)
        self.assertEqual(moved, row, "the catalog row keeps its id, currency and open date")
        self.assertEqual(record["accounts"].get(COSTS), marker)
        self.assertNotIn(OFFICE, record["accounts"])
        self.assertNotIn("stale", record)
        [event] = [e for e in self.store.load_audit_events() if e["type"] == "account-renamed"]
        self.assertEqual((event["payload"]["from"], event["payload"]["to"], event["payload"]["accounts"]),
                         (OFFICE, COSTS, [[OFFICE, COSTS]]))
        self.assertEqual(event["payload"]["plan_hash"], plan["plan_hash"])
        self.assert_valid()
        # Reports show the past entries under the new name.
        code, report, _ = books("report", "pnl", "--entity", str(self.root), "--from", "2026-01-01",
                                "--to", "2026-12-31", "--format", "json")
        self.assertEqual(code, 0, report)
        self.assertIn(COSTS, json.dumps(report))
        self.assertNotIn(f'"{OFFICE}"', json.dumps(report))

    def test_parent_rename_moves_its_children_and_parent_references(self):
        add_account(self.root, SUPPLIES, open_date="2026-01-01")
        self.post(OFFICE, "10.00", "s1")
        self.post(SUPPLIES, "25.00", "s2")
        write_json(self.root / "learned-context" / "accrual-rules.json",
                   {"rules": [{"id": "r1", "match": {"accounts": [OFFICE]}}, {"id": "r2", "match": {"accounts": [SUPPLIES]}}]})
        before = self.balances()
        plan, done = self.rename_applied()
        self.assertEqual([(a["from"], a["to"]) for a in done["accounts"]],
                         [(OFFICE, COSTS), (SUPPLIES, "Expenses:Office-Costs:Supplies")])
        self.assertEqual({a for a in self.catalog() if a.startswith("Expenses:Office")},
                         {COSTS, "Expenses:Office-Costs:Supplies"})
        rules = read_json(self.root / "learned-context" / "accrual-rules.json")["rules"]
        self.assertEqual([r["match"]["accounts"] for r in rules], [[COSTS], ["Expenses:Office-Costs:Supplies"]])
        after = self.balances()
        self.assertEqual((after[COSTS], after["Expenses:Office-Costs:Supplies"]), (before[OFFICE], before[SUPPLIES]))
        self.assert_valid()

    def test_settings_and_records_follow_the_new_name(self):
        self.post(OFFICE, "40.00", "s1")
        self.assertEqual(run_cli(["tax", "set", "--entity", str(self.root), "--return-form", "schc"])[0], 0)
        self.assertEqual(run_cli(["tax", "set", "--entity", str(self.root), "--account", OFFICE,
                                  "--line", "schc/II/18", "--account-number", "6100"])[0], 0)
        write_json(self.root / "learned-context" / "counterparties.json",
                   {"OAK CO": {"canonical_category": OFFICE, "confirmed_count": 3}})
        item = {"source_id": "q1", "status": "open", "proposed_category": OFFICE, "alternatives": [OFFICE, "Expenses:Fees"]}
        decided = {"source_id": "q2", "status": "confirmed", "confirmed_category": OFFICE}
        write_json(self.root / "review-queue" / "q1.json", item)
        write_json(self.root / "review-queue" / "q2.json", decided)
        write_json(self.root / "staging" / "split-templates.json",
                   {"t1": {"split_postings": [{"account": OFFICE, "amount": "1.00"}],
                           "transaction": {"id": "raw", "note": OFFICE}}})
        _, done = self.rename_applied()
        config = read_json(self.root / "entity.json")
        self.assertEqual(config["tax_handoff"]["line_overrides"]["schc"], {COSTS: "schc/II/18"})
        self.assertEqual(config["tax_handoff"]["account_numbers"], {COSTS: "6100"})
        self.assertEqual(read_json(self.root / "learned-context" / "counterparties.json")["OAK CO"]["canonical_category"], COSTS)
        moved = read_json(self.root / "review-queue" / "q1.json")
        self.assertEqual((moved["proposed_category"], moved["alternatives"]), (COSTS, [COSTS, "Expenses:Fees"]))
        self.assertEqual(read_json(self.root / "review-queue" / "q2.json"), decided, "a decided item is history")
        template = read_json(self.root / "staging" / "split-templates.json")["t1"]
        self.assertEqual(template["split_postings"][0]["account"], COSTS)
        self.assertEqual(template["transaction"]["note"], OFFICE, "a source row inside a record stays")
        self.assertEqual(sorted(done["files_updated"]), ["entity.json", "learned-context/counterparties.json",
                                                        "review-queue/q1.json", "staging/split-templates.json"])
        code, mapping, _ = books("tax", "map", "--entity", str(self.root), "--format", "json")
        self.assertEqual(code, 0, mapping)
        self.assertIn(COSTS, json.dumps(mapping))
        self.assert_valid()

    def test_bank_account_rename_keeps_mappings_statements_and_reconciliations(self):
        add_account(self.root, OPERATING, open_date="2026-01-01", financial_kind="bank")
        config = read_json(self.root / "entity.json")
        config["bank_account_mappings"] = {"feed-op": OPERATING, OPERATING: OPERATING}
        config["csv_account_mappings"] = {"statement:op": {"ledger_account": OPERATING, "confirmed": True}}
        write_json(self.root / "entity.json", config)
        self.post(OFFICE, "75.00", "s1", other=OPERATING)
        with self.store.transaction() as conn:
            self.store.insert_balances([Balance(date(2026, 2, 28), OPERATING, Decimal("-75.00"))], conn)
            self.store.append_audit_event("ledger-store-sealed", {"session_id": "t", "entries": 0,
                                                                  "store_sha256": self.store.content_digest(conn)}, conn)
        record_statement(self.root, {"statement_id": "st-1", "account": OPERATING, "kind": "statement",
                                     "period_start": "2026-02-01", "period_end": "2026-02-28",
                                     "opening_balance": "0.00", "closing_balance": "-75.00"})
        record_feed_window(self.root, account=OPERATING, start=date(2026, 2, 1), end=date(2026, 2, 28), source="feed-op")
        write_json(self.root / "reports" / "reconciliation.json", [{"account": OPERATING, "as_of": "2026-02-28"}])
        new = "Assets:Bank:Main-Operating"
        _, done = self.rename_applied(OPERATING, new)
        self.assertEqual(done["balance_assertions_relabelled"], 1)
        config = read_json(self.root / "entity.json")
        self.assertEqual(config["bank_account_mappings"], {"feed-op": new, OPERATING: new},
                         "the keys are provider identifiers; only the values change")
        self.assertEqual(config["csv_account_mappings"]["statement:op"],
                         {"ledger_account": new, "confirmed": True, "former_ledger_accounts": [OPERATING]})
        records, findings = load_statements(self.root)
        self.assertEqual((findings, {r.account for r in records}), ([], {new}))
        self.assertEqual(read_json(self.root / "reports" / "reconciliation.json")[0]["account"], new)
        self.assertEqual(self.store.load_financial_kinds().get(new), "bank")
        self.assertEqual([b.account for b in self.store.load_balances()], [new])
        # A CSV row parsed before the rename still matches its mapping and posts to the new name.
        row = {"id": "csv-1", "accountId": OPERATING, "csvMappingKey": "statement:op"}
        self.assertEqual(_ledger_account_for_txn(row, config["bank_account_mappings"], config["csv_account_mappings"]), new)
        self.assert_valid()

    def test_balance_prove_gives_the_same_result_under_the_new_name(self):
        from test_feed_proof import post, reading
        from test_tieout import make_company
        company = make_company(Path(self.tmp.name), {"feed-op": OPERATING})
        store = LedgerStore(company / "ledger.sqlite")
        store.initialize()
        with store.transaction() as conn:
            store.set_meta("canonical", "true", conn)
            store.set_meta("account_catalog", "sqlite", conn)
        post(company, OPERATING, "2026-08-20", "500.00")
        post(company, OPERATING, "2026-09-10", "1000.00")
        post(company, OPERATING, "2026-09-25", "-200.00", other="Expenses:Software")
        with store.transaction() as conn:
            store.append_audit_event("ledger-store-sealed", {"session_id": "t", "entries": 0,
                                                             "store_sha256": store.content_digest(conn)}, conn)
        record_feed_window(company, account=OPERATING, start=date(2026, 8, 1), end=date(2026, 10, 5), source="feed-op")
        feed = [{"account_id": "feed-op", "account_type": "checking", "time_zone": "UTC", "readings": [reading("1300.00")]}]
        before = prove(company, feed, as_of=date(2026, 10, 20))["months"]
        self.assertEqual([row["reason"] for row in before], ["proven", "proven"])
        new = "Assets:Bank:Main-Operating"
        plan = account_rename.preview(company, OPERATING, new)
        account_rename.apply(company, OPERATING, new, plan["plan_hash"])
        after = prove(company, feed, as_of=date(2026, 10, 20))["months"]
        swap = json.loads(json.dumps(after).replace(new, OPERATING))
        self.assertEqual(swap, before)
        self.assertEqual({row["ledger_account"] for row in after}, {new})
        self.assertEqual(store.verify_audit_chain(), [])

    def test_closed_periods_do_not_block_and_the_preview_lists_them(self):
        self.post(OFFICE, "10.00", "s1", date(2026, 2, 3))
        self.post(OFFICE, "20.00", "s2", date(2026, 5, 3))
        config = read_json(self.root / "entity.json")
        config.update(closed_periods=["2026-05"], closed_through="2026-02-28")
        write_json(self.root / "entity.json", config)
        plan, done = self.rename_applied()
        self.assertEqual(plan["effects"]["locked_months_with_postings"], ["2026-02", "2026-05"])
        self.assertEqual(done["locked_months_with_postings"], ["2026-02", "2026-05"])
        self.assert_valid()

    def test_an_adopted_display_name_is_cleared_and_the_number_stays(self):
        from bookkeeping import chart_presentation
        request = {"operation_id": "p1", "account": OFFICE, "display_name": "Office Expenses", "number": "6100"}
        preview = chart_presentation.update(self.root, request)
        chart_presentation.update(self.root, request, apply=True, plan_hash=preview["plan_hash"])
        _, done = self.rename_applied()
        self.assertEqual(done["display_name_cleared"], "Office Expenses")
        with self.store.connection() as conn:
            records = chart_presentation.read(conn)
        self.assertEqual(records, {COSTS: {"display_name": None, "number": "6100"}})
        self.assertEqual(chart_presentation.label(COSTS, records), "Expenses › Office-Costs")
        self.assert_valid()

    def test_case_only_rename_is_allowed(self):
        self.rename_applied("Expenses:Bank-Fees", "Expenses:BANK-Fees")
        self.assertIn("Expenses:BANK-Fees", self.catalog())

    # -- retries ---------------------------------------------------------------------------------------------

    def test_retry_is_a_no_op_success(self):
        self.post(OFFICE, "10.00", "s1")
        plan, _ = self.rename_applied()
        before = self.state()
        for extra in (("--apply", "--plan-hash", plan["plan_hash"]), ("--apply",), ()):
            code, retry, _ = self.rename(OFFICE, COSTS, *extra)
            self.assertEqual((code, retry["status"]), (0, "already_applied"), retry)
        self.assertEqual(self.state(), before)
        self.assertEqual(sum(e["type"] == "account-renamed" for e in self.store.load_audit_events()), 1)

    def test_retry_finishes_the_files_after_a_stop_behind_the_ledger_commit(self):
        write_json(self.root / "learned-context" / "counterparties.json", {"OAK CO": {"canonical_category": OFFICE}})
        code, plan, _ = self.rename()
        with mock.patch.object(account_rename, "_write_files", side_effect=OSError("stopped")):
            with self.assertRaises(OSError):
                account_rename.apply(self.root, OFFICE, COSTS, plan["plan_hash"])
        self.assertIn(COSTS, self.catalog())
        self.assertEqual(read_json(self.root / "learned-context" / "counterparties.json")["OAK CO"]["canonical_category"], OFFICE)
        code, pending, _ = self.rename()
        self.assertEqual((pending["status"], pending["files_pending"]), ("already_applied", ["learned-context/counterparties.json"]))
        code, retry, _ = self.rename(OFFICE, COSTS, "--apply")
        self.assertEqual((code, retry["status"], retry["files_completed"]),
                         (0, "already_applied", ["learned-context/counterparties.json"]))
        self.assertEqual(read_json(self.root / "learned-context" / "counterparties.json")["OAK CO"]["canonical_category"], COSTS)
        self.assert_valid()

    def interrupt(self, old: str = OFFICE, new: str = COSTS):
        """Apply the rename and stop it after the ledger commit, before any company file changes."""
        code, plan, _ = self.rename(old, new)
        with mock.patch.object(account_rename, "_write_files", side_effect=OSError("stopped")):
            with self.assertRaises(OSError):
                account_rename.apply(self.root, old, new, plan["plan_hash"])

    def test_an_unfinished_rename_blocks_every_writer_of_the_old_name(self):
        # The review repro: a custom bank account, 75 posted, a stop at the file updates, then a 5 expense
        # through the stale bank mapping.
        add_account(self.root, OPERATING, open_date="2026-01-01", financial_kind="bank")
        config = read_json(self.root / "entity.json")
        config["bank_account_mappings"] = {"feed-op": OPERATING}
        write_json(self.root / "entity.json", config)
        write_json(self.root / "trust-policy.json", {"auto_post_threshold": 1, "queue_all_until_confirmed": False})
        write_json(self.root / "learned-context" / "counterparties.json",
                   {"PAPER CO": {"canonical_category": OFFICE, "confirmed_count": 5, "reset": False}})
        self.post(OFFICE, "75.00", "s1", other=OPERATING)
        new = "Assets:Bank:Main-Operating"
        self.interrupt(OPERATING, new)
        self.assertEqual(read_json(self.root / "entity.json")["bank_account_mappings"], {"feed-op": OPERATING},
                         "the stop left the mapping on the old name")
        before = self.balances()
        self.assertEqual(self.store.verify_audit_chain(), [])
        integrity = check_integrity(load_entity(self.root))
        self.assertEqual(integrity.status, "incomplete-write")
        self.assertIn("account-rename", integrity.message)
        # Intake through the stale mapping is refused; it creates no old account and splits no balance.
        rows = Path(self.tmp.name) / "rows.json"
        rows.write_text(json.dumps([{"id": "r-5", "date": "2026-02-10", "description": "PAPER CO", "amount": "-5.00",
                                     "accountId": "feed-op"}]), encoding="utf-8")
        code, out, err = run_cli(["ingest", str(rows), "--entity", str(self.root), "--source", "test"])
        self.assertNotEqual(code, 0, (out, err))
        # Writers that check integrity refuse; the ledger store itself refuses a writer that does not.
        for writer in (lambda: self.post(OFFICE, "5.00", "s2", other=OPERATING),
                       lambda: add_account(self.root, OPERATING, open_date="2026-01-01"),
                       lambda: add_account(self.root, f"{OPERATING}:Sub", open_date="2026-01-01")):
            with self.assertRaises(ValueError):
                writer()
        for write in (lambda conn: self.store.insert_opens([Open(date(2026, 1, 1), OPERATING)], conn),
                      lambda conn: self.store.insert_balances([Balance(date(2026, 2, 1), OPERATING, Decimal("1"))], conn),
                      lambda conn: self.store.insert_entries([Entry(date=date(2026, 2, 1), narration="x", postings=(
                          Posting(f"{OPERATING}:Sub", Decimal("1")), Posting(OFFICE, Decimal("-1"))))], conn)):
            with self.assertRaises(ValueError) as caught, self.store.transaction() as conn:
                write(conn)
            self.assertIn(f"account-rename PATH --account {OPERATING} --to {new} --apply", str(caught.exception))
        self.assertNotIn(OPERATING, self.catalog())
        self.assertEqual(self.balances(), before)
        self.assertEqual({a: b for a, b in self.balances().items() if "Operating" in a}, {new: Decimal("-75.00")})
        # A second rename waits for the first one.
        for extra in ((), ("--apply",)):
            code, second, _ = self.rename(OFFICE, COSTS, *extra)
            self.assertEqual((code, second["errors"][0]["code"]), (1, "operation_pending"), second)
        self.assertIn(OFFICE, self.catalog())
        # The retry finishes the files, clears the record and seals.
        code, retry, _ = self.rename(OPERATING, new, "--apply")
        self.assertEqual((code, retry["status"], retry["files_completed"]), (0, "already_applied", ["entity.json"]), retry)
        self.assertIsNotNone(retry["finished_event"])
        self.assertEqual(read_json(self.root / "entity.json")["bank_account_mappings"], {"feed-op": new})
        self.assert_valid()
        code, out, err = run_cli(["ingest", str(rows), "--entity", str(self.root), "--source", "test"])
        self.assertEqual(code, 0, (out, err))
        self.assertNotIn(OPERATING, self.catalog())
        self.assertEqual(self.balances()[new], Decimal("-80.00"), "the new row posts under the new name")
        self.assert_valid()
        # After recovery the other rename runs.
        self.rename_applied()
        self.assert_valid()

    def test_balance_prove_and_seal_stay_valid_through_an_interrupted_rename(self):
        add_account(self.root, "Expenses:Paper", open_date="2026-01-01")
        self.post("Expenses:Paper", "12.00", "s1")
        self.interrupt("Expenses:Paper", "Expenses:Stationery")
        self.assertEqual(self.store.verify_audit_chain(), [])
        last = self.store.load_audit_events()[-1]
        self.assertEqual((last["type"], last["payload"]["store_sha256"]), ("ledger-store-sealed", self.store.content_digest()))
        code, pending, _ = self.rename("Expenses:Paper", "Expenses:Stationery")
        self.assertEqual((pending["status"], pending["finish_pending"]), ("already_applied", True))
        code, retry, _ = self.rename("Expenses:Paper", "Expenses:Stationery", "--apply")
        self.assertEqual(retry["files_completed"], [])
        self.assert_valid()
        code, again, _ = self.rename("Expenses:Paper", "Expenses:Stationery", "--apply")
        self.assertEqual((again["status"], again["finished_event"]), ("already_applied", None))

    def test_an_exact_reimport_after_a_rename_finds_the_same_statement_rows(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "statements" / "chase_like_checking.csv"
        from bookkeeping import statement_import as si
        add_account(self.root, OPERATING, open_date="2020-01-01", financial_kind="bank")
        si.save_profile(self.root, fixture, name="op", ledger_account=OPERATING)
        first = si.import_file(self.root, fixture, profile="op", ts="2026-02-01T00:00:00Z")["statements"][0]["counts"]
        self.assertEqual(first["queued_for_review"], 6)
        pending = read_json(self.root / "staging" / "pending-categorization.json")
        new = "Assets:Bank:Main-Operating"
        self.rename_applied(OPERATING, new)
        profile = read_json(self.root / "entity.json")["csv_account_mappings"]["statement:op"]
        self.assertEqual((profile["ledger_account"], profile["former_ledger_accounts"]), (new, [OPERATING]))
        again = si.import_file(self.root, fixture, profile="op", ts="2026-02-02T00:00:00Z")["statements"][0]["counts"]
        self.assertEqual((again["already_imported"], again["queued_for_review"]), (6, 0), again)
        self.assertEqual(read_json(self.root / "staging" / "pending-categorization.json"), pending)
        candidates = self.root / "staging" / "duplicate-candidates.json"
        self.assertFalse(candidates.exists() and read_json(candidates), "no row is held for duplicate review")
        # Saving the profile again keeps the earlier name, so the IDs stay the same.
        si.save_profile(self.root, fixture, name="op", ledger_account=new)
        profile = read_json(self.root / "entity.json")["csv_account_mappings"]["statement:op"]
        self.assertEqual(profile["former_ledger_accounts"], [OPERATING])
        third = si.import_file(self.root, fixture, profile="op", ts="2026-02-03T00:00:00Z")["statements"][0]["counts"]
        self.assertEqual((third["already_imported"], third["queued_for_review"]), (6, 0), third)

    def test_stale_plan_hash_is_refused(self):
        code, plan, _ = self.rename()
        add_account(self.root, "Expenses:Other-Line", open_date="2026-01-01")
        code, stale, _ = self.rename(OFFICE, COSTS, "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))
        self.assertIn(OFFICE, self.catalog())

    def test_a_renamed_category_can_still_be_removed_by_account_remove(self):
        add_account(self.root, "Income:Installs", open_date="2026-01-01")
        self.rename_applied("Income:Installs", "Income:Setup")
        code, plan, _ = books("entity", "account-remove", str(self.root), "--account", "Income:Setup")
        self.assertEqual((code, plan["changes"]), (0, True), plan)
        # A later account with the earlier name has its own account-add event.
        add_account(self.root, "Income:Installs", open_date="2026-01-01")
        code, other, _ = books("entity", "account-remove", str(self.root), "--account", "Income:Installs")
        self.assertEqual(code, 0, other)
        self.assertGreater(other["effects"]["added_event"], plan["effects"]["added_event"])

    # -- refusals --------------------------------------------------------------------------------------------

    def test_names_are_checked(self):
        self.assertEqual(self.assert_refused("invalid_account", "Office", COSTS)["code"], "invalid_account")
        self.assert_refused("invalid_name", OFFICE, "Expenses:Office Costs")
        self.assert_refused("invalid_name", OFFICE, "Expenses:office-costs")
        self.assert_refused("parent_change", OFFICE, "Expenses:Admin:Office")
        self.assert_refused("parent_change", OFFICE, "Income:Office")
        self.assert_refused("same_name", OFFICE, OFFICE)
        self.assert_refused("unknown_account", "Expenses:Never-Added", "Expenses:Other")

    def test_an_existing_name_is_refused_also_with_other_letter_case(self):
        self.assert_refused("name_exists", OFFICE, "Expenses:Software")
        self.assert_refused("name_exists", OFFICE, "Expenses:SOFTWARE")
        add_account(self.root, SUPPLIES, open_date="2026-01-01")
        add_account(self.root, "Expenses:Admin", open_date="2026-01-01")
        add_account(self.root, "Expenses:Admin:Supplies", open_date="2026-01-01")
        error = self.assert_refused("name_exists", OFFICE, "Expenses:Admin")
        self.assertIn("Expenses:Admin", error["refs"])

    def test_system_accounts_keep_their_names(self):
        self.assert_refused("protected_account", BANK, "Assets:Bank:Main")
        self.assert_refused("protected_account", "Equity:Opening-Balances", "Equity:Opening")
        self.assert_refused("protected_account", "Liabilities:CreditCard", "Liabilities:Cards")
        self.assert_refused("reserved_name", OFFICE, "Expenses:Uncategorized")

    def test_a_balance_sheet_group_with_children_keeps_its_name(self):
        add_account(self.root, OPERATING, open_date="2026-01-01")
        add_account(self.root, f"{OPERATING}:Sub", open_date="2026-01-01")
        self.assert_refused("has_children", OPERATING, "Assets:Bank:Main")

    def test_a_move_into_or_out_of_cost_of_sales_is_refused(self):
        add_account(self.root, "Expenses:COGS", open_date="2026-01-01")
        self.assert_refused("classification_change", "Expenses:COGS", "Expenses:Direct")
        self.assert_refused("classification_change", OFFICE, "Expenses:Cost-of-Sales")

    def test_a_staged_source_row_that_names_the_account_is_refused(self):
        write_json(self.root / "staging" / "pending.json", [{"id": "r1", "accountId": "x", "category": OFFICE}])
        self.assertIn("staging/pending.json:[0].category", self.assert_refused("staged_rows_name_account")["refs"])
        # accountId is a source identifier, which a CSV mapping resolves; it does not block.
        write_json(self.root / "staging" / "pending.json", [{"id": "r1", "accountId": OFFICE}])
        self.assertEqual(self.rename()[1]["status"], "preview")

    def test_an_account_that_accrual_records_name_is_refused(self):
        from bookkeeping.accrual.store import SupportingMutation, apply_mutations
        self.store.upgrade()
        with self.store.transaction(immediate=True) as conn:
            apply_mutations(self.store, [SupportingMutation("schedules", "sch-1", {"id": "sch-1", "recognition_account": OFFICE})], conn)
            self.store.append_audit_event("ledger-store-sealed", {"session_id": "t", "entries": 0,
                                                                  "store_sha256": self.store.content_digest(conn)}, conn)
        error = self.assert_refused("records_use_account")
        self.assertEqual(error["refs"], ["schedules sch-1:recognition_account"])

    def test_an_unfinished_operation_is_refused(self):
        (self.root / "review-queue" / ".transactions" / "t1").mkdir(parents=True)
        self.assert_refused("operation_pending")

    def test_a_settings_key_for_both_names_is_refused(self):
        write_json(self.root / "learned-context" / "notes.json", {OFFICE: 1, COSTS: 2})
        self.assert_refused("name_in_use")

    def test_invalid_ledger_integrity_is_refused(self):
        self.post(OFFICE, "10.00", "s1")
        with self.store.transaction() as conn:
            conn.execute("UPDATE postings SET amount='1.00' WHERE account=?", (OFFICE,))
        self.assert_refused("ledger_integrity")

    # -- transport -------------------------------------------------------------------------------------------

    def test_preview_is_not_a_write_and_preparation_refuses_the_command(self):
        argv = ["entity", "account-rename", "/books", "--account", OFFICE, "--to", COSTS]
        self.assertFalse(command_spec(argv)["mutates"])
        self.assertTrue(command_spec(argv + ["--apply", "--plan-hash", "h"])["mutates"])
        with self.assertRaises(PreparationDenied):
            supported_preparation_command(argv + ["--apply"])

    def test_text_output(self):
        code, out, _ = run_cli(["entity", "account-rename", str(self.root), "--account", OFFICE, "--to", COSTS])
        self.assertEqual(code, 0)
        self.assertIn("Preview: Expenses:Office -> Expenses:Office-Costs", out)
        self.assertIn("--apply --plan-hash", out)


if __name__ == "__main__":
    unittest.main()
