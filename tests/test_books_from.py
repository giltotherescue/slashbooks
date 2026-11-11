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
        self.assertEqual(opens["Equity:Owner-Draws"], SCOPE)
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

    def test_a_company_created_before_provenance_is_classified_conservatively(self) -> None:
        self.create()
        self.make_legacy()
        # An earlier engine added a category on the fallback date and another one on a stated date.
        with self.store.transaction(immediate=True) as conn:
            self.store.insert_opens([Open(date(2026, 1, 1), CUSTOM, ("USD",)),
                                     Open(date(2025, 3, 1), "Expenses:Stated", ("USD",))], conn)
            self.store.append_audit_event("ledger-store-sealed", {
                "session_id": "synthetic-legacy", "entries": 0, "store_sha256": self.store.content_digest(conn)}, conn)
        self.assertIsNone(self.provenance())
        # A legacy company has no recorded scope, so account-add keeps the fallback.
        self.assertEqual(add_account(self.path, "Expenses:New", category=True)["open_date"], FALLBACK)
        self.assertIsNone(self.provenance())
        plan = books_from.preview(self.path, SCOPE)
        self.assertEqual(plan["provenance"], "legacy")
        moved = {item["account"]: item["evidence"] for item in plan["accounts"]}
        kept = {item["account"]: item["reason"] for item in plan["kept"]}
        self.assertEqual(moved[SOFTWARE], "starter_chart")
        self.assertEqual(kept[CUSTOM], "uncertain_provenance")
        self.assertEqual(kept["Expenses:New"], "uncertain_provenance")
        self.assertEqual(kept["Expenses:Stated"], "not_fallback_date")  # a stated date is not a fallback
        self.assertEqual(kept[STARTER_BANK], "balance_sheet_account")
        self.assertEqual(sorted(plan["effects"]["uncertain"]), [CUSTOM, "Expenses:New"])
        # A person reviews the uncertain account and names it.
        named = books_from.preview(self.path, SCOPE, [CUSTOM])
        self.assertEqual({item["account"]: item["evidence"] for item in named["accounts"]}[CUSTOM], "named_in_request")
        self.assertEqual(books_from.apply(self.path, SCOPE, named["plan_hash"], [CUSTOM])["status"], "applied")
        opens = self.opens()
        self.assertEqual((opens[SOFTWARE], opens[CUSTOM], opens["Expenses:New"]), (SCOPE, SCOPE, FALLBACK))
        record = self.provenance()
        self.assertEqual(record["accounts"][CUSTOM]["source"], "scope")
        self.assertEqual(record["accounts"]["Expenses:New"], {"source": "legacy", "open_date": FALLBACK})
        self.assertEqual(record["accounts"][STARTER_BANK]["source"], "legacy")
        self.assert_books_valid()

    def test_a_legacy_company_with_a_cutover_has_no_starter_evidence(self) -> None:
        self.create(cutover_date="2025-07-01")
        self.make_legacy()
        with self.store.transaction(immediate=True) as conn:
            self.store.insert_opens([Open(date(2026, 1, 1), CUSTOM, ("USD",))], conn)
            self.store.append_audit_event("ledger-store-sealed", {
                "session_id": "synthetic-legacy", "entries": 0, "store_sha256": self.store.content_digest(conn)}, conn)
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
