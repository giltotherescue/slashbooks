"""Synthetic proofs for report and support defects: estimate support, snapshot aging, coverage errors,
true-up variance, settlement narration, warning scope and one-row schedules."""
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path
import json
import tempfile
import unittest

from bookkeeping.accrual import journals, reports
from bookkeeping.accrual.items import (apply_adjustment, apply_item, apply_settlement, preview_adjustment,
                                       preview_item, preview_settlement)
from bookkeeping.accrual.render import render_report
from bookkeeping.cli import main
from bookkeeping.entity import add_account, init_entity, load_entity
from bookkeeping.reports.statements import general_ledger
from tests.test_accrual_schedules import AR, BANK, EXPENSE, INCOME, AccrualFixture
from tests.test_accrual_schedules_v2 import CONTRA, EQUIPMENT, V2Fixture

ACCRUED, UTILITIES, INTEREST = "Liabilities:Other:Accrued-Liabilities", "Expenses:Utilities", "Liabilities:Other:Accrued-Interest"


class SupportFixture(AccrualFixture):
    def setUp(self):
        super().setUp()
        for account in (ACCRUED, UTILITIES, INTEREST):
            add_account(self.root, account, open_date="2000-01-01")

    def journal(self, identity, when, postings, **changes):
        request = {"id": identity, "date": when, "description": "Synthetic " + identity,
                   "postings": [{"account": a, "amount": n, "currency": "USD"} for a, n in postings], **changes}
        request.setdefault("estimate_basis", "Synthetic estimate")
        plan = journals.preview_journal(self.entity, request)
        journals.apply_journal(self.entity, plan)
        return plan

    def bonus_item(self):
        request = {"operation_id": "bonus-create", "id": "bonus", "kind": "accrual", "account": ACCRUED, "currency": "USD",
                   "original_amount": "1000.00", "as_of": "2026-01-15", "estimate_basis": "Synthetic bonus pool",
                   "origin": {"mode": "new", "source_id": "bonus-src", "counter_account": EXPENSE}}
        apply_item(self.entity, preview_item(self.entity, request))

    def row(self, as_of, account=ACCRUED):
        rows = {r["account"]: r for r in reports.report(self.entity, "reconciliation", as_of=as_of)["rows"]}
        return rows.get(account)

    def issues(self, as_of):
        return reports.report(self.entity, "reconciliation", as_of=as_of)["issues"]


class EstimateSupportTests(SupportFixture):
    def test_auto_reversing_estimate_shares_an_account_with_an_item(self):
        self.bonus_item()
        self.journal("util-acc-jan", "2026-01-31", [(UTILITIES, "450.00"), (ACCRUED, "-450.00")], reversal_date="2026-02-01")
        row = self.row("2026-01-31")
        self.assertEqual((row["status"], row["ledger_balance"], row["support_balance"]), ("reconciled", "-1450.00", "-1450.00"))
        self.assertIn("estimate:util-acc-jan:" + ACCRUED, row["support_ids"])
        self.assertEqual(self.issues("2026-01-31"), [])
        # Before the journal date it does not count; after the reversal posts it stops counting.
        self.assertEqual(self.row("2026-01-20")["support_balance"], "-1000.00")
        self.assertEqual(journals.due_reversals(self.entity, "2026-02-01", apply=True)["status"], "ok")
        row = self.row("2026-02-28")
        self.assertEqual((row["status"], row["ledger_balance"], row["support_balance"]), ("reconciled", "-1000.00", "-1000.00"))
        # Recipe 11 again for February: the reversal entry itself is posted, the new estimate supports.
        self.journal("util-acc-feb", "2026-02-28", [(UTILITIES, "480.00"), (ACCRUED, "-480.00")], reversal_date="2026-03-01")
        self.assertEqual(self.row("2026-02-28")["status"], "reconciled")
        self.assertEqual(self.row("2026-02-28")["support_balance"], "-1480.00")

    def test_estimate_alone_stays_ledger_only(self):
        self.journal("util-acc-jan", "2026-01-31", [(UTILITIES, "450.00"), (ACCRUED, "-450.00")], reversal_date="2026-02-01")
        self.assertEqual(self.row("2026-01-31")["status"], "ledger_only")

    def test_plain_journal_beside_estimate_still_blocks(self):
        self.bonus_item()
        self.journal("util-acc-jan", "2026-01-31", [(UTILITIES, "450.00"), (ACCRUED, "-450.00")], reversal_date="2026-02-01")
        self.journal("plain", "2026-01-31", [(UTILITIES, "75.00"), (ACCRUED, "-75.00")])
        row = self.row("2026-01-31")
        self.assertEqual((row["status"], row["difference"]), ("unreconciled", "75.00"))


class SnapshotAgingTests(SupportFixture):
    def snapshot(self, operation_id, as_of, balance):
        request = {"operation_id": operation_id, "account": ACCRUED, "as_of": as_of, "currency": "USD", "mode": "residual",
                   "managed_support_ids": ["bonus"], "details": [{"id": operation_id + "-d", "balance": balance, "description": "Synthetic"}],
                   "evidence_ids": [self.evidence(operation_id)]}
        reports.apply_support(self.entity, reports.preview_support(self.entity, request))

    def test_snapshot_supports_only_until_the_next_posting_and_blocker_names_the_request(self):
        self.bonus_item()
        self.journal("plain-jan", "2026-01-31", [(UTILITIES, "75.00"), (ACCRUED, "-75.00")])
        self.assertEqual(self.row("2026-01-31")["status"], "unreconciled")
        self.snapshot("snap-jan", "2026-01-31", "-75.00")
        self.assertEqual(self.row("2026-01-31")["status"], "reconciled")
        self.assertEqual(self.row("2026-02-15")["status"], "reconciled")  # no posting since
        self.journal("plain-feb", "2026-02-10", [(UTILITIES, "20.00"), (ACCRUED, "-20.00")])
        row = self.row("2026-04-30")
        self.assertEqual((row["status"], row["support_balance"], row["difference"]), ("unreconciled", "-1000.00", "95.00"))
        self.assertIn("snap-jan", row["message"])
        self.assertIn("2026-01-31", row["message"])
        self.assertIn("2026-02-10", row["message"])
        self.assertIn('"as_of": "2026-04-30"', row["action"])
        self.assertIn('"balance": "-95.00"', row["action"])
        self.assertEqual(self.row("2026-01-31")["status"], "reconciled")  # history stays exact
        self.snapshot("snap-apr", "2026-04-30", "-95.00")
        self.assertEqual(self.row("2026-04-30")["status"], "reconciled")


class CoverageErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "acc"
        init_entity(self.root, name="Synthetic", basis="accrual", cutover_date="2026-01-01")
        from bookkeeping.accrual import opening
        entity = load_entity(self.root)
        plan = opening.preview_opening(entity, {"operation_id": "op-open", "cutover": "2026-01-01", "lines": [
            {"account": "Assets:Bank:Checking", "amount": "10.00"}, {"account": "Equity:Opening-Balances", "amount": "-10.00"}]})
        self.assertEqual(plan["status"], "ready")
        opening.apply_opening(entity, plan)

    def run_cli(self, argv, save=None):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(argv)
        if save:
            save.write_text(out.getvalue())
        return status, out.getvalue(), err.getvalue()

    def test_pre_cutover_reports_give_a_clean_error_and_the_corrected_command(self):
        entity = str(self.root)
        for command, args, fixed in (
                ("pnl", ["--from", "2025-12-01", "--to", "2026-01-31"], f"books report pnl --entity {entity} --from 2026-01-01 --to 2026-01-31"),
                ("general-ledger", ["--from", "2025-12-31", "--to", "2026-01-31"], f"books report general-ledger --entity {entity} --from 2026-01-01 --to 2026-01-31"),
                ("balance-sheet", ["--as-of", "2025-06-30"], f"books report balance-sheet --entity {entity} --as-of 2025-12-31"),
                ("trial-balance", ["--as-of", "2025-06-30"], f"books report trial-balance --entity {entity} --as-of 2025-12-31")):
            with self.subTest(command=command):
                status, out, err = self.run_cli(["report", command, "--entity", entity, *args])
                self.assertEqual(status, 1)
                self.assertNotIn("Traceback", err)
                self.assertIn("unavailable", err)
                self.assertIn("The first available date is", err)
                self.assertIn(fixed, err)
                status, _, _ = self.run_cli(["report", command, "--entity", entity, *_corrected(args, fixed)])
                self.assertEqual(status, 0)


def _corrected(args, fixed):
    return fixed.split(" --entity ", 1)[1].split(" ", 1)[1].split()


class TrueUpReportTests(SupportFixture):
    def test_adjustments_report_shows_estimate_actual_and_variance(self):
        for identity, source in (("est-1", "e1"), ("est-2", "e2")):
            apply_item(self.entity, preview_item(self.entity, {"operation_id": identity, "id": identity, "kind": "accrual",
                "account": ACCRUED, "currency": "USD", "original_amount": "1000.00", "as_of": "2026-01-31", "estimate_basis": "Synthetic",
                "origin": {"mode": "new", "source_id": source, "counter_account": EXPENSE}}))
        apply_item(self.entity, preview_item(self.entity, {"operation_id": "actual", "id": "actual", "kind": "payable",
            "account": "Liabilities:Payable:Vendors", "currency": "USD", "original_amount": "1700.00", "as_of": "2026-03-31",
            "counterparty": "Staff", "origin": {"mode": "new", "source_id": "actual-src", "counter_account": EXPENSE},
            "evidence_ids": [self.evidence("actual")]}))
        for estimate in ("est-1", "est-2"):
            receipt = apply_adjustment(self.entity, preview_adjustment(self.entity, {"operation_id": "true-" + estimate, "item_id": estimate,
                "action": "true_up", "actual_item_id": "actual", "date": "2026-03-31", "counter_account": EXPENSE, "reason": "Approved"}))
        report = reports.report(self.entity, "adjustments", from_date="2026-03-01", to_date="2026-03-31")
        self.assertEqual(receipt["details"]["true_up"], {"actual_item_id": "actual", "estimate_amount": "1000.00",
                                                          "actual_amount": "1700.00", "variance": "700.00"})
        self.assertEqual([w["code"] for w in receipt["warnings"]], [])
        trued = [t for row in report["rows"] for t in row.get("true_up", [])]
        self.assertEqual(sorted((t["item_id"], t["estimate"], t["actual"], t["variance"]) for t in trued),
                         [("est-1", "1000.00", "1700.00", "700.00"), ("est-2", "1000.00", "1700.00", "700.00")])
        self.assertEqual({t["combined_variance"] for t in trued}, {"-300.00"})
        text = render_report(report)
        self.assertIn("estimate 1000.00, actual 1700.00, variance 700.00", text)
        self.assertIn("combined variance -300.00", text)


class SettlementNarrationTests(SupportFixture):
    def test_general_ledger_names_counterparty_and_reference(self):
        apply_item(self.entity, preview_item(self.entity, {"operation_id": "inv-create", "id": "inv-1", "kind": "receivable",
            "account": AR, "currency": "USD", "original_amount": "480.00", "as_of": "2026-01-10", "counterparty": "Orbit Labs",
            "reference": "INV-104", "origin": {"mode": "new", "source_id": "inv-src", "counter_account": INCOME},
            "evidence_ids": [self.evidence("inv")]}))
        apply_settlement(self.entity, preview_settlement(self.entity, {"operation_id": "pay-1", "date": "2026-02-01",
            "source": {"mode": "new", "source_id": "bank-pay-1", "bank_account": BANK, "amount": "480.00"},
            "allocations": [{"item_id": "inv-1", "amount": "480.00"}], "evidence_ids": [self.evidence("pay")]}))
        from datetime import date
        result = general_ledger(self.root, date(2026, 1, 1), date(2026, 2, 28))
        labels = [r["label"] for s in result.sections for r in s["rows"] if r["date"] == "2026-02-01"]
        self.assertTrue(labels)
        self.assertTrue(all("Orbit Labs" in label and "INV-104" in label for label in labels), labels)
        self.assertIn("Orbit Labs (INV-104)", result.to_text())


class WarningScopeTests(SupportFixture):
    def codes(self, plan):
        return [w["account"] for w in plan.get("warnings", []) if w["code"] == "control_support_required"]

    def preview(self, identity, postings, **changes):
        return journals.preview_journal(self.entity, {"id": identity, "date": "2026-01-31", "description": "Synthetic",
            "estimate_basis": "Synthetic", "postings": [{"account": a, "amount": n} for a, n in postings], **changes})

    def test_warning_only_for_item_backed_accounts_and_never_for_estimates(self):
        self.assertEqual(self.codes(self.preview("ledger-only", [(INTEREST, "-250.00"), (UTILITIES, "250.00")])), [])
        self.bonus_item()
        self.assertEqual(self.codes(self.preview("plain", [(ACCRUED, "-75.00"), (UTILITIES, "75.00")])), [ACCRUED])
        self.assertEqual(self.codes(self.preview("estimate", [(ACCRUED, "-75.00"), (UTILITIES, "75.00")], reversal_date="2026-02-01")), [])
        self.assertEqual(self.codes(self.preview("other", [(INTEREST, "-75.00"), (UTILITIES, "75.00")])), [])

    def bonus_item(self):
        apply_item(self.entity, preview_item(self.entity, {"operation_id": "bonus-create", "id": "bonus", "kind": "accrual",
            "account": ACCRUED, "currency": "USD", "original_amount": "1000.00", "as_of": "2026-01-15", "estimate_basis": "Synthetic",
            "origin": {"mode": "new", "source_id": "bonus-src", "counter_account": EXPENSE}}))


class ScheduleRowTests(V2Fixture):
    def test_depreciation_schedule_is_one_row_with_cost_accumulated_and_net_book_value(self):
        self.create(self.laptop())
        self.due("2026-03-31", "laptop-due")
        report = reports.report(self.entity, "schedules", as_of="2026-03-31")
        row, = report["rows"]
        self.assertEqual((row["id"], row["asset_cost"], row["accumulated_depreciation"], row["net_book_value"]),
                         ("laptop", "2400.00", "300.00", "2100.00"))
        text = render_report(report)
        self.assertEqual(text.count("laptop"), 1)
        self.assertIn("Net book value", text)
        # Reconciliation still supports both accounts from the same schedule.
        rows = {r["account"]: r for r in reports.report(self.entity, "reconciliation", as_of="2026-03-31")["rows"]}
        self.assertEqual((rows[EQUIPMENT]["status"], rows[CONTRA]["status"]), ("reconciled", "reconciled"))


if __name__ == "__main__":
    unittest.main()
