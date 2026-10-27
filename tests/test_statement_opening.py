"""Tests for `books statement record-opening`.

All fixtures under tests/fixtures/statements/ are synthetic.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping import statement_import as si  # noqa: E402
from bookkeeping import tieout  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore, default_store_path  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402
from bookkeeping.reports.cache import open_cache  # noqa: E402
from bookkeeping.reports.cashflow_statement import opening_entries  # noqa: E402
from bookkeeping.reports.statements import _account_balances_as_of  # noqa: E402
from bookkeeping.statement_opening import StatementOpeningError, record_opening  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "statements"
CHECKING = "Assets:Bank:Checking"
CARD = "Liabilities:CreditCard:Visa"


def _make_entity(root: Path) -> Path:
    entity_dir = root / "co"
    (entity_dir / "staging").mkdir(parents=True)
    config = {"name": "Synthetic Co", "business_type": "consulting"}
    (entity_dir / "entity.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
    (entity_dir / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n", encoding="utf-8")
    return entity_dir


def _queue_ids(entity_dir: Path) -> list[str]:
    path = entity_dir / "staging" / "pending-categorization.json"
    return [item["id"] for item in json.loads(path.read_text(encoding="utf-8"))] if path.exists() else []


class RecordOpeningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def imp(self, name: str, profile: str, account: str = CHECKING, **kwargs) -> dict:
        si.save_profile(self.entity, FIXTURES / name, name=profile, ledger_account=account, overrides={})
        result = si.import_file(self.entity, FIXTURES / name, profile=profile, ts="2026-02-01T00:00:00Z", **kwargs)
        return result["statements"][0]["statement"]

    def balances(self, day: str) -> dict:
        conn = open_cache(self.entity)
        try:
            return _account_balances_as_of(conn, date.fromisoformat(day))
        finally:
            conn.close()

    def test_first_statement_opening_is_recorded_once_against_opening_equity(self):
        record = self.imp("chase_like_checking.csv", "checking", period_start="2026-01-01", period_end="2026-01-31")
        self.assertEqual(record["opening_balance"], "1000.00")
        sid = record["statement_id"]

        preview = record_opening(self.entity, sid, dry_run=True)
        self.assertEqual(preview["status"], "dry_run")
        self.assertEqual(preview["date"], "2026-01-01")
        self.assertEqual(preview["postings"], [{"account": CHECKING, "amount": "1000.00"},
                                               {"account": "Equity:Opening-Balances", "amount": "-1000.00"}])
        self.assertEqual(self.balances("2026-01-31").get(CHECKING, Decimal(0)), Decimal(0))

        done = record_opening(self.entity, sid)
        self.assertEqual(done["status"], "recorded")
        balances = self.balances("2026-01-01")
        self.assertEqual(balances[CHECKING], Decimal("1000.00"))
        self.assertEqual(balances["Equity:Opening-Balances"], Decimal("-1000.00"))
        self.assertEqual(self.balances("2025-12-31").get(CHECKING, Decimal(0)), Decimal(0))

        again = record_opening(self.entity, sid)
        self.assertEqual(again["status"], "already_recorded")
        with LedgerStore(default_store_path(self.entity)).connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)

        # The cash flow statement takes it as a beginning balance; the statement tie-out leaves it out of the movement.
        conn = open_cache(self.entity)
        try:
            self.assertEqual(len(opening_entries(conn, date(2026, 1, 1), date(2026, 1, 31))), 1)
        finally:
            conn.close()
        postings = tieout.load_postings(self.entity, [CHECKING])[CHECKING]
        period = tieout.period_tieout(account=CHECKING, statement_id=sid, start=date(2026, 1, 1),
                                      end=date(2026, 1, 31), opening=Decimal("1000.00"),
                                      closing=Decimal("1000.00"), postings=postings, waiting=tieout.Series([]))
        self.assertEqual(period["status"], "matches")

    def test_card_opening_uses_the_ledger_sign(self):
        record = self.imp("card_debit_credit.csv", "card", account=CARD, period_start="2026-01-01",
                          period_end="2026-01-31", opening_balance="1000.00", closing_balance="769.99")
        done = record_opening(self.entity, record["statement_id"])
        self.assertEqual(done["status"], "recorded")
        self.assertEqual(done["postings"][0], {"account": CARD, "amount": "-1000.00"})
        self.assertEqual(self.balances("2026-01-01")[CARD], Decimal("-1000.00"))

    def test_only_the_first_statement_of_an_account_carries_the_opening(self):
        first = self.imp("chase_like_checking.csv", "checking", period_start="2026-01-01", period_end="2026-01-31")
        later = self.imp("chase_like_checking_later.csv", "checking-later", period_start="2026-01-15",
                         period_end="2026-02-14", opening_balance="3191.00", closing_balance="3067.26")
        refused = record_opening(self.entity, later["statement_id"])
        self.assertEqual((refused["status"], refused["reason"]), ("not_eligible", "not_first_statement"))
        self.assertEqual(record_opening(self.entity, first["statement_id"])["status"], "recorded")

    def test_entries_before_the_statement_refuse(self):
        record = self.imp("chase_like_checking.csv", "checking", period_start="2026-01-01", period_end="2026-01-31")
        store = LedgerStore(default_store_path(self.entity))
        store.initialize()
        with store.transaction() as conn:
            store.insert_opens([Open(date=date(2025, 12, 1), account=CHECKING),
                                Open(date=date(2025, 12, 1), account="Expenses:Software")], conn)
            store.insert_entries([Entry(date=date(2025, 12, 20), narration="Earlier", meta=(("source-id", "t-1"),),
                                        postings=(Posting(CHECKING, Decimal("-10.00")),
                                                  Posting("Expenses:Software", Decimal("10.00"))))], conn)
        refused = record_opening(self.entity, record["statement_id"])
        self.assertEqual((refused["status"], refused["reason"]), ("not_eligible", "books_before_statement"))
        self.assertEqual(refused["first_posting"], "2025-12-20")

    def test_unconfirmed_period_has_no_saved_statement(self):
        record = self.imp("chase_like_checking.csv", "checking")
        self.assertFalse(record["period_confirmed"])
        with self.assertRaises(StatementOpeningError) as caught:
            record_opening(self.entity, record["statement_id"])
        self.assertEqual(caught.exception.code, "statement_not_found")

    def test_printed_entry_is_marked_printed(self):
        record = self.imp("chase_like_checking.csv", "checking", period_start="2026-01-01", period_end="2026-01-31")
        self.assertEqual(record_opening(self.entity, record["statement_id"])["opening_source"], "printed")
        with LedgerStore(default_store_path(self.entity)).connection() as conn:
            meta = dict(json.loads(conn.execute("SELECT metadata_json FROM entries").fetchone()[0]))
        self.assertEqual(meta["opening-source"], "printed")

    def test_typed_amount_is_refused_when_the_statement_has_a_printed_opening(self):
        record = self.imp("chase_like_checking.csv", "checking", period_start="2026-01-01", period_end="2026-01-31")
        refused = record_opening(self.entity, record["statement_id"], amount="5000.00")
        self.assertEqual((refused["status"], refused["reason"]), ("not_eligible", "printed_opening_exists"))
        self.assertEqual(refused["printed_opening"], "1000.00")
        by_account = record_opening(self.entity, account=CHECKING, on="2026-01-01", amount="5000.00")
        self.assertEqual((by_account["status"], by_account["reason"]), ("not_eligible", "printed_opening_exists"))
        self.assertEqual(by_account["statement_id"], record["statement_id"])
        with LedgerStore(default_store_path(self.entity)).connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)

    def test_statement_without_a_printed_opening_takes_a_typed_amount(self):
        # A card CSV with no balance column has no printed opening balance.
        record = self.imp("card_debit_credit.csv", "card", account=CARD, period_start="2026-01-01",
                          period_end="2026-01-31")
        self.assertIsNone(record["opening_balance"])
        asked = record_opening(self.entity, record["statement_id"])
        self.assertEqual((asked["status"], asked["reason"]), ("not_eligible", "no_opening_balance"))
        done = record_opening(self.entity, record["statement_id"], amount="250.00")
        self.assertEqual((done["status"], done["opening_source"]), ("recorded", "person"))
        self.assertEqual(done["postings"][0], {"account": CARD, "amount": "-250.00"})
        with LedgerStore(default_store_path(self.entity)).connection() as conn:
            meta = dict(json.loads(conn.execute("SELECT metadata_json FROM entries").fetchone()[0]))
        self.assertEqual((meta["opening-source"], meta["statement-id"]), ("person", record["statement_id"]))
        self.assertEqual(record_opening(self.entity, record["statement_id"], amount="250.00")["status"],
                         "already_recorded")
        changed = record_opening(self.entity, record["statement_id"], amount="300.00")
        self.assertEqual((changed["status"], changed["reason"]), ("not_eligible", "opening_exists"))

    def test_account_and_day_take_a_typed_amount_under_the_same_rules(self):
        store = LedgerStore(default_store_path(self.entity))
        store.initialize()
        with store.transaction() as conn:
            store.insert_opens([Open(date=date(2026, 1, 1), account=CHECKING),
                                Open(date=date(2026, 1, 1), account="Expenses:Software")], conn)
            store.insert_entries([Entry(date=date(2026, 1, 5), narration="Later", meta=(("source-id", "t-1"),),
                                        postings=(Posting(CHECKING, Decimal("-10.00")),
                                                  Posting("Expenses:Software", Decimal("10.00"))))], conn)
        late = record_opening(self.entity, account=CHECKING, on="2026-01-06", amount="7907.82")
        self.assertEqual((late["status"], late["reason"]), ("not_eligible", "books_before_statement"))
        preview = record_opening(self.entity, account=CHECKING, on="2026-01-01", amount="7,907.82", dry_run=True)
        self.assertEqual((preview["status"], preview["opening_source"]), ("dry_run", "person"))
        self.assertIsNone(preview["statement_id"])
        done = record_opening(self.entity, account=CHECKING, on="2026-01-01", amount="7907.82")
        self.assertEqual(done["status"], "recorded")
        self.assertEqual(self.balances("2026-01-05")[CHECKING], Decimal("7897.82"))
        self.assertEqual(record_opening(self.entity, account=CHECKING, on="2026-01-01", amount="7907.82")["status"],
                         "already_recorded")
        unknown = record_opening(self.entity, account="Assets:Bank:Nowhere", on="2026-01-01", amount="1.00")
        self.assertEqual(unknown["reason"], "unknown_account")
        income = record_opening(self.entity, account="Expenses:Software", on="2026-01-01", amount="1.00")
        self.assertEqual(income["reason"], "not_a_balance_account")
        for kwargs in ({"account": CHECKING, "on": "2026-01-01"}, {"amount": "1.00"},
                       {"account": CHECKING, "on": "2026-13-01", "amount": "1.00"},
                       {"account": CHECKING, "on": "2026-01-01", "amount": "1.001"}):
            with self.assertRaises(StatementOpeningError):
                record_opening(self.entity, **kwargs)

    def write_csv(self, name: str, rows: list[tuple[str, str, str, str]]) -> Path:
        """Synthetic Chase-like checking export: (MM/DD/YYYY, description, amount, running balance), newest first."""
        lines = ["Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #"]
        for day, text, amount, balance in rows:
            kind = "CREDIT" if not amount.startswith("-") else "DEBIT"
            lines.append(f'{kind},{day},"{text}",{amount},ACH_{kind},{balance},,')
        path = Path(self._tmp.name) / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def import_path(self, path: Path, profile: str, account: str = CHECKING, **kwargs) -> dict:
        si.save_profile(self.entity, path, name=profile, ledger_account=account, overrides={})
        return si.import_file(self.entity, path, profile=profile, ts="2026-09-01T00:00:00Z", **kwargs)

    def july_august(self) -> tuple[Path, Path]:
        # July opens at 1000.00 and closes at 1300.00; August opens at 1300.00 and closes at 1250.00.
        july = self.write_csv("july.csv", [("07/15/2026", "NORTHWIND COFFEE", "-200.00", "1300.00"),
                                           ("07/02/2026", "ACME PAYROLL", "500.00", "1500.00")])
        august = self.write_csv("august.csv", [("08/20/2026", "INTEREST PAYMENT", "50.00", "1250.00"),
                                               ("08/03/2026", "CONTOSO SOFTWARE", "-100.00", "1200.00")])
        return july, august

    def books_balance(self, account: str, day: str) -> Decimal:
        """Posted rows plus rows waiting in review, as the statement tie-out counts the books."""
        end = date.fromisoformat(day)
        posted = tieout.load_postings(self.entity, [account])[account].through(end)
        waiting = tieout.load_waiting(load_entity(self.entity), [account])[account].through(end)
        return posted + waiting

    def test_rows_before_the_opening_are_refused_with_nothing_written(self):
        july, august = self.july_august()
        aug = self.import_path(august, "aug", period_start="2026-08-01", period_end="2026-08-31")
        self.assertEqual(record_opening(self.entity, aug["statements"][0]["statement"]["statement_id"])["status"],
                         "recorded")
        queued = _queue_ids(self.entity)
        statements_before = sorted(p.name for p in (self.entity / "ingestion" / "statements").glob("*.json"))
        for dry_run in (True, False):
            refused = self.import_path(july, "jul", period_start="2026-07-01", period_end="2026-07-31", dry_run=dry_run)
            self.assertEqual(refused["status"], "error")
            error = refused["error"]
            self.assertEqual(error["code"], "before_opening_balance")
            self.assertEqual((error["opening_date"], error["first_row_date"], error["rows_before_opening"]),
                             ("2026-08-01", "2026-07-02", 2))
            self.assertIn("2026-08-01", error["message"])
            self.assertEqual((error["statement_period_start"], error["statement_opening_balance"]),
                             ("2026-07-01", "1000.00"))
            self.assertIn("--replace", error["action"])
        self.assertEqual(_queue_ids(self.entity), queued)
        self.assertEqual(sorted(p.name for p in (self.entity / "ingestion" / "statements").glob("*.json")),
                         statements_before)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["statement", "import", str(july), "--entity", str(self.entity), "--profile", "jul",
                         "--period-start", "2026-07-01", "--period-end", "2026-07-31"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], "before_opening_balance")

    def test_replace_moves_the_opening_earlier_and_the_balance_ties_to_the_latest_statement(self):
        july, august = self.july_august()
        aug = self.import_path(august, "aug", period_start="2026-08-01", period_end="2026-08-31")
        aug_id = aug["statements"][0]["statement"]["statement_id"]
        self.assertEqual(record_opening(self.entity, aug_id)["status"], "recorded")
        self.assertEqual(self.books_balance(CHECKING, "2026-08-31"), Decimal("1250.00"))
        self.assertEqual(self.import_path(july, "jul", period_start="2026-07-01",
                                          period_end="2026-07-31")["error"]["code"], "before_opening_balance")

        # Without --replace the existing opening wins; a later day is refused even with --replace.
        self.assertEqual(record_opening(self.entity, account=CHECKING, on="2026-07-01", amount="1000.00")["reason"],
                         "opening_exists")
        self.assertEqual(record_opening(self.entity, account=CHECKING, on="2026-08-05", amount="1000.00",
                                        replace=True)["reason"], "replace_not_earlier")
        preview = record_opening(self.entity, account=CHECKING, on="2026-07-01", amount="1000.00", replace=True,
                                 dry_run=True)
        self.assertEqual((preview["status"], preview["replaces"]["date"]), ("dry_run", "2026-08-01"))
        moved = record_opening(self.entity, account=CHECKING, on="2026-07-01", amount="1000.00", replace=True)
        self.assertEqual((moved["status"], moved["date"], moved["replaces"]["amount"]),
                         ("replaced", "2026-07-01", "1300.00"))
        self.assertEqual(record_opening(self.entity, account=CHECKING, on="2026-07-01", amount="1000.00",
                                        replace=True)["status"], "already_recorded")

        jul = self.import_path(july, "jul", period_start="2026-07-01", period_end="2026-07-31")
        self.assertEqual(jul["status"], "imported")
        self.assertEqual(self.books_balance(CHECKING, "2026-07-31"), Decimal("1300.00"))
        self.assertEqual(self.books_balance(CHECKING, "2026-08-31"), Decimal("1250.00"))
        balances = self.balances("2026-08-31")
        self.assertEqual(balances["Equity:Opening-Balances"], Decimal("-1000.00"))
        # Both statements still tie out: the moved opening and its reversal are not period movement.
        for statement_id, start, end, opening, closing in (
                (jul["statements"][0]["statement"]["statement_id"], "2026-07-01", "2026-07-31", "1000.00", "1300.00"),
                (aug_id, "2026-08-01", "2026-08-31", "1300.00", "1250.00")):
            period = tieout.period_tieout(
                account=CHECKING, statement_id=statement_id, start=date.fromisoformat(start),
                end=date.fromisoformat(end), opening=Decimal(opening), closing=Decimal(closing),
                postings=tieout.load_postings(self.entity, [CHECKING])[CHECKING],
                waiting=tieout.load_waiting(load_entity(self.entity), [CHECKING])[CHECKING])
            # Rows waiting in review count with the posted rows, as the import check counts them.
            books = Decimal(period["books_balance_through"]) + Decimal(period["waiting_total"])
            self.assertEqual(books, Decimal(closing), statement_id)
        conn = open_cache(self.entity)
        try:
            self.assertEqual([o["date"] for o in opening_entries(conn, date(2026, 7, 1), date(2026, 8, 31))],
                             [date(2026, 7, 1)])
        finally:
            conn.close()
        # Once the July statement is saved, its printed opening balance wins over a typed one.
        self.assertEqual(record_opening(self.entity, account=CHECKING, on="2026-07-01", amount="1000.00",
                                        replace=True)["reason"], "printed_opening_exists")
        self.assertEqual(record_opening(self.entity, aug_id)["reason"], "opening_exists")

    def test_replace_needs_an_opening_and_no_earlier_entries(self):
        self.assertEqual(record_opening(self.entity, account=CHECKING, on="2026-07-01", amount="1.00",
                                        replace=True)["reason"], "unknown_account")
        july, august = self.july_august()
        aug = self.import_path(august, "aug", period_start="2026-08-01", period_end="2026-08-31")
        self.assertEqual(record_opening(self.entity, aug["statements"][0]["statement"]["statement_id"],
                                        replace=True)["reason"], "no_opening_to_replace")
        record_opening(self.entity, aug["statements"][0]["statement"]["statement_id"])
        store = LedgerStore(default_store_path(self.entity))
        with store.transaction() as conn:
            store.insert_opens([Open(date=date(2026, 6, 1), account="Expenses:Software")], conn)
            store.insert_entries([Entry(date=date(2026, 7, 10), narration="Earlier", meta=(("source-id", "t-1"),),
                                        postings=(Posting(CHECKING, Decimal("-10.00")),
                                                  Posting("Expenses:Software", Decimal("10.00"))))], conn)
        refused = record_opening(self.entity, account=CHECKING, on="2026-07-15", amount="1000.00", replace=True)
        self.assertEqual((refused["status"], refused["reason"]), ("not_eligible", "books_before_statement"))

    def test_accounts_without_an_opening_are_not_affected(self):
        july, august = self.july_august()
        aug = self.import_path(august, "aug", period_start="2026-08-01", period_end="2026-08-31")
        record_opening(self.entity, aug["statements"][0]["statement"]["statement_id"])
        savings = "Assets:Bank:Savings"
        store = LedgerStore(default_store_path(self.entity))
        with store.transaction() as conn:
            store.insert_opens([Open(date=date(2026, 1, 1), account=savings)], conn)
        other = self.import_path(july, "jul-savings", account=savings, period_start="2026-07-01",
                                 period_end="2026-07-31")
        self.assertEqual(other["status"], "imported")
        self.assertEqual(self.books_balance(savings, "2026-07-31"), Decimal("300.00"))

    def test_shared_importer_skips_rows_before_the_opening_for_every_source(self):
        from bookkeeping.ledger.importer import import_transactions

        july, august = self.july_august()
        aug = self.import_path(august, "aug", period_start="2026-08-01", period_end="2026-08-31")
        record_opening(self.entity, aug["statements"][0]["statement"]["statement_id"])
        entity = load_entity(self.entity)
        rows = [{"id": f"feed-{n}", "date": day, "amount": "-5.00", "description": "FEED ROW", "account": "Checking",
                 "accountId": "feed-chk", "currency": "USD"} for n, day in enumerate(("2026-07-20", "2026-08-10"))]
        config = json.loads((self.entity / "entity.json").read_text())
        config["bank_account_mappings"] = {"feed-chk": CHECKING}
        (self.entity / "entity.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        entity = load_entity(self.entity)
        result = import_transactions(entity, rows, session_id="feed-test")
        self.assertEqual([(r["source_id"], r["opening_date"]) for r in result.before_opening],
                         [("feed-0", "2026-08-01")])
        self.assertEqual(result.pending_categorization, 1)
        self.assertNotIn("feed-0", _queue_ids(self.entity))

    def test_cli_and_remote_contract(self):
        record = self.imp("chase_like_checking.csv", "checking", period_start="2026-01-01", period_end="2026-01-31")
        argv = ["statement", "record-opening", "--entity", str(self.entity), "--statement-id", record["statement_id"]]
        self.assertFalse(command_spec([*argv, "--dry-run"])["mutates"])
        self.assertTrue(command_spec(argv)["mutates"])
        typed = ["statement", "record-opening", "--entity", str(self.entity), "--account", CHECKING,
                 "--on", "2026-01-01", "--amount", "5.00"]
        self.assertFalse(command_spec([*typed, "--dry-run"])["mutates"])
        self.assertTrue(command_spec(typed)["mutates"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main([*typed, "--dry-run"]), 0)
        self.assertEqual(json.loads(out.getvalue())["reason"], "printed_opening_exists")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(argv), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "recorded")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["statement", "record-opening", "--entity", str(self.entity),
                                   "--statement-id", "missing"]), 1)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], "statement_not_found")


if __name__ == "__main__":
    unittest.main()
