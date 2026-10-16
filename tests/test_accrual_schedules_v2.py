"""Synthetic proofs for schedules as a general recognition primitive (inclusive dates, custom, depreciation, terminate)."""
from __future__ import annotations

from decimal import Decimal
import unittest

from bookkeeping.accrual import reports
from bookkeeping.accrual.cli import request_schemas
from bookkeeping.accrual.journals import JournalError, intake_evidence
from bookkeeping.accrual.schedules import (apply_change, apply_due, apply_schedule, installments, preview_change,
                                           preview_due, preview_schedule)
from bookkeeping.entity import add_account
from bookkeeping.ledger.importer import check_integrity
from bookkeeping.entity import load_entity
from tests.test_accrual_schedules import AccrualFixture, BANK, DEFERRED, EXPENSE, INCOME, PREPAID

EQUIPMENT, CONTRA, DEPRECIATION = "Assets:Fixed:Equipment", "Assets:Fixed:Accumulated-Depreciation", "Expenses:Depreciation"


class V2Fixture(AccrualFixture):
    def setUp(self):
        super().setUp()
        for account in (EQUIPMENT, CONTRA, DEPRECIATION):
            add_account(self.root, account, open_date="2000-01-01")

    def evidence_with_period(self, object_id, start, end, inclusive=True):
        return intake_evidence(self.entity, {"schema_version": 1, "source": {
            "namespace": "example.synthetic", "account_id": "example-account", "object_id": object_id, "version": "1"},
            "currency": "USD", "service_period": {"start": start, "end": end, "end_inclusive": inclusive},
            "observed_facts": {"description": "Synthetic"}})["id"]

    def create(self, request):
        plan = preview_schedule(self.entity, request)
        apply_schedule(self.entity, plan)
        return plan

    def due(self, through, name):
        return apply_due(self.entity, preview_due(self.entity, through, operation_id=name))

    def laptop(self, **changes):
        request = {"operation_id": "laptop-create", "id": "laptop", "kind": "depreciation", "account": EQUIPMENT,
                   "contra_account": CONTRA, "recognition_account": DEPRECIATION, "currency": "USD", "amount": "2400.00",
                   "service_start": "2026-01-01", "service_end": "2027-12-31", "method": "equal_month",
                   "origin": {"mode": "new", "source_id": "laptop-purchase", "date": "2026-01-05", "funding_account": BANK},
                   "evidence_ids": [self.eid]}
        request.update(changes)
        return request

    def rows(self, kind, as_of):
        return {r["id"]: r for r in reports.report(self.entity, kind, as_of=as_of)["rows"]}


class InclusiveDatesTests(V2Fixture):
    def test_old_fields_are_rejected_naming_the_new_fields(self):
        request = self.schedule()
        request.pop("service_start"), request.pop("service_end")
        request.update(start_date="2026-01-01", end_date="2027-01-01")
        with self.assertRaises(JournalError) as caught:
            preview_schedule(self.entity, request)
        self.assertEqual(caught.exception.code, "renamed_service_fields")
        self.assertIn("service_start", caught.exception.action)
        self.assertIn("service_end", caught.exception.action)
        self.assertIn("inclusive", str(caught.exception))

    def test_deferred_january_15_to_january_14_covers_365_days_daily(self):
        request = self.schedule(id="retainer", operation_id="retainer-create", kind="deferred", account=DEFERRED,
                                recognition_account=INCOME, amount="12000.00", service_start="2026-01-15",
                                service_end="2027-01-14", method="actual_day",
                                origin={"mode": "new", "source_id": "retainer-receipt", "date": "2026-01-15", "funding_account": BANK})
        plan = self.create(request)
        parts = plan["details"]["installments"]
        self.assertEqual(parts[0]["service_start"], "2026-01-15")
        self.assertEqual(parts[0]["service_end"], "2026-01-31")
        self.assertEqual(parts[-1]["service_end"], "2027-01-14")
        self.assertEqual(parts[-1]["date"], "2027-01-14")
        self.assertEqual(sum(Decimal(p["amount"]) for p in parts), Decimal("12000.00"))
        self.assertEqual(parts[0]["amount"], f"{Decimal(12000 * 17 * 100 // 365) / 100:.2f}")  # 17 of 365 days, floored
        self.due("2027-01-14", "retainer-due")
        self.assertEqual(self.balances()[DEFERRED], Decimal(0))
        self.assertEqual(self.balances()[INCOME], Decimal("-12000.00"))
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_schedule_must_fit_supporting_document_service_period(self):
        inside = self.evidence_with_period("doc-inside", "2026-01-01", "2026-12-31")
        self.create(self.schedule(evidence_ids=[inside], origin={"mode": "new", "source_id": "p1", "funding_account": BANK}))
        wrong = self.evidence_with_period("doc-short", "2026-01-01", "2026-12-31")
        with self.assertRaises(JournalError) as caught:
            preview_schedule(self.entity, self.schedule(id="late", operation_id="late", evidence_ids=[wrong], service_end="2027-01-01",
                             method="actual_day", origin={"mode": "new", "source_id": "p2", "funding_account": BANK}))
        self.assertEqual(caught.exception.code, "service_outside_supporting_document")
        self.assertIn("2026-12-31", str(caught.exception))
        self.assertIn("service_end", caught.exception.action)
        exclusive = self.evidence_with_period("doc-exclusive", "2026-01-01", "2027-01-01", inclusive=False)
        preview_schedule(self.entity, self.schedule(id="ok2", operation_id="ok2", evidence_ids=[exclusive],
                         origin={"mode": "new", "source_id": "p3", "funding_account": BANK}))

    def test_schema_describes_inclusive_dates_and_has_examples(self):
        schema = request_schemas()["schedule"]
        self.assertIn("inclusive", schema["properties"]["service_end"]["description"])
        self.assertNotIn("end_date", schema["properties"])
        kinds = {example["kind"] for example in schema["examples"]}
        self.assertEqual(kinds, {"prepaid", "deferred", "depreciation"})
        self.assertIn("residual cents", schema["properties"]["method"]["description"])
        self.assertTrue(any(example["method"] == "custom" for example in schema["examples"]))
        change = request_schemas()["schedule_change"]["properties"]
        for field in ("amount", "offset_account", "recognized_refund_amount", "remaining_refund_amount",
                      "recognized_refund_account", "effective_date", "action"):
            self.assertTrue(change[field].get("description"), field)


class EqualMonthTests(V2Fixture):
    def test_non_month_aligned_start_explains_and_suggests_alternatives(self):
        with self.assertRaises(JournalError) as caught:
            preview_schedule(self.entity, self.schedule(service_start="2026-01-15", service_end="2027-01-14"))
        error = caught.exception
        self.assertEqual(error.code, "incomplete_calendar_month")
        self.assertIn("2026-01-15 is not the first day of a month", str(error))
        self.assertIn("2027-01-14 is not the last day of a month", str(error))
        self.assertIn("actual_day", error.action)
        self.assertIn("custom", error.action)

    def test_floor_rounding_puts_residual_cents_on_the_last_installment(self):
        parts = installments({"service_start": "2026-01-01", "service_end": "2026-03-31", "remaining_amount": "100.00", "method": "equal_month"})
        self.assertEqual([p["amount"] for p in parts], ["33.33", "33.33", "33.34"])
        self.assertIn("residual", request_schemas()["schedule"]["properties"]["method"]["description"])


class CustomTests(V2Fixture):
    def usage(self, **changes):
        request = self.schedule(id="usage", operation_id="usage-create", amount="300.00", service_start="2026-01-01",
            service_end="2026-03-31", method="custom", installments=[
                {"date": "2026-01-31", "amount": "50.00"}, {"date": "2026-02-28", "amount": "100.00"}, {"date": "2026-03-31", "amount": "150.00"}],
            origin={"mode": "new", "source_id": "usage-payment", "funding_account": BANK})
        request.update(changes)
        return request

    def test_three_installment_usage_schedule_posts_exact_amounts(self):
        plan = self.create(self.usage())
        self.assertEqual([p["amount"] for p in plan["details"]["installments"]], ["50.00", "100.00", "150.00"])
        self.due("2026-02-28", "usage-due-1")
        self.assertEqual(self.balances()[EXPENSE], Decimal("150.00"))
        self.due("2026-03-31", "usage-due-2")
        self.assertEqual(self.balances()[EXPENSE], Decimal("300.00"))
        self.assertEqual(self.balances()[PREPAID], Decimal(0))

    def test_custom_validation_errors_are_specific(self):
        cases = {
            "installments_sum_mismatch": self.usage(amount="310.00"),
            "installments_not_ascending": self.usage(installments=[
                {"date": "2026-02-28", "amount": "100.00"}, {"date": "2026-01-31", "amount": "50.00"}, {"date": "2026-03-31", "amount": "150.00"}]),
            "installment_outside_service": self.usage(installments=[
                {"date": "2026-01-31", "amount": "50.00"}, {"date": "2026-02-28", "amount": "100.00"}, {"date": "2026-04-30", "amount": "150.00"}]),
            "invalid_amount": self.usage(installments=[
                {"date": "2026-01-31", "amount": "0.00"}, {"date": "2026-02-28", "amount": "150.00"}, {"date": "2026-03-31", "amount": "150.00"}]),
            "installments_not_allowed": self.usage(method="actual_day"),
        }
        for code, request in cases.items():
            with self.subTest(code=code), self.assertRaises(JournalError) as caught:
                preview_schedule(self.entity, request)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(JournalError) as caught:
            preview_schedule(self.entity, self.usage(amount="310.00"))
        self.assertIn("difference -10.00", str(caught.exception))


class DepreciationTests(V2Fixture):
    def test_24_month_laptop_credits_contra_and_keeps_cost(self):
        plan = self.create(self.laptop())
        self.assertEqual([p["amount"] for p in plan["details"]["installments"]], ["100.00"] * 24)
        self.due("2026-03-31", "laptop-due")
        balances = self.balances()
        self.assertEqual(balances[EQUIPMENT], Decimal("2400.00"))
        self.assertEqual(balances[CONTRA], Decimal("-300.00"))
        self.assertEqual(balances[DEPRECIATION], Decimal("300.00"))
        self.assertEqual(balances[BANK], Decimal("-2400.00"))
        self.due("2027-12-31", "laptop-due-all")
        balances = self.balances()
        self.assertEqual((balances[EQUIPMENT], balances[CONTRA], balances[DEPRECIATION]),
                         (Decimal("2400.00"), Decimal("-2400.00"), Decimal("2400.00")))
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_support_reconciles_asset_and_contra_at_every_date(self):
        self.create(self.laptop())
        self.due("2026-03-31", "laptop-due")
        for as_of in ("2026-01-31", "2026-03-31"):
            rows = {r["account"]: r for r in reports.report(self.entity, "reconciliation", as_of=as_of)["rows"]}
            self.assertEqual(rows[EQUIPMENT]["status"], "reconciled", as_of)
            if CONTRA in rows:
                self.assertEqual(rows[CONTRA]["status"], "reconciled", as_of)
        rows = {r["account"]: r for r in reports.report(self.entity, "reconciliation", as_of="2026-03-31")["rows"]}
        self.assertEqual(rows[CONTRA]["support_balance"], "-300.00")
        self.assertEqual(rows[EQUIPMENT]["support_balance"], "2400.00")

    def test_salvage_value_reduces_depreciable_base(self):
        plan = self.create(self.laptop(salvage_value="600.00", amount="2400.00"))
        self.assertEqual([p["amount"] for p in plan["details"]["installments"]], ["75.00"] * 24)
        self.due("2027-12-31", "laptop-due-all")
        self.assertEqual(self.balances()[CONTRA], Decimal("-1800.00"))
        self.assertEqual(self.balances()[EQUIPMENT], Decimal("2400.00"))

    def test_depreciation_works_with_actual_day_and_custom(self):
        self.create(self.laptop(id="daily", operation_id="daily", method="actual_day", service_start="2026-01-15", service_end="2027-01-14",
                                evidence_ids=[self.evidence("daily")],
                                origin={"mode": "new", "source_id": "daily-purchase", "date": "2026-01-15", "funding_account": BANK}))
        self.create(self.laptop(id="uneven", operation_id="uneven", method="custom", service_start="2026-01-01", service_end="2026-12-31",
            installments=[{"date": "2026-06-30", "amount": "1000.00"}, {"date": "2026-12-31", "amount": "1400.00"}],
            evidence_ids=[self.evidence("uneven")],
            origin={"mode": "new", "source_id": "uneven-purchase", "date": "2026-01-01", "funding_account": BANK}))
        self.due("2027-01-14", "dep-all")
        self.assertEqual(self.balances()[CONTRA], Decimal("-4800.00"))

    def test_requires_contra_and_rejects_it_elsewhere_and_bad_direction(self):
        request = self.laptop()
        request.pop("contra_account")
        with self.assertRaises(JournalError) as caught:
            preview_schedule(self.entity, request)
        self.assertEqual(caught.exception.code, "missing_fact")
        with self.assertRaises(JournalError) as caught:
            preview_schedule(self.entity, self.schedule(contra_account=CONTRA))
        self.assertEqual(caught.exception.code, "depreciation_only_field")
        with self.assertRaises(JournalError) as caught:
            preview_schedule(self.entity, self.laptop(contra_account=EQUIPMENT))
        self.assertEqual(caught.exception.code, "invalid_contra_account")

    def test_terminate_stops_depreciation_and_keeps_cost_supported(self):
        self.create(self.laptop())
        self.due("2026-02-28", "laptop-due")
        plan = preview_change(self.entity, {"operation_id": "dispose", "id": "laptop", "action": "terminate", "date": "2026-03-20",
            "effective_date": "2026-03-15", "reason": "Synthetic early retirement", "evidence_ids": [self.evidence("dispose")]})
        apply_change(self.entity, plan)
        self.assertEqual(self.balances()[CONTRA], Decimal("-248.38"))  # 2 full months of 100.00 plus 15/31 of March, floored
        self.assertEqual(self.balances()[EQUIPMENT], Decimal("2400.00"))
        rows = self.rows("schedules", "2026-03-31")
        self.assertEqual(rows["laptop"]["status"], "terminated")
        self.assertEqual(rows["laptop"]["signed_balance"], "2400.00")
        recon = {r["account"]: r["status"] for r in reports.report(self.entity, "reconciliation", as_of="2026-03-31")["rows"]}
        self.assertEqual(recon[EQUIPMENT], "reconciled")
        self.assertEqual(recon[CONTRA], "reconciled")
        with self.assertRaises(JournalError) as caught:
            preview_change(self.entity, {"operation_id": "x", "id": "laptop", "action": "cancel", "date": "2026-04-01",
                "reason": "x", "evidence_ids": [self.evidence("x")], "offset_account": BANK})
        self.assertEqual(caught.exception.code, "inactive_schedule")


class DirectionTests(V2Fixture):
    def test_direction_errors_name_field_account_and_fix(self):
        with self.assertRaises(JournalError) as caught:
            preview_schedule(self.entity, self.schedule(account=DEFERRED))
        error = caught.exception
        self.assertEqual(error.code, "wrong_account_direction")
        self.assertIn("account", str(error))
        self.assertIn(DEFERRED, str(error))
        with self.assertRaises(JournalError) as caught:
            preview_schedule(self.entity, self.schedule(kind="deferred", account=DEFERRED, recognition_account=EXPENSE))
        self.assertEqual(caught.exception.code, "wrong_account_direction")
        self.assertIn("Income:", caught.exception.action)
        with self.assertRaises(JournalError) as caught:
            preview_schedule(self.entity, self.schedule(account="Assets:Nope:Missing"))
        self.assertEqual(caught.exception.code, "unmapped_account")
        self.assertIn("account-add", caught.exception.action)


class TerminateTests(V2Fixture):
    def terminate(self, **changes):
        request = {"operation_id": "terminate-1", "id": "annual", "action": "terminate", "date": "2026-05-10",
                   "effective_date": "2026-04-30", "reason": "Synthetic cancellation with refund",
                   "evidence_ids": [self.evidence("termination")], "offset_account": BANK}
        request.update(changes)
        return request

    def test_terminate_after_four_months_with_refund(self):
        self.create(self.schedule())
        self.due("2026-02-28", "recognize-feb")
        plan = preview_change(self.entity, self.terminate())
        self.assertEqual(len(plan["entries"]), 3)  # March and April recognition plus the refund
        apply_change(self.entity, plan)
        balances = self.balances()
        self.assertEqual(balances[EXPENSE], Decimal("400.00"))
        self.assertEqual(balances[PREPAID], Decimal(0))
        self.assertEqual(balances[BANK], Decimal("-400.00"))  # paid 1200, refunded 800
        rows = self.rows("schedules", "2026-05-31")
        self.assertEqual(rows["annual"]["status"], "terminated")
        self.assertEqual(rows["annual"]["remaining_amount"], "0.00")
        self.assertEqual(rows["annual"]["recognized_amount"], "400.00")
        before = self.store.counts().entries
        self.assertEqual(len(preview_due(self.entity, "2026-12-31", operation_id="nothing-more")["entries"]), 0)
        self.assertEqual(self.store.counts().entries, before)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_mid_month_effective_date_prorates_daily_for_equal_month(self):
        self.create(self.schedule())
        apply_change(self.entity, preview_change(self.entity, self.terminate(effective_date="2026-03-15", date="2026-03-20")))
        # Two full months (200.00) plus 15 of 31 March days of 100.00 = 48.38 (floored).
        self.assertEqual(self.balances()[EXPENSE], Decimal("248.38"))
        self.assertEqual(self.balances()[BANK], Decimal("-248.38"))
        self.assertEqual(self.balances()[PREPAID], Decimal(0))

    def test_terminate_can_write_off_instead_of_refund(self):
        self.create(self.schedule())
        apply_change(self.entity, preview_change(self.entity, self.terminate(offset_account="Expenses:Bad-Debt")))
        self.assertEqual(self.balances()["Expenses:Bad-Debt"], Decimal("800.00"))
        self.assertEqual(self.balances()[PREPAID], Decimal(0))

    def test_terminate_requires_effective_date_and_explicit_offset(self):
        self.create(self.schedule())
        request = self.terminate()
        request.pop("effective_date")
        with self.assertRaises(JournalError) as caught:
            preview_change(self.entity, request)
        self.assertEqual(caught.exception.code, "effective_date_required")
        request = self.terminate()
        request.pop("offset_account")
        with self.assertRaises(JournalError) as caught:
            preview_change(self.entity, request)
        self.assertEqual(caught.exception.code, "missing_fact")

    def test_terminate_deferred_revenue_refunds_customer(self):
        self.create(self.schedule(id="retainer", operation_id="retainer", kind="deferred", account=DEFERRED, recognition_account=INCOME,
                                  origin={"mode": "new", "source_id": "retainer-receipt", "funding_account": BANK}))
        apply_change(self.entity, preview_change(self.entity, self.terminate(id="retainer")))
        self.assertEqual(self.balances()[INCOME], Decimal("-400.00"))
        self.assertEqual(self.balances()[BANK], Decimal("400.00"))
        self.assertEqual(self.balances()[DEFERRED], Decimal(0))

    def test_change_before_posted_recognition_gives_actionable_error(self):
        self.create(self.schedule())
        self.due("2026-03-31", "recognize-q1")
        for request in (self.terminate(effective_date="2026-02-15", date="2026-05-10"),
                        self.terminate(action="cancel", effective_date=None, date="2026-02-15")):
            request = {k: v for k, v in request.items() if v is not None}
            with self.subTest(action=request["action"]), self.assertRaises(JournalError) as caught:
                preview_change(self.entity, request)
            error = caught.exception
            self.assertEqual(error.code, "change_precedes_recognition")
            self.assertIn("2026-03-31", str(error))
            self.assertIn("books accrual schedule change-preview", error.action)
            self.assertIn("recognized_refund_amount", error.action)

    def test_effective_date_bounds(self):
        self.create(self.schedule())
        for code, changes in (("terminate_after_service_end", {"effective_date": "2026-12-31", "date": "2026-12-31"}),
                              ("terminate_before_service", {"effective_date": "2025-12-31"}),
                              ("change_precedes_effective", {"date": "2026-04-01"})):
            with self.subTest(code=code), self.assertRaises(JournalError) as caught:
                preview_change(self.entity, self.terminate(**changes))
            self.assertEqual(caught.exception.code, code)


class OriginSupportTests(V2Fixture):
    def test_existing_payment_supports_control_account_from_payment_date(self):
        add_account(self.root, "Expenses:Equipment", open_date="2000-01-01")
        self.post("equipment-purchase", "2026-01-05", [("Expenses:Equipment", "2400.00"), (BANK, "-2400.00")])
        request = self.laptop(service_start="2026-02-01", service_end="2028-01-31",
                              origin={"mode": "existing", "source_id": "equipment-purchase", "reclass_account": "Expenses:Equipment"})
        plan = preview_schedule(self.entity, request)
        self.assertEqual(plan["entries"][0]["date"], "2026-01-05")
        apply_schedule(self.entity, plan)
        recon = {r["account"]: r for r in reports.report(self.entity, "reconciliation", as_of="2026-01-31")["rows"]}
        self.assertEqual(recon[EQUIPMENT]["status"], "reconciled")
        self.assertEqual(reports.report(self.entity, "reconciliation", as_of="2026-01-31")["issues"], [])

    def test_prepaid_with_existing_payment_before_service_is_supported_in_gap_month(self):
        self.post("early-payment", "2026-01-05", [(PREPAID, "1200.00"), (BANK, "-1200.00")])
        plan = preview_schedule(self.entity, self.schedule(service_start="2026-03-01", service_end="2027-02-28",
                                origin={"mode": "existing", "source_id": "early-payment"}))
        apply_schedule(self.entity, plan)
        recon = {r["account"]: r for r in reports.report(self.entity, "reconciliation", as_of="2026-01-31")["rows"]}
        self.assertEqual(recon[PREPAID]["status"], "reconciled")
        self.assertEqual(recon[PREPAID]["support_balance"], "1200.00")


if __name__ == "__main__":
    unittest.main()
