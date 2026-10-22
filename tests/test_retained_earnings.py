"""Earlier fiscal years close into retained earnings in every view, when a report is made.

The balance sheet shows "Retained earnings from prior fiscal activity" (income less expenses of earlier fiscal years)
and "Net Income (current period)" (the current fiscal year). The trial balance shows income and expense accounts for
the current fiscal year and one computed retained earnings row. No closing entry is written. The fiscal year comes
from an imported opening summary when there is one, else from ``fiscal_year_start``.

Synthetic company, calendar fiscal year:

    2025  income 12,000.00, expenses 3,500.00          net income 8,500.00
    2026  Jan 1 - Sep 30: income 6,000.00, expenses 2,200.00   net income 3,800.00
          Jul 1 - Sep 30: income 2,000.00, expenses   700.00   net income 1,300.00
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping import fiscal, migration_bundle, taxmap  # noqa: E402
from bookkeeping.compare import _compare_bs  # noqa: E402
from bookkeeping.entity import init_entity, load_entity  # noqa: E402
from bookkeeping.ledger.importer import _atomic_ledger_write  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.readiness import _report_refs  # noqa: E402
from bookkeeping.reports import comparative, statements, workbook  # noqa: E402
from bookkeeping.reports.cache import open_cache  # noqa: E402
from bookkeeping.reports.statements import CURRENT_NET_INCOME_LABEL, RETAINED_LABEL  # noqa: E402
from bookkeeping.reports.structure import balance_sheet_structure  # noqa: E402

BANK, CARD = "Assets:Bank:Checking", "Liabilities:CreditCard:Card"
OPENING, CONTRIB, DRAWS, DISTRIB = "Equity:Opening-Balances", "Equity:Owner-Contributions", "Equity:Owner-Draws", "Equity:Distributions"
SERVICES, RENT, SOFTWARE = "Income:Services", "Expenses:Rent", "Expenses:Software"

TWO_YEARS = [
    ("2025-01-01", "Opening balance", [(BANK, "1000.00"), (OPENING, "-1000.00")]),
    ("2025-02-10", "Owner contribution", [(BANK, "5000.00"), (CONTRIB, "-5000.00")]),
    ("2025-03-15", "Client payment", [(BANK, "12000.00"), (SERVICES, "-12000.00")]),
    ("2025-06-20", "Rent", [(RENT, "3000.00"), (BANK, "-3000.00")]),
    ("2025-08-05", "Owner draw", [(DRAWS, "2000.00"), (BANK, "-2000.00")]),
    ("2025-11-30", "Software", [(SOFTWARE, "500.00"), (BANK, "-500.00")]),
    ("2026-01-15", "Client payment", [(BANK, "4000.00"), (SERVICES, "-4000.00")]),
    ("2026-02-10", "Rent on card", [(RENT, "1500.00"), (CARD, "-1500.00")]),
    ("2026-03-01", "Distribution", [(DISTRIB, "1000.00"), (BANK, "-1000.00")]),
    ("2026-08-20", "Client payment", [(BANK, "2000.00"), (SERVICES, "-2000.00")]),
    ("2026-09-10", "Software", [(SOFTWARE, "700.00"), (BANK, "-700.00")]),
]

LOSS_THEN_PROFIT = [
    ("2025-01-01", "Opening balance", [(BANK, "5000.00"), (OPENING, "-5000.00")]),
    ("2025-04-01", "Client payment", [(BANK, "1000.00"), (SERVICES, "-1000.00")]),
    ("2025-05-01", "Rent", [(RENT, "4000.00"), (BANK, "-4000.00")]),
    ("2026-02-01", "Client payment", [(BANK, "500.00"), (SERVICES, "-500.00")]),
]


def _company(root: Path, entries, fiscal_year_start: str | None = None) -> Path:
    init_entity(root, name="Synthetic Retained Co")
    if fiscal_year_start:
        config = json.loads((root / "entity.json").read_text())
        config["fiscal_year_start"] = fiscal_year_start
        (root / "entity.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    entity = load_entity(root)
    accounts = sorted({account for _, _, postings in entries for account, _ in postings})
    opens = [Open(date(2024, 12, 1), account, ("USD",)) for account in accounts]
    rows = [Entry(date.fromisoformat(day), text, meta=(("source-id", f"synthetic-{index}"),),
                  postings=tuple(Posting(account, Decimal(amount)) for account, amount in postings))
            for index, (day, text, postings) in enumerate(entries)]
    _atomic_ledger_write(entity, opens, rows, "synthetic", None, "Synthetic retained earnings fixture")
    return root


def equity(bs) -> dict[str, Decimal]:
    return {row["label"]: row["amount"] for row in bs.sections[2]["rows"]}


def tb_rows(tb) -> dict[str, tuple[Decimal, Decimal]]:
    return {row["label"]: (row["debit"], row["credit"]) for row in tb.meta["raw_rows"]}


class Fixture(unittest.TestCase):
    entries = TWO_YEARS
    fiscal_year_start: str | None = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = _company(Path(self.tmp.name).resolve() / "company", self.entries, self.fiscal_year_start)

    def bs(self, day: str):
        return statements.balance_sheet(self.root, date.fromisoformat(day))

    def tb(self, day: str):
        return statements.trial_balance(self.root, date.fromisoformat(day))

    def assertBalanced(self, day: str):
        bs, tb = self.bs(day), self.tb(day)
        self.assertEqual(bs.totals["total_assets"], bs.totals["total_liabilities_and_equity"], day)
        self.assertEqual(tb.totals["total_debits"], tb.totals["total_credits"], day)
        self.assertTrue(balance_sheet_structure(bs)["balanced"], day)
        return bs, tb


class CashViewTests(Fixture):
    def test_first_year_has_no_retained_line_and_no_new_metadata(self):
        bs, tb = self.assertBalanced("2025-09-30")
        self.assertNotIn(RETAINED_LABEL, equity(bs))
        self.assertEqual(equity(bs)[CURRENT_NET_INCOME_LABEL], Decimal("9000.00"))
        for key in ("fiscal_start", "cumulative_net_income", "current_fiscal_net_income"):
            self.assertNotIn(key, bs.meta)
            self.assertNotIn(key, tb.meta)
        self.assertNotIn(RETAINED_LABEL, tb_rows(tb))
        self.assertEqual(tb_rows(tb)["Income › Services"], (Decimal("0.00"), Decimal("12000.00")))

    def test_second_year_closes_the_first_year_into_retained_earnings(self):
        bs, tb = self.assertBalanced("2026-09-30")
        lines = equity(bs)
        self.assertEqual(lines[RETAINED_LABEL], Decimal("8500.00"))
        self.assertEqual(lines[CURRENT_NET_INCOME_LABEL], Decimal("3800.00"))
        # Owner lines and opening balances stay account rows; they are not earnings.
        self.assertEqual(lines["Equity › Owner-Contributions"], Decimal("5000.00"))
        self.assertEqual(lines["Equity › Owner-Draws"], Decimal("-2000.00"))
        self.assertEqual(lines["Equity › Distributions"], Decimal("-1000.00"))
        self.assertEqual(lines["Equity › Opening-Balances"], Decimal("1000.00"))
        self.assertEqual(bs.sections[2]["total"], Decimal("15300.00"))
        self.assertEqual(bs.meta["fiscal_start"], "2026-01-01")
        self.assertEqual(bs.meta["cumulative_net_income"], "12300.00")
        self.assertEqual(bs.meta["current_fiscal_net_income"], "3800.00")
        rows = tb_rows(tb)
        self.assertEqual(rows["Income › Services"], (Decimal("0.00"), Decimal("6000.00")))
        self.assertEqual(rows["Expenses › Rent"], (Decimal("1500.00"), Decimal("0.00")))
        self.assertEqual(rows["Expenses › Software"], (Decimal("700.00"), Decimal("0.00")))
        self.assertEqual(rows[RETAINED_LABEL], (Decimal("0.00"), Decimal("8500.00")))
        computed = [row for row in tb.meta["raw_rows"] if row.get("computed")]
        self.assertEqual([(row["key"], row["label"]) for row in computed], [("retained_earnings_prior", RETAINED_LABEL)])
        self.assertEqual(tb.meta["fiscal_start"], "2026-01-01")
        # The P&L of the period is unchanged: it was always the period only.
        pnl = statements.profit_and_loss(self.root, date(2026, 1, 1), date(2026, 9, 30))
        self.assertEqual(pnl.totals["net_income"], Decimal("3800.00"))

    def test_on_the_fiscal_year_start_and_the_day_before(self):
        bs, tb = self.assertBalanced("2026-01-01")
        self.assertEqual(equity(bs)[RETAINED_LABEL], Decimal("8500.00"))
        self.assertEqual(equity(bs)[CURRENT_NET_INCOME_LABEL], Decimal("0.00"))
        self.assertNotIn("Income › Services", tb_rows(tb))
        bs, tb = self.assertBalanced("2025-12-31")
        self.assertNotIn(RETAINED_LABEL, equity(bs))
        self.assertEqual(equity(bs)[CURRENT_NET_INCOME_LABEL], Decimal("8500.00"))
        self.assertNotIn(RETAINED_LABEL, tb_rows(tb))

    def test_before_the_first_posting(self):
        bs, tb = self.assertBalanced("2024-12-31")
        self.assertEqual(equity(bs), {CURRENT_NET_INCOME_LABEL: Decimal("0.00")})
        self.assertEqual(tb.meta["raw_rows"], [])

    def test_structure_keys_stay_the_same(self):
        lines = {line["key"]: line for line in balance_sheet_structure(self.bs("2026-09-30"))["lines"]}
        self.assertEqual((lines["retained_earnings_prior"]["type"], lines["retained_earnings_prior"]["amount"]), ("computed", "8500.00"))
        self.assertEqual((lines["net_income_current_period"]["type"], lines["net_income_current_period"]["amount"]), ("computed", "3800.00"))

    def test_fiscal_year_change_after_entries_moves_only_the_split(self):
        before_bs, before_tb = self.bs("2026-09-30"), self.tb("2026-09-30")
        digest = LedgerStore(self.root / "ledger.sqlite").content_digest()
        fiscal.apply(load_entity(self.root), "07-01")
        self.assertEqual(LedgerStore(self.root / "ledger.sqlite").content_digest(), digest)
        bs, tb = self.assertBalanced("2026-09-30")
        self.assertEqual(equity(bs)[RETAINED_LABEL], Decimal("11000.00"))
        self.assertEqual(equity(bs)[CURRENT_NET_INCOME_LABEL], Decimal("1300.00"))
        self.assertEqual(bs.meta["fiscal_start"], "2026-07-01")
        self.assertEqual(bs.totals, before_bs.totals)
        self.assertEqual(tb_rows(tb)["Income › Services"], (Decimal("0.00"), Decimal("2000.00")))
        self.assertEqual(tb_rows(tb)[RETAINED_LABEL], (Decimal("0.00"), Decimal("11000.00")))
        self.assertNotIn("Expenses › Rent", tb_rows(tb))
        self.assertLess(tb.totals["total_debits"], before_tb.totals["total_debits"])

    def test_every_date_balances(self):
        for day in ("2025-01-01", "2025-06-30", "2025-12-31", "2026-01-01", "2026-03-31", "2026-06-30", "2026-07-01", "2026-12-31"):
            self.assertBalanced(day)


class NonCalendarFiscalYearTests(Fixture):
    fiscal_year_start = "07-01"

    def test_july_fiscal_year(self):
        bs, tb = self.assertBalanced("2026-09-30")
        self.assertEqual(equity(bs)[RETAINED_LABEL], Decimal("11000.00"))
        self.assertEqual(equity(bs)[CURRENT_NET_INCOME_LABEL], Decimal("1300.00"))
        self.assertEqual(bs.meta["fiscal_start"], "2026-07-01")
        self.assertEqual(tb.meta["fiscal_start"], "2026-07-01")
        # June 30, 2026 is the last day of the fiscal year that started on July 1, 2025.
        bs, _ = self.assertBalanced("2026-06-30")
        self.assertEqual(equity(bs)[RETAINED_LABEL], Decimal("9000.00"))   # Jan - Jun 2025
        self.assertEqual(equity(bs)[CURRENT_NET_INCOME_LABEL], Decimal("2000.00"))  # Jul 2025 - Jun 2026


class LossYearTests(Fixture):
    entries = LOSS_THEN_PROFIT

    def test_a_loss_year_gives_negative_retained_earnings(self):
        bs, tb = self.assertBalanced("2026-03-31")
        self.assertEqual(equity(bs)[RETAINED_LABEL], Decimal("-3000.00"))
        self.assertEqual(equity(bs)[CURRENT_NET_INCOME_LABEL], Decimal("500.00"))
        self.assertEqual(tb_rows(tb)[RETAINED_LABEL], (Decimal("3000.00"), Decimal("0.00")))


class ConsumerTests(Fixture):
    def test_tax_handoff_rows_are_unchanged_and_agree(self):
        rows = {row["account"]: row for row in taxmap.period_balances(self.root, date(2026, 1, 1), date(2026, 12, 31))}
        self.assertEqual(rows[taxmap.PRIOR_EARNINGS_ACCOUNT]["balance"], Decimal("-8500.00"))
        self.assertEqual(rows[SERVICES]["balance"], Decimal("-6000.00"))
        self.assertEqual(-rows[taxmap.PRIOR_EARNINGS_ACCOUNT]["balance"], equity(self.bs("2026-12-31"))[RETAINED_LABEL])

    def test_readiness_report_refs_balance(self):
        refs = {ref["kind"]: ref["totals"] for ref in _report_refs(self.root, date(2026, 9, 1), date(2026, 9, 30), provisional=True)}
        self.assertEqual(refs["trial_balance"]["total_debits"], refs["trial_balance"]["total_credits"])
        self.assertEqual(refs["balance_sheet"]["total_assets"], refs["balance_sheet"]["total_liabilities_and_equity"])

    def test_ask_balance_sheet_shows_both_lines(self):
        from bookkeeping.reports.statements import ask
        answer = ask(self.root, "Show me the balance sheet as of 2026-09-30", today=date(2026, 10, 21))
        self.assertIn(RETAINED_LABEL, answer.answer_text)
        self.assertIn("8,500.00", answer.answer_text)
        self.assertIn("3,800.00", answer.answer_text)

    def test_comparative_balance_sheet_prior_year_without_retained_line(self):
        result = comparative.compare_balance_sheet(self.root, date(2026, 9, 30), "prior_year")
        merged = {line["key"]: line for line in result.lines}
        self.assertEqual(merged["retained_earnings_prior"]["amount"], "8500.00")
        self.assertEqual(merged["retained_earnings_prior"]["prior_amount"], "0.00")
        self.assertEqual(merged["net_income_current_period"]["prior_amount"], "9000.00")

    def test_workbook_trial_balance_and_tie_outs(self):
        package = workbook.generate_accountant_package(self.root, date(2026, 7, 1), date(2026, 9, 30), override=True)
        self.assertTrue(package.success, package.error)
        sheets = {path.stem: list(csv.reader(io.StringIO(path.read_text()))) for path in package.csv_files}
        self.assertTrue(any(RETAINED_LABEL in row for row in sheets["Trial-Balance"]))
        checks = {row[0]: row[2] for row in sheets["Checks"][1:]}
        self.assertEqual(checks["Trial balance debits equal credits"], "PASS")
        self.assertEqual(checks["Balance sheet balances"], "PASS")
        self.assertEqual(checks["P&L net income agrees to balance sheet net-income movement"], "PASS")
        summary = {row[0]: row[1] for row in sheets["Summary"] if row}
        self.assertEqual(Decimal(summary["Opening cumulative net income"]), Decimal("11000.00"))
        self.assertEqual(Decimal(summary["Closing cumulative net income"]), Decimal("12300.00"))

    def test_compare_period_matches_quickbooks_retained_earnings_and_net_income(self):
        folder = Path(self.tmp.name) / "qb"
        folder.mkdir()
        (folder / "balance_sheet.csv").write_text(
            '"Synthetic Retained Co",\nBalance Sheet,\n"As of Sep 30, 2026",\n\n,Total\nAssets,\nBank Accounts,\n'
            'Checking,"17800.00"\nTotal for Assets,"$17800.00"\nLiabilities and Equity,\nLiabilities,\n'
            'Card,"1500.00"\nTotal for Liabilities,"$1500.00"\nEquity,\nOpening Balance Equity,"1000.00"\n'
            'Owner Contributions,"5000.00"\nOwner Draws,"-2000.00"\nDistributions,"-1000.00"\n'
            'Retained Earnings,"8500.00"\nNet Income,"3800.00"\nTotal for Equity,"$15300.00"\n'
            'Total for Liabilities and Equity,"$17800.00"\n\n\n"Cash Basis Monday, October 19, 2026 10:00 AM GMT-05:00",\n')
        matched: list = []
        material, immaterial = _compare_bs(load_entity(self.root), folder, date(2026, 9, 30), {"Retained Earnings": "Equity"},
                                           Decimal("1.00"), Decimal("0.01"), matched=matched)
        diffs = {d.account: d for d in material + immaterial}
        self.assertNotIn("Net Income", diffs)
        self.assertNotIn(RETAINED_LABEL, diffs)
        self.assertNotIn(CURRENT_NET_INCOME_LABEL, diffs)
        rows = {row["name"]: row for row in matched}
        self.assertEqual(Decimal(rows["Net Income"]["ours"]), Decimal("3800.00"))
        retained = [row for account, row in rows.items() if account.startswith("Equity:") and Decimal(row["ours"]) == Decimal("8500.00")]
        self.assertEqual(len(retained), 1, rows)


class MigrationBundleTests(Fixture):
    def test_fingerprints_keep_the_earlier_layout_so_old_bundles_stay_valid(self):
        destination = Path(self.tmp.name).resolve() / "bundle.zip"
        result = migration_bundle.build_bundle(self.root, destination)
        self.assertEqual(result["status"], "exported", result)
        loaded = migration_bundle.load_bundle(destination)
        manifest, files = loaded["manifest"], loaded["files"]
        self.assertEqual(migration_bundle.validate_files(manifest, files)["status"], "validated")
        last = date.fromisoformat(manifest["reports"]["to"])
        conn = open_cache(self.root)
        try:
            legacy = statements._compute_balance_sheet(conn, last, close_all_views=False)
            closed = statements._compute_balance_sheet(conn, last)
            legacy_tb = statements._compute_trial_balance(conn, last, close_all_views=False)
        finally:
            conn.close()
        # The earlier layout: one cumulative net income line, life-to-date income and expense accounts.
        self.assertNotIn(RETAINED_LABEL, equity(legacy))
        self.assertEqual(equity(legacy)[CURRENT_NET_INCOME_LABEL], Decimal("12300.00"))
        self.assertNotIn(RETAINED_LABEL, tb_rows(legacy_tb))
        fingerprint = lambda report: migration_bundle._sha(migration_bundle._json_bytes(json.loads(report.to_json())))
        self.assertEqual(manifest["reports"]["fingerprints"]["balance_sheet"], fingerprint(legacy))
        self.assertEqual(manifest["reports"]["fingerprints"]["trial_balance"], fingerprint(legacy_tb))
        self.assertNotEqual(fingerprint(legacy), fingerprint(closed))
        changed = json.loads(json.dumps(manifest))
        changed["reports"]["fingerprints"]["balance_sheet"] = fingerprint(closed)
        with self.assertRaises(migration_bundle.BundleError) as caught:
            migration_bundle.validate_files(changed, files)
        self.assertEqual(str(caught.exception), "bundle_anchors_mismatch")


class ImportedOpeningAnchorTests(unittest.TestCase):
    """A QuickBooks-adopted accrual company keeps the calendar of its imported opening summary (fiscal start Jan 1),
    even when ``fiscal_year_start`` in entity.json says otherwise. The balance sheet and the trial balance agree."""

    def setUp(self):
        from tests import test_accrual_reports as accrual
        self.fixture = accrual.AccrualReportsTests("test_new_fiscal_year_moves_prior_profit_to_retained_without_double_counting")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tmp.cleanup)
        self.root = self.fixture.root
        self.fixture.opening()
        self.fixture.july()
        self.fixture.journal("new-year-service", "2027-01-10", [("Assets:Bank:Checking", "100.00"), ("Income:Services", "-100.00")])
        config = json.loads((self.root / "entity.json").read_text())
        config["fiscal_year_start"] = "07-01"
        (self.root / "entity.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")

    def test_opening_summary_calendar_wins_in_both_statements(self):
        bs = statements.balance_sheet(self.root, date(2027, 1, 31))
        tb = statements.trial_balance(self.root, date(2027, 1, 31))
        self.assertEqual(bs.meta["basis"], "accrual")
        self.assertEqual(bs.meta["fiscal_start"], "2027-01-01")
        self.assertEqual(tb.meta["fiscal_start"], "2027-01-01")
        self.assertEqual(equity(bs)[RETAINED_LABEL], Decimal("2560.00"))
        self.assertEqual(equity(bs)[CURRENT_NET_INCOME_LABEL], Decimal("100.00"))
        self.assertEqual(tb_rows(tb)[RETAINED_LABEL], (Decimal("0.00"), Decimal("2560.00")))
        self.assertEqual(tb_rows(tb)["Income › Services"], (Decimal("0.00"), Decimal("100.00")))
        self.assertEqual(bs.totals["total_assets"], bs.totals["total_liabilities_and_equity"])
        self.assertEqual(tb.totals["total_debits"], tb.totals["total_credits"])

    def test_inside_the_first_fiscal_year_there_is_no_retained_line(self):
        bs = statements.balance_sheet(self.root, date(2026, 7, 31))
        tb = statements.trial_balance(self.root, date(2026, 7, 31))
        self.assertEqual(bs.meta["fiscal_start"], "2026-01-01")
        self.assertNotIn(RETAINED_LABEL, equity(bs))
        self.assertNotIn(RETAINED_LABEL, tb_rows(tb))
        self.assertEqual(tb.totals["total_debits"], tb.totals["total_credits"])

    def test_compare_period_accrual_trial_balance_adds_the_computed_row_to_retained_earnings(self):
        import shutil
        from bookkeeping.compare import _compare_tb
        folder = Path(self.fixture.tmp.name).resolve() / "qb-2027"
        folder.mkdir()
        shutil.copy(self.fixture.exports / "accounts.csv", folder / "accounts.csv")
        # QuickBooks style as of Jan 31, 2027: Retained Earnings holds the imported 9,000.00 plus 2026 net income
        # 2,560.00; income and expense accounts show 2027 only.
        (folder / "trial_balance.csv").write_text(
            'Synthetic Services\nTrial Balance\n"As of January 31, 2027"\nAccount Name,Debit,Credit\n'
            'Checking,9700,\nAccounts Receivable,600,\nPrepaid Insurance,500,\nEquipment,2400,\n'
            'Accumulated Depreciation,,440\nAccounts Payable,,300\nDeferred Revenue,,500\nPayroll Payable,,300\n'
            'Retained Earnings,,11560\nServices,,100\nTOTAL,13200,13200\nAccrual Basis\n')
        matched: list = []
        material, immaterial = _compare_tb(load_entity(self.root), folder, date(2027, 1, 31), Decimal("1.00"),
                                           Decimal("0.01"), matched=matched, totals={})
        self.assertEqual(material + immaterial, [])
        rows = {row["name"]: row for row in matched}
        self.assertEqual(Decimal(rows["Equity:Retained-Earnings"]["ours"]), Decimal("-11560.00"))
        self.assertEqual(Decimal(rows["Equity:Retained-Earnings"]["reference"]), Decimal("-11560.00"))
        self.assertNotIn(RETAINED_LABEL, rows)
        self.assertEqual(Decimal(rows["Income:Services"]["ours"]), Decimal("-100.00"))


class RecordedViewTests(unittest.TestCase):
    """A cash-default company that keeps accrual records ("as recorded (cash view default)") closes earlier fiscal
    years the same way. Fiscal year moved to 04-01, so January to March 2026 is an earlier fiscal year at April 30."""

    def setUp(self):
        from test_view_outputs import TrackedCompany
        self.fixture = TrackedCompany("make")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.make("cash")
        self.root = self.fixture.root
        fiscal.apply(load_entity(self.root), "04-01")

    def test_recorded_balances_split_at_the_fiscal_year_start(self):
        earlier = statements.profit_and_loss(self.root, date(2026, 1, 1), date(2026, 3, 31)).totals["net_income"]
        april = statements.profit_and_loss(self.root, date(2026, 4, 1), date(2026, 4, 30)).totals["net_income"]
        self.assertEqual(earlier, Decimal("14543.94"))  # the cash golden company's January to March net income
        self.assertEqual(april, Decimal("-400.00"))      # the unpaid April bill, as recorded
        bs = statements.balance_sheet(self.root, date(2026, 4, 30))
        tb = statements.trial_balance(self.root, date(2026, 4, 30))
        for result in (bs, tb):
            self.assertEqual((result.meta["basis"], result.meta["basis_label"]), ("cash", "as recorded (cash view default)"))
            self.assertEqual(result.meta["fiscal_start"], "2026-04-01")
        self.assertEqual(equity(bs)[RETAINED_LABEL], earlier)
        self.assertEqual(equity(bs)[CURRENT_NET_INCOME_LABEL], april)
        self.assertEqual(bs.meta["cumulative_net_income"], str(earlier + april))
        self.assertEqual(bs.totals["total_assets"], bs.totals["total_liabilities_and_equity"])
        self.assertEqual(tb_rows(tb)[RETAINED_LABEL], (Decimal("0.00"), earlier))
        self.assertEqual(tb_rows(tb)["Expenses › Insurance"], (Decimal("400.00"), Decimal("0.00")))
        self.assertEqual(tb.totals["total_debits"], tb.totals["total_credits"])
        self.assertIn("Accounting basis: as recorded (cash view default)", bs.to_text())


if __name__ == "__main__":
    unittest.main()
