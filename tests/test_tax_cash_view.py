"""Tax outputs follow the company's tax method when the tax-method view is on.

Every expected figure is hand-computed in the comment beside it. The flag-off tests
prove that a company without the view (or with ``tax_method`` set but the view off)
produces the same tax outputs as before, byte for byte. The golden harness
(tests/test_golden_outputs.py) proves the same for the three golden companies.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import golden_companies  # noqa: E402
from golden_support import run_cli  # noqa: E402
from test_accrual_cash_view import (AP, AR, BANK, BAD_DEBT, DEFERRED, INSURANCE, PREPAID, SERVICES,  # noqa: E402
                                    SUBSCRIPTIONS, UTILITIES, CashViewFixture)

from bookkeeping import taxmap, taxview  # noqa: E402
from bookkeeping.accrual import schedules  # noqa: E402
from bookkeeping.accrual.journals import JournalError  # noqa: E402
from bookkeeping.accrual.reports import CoverageError  # noqa: E402
from bookkeeping.automation_policy import PreparationDenied  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.ledger.importer import check_integrity  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.preparation_runtime import supported_preparation_command  # noqa: E402
from bookkeeping.readiness import assess_review_readiness, local_dependency_versions  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

D = Decimal
Y2026 = (date(2026, 1, 1), date(2026, 12, 31))
Y2027 = (date(2027, 1, 1), date(2027, 12, 31))


def books(*argv: str):
    code, out, err = run_cli(list(argv))
    return code, (json.loads(out) if out.strip().startswith(("{", "[")) else out), err


def set_method(root: Path, method: str, view: str | None = "on"):
    args = ["entity", "set-tax-method", str(root), "--method", method, "--apply"]
    if view:
        args += ["--tax-view", view]
    code, result, err = books(*args)
    assert code == 0, (result, err)
    return result


def tb(handoff) -> dict[str, Decimal]:
    return {row["account"]: row["balance"] for row in handoff["trial_balance"]}


def flag_codes(handoff) -> list[str]:
    return [f["code"] for f in handoff["tax_view"]["flags"]]


class CashFilerWithAccrualRecords(CashViewFixture):
    """A native accrual company (accrual records from 2026-01-01) that files on the cash method."""

    def handoff(self, period=Y2026):
        return taxmap.build_handoff(self.root, *period, form="schc")

    def cash_filer(self):
        set_method(self.root, "cash")
        self.entity = load_entity(self.root)

    def test_unpaid_bill_is_not_in_the_tax_figures(self):
        # Invoice 1,000.00 collected; bill 500.00 for utilities never paid.
        self.item("inv", "receivable", AR, "1000.00", "2026-03-01", "Cust", counter=SERVICES)
        self.settle("pay", "2026-03-20", "1000.00", [("inv", "1000.00")])
        self.item("bill", "payable", AP, "500.00", "2026-04-01", "Power Co", counter=UTILITIES)
        before = self.handoff()
        self.assertEqual(tb(before)[UTILITIES], D("500.00"))
        self.assertEqual(tb(before)[AP], D("-500.00"))
        self.cash_filer()
        after = self.handoff()
        rows = tb(after)
        # Cash method: services 1,000.00 received; the unpaid bill is neither expense nor payable.
        self.assertEqual(rows[SERVICES], D("-1000.00"))
        self.assertNotIn(UTILITIES, rows)
        self.assertNotIn(AP, rows)
        self.assertNotIn(AR, rows)
        self.assertEqual(after["totals"]["difference"], D("0.00"))
        self.assertEqual((after["tax_method"], after["view"], after["accounting_basis"]), ("cash", "cash", "cash"))
        self.assertIn("receivables_payables_excluded", flag_codes(after))
        difference = next(f for f in after["tax_view"]["flags"] if f["code"] == "difference_from_accrual_view")
        # Accrual net income 1000 - 500 = 500; cash 1000; cash minus accrual = 500.
        self.assertEqual((difference["accrual_net_income"], difference["cash_net_income"], difference["amount"]), ("500.00", "1000.00", "500.00"))

    def test_bill_paid_next_year_and_the_year_end_boundary(self):
        # Bill 300.00 on 2026-12-15, paid 2027-01-01. Bill 200.00 on 2026-12-01, paid 2026-12-31.
        self.item("late", "payable", AP, "300.00", "2026-12-15", "Late Co", counter=UTILITIES)
        self.item("edge", "payable", AP, "200.00", "2026-12-01", "Edge Co", counter=UTILITIES)
        self.settle("pay-edge", "2026-12-31", "-200.00", [("edge", "200.00")])
        self.settle("pay-late", "2027-01-01", "-300.00", [("late", "300.00")])
        self.cash_filer()
        # 2026: only the bill paid on 2026-12-31 (200.00). 2027: the bill paid on 2027-01-01 (300.00).
        self.assertEqual(tb(self.handoff(Y2026))[UTILITIES], D("200.00"))
        y2027 = tb(self.handoff(Y2027))
        self.assertEqual(y2027[UTILITIES], D("300.00"))
        # Prior-year cash net income: -200.00 of expense, debit-positive +200.00.
        self.assertEqual(y2027[taxmap.PRIOR_EARNINGS_ACCOUNT], D("200.00"))

    def test_prior_year_invoice_collected_this_year_is_income_on_receipt(self):
        self.item("inv", "receivable", AR, "800.00", "2026-11-01", "Cust", counter=SERVICES)
        self.settle("pay", "2027-02-01", "800.00", [("inv", "800.00")])
        self.cash_filer()
        self.assertNotIn(SERVICES, tb(self.handoff(Y2026)))
        y2027 = self.handoff(Y2027)
        self.assertEqual(tb(y2027)[SERVICES], D("-800.00"))
        self.assertNotIn(taxmap.PRIOR_EARNINGS_ACCOUNT, tb(y2027))
        self.assertEqual(y2027["totals"]["difference"], D("0.00"))

    def test_unset_prepaid_is_capitalized_and_flagged(self):
        # 2,400.00 paid 2026-01-01 for 24 months (equal_month, 100.00 a month). 2026 recognition: 12 x 100 = 1,200.00.
        # No tax treatment is recorded, so the tax figures follow the books.
        self.schedule("ins24", "prepaid", PREPAID, INSURANCE, "2400.00", "2026-01-01", "2027-12-31", "equal_month", BANK)
        self.due("2026-12-31")
        self.cash_filer()
        handoff = self.handoff()
        rows = tb(handoff)
        self.assertEqual(rows[INSURANCE], D("1200.00"))
        # The unused 1,200.00 stays an asset.
        self.assertEqual(rows[PREPAID], D("1200.00"))
        capitalized = next(f for f in handoff["tax_view"]["flags"] if f["code"] == "prepaid_capitalized")
        self.assertEqual((capitalized["refs"], capitalized["treatment_source"]), (["ins24"], "not_recorded"))
        not_recorded = next(f for f in handoff["tax_view"]["flags"] if f["code"] == "prepaid_tax_treatment_not_recorded")
        self.assertEqual(not_recorded["refs"], ["ins24"])
        self.assertNotIn("prepaid_expensed_by_default", flag_codes(handoff))
        for message in (f["message"] for f in handoff["tax_view"]["flags"]):
            self.assertNotIn("12-month", message)
            self.assertNotIn("12 months", message)
        self.assertEqual(handoff["totals"]["difference"], D("0.00"))

    def test_unset_short_prepaid_is_not_expensed_until_a_person_records_it(self):
        # 1,200.00 paid 2026-07-01 for 2026-07-01 to 2027-06-30 (100.00 a month). Unset: 6 x 100 = 600.00 used, 600.00 asset.
        self.schedule("ins12", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-07-01", "2027-06-30", "equal_month", BANK)
        self.due("2026-12-31")
        self.cash_filer()
        rows = tb(self.handoff())
        self.assertEqual((rows[INSURANCE], rows[PREPAID]), (D("600.00"), D("600.00")))
        self.assertIn("prepaid_tax_treatment_not_recorded", flag_codes(self.handoff()))
        # A person records expense_when_paid: 2026 deducts the full 1,200.00 and no asset stays.
        books("accrual", "schedule", "tax-treatment", "--entity", str(self.root), "--id", "ins12", "--treatment", "expense_when_paid", "--apply")
        handoff = self.handoff()
        rows = tb(handoff)
        self.assertEqual(rows[INSURANCE], D("1200.00"))
        self.assertNotIn(PREPAID, rows)
        self.assertFalse({"prepaid_capitalized", "prepaid_tax_treatment_not_recorded"} & set(flag_codes(handoff)))
        self.assertEqual(handoff["totals"]["difference"], D("0.00"))

    def test_recorded_capitalize_is_capitalized(self):
        self.schedule("ins12", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-07-01", "2027-06-30", "equal_month", BANK)
        self.due("2026-12-31")
        self.cash_filer()
        # A person records capitalize: 2026 deducts only the 6 months used, 6 x 100 = 600.00.
        code, plan, _ = books("accrual", "schedule", "tax-treatment", "--entity", str(self.root), "--id", "ins12", "--treatment", "capitalize")
        self.assertEqual((code, plan["changes"]), (0, True))
        self.assertNotIn("tax_treatment", schedules.list_schedules(self.entity)[0])
        code, done, _ = books("accrual", "schedule", "tax-treatment", "--entity", str(self.root), "--id", "ins12", "--treatment", "capitalize",
                              "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"]), (0, "applied"))
        code, again, _ = books("accrual", "schedule", "tax-treatment", "--entity", str(self.root), "--id", "ins12", "--treatment", "capitalize", "--apply")
        self.assertEqual(again["status"], "already_applied")
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        handoff = self.handoff()
        rows = tb(handoff)
        self.assertEqual((rows[INSURANCE], rows[PREPAID]), (D("600.00"), D("600.00")))
        capitalized = next(f for f in handoff["tax_view"]["flags"] if f["code"] == "prepaid_capitalized")
        self.assertEqual(capitalized["treatment_source"], "recorded")
        self.assertNotIn("prepaid_tax_treatment_not_recorded", flag_codes(handoff))
        listing = schedules.list_schedules(self.entity)
        self.assertEqual(listing[0]["tax_treatment"], "capitalize")
        code, text, _ = books("accrual", "report", "--entity", str(self.root), "--kind", "schedules", "--as-of", "2026-12-31", "--format", "text")
        self.assertIn("Tax treatment", text)
        events = [e["type"] if isinstance(e, dict) else e.type for e in LedgerStore(self.root / "ledger.sqlite").load_audit_events()]
        self.assertIn("schedule-tax-treatment-set", events)

    def test_export_ties_to_the_accrual_books_with_recorded_and_unset_prepaids(self):
        # Unset: 2,400.00 for 24 months, 100.00 a month; 2026 uses 1,200.00 in both views.
        # Recorded expense_when_paid: 1,200.00 for 2026-07-01 to 2027-06-30; accrual uses 600.00, cash deducts 1,200.00.
        self.schedule("long", "prepaid", PREPAID, INSURANCE, "2400.00", "2026-01-01", "2027-12-31", "equal_month", BANK)
        self.schedule("short", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-07-01", "2027-06-30", "equal_month", BANK,
                      tax_treatment="expense_when_paid")
        self.due("2026-12-31")
        self.cash_filer()
        handoff = self.handoff()
        # Accrual net income -1,800.00; cash -2,400.00; cash minus accrual -600.00, to the cent.
        difference = next(f for f in handoff["tax_view"]["flags"] if f["code"] == "difference_from_accrual_view")
        self.assertEqual((difference["accrual_net_income"], difference["cash_net_income"], difference["amount"]),
                         ("-1800.00", "-2400.00", "-600.00"))
        self.assertEqual(tb(handoff)[PREPAID], D("1200.00"))
        self.assertEqual(handoff["totals"]["difference"], D("0.00"))
        self.assertEqual(sum(tb(handoff).values(), D("0")), D("0.00"))
        out = Path(self.tmp.name) / "tie-out"
        code, _, err = books("tax", "export", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--output-dir", str(out), "--formats", "csv")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads((out / "manifest.json").read_text())["view"], "cash")

    def test_deferred_revenue_is_income_on_receipt(self):
        # 1,200.00 received 2026-10-01 for 2026-10-01 to 2027-09-30, 100.00 a month. Accrual 2026: 300.00.
        self.schedule("sub", "deferred", DEFERRED, SUBSCRIPTIONS, "1200.00", "2026-10-01", "2027-09-30", "equal_month", BANK)
        self.due("2026-12-31")
        self.cash_filer()
        handoff = self.handoff()
        self.assertEqual(tb(handoff)[SUBSCRIPTIONS], D("-1200.00"))
        self.assertNotIn(DEFERRED, tb(handoff))
        deferred = next(f for f in handoff["tax_view"]["flags"] if f["code"] == "deferred_revenue_on_receipt")
        # 1,200.00 cash minus 300.00 accrual = 900.00 more income.
        self.assertEqual(deferred["amount"], "900.00")

    def test_write_off_is_flagged_as_not_deductible(self):
        self.item("ridge", "receivable", AR, "300.00", "2026-01-10", "Ridge", counter=SERVICES)
        self.adjust("wo", "ridge", "writeoff", "300.00", "2026-03-15", BAD_DEBT)
        self.cash_filer()
        handoff = self.handoff()
        self.assertNotIn(BAD_DEBT, tb(handoff))
        self.assertNotIn(SERVICES, tb(handoff))
        self.assertIn("bad_debt_not_deductible", flag_codes(handoff))

    def test_fixed_assets_are_flagged_depreciation_not_tax_adjusted(self):
        self.schedule("van", "depreciation", "Assets:Fixed:Equipment", "Expenses:Depreciation", "3600.00", "2026-01-01", "2028-12-31",
                      "equal_month", BANK, contra_account="Assets:Fixed:Accumulated-Depreciation")
        self.due("2026-12-31")
        self.cash_filer()
        handoff = self.handoff()
        rows = tb(handoff)
        # Book depreciation as recorded: 3,600 / 36 x 12 = 1,200.00. The equipment stays on the balance sheet.
        self.assertEqual(rows["Expenses:Depreciation"], D("1200.00"))
        self.assertEqual(rows["Assets:Fixed:Equipment"], D("3600.00"))
        self.assertIn("depreciation_not_tax_adjusted", flag_codes(handoff))

    def test_export_stamps_view_and_writes_an_accurate_basis_line(self):
        self.item("bill", "payable", AP, "500.00", "2026-04-01", "Power Co", counter=UTILITIES)
        self.cash_filer()
        out = Path(self.tmp.name) / "tax-out"
        code, _, err = books("tax", "export", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--output-dir", str(out), "--formats", "csv")
        self.assertEqual(code, 0, err)
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual((manifest["tax_method"], manifest["view"], manifest["accounting_basis"]), ("cash", "cash", "cash"))
        readme = (out / "README.txt").read_text()
        self.assertIn("Basis: Cash method (tax): cash view derived from the accrual records", readme)
        self.assertNotIn("Book balances as recorded, not a tax cash-basis view", readme)
        self.assertIn("Tax method: Cash method", readme)
        code, mapping, _ = books("tax", "map", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--format", "json")
        self.assertEqual((mapping["tax_method"], mapping["view"]), ("cash", "cash"))
        self.assertNotIn(UTILITIES, [a["account"] for a in mapping["accounts"] if a.get("balance") not in (None, "0.00")])

    def test_range_before_the_cutover_fails_closed(self):
        self.cash_filer()
        out = Path(self.tmp.name) / "refused"
        code, _, err = books("tax", "export", "--entity", str(self.root), "--from", "2025-07-01", "--to", "2026-06-30", "--form", "schc",
                             "--output-dir", str(out))
        self.assertEqual(code, 1)
        self.assertIn("not available before the accrual cutover 2026-01-01", err)
        self.assertFalse(out.exists())
        with self.assertRaises(CoverageError):
            taxmap.build_handoff(self.root, date(2025, 7, 1), date(2026, 6, 30), form="schc")

    def test_accrual_method_reads_the_recorded_accrual_books(self):
        self.item("bill", "payable", AP, "500.00", "2026-04-01", "Power Co", counter=UTILITIES)
        before = self.handoff()
        set_method(self.root, "accrual")
        after = self.handoff()
        self.assertEqual(tb(after), tb(before))
        self.assertEqual((after["tax_method"], after["view"]), ("accrual", "accrual"))
        self.assertTrue(after["basis_label"].startswith("Accrual method (tax)"))


class FlagOffIsUnchanged(CashViewFixture):
    def test_tax_method_without_the_view_changes_no_tax_output(self):
        self.item("bill", "payable", AP, "500.00", "2026-04-01", "Power Co", counter=UTILITIES)
        self.schedule("ins24", "prepaid", PREPAID, INSURANCE, "2400.00", "2026-01-01", "2027-12-31", "equal_month", BANK)
        argv = ["tax", "map", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--format", "json"]
        before = run_cli(argv)
        out_before = Path(self.tmp.name) / "before"
        books("tax", "export", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--output-dir", str(out_before), "--formats", "csv")
        set_method(self.root, "cash", view=None)
        config = json.loads((self.root / "entity.json").read_text())
        self.assertEqual((config["tax_method"], config["tax_method_view"]), ("cash", False))
        self.assertEqual(run_cli(argv), before)
        out_after = Path(self.tmp.name) / "after"
        books("tax", "export", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--output-dir", str(out_after), "--formats", "csv")
        for path in sorted(out_before.iterdir()):
            text = path.read_text().replace(str(out_before), "OUT")
            self.assertEqual(text, (out_after / path.name).read_text().replace(str(out_after), "OUT"), path.name)


class CashCompany(unittest.TestCase):
    """The golden cash company: cash books, then accrual turned on later with a tracking start."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()
        self.root = self.work / "co"
        golden_companies.build_cash(self.root)

    def test_cash_method_on_cash_books_reads_the_recorded_books(self):
        period = (date(2026, 1, 1), date(2026, 3, 31))
        before = taxmap.build_handoff(self.root, *period, form="schc")
        set_method(self.root, "cash")
        after = taxmap.build_handoff(self.root, *period, form="schc")
        self.assertEqual(tb(after), tb(before))
        self.assertEqual((after["view"], after["tax_view"]["source"]), ("cash", "recorded"))

    def test_accrual_method_on_cash_books_fails_closed(self):
        set_method(self.root, "accrual")
        with self.assertRaises(CoverageError) as caught:
            taxmap.build_handoff(self.root, date(2026, 1, 1), date(2026, 3, 31), form="schc")
        self.assertIn("accrual view is not available", str(caught.exception))

    def test_accrual_method_before_the_tracking_start_fails_closed_without_fallback(self):
        code, plan, err = books("accrual", "enable", "preview", "--entity", str(self.root), "--tracking-start", "2026-04-01")
        self.assertEqual(code, 0, err)
        path = self.work / "plan.json"
        path.write_text(json.dumps(plan))
        self.assertEqual(books("accrual", "enable", "apply", "--entity", str(self.root), "--input", str(path))[0], 0)
        set_method(self.root, "accrual")
        with self.assertRaises(CoverageError) as caught:
            taxmap.build_handoff(self.root, date(2026, 1, 1), date(2026, 6, 30), form="schc")
        self.assertIn("2026-04-01", str(caught.exception))
        # The cash method on the same company works for the same range: before the tracking start the books are cash books.
        set_method(self.root, "cash")
        handoff = taxmap.build_handoff(self.root, date(2026, 1, 1), date(2026, 6, 30), form="schc")
        self.assertEqual((handoff["view"], handoff["tax_view"]["source"]), ("cash", "derived"))
        self.assertEqual(handoff["totals"]["difference"], D("0.00"))

    def test_setter_preview_apply_idempotent_and_audit(self):
        config_before = (self.root / "entity.json").read_text()
        code, plan, _ = books("entity", "set-tax-method", str(self.root), "--method", "cash", "--tax-view", "on")
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "entity.json").read_text(), config_before)
        self.assertIn("tax_method_view", plan["effects"]["flag"])
        self.assertEqual(plan["effects"]["published_statements_changed"], False)
        code, stale, _ = books("entity", "set-tax-method", str(self.root), "--method", "cash", "--tax-view", "on", "--apply", "--plan-hash", "0" * 64)
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))
        code, done, _ = books("entity", "set-tax-method", str(self.root), "--method", "cash", "--tax-view", "on", "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"]), (0, "applied"))
        config = json.loads((self.root / "entity.json").read_text())
        self.assertEqual((config["tax_method"], config["tax_method_view"]), ("cash", True))
        code, same, _ = books("entity", "set-tax-method", str(self.root), "--method", "cash", "--apply")
        self.assertEqual(same["status"], "already_applied")
        events = [e["type"] if isinstance(e, dict) else e.type for e in LedgerStore(self.root / "ledger.sqlite").load_audit_events()]
        self.assertEqual(events.count("tax-method-set"), 1)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        self.assertEqual(books("entity", "set-tax-method", str(self.root), "--method", "monthly")[0], 2)

    def test_invalid_tax_method_in_entity_json_is_refused(self):
        path = self.root / "entity.json"
        config = json.loads(path.read_text())
        path.write_text(json.dumps({**config, "tax_method": "hybrid", "tax_method_view": True}))
        with self.assertRaises(ValueError):
            taxmap.build_handoff(self.root, date(2026, 1, 1), date(2026, 3, 31), form="schc")

    def test_remote_contract_and_preparation_classification(self):
        preview = command_spec(["entity", "set-tax-method", "E", "--method", "cash"])
        apply = command_spec(["entity", "set-tax-method", "E", "--method", "cash", "--apply"])
        self.assertEqual((preview["mutates"], apply["mutates"]), (False, True))
        preview = command_spec(["accrual", "schedule", "tax-treatment", "--entity", "E", "--id", "s", "--treatment", "capitalize"])
        apply = command_spec(["accrual", "schedule", "tax-treatment", "--entity", "E", "--id", "s", "--treatment", "capitalize", "--apply"])
        self.assertEqual((preview["mutates"], apply["mutates"]), (False, True))
        for argv in (["entity", "set-tax-method", "E", "--method", "cash", "--apply"],
                     ["accrual", "schedule", "tax-treatment", "--entity", "E", "--id", "s", "--treatment", "capitalize", "--apply"]):
            with self.assertRaises(PreparationDenied):
                supported_preparation_command(argv)


class ScheduleTaxTreatment(CashViewFixture):
    def test_enum_is_validated_and_only_prepaid_accepts_it(self):
        with self.assertRaises(JournalError) as caught:
            self.schedule("bad", "prepaid", PREPAID, INSURANCE, "100.00", "2026-01-01", "2026-12-31", "equal_month", BANK, tax_treatment="deduct")
        self.assertEqual(caught.exception.code, "invalid_tax_treatment")
        with self.assertRaises(JournalError) as caught:
            self.schedule("dr", "deferred", DEFERRED, SUBSCRIPTIONS, "100.00", "2026-01-01", "2026-12-31", "equal_month", BANK,
                          tax_treatment="capitalize")
        self.assertEqual(caught.exception.code, "tax_treatment_prepaid_only")
        self.schedule("ok", "prepaid", PREPAID, INSURANCE, "100.00", "2026-01-01", "2026-12-31", "equal_month", BANK, tax_treatment="capitalize")
        self.assertEqual(schedules.list_schedules(self.entity)[0]["tax_treatment"], "capitalize")

    def test_treatment_comes_only_from_the_record(self):
        # The engine applies no tax rule: the dates do not matter, only a recorded value.
        for start, end in (("2026-01-01", "2026-12-31"), ("2026-07-01", "2027-07-01")):
            record = {"kind": "prepaid", "service_start": start, "service_end": end}
            self.assertEqual(schedules.effective_tax_treatment(record), ("capitalize", "not_recorded"))
            for value in ("expense_when_paid", "capitalize"):
                self.assertEqual(schedules.effective_tax_treatment({**record, "tax_treatment": value}), (value, "recorded"))
        self.assertEqual(schedules.effective_tax_treatment({"kind": "deferred"}), (None, "not_recorded"))
        self.assertFalse(hasattr(schedules, "default_tax_treatment"))

    def test_readiness_note_for_a_cash_filer_with_untreated_prepaid(self):
        self.schedule("ins", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-01-01", "2026-12-31", "equal_month", BANK)

        def codes():
            document = assess_review_readiness(self.root, "2026-01", local_dependency_versions(self.root), as_of=date(2026, 3, 1)).document
            return [w["code"] for w in document["warnings"]], [b["code"] for b in document["blockers"]]
        warnings, _ = codes()
        self.assertNotIn("schedule_tax_treatment_missing", warnings)
        set_method(self.root, "cash", view=None)
        warnings, blockers = codes()
        self.assertIn("schedule_tax_treatment_missing", warnings)
        self.assertNotIn("schedule_tax_treatment_missing", blockers)
        note = next(n for n in taxview.readiness_notes(load_entity(self.root)) if n["code"] == "schedule_tax_treatment_missing")
        self.assertNotIn("12-month", note["message"])
        self.assertIn("as the books record them", note["message"])
        books("accrual", "schedule", "tax-treatment", "--entity", str(self.root), "--id", "ins", "--treatment", "expense_when_paid", "--apply")
        self.assertNotIn("schedule_tax_treatment_missing", codes()[0])


class ReviewRegressions(CashViewFixture):
    handoff = CashFilerWithAccrualRecords.handoff
    cash_filer = CashFilerWithAccrualRecords.cash_filer
    def test_mixed_prepaids_keep_only_capitalized_asset(self):
        self.schedule("long", "prepaid", PREPAID, INSURANCE, "2400.00", "2026-01-01", "2027-12-31", "equal_month", BANK)
        self.schedule("short", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-07-01", "2027-06-30", "equal_month", BANK,
                      tax_treatment="expense_when_paid")
        self.due("2026-12-31")
        self.cash_filer()
        result = self.handoff()
        # Long policy (not recorded): 1,200 used, 1,200 asset. Short policy (recorded expense_when_paid): all 1,200 deducted.
        self.assertEqual(tb(result)[PREPAID], D("1200.00"))
        self.assertEqual(tb(result)[INSURANCE], D("2400.00"))
        self.assertEqual(result["totals"]["difference"], D("0.00"))
        self.due("2027-12-31")
        # Later recognition must not change the prior year asset.
        self.assertEqual(tb(self.handoff())[PREPAID], D("1200.00"))
        rows = {r["account"]: r for r in self.handoff(Y2027)["trial_balance"]}
        self.assertEqual(rows[PREPAID]["beginning_balance"], D("1200.00"))
        self.assertEqual(rows[PREPAID]["balance"], D("0.00"))

    def test_schedule_tax_change_refuses_unsealed_ledger(self):
        self.schedule("ins", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-01-01", "2026-12-31", "equal_month", BANK)
        with self.store.transaction(immediate=True) as conn:
            conn.execute("UPDATE postings SET amount='999.00' WHERE account=?", (PREPAID,))
        before = self.store.content_digest()
        with self.assertRaises(JournalError) as caught:
            schedules.apply_tax_treatment(self.entity, "ins", "capitalize")
        self.assertEqual(caught.exception.code, "ledger_integrity")
        self.assertEqual(self.store.content_digest(), before)
        self.assertNotEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_schedule_creation_cannot_set_tax_policy_under_standing_authority(self):
        from unittest.mock import patch
        with patch("bookkeeping.preparation_runtime.is_preparation", return_value=True):
            with self.assertRaises(JournalError) as caught:
                self.schedule("ins", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-01-01", "2026-12-31", "equal_month", BANK,
                              tax_treatment="expense_when_paid")
        self.assertEqual(caught.exception.code, "tax_treatment_person_only")
        self.assertEqual(schedules.list_schedules(self.entity), [])

    def test_fiscal_year_does_not_change_prepaid_treatment_but_stales_preview(self):
        from bookkeeping import fiscal
        # 1,200.00 for 2026-08-01 to 2027-07-31, 100.00 a month. Unset: 5 months used in 2026 (500.00), 700.00 asset.
        self.schedule("ins", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-08-01", "2027-07-31", "equal_month", BANK, paid="2026-06-15")
        self.due("2026-12-31")
        self.cash_filer()
        self.assertEqual((tb(self.handoff())[INSURANCE], tb(self.handoff())[PREPAID]), (D("500.00"), D("700.00")))
        old = schedules.preview_tax_treatment(self.entity, "ins", "capitalize")
        fiscal.apply(self.entity, "07-01")
        with self.assertRaises(JournalError) as caught:
            schedules.apply_tax_treatment(self.entity, "ins", "capitalize", old["plan_hash"])
        self.assertEqual(caught.exception.code, "stale_preview")
        result = self.handoff()
        self.assertEqual(tb(result)[INSURANCE], D("500.00"))
        self.assertEqual(tb(result)[PREPAID], D("700.00"))
        self.assertEqual(result["tax_view"]["fiscal_year_start"], "07-01")

    def test_tax_setting_file_failure_rolls_back_audit(self):
        from unittest.mock import patch
        original = (self.root / "entity.json").read_bytes()
        before = self.store.load_audit_events()
        with patch("os.replace", side_effect=OSError("synthetic rename failure")):
            with self.assertRaises(OSError):
                taxview.apply_set_tax_method(self.entity, "cash", "on")
        self.assertEqual((self.root / "entity.json").read_bytes(), original)
        self.assertEqual(self.store.load_audit_events(), before)
        self.assertFalse((self.root / "entity.json.tax-method.tmp").exists())
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_balance_sheet_earnings_use_configured_fiscal_start(self):
        from bookkeeping import fiscal
        from bookkeeping.reports.statements import balance_sheet
        self.item("prior", "receivable", AR, "200.00", "2026-06-01", "Synthetic", counter=SERVICES)
        self.item("current", "receivable", AR, "300.00", "2026-07-01", "Synthetic", counter=SERVICES)
        fiscal.apply(self.entity, "07-01")
        result = balance_sheet(self.root, date(2026, 12, 31))
        self.assertEqual(result.meta["fiscal_start"], "2026-07-01")
        self.assertEqual(D(result.meta["current_fiscal_net_income"]), D("300.00"))
        self.assertEqual(D(result.meta["cumulative_net_income"]), D("500.00"))

    def test_adjustment_row_cannot_hide_an_unbalanced_ledger(self):
        from bookkeeping.reports.cashview import CashViewError
        self.item("inv", "receivable", AR, "100.00", "2026-01-01", "Synthetic", counter=SERVICES)
        self.cash_filer()
        with self.store.transaction(immediate=True) as conn:
            conn.execute("UPDATE postings SET amount='90.00' WHERE account=?", (AR,))
        with self.assertRaisesRegex(CashViewError, "trial balance"):
            self.handoff()

    def test_invalid_enabled_tax_method_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "no tax method"):
            taxview.active_method({"tax_method_view": True})
        with self.assertRaises(ValueError):
            taxview.active_method({"tax_method_view": "on"})
        with self.assertRaises(JournalError):
            schedules.effective_tax_treatment({"kind": "prepaid", "tax_treatment": "typo"})


if __name__ == "__main__":
    unittest.main()
