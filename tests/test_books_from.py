"""The books scope (``books_from``) opens income, expense and equity accounts; it never opens a bank account.

Synthetic catch-up company: its books start in 2026, but it must record its 2025 history. A fiscal-year
boundary is not a posting start. Before ``books_from``, every starter category opened on a fixed fallback date
(2026-01-01), so the ledger write refused each 2025 entry with ``category_catalog_conflict``.

The bank account ``Assets:Bank:Operating`` has its own verified start (an explicit ``--open-date``). The starter
``Assets:Bank:Checking`` keeps the fallback date; the books scope does not open it.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bookkeeping import books_from, cli, queue  # noqa: E402
from bookkeeping.books_from import BooksFromError  # noqa: E402
from bookkeeping.entity import add_account, init_entity, load_entity  # noqa: E402
from bookkeeping.ledger.books_policy import BooksPolicyError  # noqa: E402
from bookkeeping.ledger.importer import _atomic_ledger_write, check_integrity, import_transactions  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.open_dates import AccountNotOpenError  # noqa: E402
from bookkeeping.ledger.projections import render_store_ledger  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.ledger.validator import validate  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

FALLBACK = "2026-01-01"
SCOPE = "2025-01-01"
OPERATING = "Assets:Bank:Operating"  # a real bank account with its own verified start
SAVINGS = "Assets:Bank:Savings"
STARTER_BANK = "Assets:Bank:Checking"
SOFTWARE = "Expenses:Software"  # a starter category
CUSTOM = "Expenses:Field-Supplies"  # a category that the host adds later
POLICY_ENV = "SLASHBOOKS_BOOKS_POLICY"


def row(sid: str, day: str, amount: str = "-40.00", feed: str = "feed-1") -> dict:
    return {"id": sid, "accountId": feed, "accountName": "Synthetic account", "accountType": "checking",
            "date": day, "amount": amount, "currency": "USD", "description": "SYNTHETIC VENDOR", "pending": False}


class Company(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "books"

    # -- helpers ---------------------------------------------------------------

    def create(self, **kwargs) -> None:
        init_entity(self.path, name="Synthetic Catch-up Co", basis="cash", **kwargs)

    @property
    def store(self) -> LedgerStore:
        return LedgerStore(self.path / "ledger.sqlite")

    def opens(self) -> dict[str, str]:
        with self.store.connection() as conn:
            return {name: opened for name, opened in conn.execute("SELECT name, open_date FROM accounts")}

    def provenance(self) -> dict | None:
        with self.store.connection() as conn:
            return self.store.load_open_date_provenance(conn)

    def map_feed(self, feed: str, account: str, open_date: str | None = SCOPE) -> None:
        if open_date is not None:
            add_account(self.path, account, open_date=open_date, currency="USD")
        config_path = self.path / "entity.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.setdefault("bank_account_mappings", {})[feed] = account
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    def set_config(self, **values) -> None:
        config_path = self.path / "entity.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.update(values)
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    def stage(self, *rows: dict) -> None:
        import_transactions(load_entity(self.path), list(rows), "intake", intake_only=True)

    def confirm(self, sid: str, category: str = SOFTWARE) -> dict:
        entity = load_entity(self.path)
        queue.propose(entity, sid, category, "Synthetic person choice")
        return queue.confirm(entity, sid, "person-review")

    def posted(self) -> set[str]:
        return {e.source_id for e in self.store.load_entries() if e.source_id}

    def snapshot(self) -> tuple[str, int]:
        return self.store.content_digest(), len(self.store.load_audit_events())

    def assert_books_valid(self) -> None:
        self.assertEqual(check_integrity(load_entity(self.path)).status, "ok")
        self.assertEqual(validate(render_store_ledger(self.store.path)), [])

    def make_legacy(self) -> None:
        """Remove the provenance record, as in a company that an earlier engine created."""
        with self.store.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM meta WHERE key='open_date_provenance'")
            self.store.append_audit_event("ledger-store-sealed", {
                "session_id": "synthetic-legacy", "entries": 0, "store_sha256": self.store.content_digest(conn)}, conn)

    def legacy_write(self, opens: list) -> None:
        """A catalog write by an earlier engine: no provenance record is created or kept."""
        with self.store.transaction(immediate=True) as conn:
            self.store.insert_opens(opens, conn, provenance=None)
            self.store.append_audit_event("ledger-store-sealed", {
                "session_id": "synthetic-legacy", "entries": 0, "store_sha256": self.store.content_digest(conn)}, conn)

    def older_writer(self, *statements: tuple) -> None:
        """Raw catalog SQL, as an earlier engine writes it after a rollback: the record is not kept."""
        with self.store.transaction(immediate=True) as conn:
            for statement in statements:
                conn.execute(*statement)
            self.store.append_audit_event("ledger-store-sealed", {
                "session_id": "synthetic-older-writer", "entries": 0,
                "store_sha256": self.store.content_digest(conn)}, conn)

    @contextmanager
    def policy(self, accounts: dict):
        document = {"schema_version": 1, "mode": "enforced", "epoch": 1, "accounts": accounts}
        policy_path = self.root / f"policy-{len(list(self.root.glob('policy-*')))}.json"
        policy_path.write_text(json.dumps(document), encoding="utf-8")
        with patch.dict(os.environ, {POLICY_ENV: str(policy_path)}):
            yield

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()):
            code = cli.main(list(argv))
        return code, json.loads(out.getvalue())


class CreationTests(Company):
    def test_prior_year_positive_control(self) -> None:
        self.create(books_from=SCOPE)
        opens = self.opens()
        self.assertEqual(opens[SOFTWARE], SCOPE)
        self.assertEqual((opens["Equity:Owner-Draws"], opens["Equity:Owner-Contributions"]), (SCOPE, SCOPE))
        self.assertEqual(opens["Equity:Opening-Balances"], FALLBACK)  # control equity: an opening sets its date
        self.assertEqual(opens[STARTER_BANK], FALLBACK)  # a bank account keeps its own start
        self.assertEqual(opens["Liabilities:CreditCard"], FALLBACK)
        report = add_account(self.path, CUSTOM, category=True)
        self.assertEqual((report["open_date"], report["open_date_source"]), (SCOPE, "scope"))
        record = self.provenance()
        self.assertEqual((record["books_from"], record["books_from_source"]), (SCOPE, "scope"))
        self.assertEqual(record["accounts"][SOFTWARE], {"source": "scope", "open_date": SCOPE})
        self.assertEqual(record["accounts"][STARTER_BANK], {"source": "default", "open_date": FALLBACK})
        self.assertEqual(record["accounts"][CUSTOM], {"source": "scope", "open_date": SCOPE})
        self.map_feed("feed-1", OPERATING)
        self.assertEqual(self.provenance()["accounts"][OPERATING]["source"], "user")
        self.stage(row("r-2025-starter", "2025-03-04"), row("r-2025-custom", "2025-09-30", "-12.00"))
        self.assertEqual(self.confirm("r-2025-starter")["status"], "confirmed")
        self.assertEqual(self.confirm("r-2025-custom", CUSTOM)["status"], "confirmed")
        self.assertEqual(self.posted(), {"r-2025-starter", "r-2025-custom"})
        self.assert_books_valid()

    def test_the_scope_does_not_open_a_bank_account(self) -> None:
        self.create(books_from=SCOPE)
        self.map_feed("feed-1", STARTER_BANK, open_date=None)  # the starter bank account opens on 2026-01-01
        self.stage(row("r-2025", "2025-06-15"), row("r-2026", "2026-02-02"))
        before = self.snapshot()
        with self.assertRaises(AccountNotOpenError) as caught:
            self.confirm("r-2025")
        self.assertEqual((caught.exception.code, caught.exception.account), ("source_catalog_conflict", STARTER_BANK))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.confirm("r-2026")["status"], "confirmed")
        # A repair to an earlier scope never moves the bank account either.
        plan = books_from.preview(self.path, "2024-01-01")
        kept = {item["account"]: item["reason"] for item in plan["kept"]}
        self.assertEqual(kept[STARTER_BANK], "balance_sheet_account")
        self.assertNotIn(STARTER_BANK, {item["account"] for item in plan["accounts"]})

    def test_without_a_scope_the_fallback_stays_and_is_marked_default(self) -> None:
        self.create()
        self.assertEqual(set(self.opens().values()), {FALLBACK})
        record = self.provenance()
        self.assertIsNone(record["books_from"])
        self.assertEqual({marker["source"] for marker in record["accounts"].values()}, {"default"})
        self.assertEqual(add_account(self.path, CUSTOM, category=True)["open_date_source"], "default")
        # The defect, kept for a company with no scope: a 2025 entry on a category is refused.
        self.map_feed("feed-1", OPERATING)
        self.stage(row("r-2025", "2025-06-15"))
        with self.assertRaises(AccountNotOpenError) as caught:
            self.confirm("r-2025")
        self.assertEqual((caught.exception.code, caught.exception.account), ("category_catalog_conflict", SOFTWARE))
        self.assertIn("books entity set-books-from", caught.exception.action)

    def test_cross_year_scope(self) -> None:
        self.create(books_from="2025-07-01")
        self.map_feed("feed-1", OPERATING, open_date="2025-07-01")
        self.stage(row("r-0630", "2025-06-30"), row("r-0701", "2025-07-01"), row("r-0202", "2026-02-02"))
        with self.assertRaises(AccountNotOpenError) as caught:
            self.confirm("r-0630")
        self.assertEqual(caught.exception.code, "source_catalog_conflict")  # the bank account starts on 07-01
        self.confirm("r-0701")
        self.confirm("r-0202")
        self.assertEqual(self.posted(), {"r-0701", "r-0202"})
        self.assert_books_valid()

    def test_a_cutover_is_the_scope_and_a_scope_before_it_is_refused(self) -> None:
        self.create(cutover_date="2026-03-01")
        self.assertEqual(set(self.opens().values()), {"2026-03-01"})
        record = self.provenance()
        self.assertEqual((record["books_from"], record["books_from_source"]), ("2026-03-01", "cutover"))
        self.assertEqual(add_account(self.path, CUSTOM, category=True)["open_date"], "2026-03-01")
        with self.assertRaises(BooksFromError) as caught:
            books_from.preview(self.path, SCOPE)
        self.assertEqual(caught.exception.code, "scope_before_cutover")
        self.assertFalse(books_from.preview(self.path, "2026-03-01")["changes"])
        # Rows before the cutover stay refused on the cut-over account.
        self.map_feed("feed-1", STARTER_BANK, open_date=None)
        self.stage(row("r-feb", "2026-02-27"), row("r-mar", "2026-03-02"))
        with self.assertRaises(AccountNotOpenError) as refused:
            self.confirm("r-feb")
        self.assertEqual((refused.exception.code, refused.exception.account), ("source_catalog_conflict", STARTER_BANK))
        self.confirm("r-mar")

    def test_init_refuses_a_scope_that_differs_from_the_cutover_and_a_bad_date(self) -> None:
        with self.assertRaises(ValueError):
            init_entity(self.root / "a", name="A", books_from=SCOPE, cutover_date="2026-03-01")
        self.assertFalse((self.root / "a" / "ledger.sqlite").exists())
        with self.assertRaises(ValueError):
            init_entity(self.root / "b", name="B", books_from="2025-1-1")
        init_entity(self.root / "c", name="C", books_from="2026-03-01", cutover_date="2026-03-01")

    def test_account_add_does_not_move_an_existing_account_without_an_open_date(self) -> None:
        self.create(books_from=SCOPE)
        add_account(self.path, "Expenses:Late", open_date="2026-03-01")
        report = add_account(self.path, "Expenses:Late", category=True)
        self.assertEqual((report["status"], report["open_date"]), ("existed", "2026-03-01"))
        self.assertEqual(self.opens()["Expenses:Late"], "2026-03-01")

    def test_the_cli_takes_books_from(self) -> None:
        out = StringIO()
        with redirect_stdout(out):
            code = cli.main(["entity", "init", str(self.path), "--name", "Synthetic", "--books-from", SCOPE])
        self.assertEqual(code, 0)
        self.assertEqual(self.opens()[SOFTWARE], SCOPE)


class RepairTests(Company):
    def existing_company(self) -> None:
        """A company created with no scope, a custom category, and a bank account with a verified 2025 start."""
        self.create()
        add_account(self.path, CUSTOM, category=True)
        self.map_feed("feed-1", OPERATING)
        self.stage(row("r-2025", "2025-06-15"), row("r-2025-custom", "2025-07-01", "-9.00"))

    def test_preview_and_apply_repair_an_existing_company(self) -> None:
        self.existing_company()
        before = self.snapshot()
        plan = books_from.preview(self.path, SCOPE)
        self.assertEqual(self.snapshot(), before)  # a preview writes nothing
        self.assertTrue(plan["changes"])
        self.assertEqual(plan["provenance"], "recorded")
        moved = {item["account"]: item for item in plan["accounts"]}
        self.assertIn(SOFTWARE, moved)
        self.assertIn(CUSTOM, moved)
        self.assertIn("Equity:Owner-Contributions", moved)
        self.assertEqual(moved[SOFTWARE]["evidence"], "recorded_default")
        self.assertEqual((moved[SOFTWARE]["open_date"], moved[SOFTWARE]["to"]), (FALLBACK, SCOPE))
        kept = {item["account"]: item["reason"] for item in plan["kept"]}
        self.assertEqual(kept[STARTER_BANK], "balance_sheet_account")
        self.assertEqual(kept[OPERATING], "balance_sheet_account")
        self.assertEqual(kept["Liabilities:CreditCard"], "balance_sheet_account")
        self.assertEqual(kept["Equity:Opening-Balances"], "control_equity_account")
        self.assertEqual(plan["effects"]["entries_changed"], 0)

        with self.assertRaises(BooksFromError) as missing:
            books_from.apply(self.path, SCOPE, None)
        self.assertEqual(missing.exception.code, "plan_hash_required")
        with self.assertRaises(BooksFromError) as stale:
            books_from.apply(self.path, SCOPE, "0" * 64)
        self.assertEqual(stale.exception.code, "stale_preview")
        self.assertEqual(self.snapshot(), before)

        result = books_from.apply(self.path, SCOPE, plan["plan_hash"])
        self.assertEqual(result["status"], "applied")
        opens = self.opens()
        self.assertEqual((opens[SOFTWARE], opens[CUSTOM]), (SCOPE, SCOPE))
        self.assertEqual((opens[STARTER_BANK], opens["Liabilities:CreditCard"]), (FALLBACK, FALLBACK))
        record = self.provenance()
        self.assertEqual((record["books_from"], record["books_from_source"]), (SCOPE, "scope"))
        self.assertEqual(record["accounts"][SOFTWARE], {"source": "scope", "open_date": SCOPE})
        self.assertEqual(add_account(self.path, "Expenses:Later-Added", category=True)["open_date"], SCOPE)
        self.confirm("r-2025")
        self.confirm("r-2025-custom", CUSTOM)
        self.assertEqual(self.posted(), {"r-2025", "r-2025-custom"})
        self.assert_books_valid()

    def test_identical_replay_of_apply_writes_nothing(self) -> None:
        self.existing_company()
        plan = books_from.preview(self.path, SCOPE)
        self.assertEqual(books_from.apply(self.path, SCOPE, plan["plan_hash"])["status"], "applied")
        after = self.snapshot()
        for _ in range(2):
            replay = books_from.apply(self.path, SCOPE, plan["plan_hash"])
            self.assertEqual((replay["status"], replay["replayed"]), ("already_applied", True))
            self.assertEqual(self.snapshot(), after)
        again = books_from.preview(self.path, SCOPE)
        self.assertFalse(again["changes"])
        self.assertEqual(books_from.apply(self.path, SCOPE, again["plan_hash"])["status"], "already_applied")
        self.assertEqual(self.snapshot(), after)

    def test_a_scope_later_than_the_current_scope_is_refused(self) -> None:
        self.existing_company()
        with self.assertRaises(BooksFromError) as caught:
            books_from.preview(self.path, "2026-02-01")
        self.assertEqual(caught.exception.code, "scope_not_earlier")

    def test_user_or_import_provenance_on_the_fallback_date_is_not_repaired(self) -> None:
        self.create()
        add_account(self.path, "Expenses:Chosen", open_date=FALLBACK)  # a person chose this date
        add_account(self.path, SOFTWARE, open_date=FALLBACK)  # a person confirms the starter date
        _atomic_ledger_write(load_entity(self.path), [Open(date(2026, 1, 1), "Expenses:Imported", ("USD",))], [],
                             "synthetic-import", None, "synthetic import of a chart")
        record = self.provenance()["accounts"]
        self.assertEqual([record[name]["source"] for name in ("Expenses:Chosen", SOFTWARE, "Expenses:Imported")],
                         ["user", "user", "import"])
        plan = books_from.preview(self.path, SCOPE)
        kept = {item["account"]: item["reason"] for item in plan["kept"]}
        self.assertEqual(kept["Expenses:Chosen"], "provenance_user")
        self.assertEqual(kept[SOFTWARE], "provenance_user")
        self.assertEqual(kept["Expenses:Imported"], "provenance_import")
        self.assertIn("Expenses:Rent", {item["account"] for item in plan["accounts"]})  # still default
        with self.assertRaises(BooksFromError) as caught:
            books_from.preview(self.path, SCOPE, ["Expenses:Imported"])
        self.assertEqual(caught.exception.code, "account_not_repairable")

    def test_a_company_created_before_provenance_needs_a_person_for_each_account(self) -> None:
        self.create()
        self.make_legacy()
        # An earlier engine added a category on the fallback date and another one on a stated date.
        self.legacy_write([Open(date(2026, 1, 1), CUSTOM, ("USD",)), Open(date(2025, 3, 1), "Expenses:Stated", ("USD",))])
        self.assertIsNone(self.provenance())
        # A legacy company has no recorded scope, so account-add keeps the fallback, and the write creates the record.
        self.assertEqual(add_account(self.path, "Expenses:New", category=True)["open_date"], FALLBACK)
        record = self.provenance()
        self.assertEqual(record["accounts"]["Expenses:New"], {"source": "default", "open_date": FALLBACK})
        self.assertEqual(record["accounts"][SOFTWARE], {"source": "legacy", "open_date": FALLBACK})
        plan = books_from.preview(self.path, SCOPE)
        self.assertEqual(sorted(item["account"] for item in plan["accounts"]), ["Expenses:New"])
        kept = {item["account"]: item for item in plan["kept"]}
        # A starter name and the fallback date are not evidence: the starter account waits for a person.
        self.assertEqual(kept[SOFTWARE]["reason"], "uncertain_provenance")
        self.assertEqual(kept[SOFTWARE]["known"], {"in_starter_chart": True, "audit_events": []})
        self.assertEqual(kept[CUSTOM]["reason"], "uncertain_provenance")
        self.assertFalse(kept[CUSTOM]["known"]["in_starter_chart"])
        self.assertEqual(kept["Expenses:Stated"]["reason"], "not_fallback_date")
        self.assertEqual(kept[STARTER_BANK]["reason"], "balance_sheet_account")
        self.assertIn(SOFTWARE, plan["effects"]["uncertain"])
        # A person reviews the uncertain accounts and names two of them.
        named = books_from.preview(self.path, SCOPE, [SOFTWARE, CUSTOM])
        evidence = {item["account"]: item["evidence"] for item in named["accounts"]}
        self.assertEqual((evidence[SOFTWARE], evidence[CUSTOM]), ("named_by_person", "named_by_person"))
        self.assertEqual(books_from.apply(self.path, SCOPE, named["plan_hash"], [SOFTWARE, CUSTOM])["status"], "applied")
        opens = self.opens()
        self.assertEqual((opens[SOFTWARE], opens[CUSTOM], opens["Expenses:Rent"]), (SCOPE, SCOPE, FALLBACK))
        event = [e for e in self.store.load_audit_events() if e["type"] == "books-from-set"][-1]["payload"]
        self.assertEqual({item["account"]: item["evidence"] for item in event["accounts"]}[SOFTWARE], "named_by_person")
        record = self.provenance()
        self.assertEqual(record["accounts"][CUSTOM]["source"], "scope")
        self.assertEqual(record["accounts"]["Expenses:Rent"], {"source": "legacy", "open_date": FALLBACK})
        self.assert_books_valid()

    def test_an_explicit_date_in_a_legacy_ledger_is_marked_and_never_moved(self) -> None:
        # The reviewer's case: a person states the starter date of Expenses:Software in an older company.
        self.create()
        self.make_legacy()
        add_account(self.path, SOFTWARE, open_date=FALLBACK)
        record = self.provenance()
        self.assertEqual(record["accounts"][SOFTWARE], {"source": "user", "open_date": FALLBACK})
        self.assertEqual(record["accounts"]["Expenses:Rent"]["source"], "legacy")
        plan = books_from.preview(self.path, SCOPE)
        self.assertEqual({item["account"]: item["reason"] for item in plan["kept"]}[SOFTWARE], "provenance_user")
        self.assertNotIn(SOFTWARE, {item["account"] for item in plan["accounts"]})
        with self.assertRaises(BooksFromError) as caught:
            books_from.preview(self.path, SCOPE, [SOFTWARE])
        self.assertEqual(caught.exception.code, "account_not_repairable")

    def test_a_legacy_company_with_a_cutover_has_no_starter_evidence(self) -> None:
        self.create(cutover_date="2025-07-01")
        self.make_legacy()
        self.legacy_write([Open(date(2026, 1, 1), CUSTOM, ("USD",))])
        with self.assertRaises(BooksFromError) as caught:
            books_from.preview(self.path, SCOPE)
        self.assertEqual(caught.exception.code, "scope_before_cutover")
        plan = books_from.preview(self.path, "2025-07-01")
        self.assertEqual(plan["accounts"], [])
        self.assertEqual({item["account"]: item["reason"] for item in plan["kept"]}[CUSTOM], "uncertain_provenance")
        named = books_from.preview(self.path, "2025-07-01", [CUSTOM])
        self.assertEqual([item["account"] for item in named["accounts"]], [CUSTOM])

    def record_opening(self, sid: str, day: date, account: str, import_source: str) -> None:
        _atomic_ledger_write(load_entity(self.path), [Open(day, "Equity:Opening-Balances")], [Entry(
            date=day, narration="Opening balance", meta=(("source-id", sid), ("import-source", import_source)),
            postings=(Posting(account, Decimal("100.00")), Posting("Equity:Opening-Balances", Decimal("-100.00"))))],
            "opening", None, "synthetic opening", allow_earlier_open_dates=True)

    def test_a_company_opening_bounds_the_scope(self) -> None:
        self.existing_company()
        self.record_opening("synthetic-company-opening", date(2025, 6, 1), OPERATING, "quickbooks-opening")
        with self.assertRaises(BooksFromError) as caught:
            books_from.preview(self.path, SCOPE)
        self.assertEqual(caught.exception.code, "scope_before_opening")
        self.assertEqual(caught.exception.detail["openings"][0]["source_id"], "synthetic-company-opening")
        plan = books_from.preview(self.path, "2025-06-01")
        self.assertIn(SOFTWARE, {item["account"] for item in plan["accounts"]})

    def test_a_statement_opening_of_one_account_is_listed_and_does_not_refuse(self) -> None:
        # A card that starts later than the bank account: its opening fences only the card.
        self.existing_company()
        add_account(self.path, "Liabilities:Card", open_date="2025-04-01", currency="USD")
        self.record_opening("synthetic-card-opening", date(2025, 4, 1), "Liabilities:Card", "statement-opening")
        plan = books_from.preview(self.path, SCOPE)
        self.assertEqual([item["source_id"] for item in plan["openings"]], ["synthetic-card-opening"])
        self.assertEqual({item["account"]: item["reason"] for item in plan["kept"]}["Liabilities:Card"],
                         "balance_sheet_account")
        books_from.apply(self.path, SCOPE, plan["plan_hash"])
        self.confirm("r-2025")  # a 2025 row on the bank account and a category records
        self.assert_books_valid()

    def test_a_closed_period_refuses_the_repair(self) -> None:
        self.existing_company()
        self.set_config(closed_through="2025-03-31")
        with self.assertRaises(BooksFromError) as caught:
            books_from.preview(self.path, SCOPE)
        self.assertEqual(caught.exception.code, "closed_period")
        self.assertIn(SOFTWARE, {item["account"] for item in books_from.preview(self.path, "2025-04-01")["accounts"]})
        self.set_config(closed_through=None, closed_periods=["2025-08"])
        with self.assertRaises(BooksFromError) as month:
            books_from.preview(self.path, SCOPE)
        self.assertEqual(month.exception.code, "closed_period")
        books_from.preview(self.path, "2025-09-01")

    def test_a_pending_opening_still_refuses_after_the_repair(self) -> None:
        self.existing_company()
        plan = books_from.preview(self.path, SCOPE)
        books_from.apply(self.path, SCOPE, plan["plan_hash"])
        before = self.snapshot()
        with self.policy({OPERATING: {"state": "authorized_pending_opening", "start_day": SCOPE, "version": 1}}):
            with self.assertRaises(BooksPolicyError) as caught:
                self.confirm("r-2025")
        self.assertEqual(caught.exception.code, "OPENING_REQUIRED")
        self.assertEqual(self.snapshot(), before)

    def test_the_cli_previews_applies_and_refuses_as_json(self) -> None:
        self.existing_company()
        code, plan = self.run_cli("entity", "set-books-from", str(self.path), "--books-from", SCOPE)
        self.assertEqual(code, 0)
        self.assertEqual(plan["kind"], "entity_books_from")
        code, refused = self.run_cli("entity", "set-books-from", str(self.path), "--books-from", SCOPE, "--apply")
        self.assertEqual((code, refused["status"], refused["errors"][0]["code"]), (1, "blocked", "plan_hash_required"))
        code, applied = self.run_cli("entity", "set-books-from", str(self.path), "--books-from", SCOPE,
                                     "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, applied["status"]), (0, "applied"))
        code, error = self.run_cli("entity", "set-books-from", str(self.path), "--books-from", "2024-01-01",
                                   "--account", STARTER_BANK)
        self.assertEqual((code, error["errors"][0]["code"]), (1, "account_not_repairable"))

    def test_the_remote_contract_classifies_preview_and_apply(self) -> None:
        preview = command_spec(["entity", "set-books-from", "@entity", "--books-from", SCOPE])
        apply = command_spec(["entity", "set-books-from", "@entity", "--books-from", SCOPE, "--apply",
                              "--plan-hash", "a" * 64])
        self.assertEqual((preview["mutates"], apply["mutates"]), (False, True))
        self.assertEqual(preview["entity_fields"], ["path"])

    def test_standing_automation_cannot_set_the_books_scope(self) -> None:
        from bookkeeping.automation_policy import PreparationDenied
        from bookkeeping.preparation_runtime import supported_preparation_command
        for argv in (["entity", "set-books-from", "@entity", "--books-from", SCOPE],
                     ["entity", "set-books-from", "@entity", "--books-from", SCOPE, "--apply", "--plan-hash", "a" * 64]):
            with self.subTest(argv=argv), self.assertRaises(PreparationDenied):
                supported_preparation_command(argv)


class ProvenancePathTests(Company):
    """Every path that creates, changes, removes or adds an account again keeps the provenance record exact."""

    def marker(self, account: str) -> dict | None:
        return self.provenance()["accounts"].get(account)

    def reasons(self, books_from_day: str = SCOPE) -> dict[str, str]:
        plan = books_from.preview(self.path, books_from_day)
        found = {item["account"]: item["reason"] for item in plan["kept"]}
        found.update({item["account"]: item["evidence"] for item in plan["accounts"]})
        return found

    def test_account_add_paths(self) -> None:
        self.create()
        add_account(self.path, CUSTOM, category=True)
        self.assertEqual(self.marker(CUSTOM), {"source": "default", "open_date": FALLBACK})
        add_account(self.path, CUSTOM, category=True)  # no date, existing: no change
        self.assertEqual(self.marker(CUSTOM)["source"], "default")
        add_account(self.path, CUSTOM, open_date="2026-03-01")  # a later date changes nothing
        self.assertEqual(self.marker(CUSTOM), {"source": "default", "open_date": FALLBACK})
        add_account(self.path, CUSTOM, open_date="2025-06-01")
        self.assertEqual(self.marker(CUSTOM), {"source": "user", "open_date": "2025-06-01"})
        add_account(self.path, "Assets:Bank:Reserve")  # a balance sheet account without a date
        self.assertEqual(self.marker("Assets:Bank:Reserve"), {"source": "default", "open_date": FALLBACK})
        add_account(self.path, "Equity:Partner-Capital")  # equity of an unknown role keeps the fallback
        self.assertEqual(self.reasons()["Equity:Partner-Capital"], "control_equity_account")

    def test_account_remove_and_add_again(self) -> None:
        from bookkeeping import account_remove
        self.create(books_from=SCOPE)
        add_account(self.path, CUSTOM, open_date=FALLBACK)
        self.assertEqual(self.marker(CUSTOM)["source"], "user")
        plan = account_remove.preview(self.path, CUSTOM)
        account_remove.apply(self.path, CUSTOM, plan["plan_hash"])
        self.assertIsNone(self.marker(CUSTOM))
        add_account(self.path, CUSTOM, category=True)  # added again without a date: the scope
        self.assertEqual(self.marker(CUSTOM), {"source": "scope", "open_date": SCOPE})
        self.assertFalse(self.provenance().get("stale"))

    def test_a_ledger_write_that_opens_a_new_account_marks_it_import(self) -> None:
        self.create(books_from=SCOPE)
        self.map_feed("feed-1", OPERATING)
        # An import writes an entry on an account that is not in the catalog; the write opens it.
        _atomic_ledger_write(load_entity(self.path), [Open(date(2000, 1, 1), "Expenses:Brand-New")], [Entry(
            date=date(2025, 5, 5), narration="Synthetic import", meta=(("source-id", "r-new"),),
            postings=(Posting(OPERATING, Decimal("-5.00")), Posting("Expenses:Brand-New", Decimal("5.00"))))],
            "synthetic-import", None, "synthetic import")
        self.assertEqual(self.marker("Expenses:Brand-New"), {"source": "import", "open_date": "2000-01-01"})
        self.assertEqual(self.marker(OPERATING)["source"], "user")  # an existing account keeps its marker

    def test_a_statement_opening_marks_opening(self) -> None:
        from bookkeeping.statement_opening import record_opening
        self.create()
        self.map_feed("feed-1", OPERATING, open_date=None)
        add_account(self.path, OPERATING, currency="USD")  # no date: the fallback, marked default
        self.assertEqual(self.marker(OPERATING)["source"], "default")
        result = record_opening(self.path, account=OPERATING, on=FALLBACK, amount="100.00")
        self.assertEqual(result["status"], "recorded")
        # The opening states the same date: the date now comes from the opening, never from the fallback.
        self.assertEqual(self.marker(OPERATING), {"source": "opening", "open_date": FALLBACK})
        self.assertEqual(self.marker("Equity:Opening-Balances"), {"source": "opening", "open_date": FALLBACK})

    def test_a_quickbooks_opening_marks_opening_on_the_fallback_date(self) -> None:
        from bookkeeping import quickbooks_sync as qb_sync
        self.create()
        fixture = Path(__file__).parent / "fixtures" / "quickbooks_api" / "snapshot.json"
        applied = qb_sync.sync_opening(load_entity(self.path), fixture, date(2026, 1, 1), apply=True)
        self.assertTrue(applied["applied"], applied)
        record = self.provenance()["accounts"]
        self.assertEqual(record[STARTER_BANK], {"source": "opening", "open_date": FALLBACK})
        self.assertEqual(record["Equity:Opening-Balances"]["source"], "opening")
        self.assertEqual(self.reasons("2026-01-01").get(STARTER_BANK), "balance_sheet_account")
        with self.assertRaises(BooksFromError) as caught:  # a company opening on 2026-01-01 bounds the scope
            books_from.preview(self.path, SCOPE)
        self.assertEqual(caught.exception.code, "scope_before_opening")

    def test_chart_adoption_marks_import(self) -> None:
        from bookkeeping.chart_adoption import adopt
        self.create()
        manifest = {"version": 1, "operation_id": "adopt-1", "source": "synthetic-export", "decision": "preserve",
                    "scope": "partial", "open_date": FALLBACK, "accounts": [
                        {"source_id": "1", "name": "Design", "number": "4100", "parent_id": None, "type": "Income",
                         "account": "Income:Design"}]}
        plan = adopt(self.path, manifest)
        adopt(self.path, manifest, apply=True, plan_hash=plan["plan_hash"])
        self.assertEqual(self.marker("Income:Design"), {"source": "import", "open_date": FALLBACK})
        self.assertEqual(self.reasons()["Income:Design"], "provenance_import")

    def test_a_connector_account_marks_import(self) -> None:
        self.create()
        add_account(self.path, "Assets:Stripe:Clearing", open_date=FALLBACK, currency="USD", open_date_source="import")
        self.assertEqual(self.marker("Assets:Stripe:Clearing")["source"], "import")
        self.assertIn('open_date_source="import"', (Path(books_from.__file__).parent / "stripe.py").read_text())

    def test_accrual_tracking_start_marks_opening(self) -> None:
        from bookkeeping.accrual import enable as accrual_enable
        self.create(books_from=SCOPE)
        plan = accrual_enable.preview_enable(load_entity(self.path), "2025-06-01")
        accrual_enable.apply_enable(load_entity(self.path), plan)
        self.assertEqual(self.marker("Assets:Receivable:Accounts-Receivable"),
                         {"source": "opening", "open_date": "2025-06-01"})
        self.assertEqual(self.marker("Expenses:Depreciation"), {"source": "opening", "open_date": "2025-06-01"})

    def test_starter_accounts_and_basis_follow_the_scope(self) -> None:
        from bookkeeping.entity import apply_starter_accounts, configure_basis
        self.create(books_from=SCOPE)
        apply_starter_accounts(self.path, "retail")
        self.assertEqual(self.marker("Income:Sales"), {"source": "scope", "open_date": SCOPE})
        self.assertEqual(self.marker("Assets:Other:Inventory"), {"source": "default", "open_date": FALLBACK})
        self.assertIsNone(self.marker("Income:Consulting"))  # removed with the previous starter chart
        configure_basis(self.path, "accrual")
        self.assertEqual(self.marker("Expenses:Depreciation"), {"source": "scope", "open_date": SCOPE})
        self.assertEqual(self.marker("Liabilities:Payable:Accounts-Payable")["source"], "default")
        self.assertFalse(self.provenance().get("stale"))

    def test_a_migrated_beancount_ledger_marks_import(self) -> None:
        from bookkeeping.ledger.migrate import migrate_beancount_to_store
        other = self.root / "migrated"
        other.mkdir()
        (other / "entity.json").write_text(json.dumps({"name": "Synthetic"}), encoding="utf-8")
        (other / "books.beancount").write_text(
            'option "operating_currency" "USD"\n2026-01-01 open Expenses:Software USD\n'
            '2026-01-01 open Assets:Bank:Checking USD\n', encoding="utf-8")
        self.assertTrue(migrate_beancount_to_store(other).success)
        store = LedgerStore(other / "ledger.sqlite")
        with store.connection() as conn:
            record = store.load_open_date_provenance(conn)
        self.assertEqual(record["accounts"][SOFTWARE], {"source": "import", "open_date": FALLBACK})

    def test_a_cutover_change_in_entity_json_moves_nothing(self) -> None:
        self.create()
        before = self.opens()
        self.set_config(cutover_date="2025-07-01")
        self.assertEqual(self.opens(), before)
        with self.assertRaises(BooksFromError) as caught:
            books_from.preview(self.path, SCOPE)
        self.assertEqual(caught.exception.code, "scope_before_cutover")
        # The record states no scope, so a new category takes the company cutover, as an older company does.
        report = add_account(self.path, CUSTOM, category=True)
        self.assertEqual((report["open_date"], report["open_date_source"]), ("2025-07-01", "cutover"))

    def test_a_correction_marks_only_new_accounts(self) -> None:
        from bookkeeping.ledger.importer import reverse_and_correct
        self.create(books_from=SCOPE)
        self.map_feed("feed-1", OPERATING)
        self.stage(row("r-fix", "2025-05-05"))
        self.confirm("r-fix")
        markers = dict(self.provenance()["accounts"])
        reverse_and_correct(load_entity(self.path), "r-fix", Entry(
            date=date(2025, 5, 5), narration="Corrected", meta=(("source-id", "r-fix-corrected"),),
            postings=(Posting(OPERATING, Decimal("-40.00")), Posting("Expenses:Corrected", Decimal("40.00")))),
            "synthetic-correction")
        after = self.provenance()["accounts"]
        self.assertEqual(after["Expenses:Corrected"], {"source": "import", "open_date": "2000-01-01"})
        self.assertEqual({k: v for k, v in after.items() if k != "Expenses:Corrected"}, markers)

    def test_only_ordinary_owner_equity_follows_the_scope(self) -> None:
        self.create(books_from=SCOPE)
        control = ("Equity:Opening-Balances", "Equity:Retained-Earnings", "Equity:Prior-Periods-Net-Income",
                   "Equity:Opening-Balance-Adjustments", "Equity:Accrual-To-Cash-Adjustment", "Equity:Partner-Capital")
        for account in control[1:]:
            report = add_account(self.path, account)
            self.assertEqual((report["open_date"], report["open_date_source"]), (FALLBACK, "default"), account)
        opens = self.opens()
        self.assertEqual({opens[name] for name in books_from.ORDINARY_EQUITY}, {SCOPE})
        self.assertEqual({opens[name] for name in control}, {FALLBACK})
        reasons = self.reasons("2024-01-01")
        for account in control:
            self.assertEqual(reasons[account], "control_equity_account", account)
        for account in books_from.ORDINARY_EQUITY:
            self.assertEqual(reasons[account], "recorded_scope", account)
        with self.assertRaises(BooksFromError) as caught:
            books_from.preview(self.path, "2024-01-01", ["Equity:Retained-Earnings"])
        self.assertEqual(caught.exception.code, "account_not_repairable")

    # -- an earlier engine writes the catalog (a rollback round trip) ---------------

    def test_an_older_writer_removal_and_new_add_makes_every_marker_stale(self) -> None:
        self.create()
        self.assertEqual(self.reasons()[SOFTWARE], "recorded_default")
        # An earlier account-remove and account-add, the same name and date: a new row id.
        self.older_writer(("DELETE FROM accounts WHERE name=?", (SOFTWARE,)),
                          ("INSERT INTO accounts (name, currency, open_date) VALUES (?, 'USD', ?)", (SOFTWARE, FALLBACK)))
        plan = books_from.preview(self.path, SCOPE)
        self.assertEqual(plan["provenance"], "stale")
        self.assertEqual(plan["accounts"], [])  # no marker is evidence now
        self.assertEqual({item["account"]: item["reason"] for item in plan["kept"]}[SOFTWARE], "uncertain_provenance")

    def test_an_older_writer_date_change_makes_every_marker_stale(self) -> None:
        self.create()
        self.older_writer(("UPDATE accounts SET open_date='2025-09-01' WHERE name=?", ("Expenses:Meals",)))
        self.assertEqual(books_from.preview(self.path, SCOPE)["provenance"], "stale")

    def test_a_new_write_after_an_older_writer_keeps_the_downgrade(self) -> None:
        self.create()
        self.older_writer(("INSERT INTO accounts (name, currency, open_date) VALUES ('Expenses:Old-Writer', 'USD', ?)",
                           (FALLBACK,)))
        add_account(self.path, CUSTOM, category=True)  # this engine writes again
        record = self.provenance()
        self.assertFalse(record.get("stale"))
        self.assertEqual(record["accounts"][SOFTWARE], {"source": "legacy", "open_date": FALLBACK})
        self.assertEqual(record["accounts"][CUSTOM], {"source": "default", "open_date": FALLBACK})
        self.assertNotIn("Expenses:Old-Writer", record["accounts"])
        reasons = self.reasons()
        self.assertEqual(reasons[SOFTWARE], "uncertain_provenance")
        self.assertEqual(reasons["Expenses:Old-Writer"], "uncertain_provenance")
        self.assertEqual(reasons[CUSTOM], "recorded_default")

    def test_a_tampered_record_refuses(self) -> None:
        self.create()
        with self.store.transaction(immediate=True) as conn:
            conn.execute("UPDATE meta SET value='{\"version\": 1}' WHERE key='open_date_provenance'")
        with self.assertRaises(ValueError):
            add_account(self.path, CUSTOM, category=True)


class StatementOpeningTests(Company):
    """The reviewer's case: a typed opening, then historical intake for the same account."""

    def test_earlier_rows_of_the_account_stay_out_of_review(self) -> None:
        from bookkeeping.statement_opening import record_opening
        self.create(books_from=SCOPE)
        self.map_feed("feed-1", OPERATING)  # the account opens on 2025-01-01
        result = record_opening(self.path, account=OPERATING, on="2025-03-01", amount="100.00")
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(self.opens()[OPERATING], SCOPE)  # the catalog date alone does not fence these rows
        imported = import_transactions(load_entity(self.path), [row("early", "2025-02-15"), row("late", "2025-03-05")],
                                       "intake", intake_only=True)
        # The account-specific opening control keeps the earlier row out of review.
        self.assertEqual([item["source_id"] for item in imported.before_opening], ["early"])
        pending = json.loads((self.path / "staging" / "pending-categorization.json").read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in pending], ["late"])
        self.assertEqual(self.confirm("late")["status"], "confirmed")
        self.assertNotIn("early", self.posted())

    def test_a_statement_opening_after_waiting_earlier_rows_is_refused(self) -> None:
        from bookkeeping.statement_opening import record_opening
        self.create(books_from=SCOPE)
        self.map_feed("feed-1", OPERATING)
        self.stage(row("early", "2025-02-15"))
        result = record_opening(self.path, account=OPERATING, on="2025-03-01", amount="100.00")
        self.assertEqual((result["status"], result["reason"]), ("not_eligible", "rows_waiting_before"))


class EnforcedPolicyTests(Company):
    """A wider category open date never lets through a write that the books start policy refuses."""

    def setUp(self) -> None:
        super().setUp()
        self.create(books_from=SCOPE)
        self.map_feed("feed-1", OPERATING)
        self.map_feed("feed-2", SAVINGS)
        self.stage(row("r-2025", "2025-06-15"))

    def test_a_row_before_the_authorized_start_is_refused(self) -> None:
        before = self.snapshot()
        with self.policy({OPERATING: {"state": "authorized", "start_day": "2026-03-15", "version": 1}}):
            with self.assertRaises(BooksPolicyError) as caught:
                self.confirm("r-2025")
        self.assertEqual((caught.exception.code, caught.exception.account),
                         ("BOOKS_DATE_BEFORE_AUTHORIZED_START", OPERATING))
        self.assertEqual(self.snapshot(), before)
        with self.policy({OPERATING: {"state": "authorized", "start_day": SCOPE, "version": 1}}):
            self.assertEqual(self.confirm("r-2025")["status"], "confirmed")  # positive control

    def test_both_sides_of_a_transfer_are_checked(self) -> None:
        transfer = Entry(date=date(2025, 5, 1), narration="Synthetic transfer", meta=(("source-id", "t-2025"),),
                         postings=(Posting(SAVINGS, Decimal("50.00")), Posting(OPERATING, Decimal("-50.00"))))
        policy = {OPERATING: {"state": "authorized", "start_day": SCOPE, "version": 1},
                  SAVINGS: {"state": "authorized", "start_day": "2026-03-15", "version": 1}}
        before = self.snapshot()
        with self.policy(policy), self.assertRaises(BooksPolicyError) as caught:
            _atomic_ledger_write(load_entity(self.path), [], [transfer], "transfer", None, "synthetic transfer")
        self.assertEqual((caught.exception.code, caught.exception.account),
                         ("BOOKS_DATE_BEFORE_AUTHORIZED_START", SAVINGS))
        self.assertEqual(self.snapshot(), before)

    def test_a_policy_account_with_no_start_is_refused(self) -> None:
        with self.policy({OPERATING: {"state": "unset"}}), self.assertRaises(BooksPolicyError) as caught:
            self.confirm("r-2025")
        self.assertEqual(caught.exception.code, "BOOKS_START_UNSET")


if __name__ == "__main__":
    unittest.main()
