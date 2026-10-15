from __future__ import annotations

from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.cli import main
from bookkeeping.coverage import (
    check_coverage,
    format_range,
    load_statements,
    readiness_items,
    record_statement,
)
from bookkeeping.ledger.model import Entry, Open, Posting
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.remote_contract import command_spec


CHECKING = "Assets:Bank:Operating-Checking"
CARD = "Liabilities:CreditCard:Business-Card"


def make_company(root: Path, *, config: dict | None = None) -> Path:
    """Synthetic company with a checking account and a card."""
    company = root / "company"
    company.mkdir()
    (company / "entity.json").write_text(json.dumps({
        "name": "Synthetic Coverage Co",
        "business_type": "consulting",
        "bank_account_mappings": {"feed-checking": CHECKING, "feed-card": CARD},
        **(config or {}),
    }) + "\n")
    store = LedgerStore(company / "ledger.sqlite")
    store.initialize()
    with store.transaction() as conn:
        store.insert_opens([Open(date(2026, 1, 1), account, ("USD",)) for account in (
            CHECKING, CARD, "Income:Consulting", "Expenses:Software", "Equity:Opening-Balances")], conn)
    return company


def post(company: Path, account: str, days: list[str], *, source: bool = True) -> None:
    store = LedgerStore(company / "ledger.sqlite")
    with store.transaction() as conn:
        entries = []
        for index, day in enumerate(days):
            meta = (("source-id", f"synthetic-{account}-{day}-{index}"),) if source else ()
            entries.append(Entry(
                date=date.fromisoformat(day), narration="Synthetic activity", meta=meta,
                postings=(Posting(account, Decimal("10.00")), Posting("Income:Consulting", Decimal("-10.00"))),
            ))
        store.insert_entries(entries, conn)


def statement(company: Path, statement_id: str, start: str, end: str, opening=None, closing=None,
              *, account: str = CHECKING, kind: str = "statement", source: str = "") -> None:
    record_statement(company, {
        "statement_id": statement_id, "account": account, "kind": kind,
        "period_start": start, "period_end": end,
        "opening_balance": opening, "closing_balance": closing, "source": source,
    })


def month(document: dict, account: str, period: str) -> dict:
    row = next(item for item in document["accounts"] if item["account"] == account)
    return next(item for item in row["months"] if item["month"] == period)


class FormatTests(unittest.TestCase):
    def test_ranges_are_plain_language(self):
        self.assertEqual(format_range(date(2026, 3, 3), date(2026, 3, 31)), "Mar 3–31")
        self.assertEqual(format_range(date(2026, 3, 3), date(2026, 3, 3)), "Mar 3")
        self.assertEqual(format_range(date(2026, 3, 30), date(2026, 4, 2)), "Mar 30–Apr 2")
        self.assertEqual(format_range(date(2025, 12, 30), date(2026, 1, 2)), "Dec 30, 2025–Jan 2, 2026")


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.company = make_company(Path(self.temp.name))

    def check(self, start="2026-01-01", end="2026-03-31", **kwargs):
        return check_coverage(self.company, date.fromisoformat(start), date.fromisoformat(end), **kwargs)

    def test_transaction_fallback_reports_trailing_gap_and_missing_month(self):
        post(self.company, CHECKING, ["2026-01-02", "2026-01-20", "2026-02-05", "2026-02-25", "2026-03-02"])
        document = self.check(accounts=[CHECKING])
        jan, mar = month(document, CHECKING, "2026-01"), month(document, CHECKING, "2026-03")
        self.assertEqual(jan["status"], "partial")
        self.assertEqual(jan["gaps"], [{"from": "2026-01-01", "to": "2026-01-01", "days": 1}])
        self.assertEqual(mar["status"], "partial")
        self.assertEqual(mar["reasons"], ["Operating Checking has no transactions for Mar 3–31."])
        self.assertFalse(month(document, CHECKING, "2026-02")["verified"])
        self.assertIn("inferred from transaction dates only", month(document, CHECKING, "2026-02")["notes"][0])
        self.assertEqual(document["status"], "incomplete")

        empty = self.check(start="2026-04-01", end="2026-04-30", accounts=[CHECKING])
        self.assertEqual(month(empty, CHECKING, "2026-04")["status"], "missing")
        self.assertEqual(month(empty, CHECKING, "2026-04")["basis"], [])

    def test_quiet_gap_longer_than_limit_is_a_gap(self):
        post(self.company, CHECKING, ["2026-01-01", "2026-01-31", "2026-03-15", "2026-03-31"])
        document = self.check(accounts=[CHECKING], max_quiet_days=31)
        self.assertEqual(month(document, CHECKING, "2026-02")["status"], "missing")
        self.assertEqual(month(document, CHECKING, "2026-03")["gaps"][0], {"from": "2026-03-01", "to": "2026-03-14", "days": 14})
        loose = self.check(accounts=[CHECKING], max_quiet_days=60)
        self.assertEqual(month(loose, CHECKING, "2026-02")["status"], "complete")

    def test_entries_without_source_ids_do_not_prove_coverage(self):
        post(self.company, CHECKING, ["2026-01-01", "2026-01-31"], source=False)
        document = self.check(end="2026-01-31", accounts=[CHECKING])
        self.assertEqual(month(document, CHECKING, "2026-01")["status"], "missing")

    def test_statements_verify_months_and_continuity_holds(self):
        statement(self.company, "chk-2026-01", "2026-01-01", "2026-01-31", "100.00", "150.00", source="Operating Checking")
        statement(self.company, "chk-2026-02", "2026-02-01", "2026-02-28", "150.00", "175.00")
        statement(self.company, "chk-2026-03", "2026-03-01", "2026-03-31", "175.00", "90.00")
        document = self.check(accounts=[CHECKING])
        self.assertEqual(document["status"], "complete")
        self.assertTrue(all(m["verified"] and m["basis"] == ["statement"]
                            for m in document["accounts"][0]["months"]))
        self.assertEqual(document["findings"], [])

    def test_balance_discontinuity_is_an_error(self):
        statement(self.company, "chk-2026-01", "2026-01-01", "2026-01-31", "100.00", "150.00")
        statement(self.company, "chk-2026-02", "2026-02-01", "2026-02-28", "140.00", "175.00")
        document = self.check(end="2026-02-28", accounts=[CHECKING])
        finding = next(item for item in document["findings"] if item["code"] == "balance_discontinuity")
        self.assertEqual(finding["severity"], "error")
        self.assertEqual(finding["difference"], "-10.00")
        self.assertIn("closes at 150.00, but the next statement opens at 140.00", finding["message"])
        self.assertEqual(document["status"], "incomplete")

    def test_overlapping_duplicate_and_conflicting_statements(self):
        statement(self.company, "chk-a", "2026-01-01", "2026-01-31", "100.00", "150.00")
        statement(self.company, "chk-b", "2026-01-01", "2026-01-31", "100.00", "150.00")
        statement(self.company, "chk-c", "2026-01-20", "2026-02-28", "120.00", "160.00")
        document = self.check(end="2026-02-28", accounts=[CHECKING])
        codes = {item["code"]: item for item in document["findings"]}
        self.assertEqual(codes["statement_duplicate"]["severity"], "warning")
        self.assertEqual((codes["statement_overlap"]["from"], codes["statement_overlap"]["to"]), ("2026-01-20", "2026-01-31"))
        self.assertEqual(month(document, CHECKING, "2026-02")["status"], "complete")

        record_statement(self.company, {"statement_id": "chk-b", "account": CHECKING, "period_start": "2026-01-01",
                                        "period_end": "2026-01-31", "opening_balance": "100.00",
                                        "closing_balance": "999.00"}, replace=True)
        conflict = self.check(end="2026-01-31", accounts=[CHECKING])
        self.assertIn("statement_conflict", {item["code"] for item in conflict["findings"]})

    def test_statement_gap_and_feed_window_fill_it(self):
        statement(self.company, "chk-01", "2026-01-01", "2026-01-31", "100.00", "150.00")
        statement(self.company, "chk-03", "2026-03-01", "2026-03-31", "150.00", "160.00")
        document = self.check(accounts=[CHECKING])
        self.assertEqual(month(document, CHECKING, "2026-02")["status"], "missing")
        self.assertIn("statement_gap", {item["code"] for item in document["findings"]})
        statement(self.company, "feed-02", "2026-02-01", "2026-02-28", kind="feed_sync")
        filled = self.check(accounts=[CHECKING])
        self.assertEqual(month(filled, CHECKING, "2026-02")["basis"], ["feed_sync"])
        self.assertTrue(month(filled, CHECKING, "2026-02")["verified"])

    def test_declared_coverage_counts_as_verified(self):
        config = json.loads((self.company / "entity.json").read_text())
        config["declared_sources"] = [{"name": "Company card", "account": CARD,
                                       "coverage_from": "2026-01-10", "coverage_to": "2026-12-31"}]
        (self.company / "entity.json").write_text(json.dumps(config))
        document = self.check(end="2026-01-31", accounts=[CARD])
        jan = month(document, CARD, "2026-01")
        self.assertEqual(jan["status"], "partial")
        self.assertEqual(jan["reasons"], ["Company card has no statement, feed window, declared coverage or transactions for Jan 1–9."])

    def test_default_scope_uses_mapped_accounts_and_reports_missing_scope(self):
        document = self.check(end="2026-01-31")
        self.assertEqual([row["account"] for row in document["accounts"]], [CHECKING, CARD])
        config = json.loads((self.company / "entity.json").read_text())
        config.pop("bank_account_mappings")
        (self.company / "entity.json").write_text(json.dumps(config))
        empty = self.check(end="2026-01-31")
        self.assertEqual(empty["findings"][0]["code"], "source_scope_missing")

    def test_invalid_records_are_reported_and_ignored(self):
        directory = self.company / "ingestion" / "statements"
        directory.mkdir(parents=True)
        (directory / "bad.json").write_text(json.dumps({
            "statement_id": "bad", "account": "Expenses:Software",
            "period_start": "2026-01-01", "period_end": "2026-01-31"}))
        (directory / "broken.json").write_text("{not json")
        (directory / "feed.json").write_text(json.dumps([{
            "statement_id": "feed", "account": CHECKING, "kind": "feed_sync",
            "period_start": "2026-01-01", "period_end": "2026-01-31", "closing_balance": "1.00"}]))
        records, findings = load_statements(self.company)
        self.assertEqual(records, [])
        self.assertEqual(len(findings), 3)
        document = self.check(end="2026-01-31", accounts=[CHECKING])
        self.assertEqual(month(document, CHECKING, "2026-01")["status"], "missing")
        self.assertEqual(document["summary"]["errors"], 3)

    def test_record_statement_validates_and_refuses_silent_replace(self):
        with self.assertRaisesRegex(ValueError, "not in the account catalog"):
            statement(self.company, "x", "2026-01-01", "2026-01-31", account="Assets:Bank:Ghost")
        with self.assertRaisesRegex(ValueError, "must not be after"):
            statement(self.company, "x", "2026-02-01", "2026-01-31")
        with self.assertRaisesRegex(ValueError, "two decimal places"):
            statement(self.company, "x", "2026-01-01", "2026-01-31", "1.005", None)
        statement(self.company, "x", "2026-01-01", "2026-01-31")
        with self.assertRaisesRegex(ValueError, "already exists"):
            statement(self.company, "x", "2026-01-01", "2026-01-31")

    def test_readiness_items_shape(self):
        post(self.company, CHECKING, ["2026-01-01", "2026-01-15"])
        statement(self.company, "chk-01", "2026-01-01", "2026-01-31", "100.00", "150.00")
        statement(self.company, "chk-02", "2026-02-01", "2026-02-28", "149.00", "150.00")
        blockers, warnings = readiness_items(self.check(end="2026-02-28", accounts=[CHECKING]))
        self.assertEqual({item["code"] for item in blockers}, {"source_balance_discontinuity"})
        self.assertTrue(all(set(item) == {"code", "message", "refs"} for item in blockers + warnings))
        blockers, _ = readiness_items(self.check(start="2026-03-01", end="2026-03-31", accounts=[CHECKING]))
        self.assertEqual(blockers[0]["code"], "source_month_missing")

    def test_check_is_deterministic(self):
        post(self.company, CHECKING, ["2026-01-05"])
        self.assertEqual(self.check(), self.check())


class CoverageCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.company = make_company(Path(self.temp.name))

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_statement_add_and_check_json(self):
        code, _ = self.run_cli("coverage", "statement-add", "--entity", str(self.company), "--statement-id", "c1",
                               "--account", CHECKING, "--from", "2026-01-01", "--to", "2026-01-31",
                               "--opening-balance", "0.00", "--closing-balance", "10.00")
        self.assertEqual(code, 0)
        code, output = self.run_cli("coverage", "check", "--entity", str(self.company), "--from", "2026-01-01",
                                    "--to", "2026-01-31", "--account", CHECKING, "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "complete")
        code, output = self.run_cli("coverage", "statement-list", "--entity", str(self.company))
        self.assertEqual(json.loads(output)["statements"][0]["statement_id"], "c1")

    def test_remote_contract_classifies_commands(self):
        read = command_spec(["coverage", "check", "--entity", "@entity", "--from", "2026-01-01", "--to", "2026-01-31"])
        self.assertFalse(read["mutates"])
        write = command_spec(["coverage", "statement-add", "--entity", "@entity", "--statement-id", "c1",
                              "--account", CHECKING, "--from", "2026-01-01", "--to", "2026-01-31"])
        self.assertTrue(write["mutates"])


if __name__ == "__main__":
    unittest.main()
