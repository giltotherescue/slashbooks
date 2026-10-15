"""End-to-end catch-up flow: statement import feeds the coverage check and the tax export.

inspect -> map -> import -> coverage check -> readiness -> import the missing
statement -> resolve duplicates -> review -> tax map -> tax export.

Every file under tests/fixtures/catch_up/ is synthetic. The year has:

- an OFX statement for Jan 1-Apr 30,
- no statement for May (the deliberate gap; the file arrives later),
- a CSV export for Jun-Sep whose period the person confirms,
- an OFX statement for Oct 1-Dec 20 whose ledger balance is 50.00 lower than
  the CSV closing balance implies (the deliberate balance break), so Dec 21-31
  has no source.
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.cli import main  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.taxmap import DISCLAIMER  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "catch_up"
CHECKING = "Assets:Bank:Operating-Checking"
ACCOUNTS = (CHECKING, "Income:Consulting", "Expenses:Software", "Equity:Opening-Balances")


def make_company(root: Path) -> Path:
    company = root / "company"
    (company / "staging").mkdir(parents=True)
    (company / "entity.json").write_text(json.dumps({
        "name": "Synthetic Catch-Up Co",
        "business_type": "consulting",
        "legal_structure": "S corporation",
        "bank_account_mappings": {"feed-chk": CHECKING},
    }) + "\n", encoding="utf-8")
    (company / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n", encoding="utf-8")
    store = LedgerStore(company / "ledger.sqlite")
    store.initialize()
    with store.transaction() as conn:
        store.insert_opens([Open(date(2025, 12, 1), account, ("USD",)) for account in ACCOUNTS], conn)
        store.insert_entries([Entry(
            date=date(2025, 12, 31), narration="Opening balance", meta=(("source-id", "opening-2025"),),
            postings=(Posting(CHECKING, Decimal("10000.00")),
                      Posting("Equity:Opening-Balances", Decimal("-10000.00"))))], conn)
    return company


class CatchUpPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.company = make_company(self.root)
        self.entity = str(self.company)

    def cli(self, *argv: str, expect: int = 0) -> str:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        self.assertEqual(code, expect, f"books {' '.join(argv)}\n{out.getvalue()}\n{err.getvalue()}")
        return out.getvalue()

    def cli_json(self, *argv: str, expect: int = 0) -> dict:
        return json.loads(self.cli(*argv, expect=expect))

    def coverage(self) -> dict:
        return self.cli_json("coverage", "check", "--entity", self.entity, "--from", "2026-01-01",
                             "--to", "2026-12-31", "--format", "json")

    def months(self, document: dict) -> dict[str, tuple[str, bool]]:
        [account] = document["accounts"]
        self.assertEqual(account["account"], CHECKING)
        return {m["month"]: (m["status"], m["verified"]) for m in account["months"]}

    def readiness(self, period: str) -> dict:
        out = self.root / f"readiness-{period}.json"
        self.cli("readiness", "--entity", self.entity, "--period", period, "--as-of", "2027-01-15",
                 "--output", str(out))
        return json.loads(out.read_text(encoding="utf-8"))

    def pending(self) -> list[dict]:
        path = self.company / "staging" / "pending-categorization.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def test_statements_drive_coverage_readiness_and_the_tax_export(self):
        # A bank feed already delivered one January charge.
        feed = self.root / "feed.json"
        feed.write_text(json.dumps({"transactions": [{
            "id": "feed-0113", "date": "2026-01-13", "description": "CONTOSO SOFTWARE",
            "amount": "-49.99", "accountId": "feed-chk", "pending": False}]}), encoding="utf-8")
        self.cli("ingest", str(feed), "--entity", self.entity, "--source", "bank-feed")

        # 1. Inspect and map the OFX account, then import Jan-Apr.
        ofx_jan = str(FIXTURES / "checking-2026-01-to-04.ofx")
        inspected = self.cli_json("statement", "inspect", ofx_jan, "--entity", self.entity)
        self.assertEqual(inspected["status"], "ready_to_confirm")
        self.assertEqual(inspected["accounts"][0]["ledger_balance_date"], "2026-04-30")
        self.cli_json("statement", "map", ofx_jan, "--entity", self.entity, "--profile", "checking-ofx",
                      "--ledger-account", CHECKING, "--account-name", "Operating Checking")
        [jan] = self.cli_json("statement", "import", ofx_jan, "--entity", self.entity)["statements"]
        self.assertEqual(jan["coverage"], "recorded")
        self.assertEqual((jan["statement"]["opening_balance"], jan["statement"]["closing_balance"]),
                         ("10000.00", "29800.04"))
        # The feed charge and the statement row are the same payment: held, not posted twice.
        [held] = jan["held"]
        self.assertEqual(held["existing_source_id"], "feed-0113")

        # 2. Inspect and map the CSV export; import it, then confirm its period.
        csv_jun = str(FIXTURES / "checking-2026-06-to-09.csv")
        inspected = self.cli_json("statement", "inspect", csv_jun, "--ledger-account", CHECKING)
        # Every day in the file is the 5th or the 12th, so the date format is the
        # person's decision, never a guess.
        self.assertEqual((inspected["status"], inspected["uncertain"]), ("needs_mapping", ["date_format"]))
        self.cli_json("statement", "map", csv_jun, "--entity", self.entity, "--profile", "checking-csv",
                      "--ledger-account", CHECKING, "--account-name", "Operating Checking",
                      "--date-format", "%m/%d/%Y")
        [jun] = self.cli_json("statement", "import", csv_jun, "--entity", self.entity,
                              "--profile", "checking-csv")["statements"]
        self.assertEqual(jun["coverage"], "needs_period")
        [jun] = self.cli_json("statement", "import", csv_jun, "--entity", self.entity, "--profile",
                              "checking-csv", "--period-start", "2026-06-01",
                              "--period-end", "2026-09-30")["statements"]
        self.assertEqual(jun["coverage"], "recorded")
        self.assertEqual(jun["counts"]["already_imported"], 8)

        # 3. Import Oct 1-Dec 20.
        [octo] = self.cli_json("statement", "import", str(FIXTURES / "checking-2026-10-to-12.ofx"),
                               "--entity", self.entity)["statements"]
        self.assertEqual(octo["statement"]["opening_balance"], "54500.09")

        # 4. Coverage: complete, missing and partial months, the gap and the break.
        document = self.coverage()
        months = self.months(document)
        for month in ("2026-01", "2026-02", "2026-03", "2026-04", "2026-06", "2026-07", "2026-08",
                      "2026-09", "2026-10", "2026-11"):
            self.assertEqual(months[month], ("complete", True), month)
        self.assertEqual(months["2026-05"], ("missing", False))
        self.assertEqual(months["2026-12"], ("partial", False))
        december = document["accounts"][0]["months"][-1]
        self.assertEqual(december["gaps"], [{"from": "2026-12-21", "to": "2026-12-31", "days": 11}])
        self.assertEqual(document["status"], "incomplete")
        findings = {(f["code"], f["severity"]): f for f in document["findings"]}
        self.assertEqual(set(findings), {("statement_gap", "warning"), ("balance_discontinuity", "error")})
        self.assertEqual((findings[("statement_gap", "warning")]["from"],
                          findings[("statement_gap", "warning")]["to"]), ("2026-05-01", "2026-05-31"))
        brk = findings[("balance_discontinuity", "error")]
        self.assertEqual((brk["previous_closing_balance"], brk["next_opening_balance"], brk["difference"]),
                         ("54550.09", "54500.09", "-50.00"))
        text = self.cli("coverage", "check", "--entity", self.entity, "--from", "2026-01-01",
                        "--to", "2026-12-31")
        self.assertIn("Operating Checking has no statement, feed window, declared coverage or transactions "
                      "for May 1–31.", text)
        self.assertIn("Operating Checking statement ending Sep 30, 2026 closes at 54,550.09, but the next "
                      "statement opens at 54,500.09 (difference -50.00).", text)

        # 5. Readiness shows the blockers.
        may = self.readiness("2026-05")
        self.assertIn("source_month_missing", {b["code"] for b in may["blockers"]})
        self.assertEqual(may["state"], "blocked")
        october = self.readiness("2026-10")
        self.assertIn("source_balance_discontinuity", {b["code"] for b in october["blockers"]})

        # 6. The missing statement arrives; May becomes verified.
        [may_stmt] = self.cli_json("statement", "import", str(FIXTURES / "checking-2026-05.ofx"),
                                   "--entity", self.entity)["statements"]
        self.assertEqual((may_stmt["statement"]["opening_balance"], may_stmt["statement"]["closing_balance"]),
                         ("29800.04", "34750.05"))
        document = self.coverage()
        self.assertEqual(self.months(document)["2026-05"], ("complete", True))
        self.assertEqual({f["code"] for f in document["findings"]}, {"balance_discontinuity"})
        may = self.readiness("2026-05")
        codes = {b["code"] for b in may["blockers"]}
        self.assertNotIn("source_month_missing", codes)
        # Verified statement coverage also counts as affirmative source coverage.
        self.assertNotIn("source_coverage_incomplete", codes)
        listed = self.cli_json("statement", "list", "--entity", self.entity, "--account", CHECKING)
        self.assertEqual([r["period_start"] for r in listed["statements"]],
                         ["2026-01-01", "2026-05-01", "2026-06-01", "2026-10-01"])
        self.assertTrue(all(r["period_confirmed"] for r in listed["statements"]))

        # 7. Resolve the held duplicate.
        self.cli("queue", "resolve-duplicate", "--entity", self.entity, "--source-id", held["source_id"],
                 "--decision", "duplicate")

        # 8. Review: categorize every staged row.
        staged = self.pending()
        self.assertEqual(len(staged), 24)  # 12 deposits + 11 statement charges + 1 feed charge
        groups = {"Income:Consulting": [], "Expenses:Software": []}
        for txn in staged:
            groups["Income:Consulting" if Decimal(str(txn["amount"])) > 0 else "Expenses:Software"].append(txn["id"])
        for category, ids in groups.items():
            argv = ["queue", "propose-group", "--entity", self.entity, "--category", category,
                    "--reasoning", "Recurring client payment or software subscription"]
            for source_id in ids:
                argv += ["--source-id", source_id]
            self.cli(*argv)
            self.cli("queue", "confirm-group", "--entity", self.entity, "--category", category)
        self.assertEqual(self.pending(), [])

        # 9. Tax map, then export.
        mapping = self.cli_json("tax", "map", "--entity", self.entity, "--year", "2026", "--format", "json")
        self.assertEqual(mapping["form"], "1120s")
        self.assertEqual(mapping["disclaimer"], DISCLAIMER)
        for account in mapping["unmapped"]:
            self.cli("tax", "set", "--entity", self.entity, "--account", account, "--line", "1120s/p1/19")
        out = self.root / "tax"
        printed = self.cli("tax", "export", "--entity", self.entity, "--year", "2026", "--output-dir", str(out),
                           "--formats", "csv", "--require-mapped")
        self.assertEqual(printed.splitlines()[0], DISCLAIMER)
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["unmapped"], manifest["totals"]["difference"]), ([], "0.00"))
        with (out / "tax-trial-balance-1120s-2026-01-01_2026-12-31.csv").open(encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], [DISCLAIMER])
        header, body = rows[1], {row[1]: dict(zip(rows[1], row)) for row in rows[2:]}
        self.assertEqual(header[:3], ["Account Number", "Account", "Account Type"])
        # The ledger holds every source row once: 12 deposits and 12 charges.
        self.assertEqual(body["Assets › Bank › Operating-Checking"]["Balance"], "69400.12")
        self.assertEqual(body["Income › Consulting"]["Balance"], "-60000.00")
        self.assertEqual(body["Expenses › Software"]["Balance"], "599.88")
        self.assertEqual(body["Income › Consulting"]["Tax Line Code"], "1120s/p1/1a")
        self.assertTrue(all(row["Tax Line Code"] != "UNMAPPED" for name, row in body.items() if name != "Total"))

    def test_statement_import_reports_an_imported_work_event(self):
        ofx = str(FIXTURES / "checking-2026-01-to-04.ofx")
        self.cli_json("statement", "map", ofx, "--entity", self.entity, "--profile", "checking-ofx",
                      "--ledger-account", CHECKING, "--account-name", "Operating Checking")
        first = self.root / "work-first.json"
        self.cli("statement", "import", ofx, "--entity", self.entity, "--work-out", str(first))
        events = json.loads(first.read_text(encoding="utf-8"))["work"]
        [imported] = [event for event in events if event["kind"] == "imported"]
        self.assertGreater(imported["counts"]["transactions"], 0)

        # The same file again adds nothing: no imported event, and coverage commands never emit one.
        again = self.root / "work-again.json"
        self.cli("statement", "import", ofx, "--entity", self.entity, "--work-out", str(again))
        repeated = json.loads(again.read_text(encoding="utf-8"))["work"]
        self.assertEqual([event for event in repeated if event["kind"] == "imported"], [])


if __name__ == "__main__":
    unittest.main()
