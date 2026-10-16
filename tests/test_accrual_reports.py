"""Independent multi-period financial and historical-support expectations."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
import csv
import io
from pathlib import Path
import unittest

from bookkeeping.accrual import journals, items, migration, reports, schedules
from bookkeeping.accrual.store import list_records, load_record
from bookkeeping.entity import load_entity
from bookkeeping.readiness import assess_review_readiness, local_dependency_versions
from bookkeeping.reports import statements, workbook
from tests import test_accrual_migration as fixtures
from tests import test_accrual_items as item_fixtures
from tests.test_accrual_schedules import AccrualFixture, AR, AP, BANK, EXPENSE, INCOME


class AdjustmentRegisterTests(AccrualFixture):
    item = item_fixtures.ItemTests.item
    settlement = item_fixtures.ItemTests.settlement
    create = item_fixtures.ItemTests.create

    def register(self, start="2026-01-01", end="2026-01-31"):
        return reports.report(self.entity, "adjustments", from_date=start, to_date=end)["rows"]

    def test_credit_writeoff_and_arbitrary_bank_sources_have_complete_effects_and_trace(self):
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry, Posting
        self.create()
        request = {"operation_id": "credit-1", "item_id": "invoice-1", "action": "credit", "amount": "200.00",
                   "date": "2026-01-10", "counter_account": INCOME, "source_id": "credit-note-1",
                   "reason": "Synthetic credit", "evidence_ids": [self.evidence("credit")]}
        items.apply_adjustment(self.entity, items.preview_adjustment(self.entity, request))
        writeoff = {**request, "operation_id": "writeoff-1", "action": "writeoff", "amount": "100.00",
                    "counter_account": "Expenses:Bad-Debt", "source_id": "writeoff-source",
                    "evidence_ids": [self.evidence("writeoff")]}
        items.apply_adjustment(self.entity, items.preview_adjustment(self.entity, writeoff))
        items.apply_settlement(self.entity, items.preview_settlement(self.entity, self.settlement()))
        manual = Entry(date(2026, 1, 12), "Synthetic unmanaged posting",
                       meta=(("source-id", "accrual:manual"), ("accrual-operation", "invoice-create")),
                       postings=(Posting(EXPENSE, Decimal("1.00")), Posting(BANK, Decimal("-1.00"))))
        _atomic_ledger_write(self.entity, [], [manual], "manual", None, "Synthetic unmanaged entry")
        rows = self.register()
        self.assertEqual([row["source_id"] for row in rows],
                         ["accrual:invoice-create", "adjustment:credit-1", "adjustment:writeoff-1", "bank-payment-1"])
        credit = rows[1]
        self.assertEqual(credit["operations"][0]["id"], "credit-1")
        self.assertEqual(credit["operations"][0]["evidence_ids"], request["evidence_ids"])
        self.assertEqual(credit["operations"][0]["role"], "posted_effect")
        self.assertEqual({p["account"]: Decimal(p["amount"]) for p in credit["postings"]},
                         {AR: Decimal("-200.00"), INCOME: Decimal("200.00")})
        self.assertEqual([row["source_id"] for row in self.register(end="2026-01-09")], ["accrual:invoice-create"])
        self.assertEqual([row["source_id"] for row in self.register(start="2026-01-11")], ["bank-payment-1"])

    def test_existing_control_payment_is_linked_once_across_multiple_nonposting_allocations(self):
        self.create()
        self.post("provider-bank-object", "2026-01-15", [(BANK, "1000.00"), (AR, "-1000.00")])
        for identity, amount in (("allocate-first", "400.00"), ("allocate-rest", "600.00")):
            request = self.settlement(operation_id=identity,
                source={"mode": "existing", "source_id": "provider-bank-object", "bank_account": BANK, "amount": "1000.00"},
                allocations=[{"item_id": "invoice-1", "amount": amount}])
            receipt = items.apply_settlement(self.entity, items.preview_settlement(self.entity, request))
            self.assertEqual(receipt["entry_source_ids"], [])
        rows = self.register()
        self.assertEqual([row["source_id"] for row in rows], ["accrual:invoice-create", "provider-bank-object"])
        self.assertEqual([operation["id"] for operation in rows[1]["operations"]], ["allocate-first", "allocate-rest"])
        self.assertEqual({operation["role"] for operation in rows[1]["operations"]}, {"linked_existing_source"})

    def test_later_existing_origin_link_does_not_leak_into_earlier_register(self):
        self.post("existing-provider-invoice", "2026-01-15", [(AR, "1000.00"), (INCOME, "-1000.00")])
        self.assertEqual(self.register(), [])
        self.create(as_of="2026-02-01", origin={"mode": "existing", "source_id": "existing-provider-invoice"})
        self.assertEqual(self.register(), [])
        row, = self.register(end="2026-02-28")
        self.assertEqual(row["date"], "2026-01-15")
        self.assertEqual(row["operations"][0]["id"], "invoice-create")
        self.assertEqual(self.register(start="2026-02-01", end="2026-02-28"), [])


class AccrualReportsTests(unittest.TestCase):
    setUp = fixtures.AccrualMigrationTests.setUp
    change = fixtures.AccrualMigrationTests.change
    preview = fixtures.AccrualMigrationTests.preview

    def opening(self):
        # Independently specified accounting truth: June assets 13,600,
        # liabilities 1,400, source retained equity 9,000 and YTD profit 3,200.
        edits = {
            "trial_balance.csv": [("Accounts Payable,,700", "Accounts Payable,,500"), ("Deferred Revenue,,900", "Deferred Revenue,,600"),
                ("Payroll Payable,,200", "Payroll Payable,,300"), ("Retained Earnings,,5000", "Retained Earnings,,9000"),
                ("Services,,10000", "Services,,12000"), ("Operating Costs,3200,", "Operating Costs,8800,"), ("TOTAL,17200,17200", "TOTAL,22800,22800")],
            "balance_sheet.csv": [("Accounts Payable,700", "Accounts Payable,500"), ("Deferred Revenue,900", "Deferred Revenue,600"),
                ("Payroll Payable,200", "Payroll Payable,300"), ("Retained Earnings,5000", "Retained Earnings,9000"), ("Net Income,6800", "Net Income,3200")],
            "profit_and_loss.csv": [("Services,10000", "Services,12000"), ("Operating Costs,3200", "Operating Costs,8800"), ("Net Income,6800", "Net Income,3200")],
        }
        for filename, changes in edits.items():
            for old, new in changes:
                self.change(filename, old, new)
        self.support["items"][1].update(original_amount="500.00", remaining_amount="500.00")
        self.support["schedules"][1].update(amount="1200.00", recognized_amount="600.00", remaining_amount="600.00")
        self.support["external"][2]["balance"] = "-300.00"
        migration.apply(self.entity, self.preview())
        self.evidence = list_records(self.store, "evidence")[0]["id"]

    def evidence_for(self, name, when="2026-07-15"):
        return journals.intake_evidence(self.entity, {"schema_version": 1, "source": {"namespace": "synthetic.provider",
            "account_id": "synthetic-account", "object_id": name, "version": "1"}, "currency": "USD", "dates": {"date": when},
            "observed_facts": {"fixture": name}})["id"]

    def journal(self, identity, when, postings, **extra):
        request = {"id": identity, "date": when, "description": "Synthetic " + identity,
                   "evidence_ids": [self.evidence_for(identity, when)],
                   "postings": [{"account": a, "amount": n, "currency": "USD"} for a, n in postings], **extra}
        return journals.apply_journal(self.entity, journals.preview_journal(self.entity, request))

    def settle(self, identity, item, amount, when="2026-07-15"):
        request = {"operation_id": identity, "date": when, "source": {"mode": "new", "source_id": identity,
            "bank_account": "Assets:Bank:Checking", "amount": amount}, "allocations": [{"item_id": item, "amount": str(abs(Decimal(amount)))}],
            "evidence_ids": [self.evidence_for(identity, when)]}
        return items.apply_settlement(self.entity, items.preview_settlement(self.entity, request))

    def july(self):
        self.settle("customer-receipt", "customer-open", "400.00")
        self.settle("vendor-payment", "vendor-open", "-200.00")
        schedules.apply_due(self.entity, schedules.preview_due(self.entity, "2026-07-31", operation_id="july-recognition"))
        request = {"operation_id": "payroll-origin", "id": "july-payroll", "kind": "accrual", "account": "Liabilities:Other:Payroll-Payable",
                   "original_amount": "600.00", "as_of": "2026-07-20", "currency": "USD",
                   "origin": {"mode": "new", "source_id": "july-provider-payroll", "counter_account": "Expenses:Operating-Costs"},
                   "evidence_ids": [self.evidence_for("july-provider-payroll", "2026-07-20")]}
        items.apply_item(self.entity, items.preview_item(self.entity, request))
        self.settle("payroll-payment", "july-payroll", "-600.00", "2026-07-25")
        self.journal("depreciation", "2026-07-31", [("Expenses:Operating-Costs", "40.00"), ("Assets:Fixed:Accumulated-Depreciation", "-40.00")])
        support = {"operation_id": "july-depreciation-support", "account": "Assets:Fixed:Accumulated-Depreciation", "as_of": "2026-07-31",
                   "currency": "USD", "details": [{"id": "asset-depreciation", "balance": "-440.00", "description": "Provider depreciation roll-forward"}],
                   "evidence_ids": [self.evidence_for("depreciation-support", "2026-07-31")]}
        reports.apply_support(self.entity, reports.preview_support(self.entity, support))

    def test_independent_midyear_financial_and_supporting_truth(self):
        self.opening()
        self.july()
        july = statements.profit_and_loss(self.root, date(2026, 7, 1), date(2026, 7, 31))
        ytd = statements.profit_and_loss(self.root, date(2026, 1, 1), date(2026, 7, 31))
        june = statements.profit_and_loss(self.root, date(2026, 1, 1), date(2026, 6, 30))
        self.assertEqual(july.sections[0]["total"], Decimal("100.00"))
        self.assertEqual(july.sections[1]["total"], Decimal("740.00"))
        self.assertEqual(july.totals["net_income"], Decimal("-640.00"))
        self.assertEqual(ytd.totals["net_income"], Decimal("2560.00"))
        self.assertEqual(june.totals["net_income"], Decimal("3200.00"))
        self.assertEqual(july.meta["basis"], "accrual")
        self.assertFalse(july.meta["summary_included"])
        self.assertTrue(ytd.meta["summary_included"])
        text = ytd.to_text()
        self.assertIn("Accounting basis: accrual", text)
        self.assertIn("Activity cutover: 2026-07-01, opening balances as of 2026-06-30", text)
        self.assertIn("Imported activity summary: 2026-01-01 through 2026-06-30 (included)", text)
        self.assertIn("not included in this report", july.to_text())
        ytd.meta["unresolved_issues"] = [{"code": "external_support_missing", "message": "Update supporting detail.", "refs": ["synthetic-support"]}]
        self.assertIn("Unresolved accounting issues:\n  - Update supporting detail. [synthetic-support]", ytd.to_text())
        self.assertEqual(ytd.totals["net_income"], Decimal("2560.00"))
        bs = statements.balance_sheet(self.root, date(2026, 7, 31))
        self.assertIn("Retained fiscal summary coverage: 2026-01-01 through 2026-06-30", bs.to_text())
        self.assertNotIn("not included in this report", bs.to_text())
        self.assertEqual(bs.totals["total_assets"], Decimal("12660.00"))
        self.assertEqual(bs.totals["total_liabilities_and_equity"], Decimal("12660.00"))
        ledger = {r["label"].replace(" › ", ":"): r["amount"] for section in statements.trial_balance(self.root, date(2026, 7, 31)).sections for r in section["rows"]}
        self.assertEqual({a: ledger[a] for a in ("Assets:Bank:Checking", "Assets:Receivable:Accounts-Receivable", "Assets:Other:Prepaid-Insurance",
            "Assets:Fixed:Equipment", "Assets:Fixed:Accumulated-Depreciation", "Liabilities:Payable:Accounts-Payable", "Liabilities:Other:Deferred-Revenue", "Liabilities:Other:Payroll-Payable")},
            {"Assets:Bank:Checking": Decimal("9600"), "Assets:Receivable:Accounts-Receivable": Decimal("600"), "Assets:Other:Prepaid-Insurance": Decimal("500"),
             "Assets:Fixed:Equipment": Decimal("2400"), "Assets:Fixed:Accumulated-Depreciation": Decimal("-440"), "Liabilities:Payable:Accounts-Payable": Decimal("-300"),
             "Liabilities:Other:Deferred-Revenue": Decimal("-500"), "Liabilities:Other:Payroll-Payable": Decimal("-300")})
        recon = reports.report(self.entity, "reconciliation", as_of="2026-07-31")
        self.assertTrue(all(r["difference"] == "0.00" for r in recon["rows"]), recon)
        self.assertEqual(recon["issues"], [])

    def test_partial_imported_subperiods_are_explicitly_unavailable(self):
        self.opening()
        for start, end in ((date(2026, 3, 1), date(2026, 7, 31)), (date(2026, 1, 1), date(2026, 3, 31)), (date(2026, 6, 1), date(2026, 6, 30))):
            with self.subTest(start=start), self.assertRaises(reports.CoverageError):
                statements.profit_and_loss(self.root, start, end)
        with self.assertRaises(reports.CoverageError):
            statements.balance_sheet(self.root, date(2026, 5, 31))

    def test_ask_uses_coverage_and_recognition_without_inventing_cash_payments(self):
        self.opening()
        self.july()
        for question in ("What was Q1 2026 revenue?", "What were Q1 2026 expenses?", "Owner draws Q1 2026", "How much did we pay Acme Q1 2026?"):
            with self.subTest(question=question), self.assertRaises(reports.CoverageError):
                statements.ask(self.root, question, today=date(2026, 7, 31))
        for question, key, expected in (("July 2026 revenue", "total_revenue", "100.00"),
                ("July 2026 expenses", "total_expenses", "740.00"), ("Net income YTD", "net_income", "2560.00")):
            answer = statements.ask(self.root, question, today=date(2026, 7, 31))
            self.assertEqual(answer.data[key], expected)
            self.assertEqual(answer.data["meta"]["basis"], "accrual")
            self.assertIn("Accounting basis: accrual", answer.answer_text)
            self.assertIn("Activity cutover: 2026-07-01", answer.answer_text)
        cash = statements.ask(self.root, "How much did we pay Acme July 2026?", today=date(2026, 7, 31))
        self.assertEqual(cash.data["unavailable"], "cash_payment_linkage_required")
        self.assertNotIn("total", cash.data)

    def test_historical_support_survives_later_settlements_recognition_and_external_refresh(self):
        self.opening()
        self.july()
        before = {kind: reports.report(self.entity, kind, as_of="2026-07-31")["rows"] for kind in ("outstanding", "schedules", "support", "reconciliation")}
        self.settle("august-customer-receipt", "customer-open", "200.00", "2026-08-10")
        schedules.apply_due(self.entity, schedules.preview_due(self.entity, "2026-08-31", operation_id="august-recognition"))
        self.journal("august-depreciation", "2026-08-31", [("Expenses:Operating-Costs", "40.00"), ("Assets:Fixed:Accumulated-Depreciation", "-40.00")])
        support = {"operation_id": "august-depreciation-support", "account": "Assets:Fixed:Accumulated-Depreciation", "as_of": "2026-08-31", "currency": "USD",
                   "details": [{"id": "asset-depreciation", "balance": "-480.00"}], "evidence_ids": [self.evidence_for("august-depreciation-support", "2026-08-31")]}
        reports.apply_support(self.entity, reports.preview_support(self.entity, support))
        after = {kind: reports.report(self.entity, kind, as_of="2026-07-31")["rows"] for kind in before}
        # Current operational remainder fields may advance; historical amounts
        # and reconciliation must remain derived from dated immutable events.
        for kind in ("outstanding", "schedules"):
            self.assertEqual({r["id"]: r["remaining_as_of"] for r in before[kind]}, {r["id"]: r["remaining_as_of"] for r in after[kind]})
        self.assertEqual(before["support"], after["support"])
        self.assertEqual(before["reconciliation"], after["reconciliation"])

    def test_external_support_is_nonposting_exact_immutable_and_replay_safe(self):
        self.opening()
        request = {"operation_id": "external-refresh", "account": "Assets:Fixed:Equipment", "as_of": "2026-07-31", "currency": "USD",
                   "details": [{"id": "equipment", "balance": "2400.00"}], "evidence_ids": [self.evidence_for("asset-source", "2026-07-31")]}
        bad = deepcopy(request)
        bad["details"][0]["balance"] = "2399.99"
        with self.assertRaisesRegex(ValueError, "difference -0.01"):
            reports.preview_support(self.entity, bad)
        count = self.store.counts().entries
        plan = reports.preview_support(self.entity, request)
        receipt = reports.apply_support(self.entity, plan)
        self.assertEqual(reports.apply_support(self.entity, plan), receipt)
        self.assertEqual(self.store.counts().entries, count)
        self.assertTrue(load_record(self.store, "coverage", "opening-support:equipment-open"))
        correction = deepcopy(request)
        correction.update(operation_id="aaa-sorts-before-original", supersedes="external-support:external-refresh")
        reports.apply_support(self.entity, reports.preview_support(self.entity, correction))
        selected = next(r for r in reports.report(self.entity, "support", as_of="2026-07-31")["rows"] if r["account"] == request["account"])
        self.assertEqual(selected["id"], "external-support:aaa-sorts-before-original")
        self.assertEqual(selected["snapshot_revision"], 2)

    def test_mixed_control_support_requires_explicit_residual_ownership(self):
        self.opening()
        request = {"operation_id": "residual-ownership", "account": "Assets:Receivable:Accounts-Receivable", "as_of": "2026-07-31", "currency": "USD",
                   "details": [{"id": "no-external-receivable", "balance": "0.00"}], "evidence_ids": [self.evidence]}
        with self.assertRaisesRegex(ValueError, "explicit residual mode"):
            reports.preview_support(self.entity, request)
        request.update(mode="residual", managed_support_ids=["customer-open"])
        reports.apply_support(self.entity, reports.preview_support(self.entity, request))
        self.assertTrue(all(r["difference"] == "0.00" for r in reports.report(self.entity, "reconciliation", as_of="2026-07-31")["rows"]))

    def test_refund_and_supersession_preserve_historical_schedule_definitions_and_balances(self):
        self.opening()
        self.july()
        before = {r["id"]: (r["remaining_amount"], r["recognized_amount"], r["status"]) for r in reports.report(self.entity, "schedules", as_of="2026-07-31")["rows"]}
        schedules.apply_due(self.entity, schedules.preview_due(self.entity, "2026-08-31", operation_id="recognize-before-change"))
        refund = {"operation_id": "prepaid-refund", "id": "prepaid-open", "action": "refund", "date": "2026-08-31", "reason": "Synthetic provider refund",
                  "amount": "100.00", "offset_account": "Assets:Bank:Checking", "evidence_ids": [self.evidence_for("refund-source", "2026-08-31")]}
        schedules.apply_change(self.entity, schedules.preview_change(self.entity, refund))
        supersede = {"operation_id": "deferred-supersession", "id": "deferred-open", "action": "supersede", "date": "2026-09-01", "reason": "Synthetic revised service",
                     "evidence_ids": [self.evidence_for("revised-service", "2026-09-01")],
                     "replacement": {"id": "deferred-replacement", "start_date": "2026-09-01", "end_date": "2027-01-01", "method": "equal_month"}}
        schedules.apply_change(self.entity, schedules.preview_change(self.entity, supersede))
        historical = {r["id"]: (r["remaining_amount"], r["recognized_amount"], r["status"]) for r in reports.report(self.entity, "schedules", as_of="2026-07-31")["rows"]}
        self.assertEqual(before, historical)
        current = reports.report(self.entity, "reconciliation", as_of="2026-09-01")
        self.assertTrue(all(r["difference"] == "0.00" for r in current["rows"]), current)

    def test_workbook_exposes_basis_coverage_support_and_correct_monthly_ytd_checks(self):
        self.opening()
        self.july()
        for start in (date(2026, 7, 1), date(2026, 1, 1)):
            package = workbook.generate_accountant_package(self.root, start, date(2026, 7, 31), override=True)
            self.assertTrue(package.success, package.error)
            by_name = {path.stem: list(csv.reader(io.StringIO(path.read_text()))) for path in package.csv_files}
            self.assertTrue({"Recognition-Schedules", "Outstanding-Balances", "External-Support", "Control-Reconciliation", "Accrual-Adjustments", "Accounting-Coverage"} <= set(by_name))
            self.assertIn(["Accounting basis", "Accrual basis"], by_name["Summary"])
            self.assertIn(["Report metadata", "Basis", "Accrual basis"], by_name["P-and-L"])
            self.assertTrue(all(row[2] == "PASS" for row in by_name["Checks"][1:4]), by_name["Checks"])

    def test_new_fiscal_year_moves_prior_profit_to_retained_without_double_counting(self):
        self.opening()
        self.july()
        self.journal("new-year-service", "2027-01-10", [("Assets:Bank:Checking", "100.00"), ("Income:Services", "-100.00")])
        pnl = statements.profit_and_loss(self.root, date(2027, 1, 1), date(2027, 1, 31))
        bs = statements.balance_sheet(self.root, date(2027, 1, 31))
        self.assertEqual(pnl.totals["net_income"], Decimal("100.00"))
        self.assertEqual(bs.meta["current_fiscal_net_income"], "100.00")
        self.assertEqual(bs.meta["cumulative_net_income"], "2660.00")
        self.assertEqual(bs.totals["total_assets"], bs.totals["total_liabilities_and_equity"])
        retained = next(r for section in bs.sections for r in section["rows"] if r["label"] == "Retained earnings from prior fiscal activity")
        self.assertEqual(retained["amount"], Decimal("2560.00"))

    def test_disable_writes_preserves_support_and_financial_reads_and_export(self):
        self.opening()
        config = self.root / "entity.json"
        values = json.loads(config.read_text())
        values["accrual_capability_version"] = 0
        config.write_text(json.dumps(values))
        disabled = load_entity(self.root)
        self.assertTrue(reports.report(disabled, "schedules", as_of="2026-06-30")["rows"])
        self.assertEqual(statements.profit_and_loss(self.root, date(2026, 1, 1), date(2026, 6, 30)).totals["net_income"], Decimal("3200.00"))
        from bookkeeping import migration_bundle
        result = migration_bundle.build_bundle(self.root, self.root.parent / "disabled-readable.zip")
        self.assertEqual(result["status"], "exported", result)
        with self.assertRaisesRegex(ValueError, "capability"):
            reports.preview_support(disabled, {"operation_id": "disabled-write", "account": "Assets:Fixed:Equipment", "as_of": "2026-07-31", "currency": "USD",
                "details": [{"id": "equipment", "balance": "2400.00"}], "evidence_ids": [self.evidence]})

    def test_generic_balanced_journal_without_support_is_visible_and_not_ready(self):
        self.opening()
        self.journal("unsupported-accrual", "2026-07-15", [("Expenses:Operating-Costs", "25.00"), ("Liabilities:Other:Payroll-Payable", "-25.00")])
        result = reports.report(self.entity, "reconciliation", as_of="2026-07-31")
        self.assertTrue(any(i["code"] == "control_balance_unreconciled" and "-" not in i["refs"][0][:1] for i in result["issues"]))
        assessment = assess_review_readiness(self.root, "2026-07", local_dependency_versions(self.root), as_of=date(2026, 8, 2)).to_document()
        self.assertTrue(any(i["code"] == "control_balance_unreconciled" for i in assessment["blockers"]))
        self.assertTrue(any(i["code"] == "recognition_due" for i in assessment["blockers"]))
