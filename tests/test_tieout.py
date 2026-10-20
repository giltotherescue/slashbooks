"""Tests for tie-out proof (import, coverage per month) and bank balance against books.

All data is synthetic.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bookkeeping import statement_import as si  # noqa: E402
from bookkeeping import tieout  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.coverage import check_coverage, readiness_items, record_statement  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "statements"
CHECKING = "Assets:Bank:Checking"
CARD = "Liabilities:CreditCard:Visa"


def make_company(root: Path, mappings: dict | None = None) -> Path:
    company = root / "co"
    (company / "staging").mkdir(parents=True)
    (company / "entity.json").write_text(json.dumps({
        "name": "Synthetic Co", "business_type": "consulting",
        "bank_account_mappings": mappings if mappings is not None else {"feed-chk": CHECKING, "feed-card": CARD},
    }) + "\n")
    (company / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n")
    return company


def post(company: Path, account: str, day: str, amount: str, *, other: str = "Income:Consulting") -> None:
    """Post one two-sided entry; ``amount`` is the ledger-sign amount on ``account``."""
    store = LedgerStore(company / "ledger.sqlite")
    store.initialize()
    with store.transaction() as conn:
        store.insert_opens([Open(date(2026, 1, 1), name, ("USD",)) for name in (account, other)], conn)
        store.insert_entries([Entry(
            date=date.fromisoformat(day), narration="Synthetic", meta=(),
            postings=(Posting(account, Decimal(amount)), Posting(other, -Decimal(amount))))], conn)


def stage(company: Path, rows: list[dict]) -> None:
    (company / "staging" / "pending-categorization.json").write_text(json.dumps(rows) + "\n")


def statement(company: Path, sid: str, start: str, end: str, opening, closing, account: str = CHECKING) -> None:
    record_statement(company, {
        "statement_id": sid, "account": account, "kind": "statement", "period_start": start,
        "period_end": end, "opening_balance": opening, "closing_balance": closing})


def run_cli(*argv: str):
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(list(argv))
    return code, json.loads(out.getvalue())


class StatementTieoutUnitTests(unittest.TestCase):
    def test_matches_and_differs_in_presentation_sign(self):
        record = {"opening_balance": "100.00", "closing_balance": "130.00",
                  "transactions_total": "30.00", "transaction_count": 2}
        tie = tieout.statement_tieout(record, 1)
        self.assertEqual(tie, {"status": "matches", "opening": "100.00", "rows_total": "30.00",
                               "expected_closing": "130.00", "closing": "130.00", "difference": "0.00",
                               "row_count": 2})
        record["closing_balance"] = "131.00"
        tie = tieout.statement_tieout(record, 1)
        self.assertEqual((tie["status"], tie["difference"]), ("differs", "1.00"))

    def test_liability_sign_is_applied_once(self):
        # A charge of 20.00 is -20.00 in the ledger and raises the amount owed.
        record = {"opening_balance": "100.00", "closing_balance": "120.00",
                  "transactions_total": "-20.00", "transaction_count": 1}
        self.assertEqual(tieout.statement_tieout(record, tieout.presentation_sign(CARD))["status"], "matches")
        self.assertEqual(tieout.statement_tieout(record, tieout.presentation_sign(CHECKING))["status"], "differs")

    def test_missing_balance_is_no_balances(self):
        tie = tieout.statement_tieout({"opening_balance": None, "closing_balance": "5.00",
                                       "transactions_total": "1.00", "transaction_count": 1}, 1)
        self.assertEqual((tie["status"], tie["difference"], tie["expected_closing"]), ("no_balances", None, None))


class ImportTieoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.entity = make_company(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def map(self, name, profile, account=CHECKING, **kw):
        return si.save_profile(self.entity, FIXTURES / name, name=profile, ledger_account=account, overrides=kw)

    def imp(self, name, profile=None, **kw):
        return si.import_file(self.entity, FIXTURES / name, profile=profile, ts="2026-02-01T00:00:00Z", **kw)

    def test_csv_running_balances_match(self):
        self.map("chase_like_checking.csv", "chase")
        [entry] = self.imp("chase_like_checking.csv", "chase")["statements"]
        self.assertEqual(entry["tieout"], {
            "status": "matches", "opening": "1000.00", "rows_total": "2142.26",
            "expected_closing": "3142.26", "closing": "3142.26", "difference": "0.00", "row_count": 6})
        self.assertEqual(entry["statement"]["tieout"], entry["tieout"])
        self.assertEqual(entry["counts"]["to_import"], 6)

    def test_ofx_ledger_balance_matches(self):
        self.map("checking.ofx", "checking-ofx")
        [entry] = self.imp("checking.ofx")["statements"]
        self.assertEqual((entry["tieout"]["status"], entry["tieout"]["closing"]), ("matches", "3142.26"))

    def test_card_sign_and_override_balances(self):
        self.map("card_debit_credit.csv", "card", account=CARD)
        entry = self.imp("card_debit_credit.csv", "card", period_start="2026-01-01", period_end="2026-01-31",
                         opening_balance="1000.00", closing_balance="769.99")["statements"][0]
        self.assertEqual((entry["tieout"]["status"], entry["tieout"]["rows_total"]), ("matches", "-230.01"))
        self.assertEqual(entry["warnings"], [])
        wrong = self.imp("card_debit_credit.csv", "card", period_start="2026-01-01", period_end="2026-01-31",
                         opening_balance="1000.00", closing_balance="900.00")["statements"][0]
        self.assertEqual((wrong["tieout"]["status"], wrong["tieout"]["difference"]), ("differs", "130.01"))
        self.assertTrue(any("check for missing or extra rows" in w for w in wrong["warnings"]))

    def test_file_without_balances_is_no_balances(self):
        self.map("amex_like_card.csv", "amex", account=CARD)
        [entry] = self.imp("amex_like_card.csv", "amex")["statements"]
        self.assertEqual(entry["tieout"]["status"], "no_balances")
        self.assertIsNone(entry["tieout"]["difference"])
        self.assertEqual(entry["tieout"]["row_count"], 4)

    def test_default_differs_is_a_warning_and_imports(self):
        self.map("card_debit_credit.csv", "card", account=CARD)
        result = self.imp("card_debit_credit.csv", "card", opening_balance="1000.00", closing_balance="900.00")
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["statements"][0]["counts"]["queued_for_review"], 3)

    def test_require_tieout_blocks_with_no_write(self):
        self.map("card_debit_credit.csv", "card", account=CARD)
        before = (self.entity / "entity.json").read_text()
        result = self.imp("card_debit_credit.csv", "card", opening_balance="1000.00",
                          closing_balance="900.00", require_tieout=True)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["error"]["code"], "tieout_differs")
        self.assertEqual(result["statements"][0]["tieout"]["status"], "differs")
        self.assertFalse((self.entity / "ledger.sqlite").exists())
        self.assertFalse((self.entity / "staging" / "pending-categorization.json").exists())
        self.assertFalse((self.entity / "ingestion").exists())
        self.assertEqual((self.entity / "entity.json").read_text(), before)
        # A matching tie-out and a file with no balances both import.
        ok = self.imp("card_debit_credit.csv", "card", opening_balance="1000.00",
                      closing_balance="769.99", require_tieout=True)
        self.assertEqual(ok["status"], "imported")
        self.map("amex_like_card.csv", "amex", account=CARD)
        self.assertEqual(self.imp("amex_like_card.csv", "amex", require_tieout=True)["status"], "imported")

    def test_require_tieout_cli_exit_code_and_no_write(self):
        path = str(FIXTURES / "card_debit_credit.csv")
        self.map("card_debit_credit.csv", "card", account=CARD)
        args = ["statement", "import", path, "--entity", str(self.entity), "--profile", "card",
                "--opening-balance", "1000.00", "--closing-balance", "900.00"]
        code, result = run_cli(*args, "--require-tieout")
        self.assertEqual((code, result["status"]), (4, "needs_review"))
        self.assertFalse((self.entity / "staging" / "pending-categorization.json").exists())
        code, result = run_cli(*args)
        self.assertEqual((code, result["status"]), (0, "imported"))

    def test_remote_contract_still_classifies_import_as_write(self):
        spec = command_spec(["statement", "import", "a.csv", "--entity", "e", "--require-tieout"])
        self.assertTrue(spec["mutates"])
        spec = command_spec(["statement", "import", "a.csv", "--entity", "e", "--require-tieout", "--dry-run"])
        self.assertFalse(spec["mutates"])


class CoverageTieoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.company = make_company(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, start="2026-01-01", end="2026-03-31"):
        return check_coverage(self.company, date.fromisoformat(start), date.fromisoformat(end),
                              accounts=[CHECKING])

    def months(self, document):
        return {m["month"]: m for m in document["accounts"][0]["months"]}

    def test_month_aligned_statement_ties_and_differs(self):
        post(self.company, CHECKING, "2026-01-10", "300.00")
        post(self.company, CHECKING, "2026-01-20", "-50.00")
        statement(self.company, "jan", "2026-01-01", "2026-01-31", "1000.00", "1250.00")
        tie = self.months(self.check())["2026-01"]["tieout"]
        self.assertEqual(tie, {"status": "matches", "statement_id": "jan", "statement_balance": "1250.00",
                               "books_balance_through": "1250.00", "difference": "0.00",
                               "waiting_rows": 0, "waiting_total": "0.00"})
        statement(self.company, "jan2", "2026-02-01", "2026-02-28", "1250.00", "1400.00")
        tie = self.months(self.check())["2026-02"]["tieout"]
        self.assertEqual((tie["status"], tie["difference"], tie["books_balance_through"]),
                         ("differs", "150.00", "1250.00"))

    def test_posting_on_the_period_start_day_counts(self):
        post(self.company, CHECKING, "2026-01-01", "-10.00")
        post(self.company, CHECKING, "2026-01-15", "100.00")
        statement(self.company, "jan", "2026-01-01", "2026-01-31", "500.00", "590.00")
        tie = self.months(self.check())["2026-01"]["tieout"]
        self.assertEqual((tie["status"], tie["books_balance_through"], tie["difference"]),
                         ("matches", "590.00", "0.00"))

    def test_two_statements_chain_each_month_ties(self):
        post(self.company, CHECKING, "2026-01-10", "100.00")
        post(self.company, CHECKING, "2026-02-10", "-40.00")
        statement(self.company, "jan", "2026-01-01", "2026-01-31", "500.00", "600.00")
        statement(self.company, "feb", "2026-02-01", "2026-02-28", "600.00", "560.00")
        months = self.months(self.check("2026-01-01", "2026-02-28"))
        self.assertEqual([months[m]["tieout"]["status"] for m in ("2026-01", "2026-02")], ["matches", "matches"])
        self.assertEqual(self.check("2026-01-01", "2026-02-28")["findings"], [])

    def test_statement_spanning_months_is_not_aligned(self):
        post(self.company, CHECKING, "2026-01-20", "100.00")
        statement(self.company, "span", "2026-01-15", "2026-02-14", "0.00", "100.00")
        document = self.check("2026-01-01", "2026-02-28")
        months = self.months(document)
        for key in ("2026-01", "2026-02"):
            self.assertEqual(months[key]["tieout"]["status"], "not_aligned")
            self.assertIsNone(months[key]["tieout"]["difference"])
        # The statement itself still proves its own period.
        self.assertEqual(document["accounts"][0]["statements"][0]["tieout"]["status"], "matches")
        # A month with no statement has no tie-out.
        self.assertIsNone(self.months(self.check())["2026-03"]["tieout"])

    def test_reversal_nets_out(self):
        post(self.company, CHECKING, "2026-01-10", "300.00")
        post(self.company, CHECKING, "2026-01-12", "-300.00")  # the reversal
        post(self.company, CHECKING, "2026-01-15", "25.00")
        statement(self.company, "jan", "2026-01-01", "2026-01-31", "100.00", "125.00")
        self.assertEqual(self.months(self.check())["2026-01"]["tieout"]["status"], "matches")

    def test_waiting_rows_are_counted_for_the_account_and_period(self):
        post(self.company, CHECKING, "2026-01-10", "100.00")
        stage(self.company, [
            {"id": "a", "date": "2026-01-12", "amount": "40.00", "accountId": "feed-chk"},
            {"id": "b", "date": "2026-01-30", "amount": "-15.00", "accountId": "feed-chk"},
            {"id": "c", "date": "2026-02-02", "amount": "9.00", "accountId": "feed-chk"},
            {"id": "d", "date": "2026-01-12", "amount": "7.00", "accountId": "feed-card"}])
        statement(self.company, "jan", "2026-01-01", "2026-01-31", "0.00", "125.00")
        tie = self.months(self.check())["2026-01"]["tieout"]
        self.assertEqual((tie["status"], tie["difference"], tie["waiting_rows"], tie["waiting_total"]),
                         ("differs", "25.00", 2, "25.00"))

    def test_card_liability_sign(self):
        post(self.company, CARD, "2026-01-05", "-80.00", other="Expenses:Software")
        statement(self.company, "card-jan", "2026-01-01", "2026-01-31", "20.00", "100.00", account=CARD)
        document = check_coverage(self.company, date(2026, 1, 1), date(2026, 1, 31), accounts=[CARD])
        tie = document["accounts"][0]["months"][0]["tieout"]
        self.assertEqual((tie["status"], tie["books_balance_through"]), ("matches", "100.00"))

    def test_no_balances_and_unknown_account_and_missing_ledger(self):
        statement(self.company, "jan", "2026-01-01", "2026-01-31", None, None)
        tie = self.months(self.check())["2026-01"]["tieout"]
        self.assertEqual((tie["status"], tie["difference"], tie["books_balance_through"]), ("no_balances", None, None))
        other = "Assets:Bank:Never-Seen"
        statement(self.company, "ghost", "2026-02-01", "2026-02-28", "0.00", "50.00", account=other)
        document = check_coverage(self.company, date(2026, 2, 1), date(2026, 2, 28), accounts=[other])
        tie = document["accounts"][0]["months"][0]["tieout"]
        self.assertEqual((tie["status"], tie["difference"]), ("differs", "50.00"))
        # No ledger file at all: movement is zero, nothing raises.
        with tempfile.TemporaryDirectory() as tmp:
            bare = make_company(Path(tmp))
            statement(bare, "x", "2026-01-01", "2026-01-31", "10.00", "10.00")
            document = check_coverage(bare, date(2026, 1, 1), date(2026, 1, 31), accounts=[CHECKING])
            self.assertEqual(document["accounts"][0]["months"][0]["tieout"]["status"], "matches")

    def test_readiness_warning_not_blocker(self):
        post(self.company, CHECKING, "2026-01-10", "100.00")
        statement(self.company, "jan", "2026-01-01", "2026-01-31", "0.00", "90.00")
        blockers, warnings = readiness_items(self.check("2026-01-01", "2026-01-31"))
        self.assertNotIn("statement_tieout_differs", [b["code"] for b in blockers])
        [warning] = [w for w in warnings if w["code"] == "statement_tieout_differs"]
        self.assertEqual(warning["refs"], [CHECKING, "jan"])
        self.assertIn("-10.00", warning["message"])
        statement(self.company, "feb", "2026-02-01", "2026-02-28", "90.00", "90.00")
        _, clean = readiness_items(self.check("2026-02-01", "2026-02-28"))
        self.assertEqual([w for w in clean if w["code"] == "statement_tieout_differs"], [])

    def test_coverage_check_stays_read_only(self):
        post(self.company, CHECKING, "2026-01-10", "100.00")
        statement(self.company, "jan", "2026-01-01", "2026-01-31", "0.00", "100.00")
        files = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.company.rglob("*") if p.is_file()}
        self.check()
        run_cli("coverage", "check", "--entity", str(self.company), "--from", "2026-01-01",
                "--to", "2026-01-31", "--format", "json")
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.company.rglob("*") if p.is_file()}
        self.assertEqual(files, after)


class BalanceCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.company = make_company(Path(self._tmp.name), {
            "feed-chk": CHECKING, "feed-chk-2": CHECKING, "feed-card": CARD})
        post(self.company, CHECKING, "2026-01-10", "1000.00")
        post(self.company, CHECKING, "2026-01-20", "-200.00")

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, *items):
        return tieout.balance_check(self.company, list(items))["accounts"]

    def item(self, balance, **kw):
        return {"account_id": "feed-chk", "balance": balance, "as_of": "2026-01-31",
                "data_through": "2026-01-31", **kw}

    def test_matches(self):
        [row] = self.check(self.item("800.00"))
        self.assertEqual(row, {
            "status": "matches", "account_ids": ["feed-chk"], "ledger_account": CHECKING,
            "bank_balance": "800.00", "books_balance": "800.00", "difference": "0.00",
            "waiting_rows": 0, "waiting_total": "0.00", "compared_through": "2026-01-31",
            "compared_basis": "as_of"})

    def test_waiting_in_review_and_plain_differs(self):
        stage(self.company, [
            {"id": "a", "date": "2026-01-25", "amount": "30.00", "accountId": "feed-chk"},
            {"id": "b", "date": "2026-01-26", "amount": "10.00", "accountId": "feed-chk"},
            {"id": "late", "date": "2026-02-05", "amount": "99.00", "accountId": "feed-chk"}])
        [row] = self.check(self.item("840.00"))
        self.assertEqual((row["status"], row["difference"], row["waiting_rows"], row["waiting_total"]),
                         ("waiting_in_review", "40.00", 2, "40.00"))
        [row] = self.check(self.item("845.00"))
        self.assertEqual((row["status"], row["difference"]), ("differs", "45.00"))

    def test_bank_newer_and_date_fallbacks(self):
        [row] = self.check(self.item("800.00", as_of="2026-02-03", data_through="2026-01-31"))
        self.assertEqual(row["status"], "bank_newer")
        [row] = self.check({"account_id": "feed-chk", "balance": "1000.00", "as_of": None,
                            "retrieved_at": "2026-01-15T08:00:00Z", "data_through": "2026-01-31"})
        self.assertEqual((row["status"], row["compared_through"], row["compared_basis"]),
                         ("matches", "2026-01-15", "retrieved_at"))
        [row] = self.check({"account_id": "feed-chk", "balance": "800.00", "data_through": "2026-01-31"})
        self.assertEqual((row["compared_through"], row["compared_basis"]), ("2026-01-31", "data_through"))

    def test_unmapped_and_no_bank_balance(self):
        rows = self.check({"account_id": "nope", "balance": "5.00", "as_of": "2026-01-31"},
                          {"account_id": "feed-chk", "balance": None, "as_of": "2026-01-31"})
        self.assertEqual(sorted(r["status"] for r in rows), ["no_bank_balance", "unmapped"])
        self.assertEqual([r for r in rows if r["status"] == "unmapped"][0]["account_ids"], ["nope"])

    def test_liability_sign_presentation_and_ledger(self):
        post(self.company, CARD, "2026-01-05", "-80.00", other="Expenses:Software")
        item = {"account_id": "feed-card", "balance": "80.00", "as_of": "2026-01-31", "data_through": "2026-01-31"}
        [row] = self.check(item)
        self.assertEqual((row["status"], row["books_balance"]), ("matches", "80.00"))
        [row] = self.check({**item, "balance": "-80.00", "sign": "ledger"})
        self.assertEqual((row["status"], row["bank_balance"]), ("matches", "80.00"))

    def test_many_feeds_to_one_ledger_account_are_summed(self):
        [row] = self.check(self.item("500.00"), {**self.item("300.00"), "account_id": "feed-chk-2"})
        self.assertEqual((row["status"], row["account_ids"], row["bank_balance"]),
                         ("matches", ["feed-chk", "feed-chk-2"], "800.00"))

    def test_reversal_in_range_and_compare_date_cap(self):
        post(self.company, CHECKING, "2026-01-25", "-200.00")
        post(self.company, CHECKING, "2026-01-26", "200.00")  # reversal nets out
        [row] = self.check(self.item("800.00"))
        self.assertEqual(row["status"], "matches")
        # Books are compared through the bank's day, not through all postings.
        [row] = self.check(self.item("1000.00", as_of="2026-01-15", data_through="2026-01-31"))
        self.assertEqual(row["status"], "matches")

    def test_cli_is_read_only_and_classified_as_read(self):
        input_path = Path(self._tmp.name) / "balances.json"
        input_path.write_text(json.dumps([self.item("800.00")]))
        # A first coherent read may create only the shared empty operational lock.
        # The dedicated concurrency regression also asserts every accounting byte stays unchanged.
        tieout.balance_check(self.company, [self.item("800.00")])
        files = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.company.rglob("*") if p.is_file()}
        code, result = run_cli("balance", "check", "--entity", str(self.company), "--input", str(input_path))
        self.assertEqual((code, result["accounts"][0]["status"]), (0, "matches"))
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.company.rglob("*") if p.is_file()}
        self.assertEqual(files, after)
        spec = command_spec(["balance", "check", "--entity", "e", "--input", "i.json"])
        self.assertFalse(spec["mutates"])
        self.assertEqual((spec["entity_fields"], spec["input_fields"]), (["entity"], ["input"]))


if __name__ == "__main__":
    unittest.main()
