"""Synthetic independent recognition, origin-linkage and lifecycle proofs."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from bookkeeping.accrual.journals import intake_evidence
from bookkeeping.accrual.schedules import (apply_change, apply_due, apply_schedule, installments,
    list_schedules, preview_change, preview_due, preview_schedule)
from bookkeeping.accrual.store import SupportingMutation, list_records, load_record
from bookkeeping.entity import add_account, init_entity, load_entity
from bookkeeping.ledger.importer import _atomic_ledger_write, check_integrity
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.ledger.store import LedgerStore

BANK, PREPAID, DEFERRED = "Assets:Bank:Checking", "Assets:Prepaid:Services", "Liabilities:Deferred:Revenue"
EXPENSE, INCOME = "Expenses:Software", "Income:Consulting"
AR, AP = "Assets:Receivable:Customers", "Liabilities:Payable:Vendors"


class AccrualFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "company"
        init_entity(self.root, basis="accrual")
        for account in (PREPAID, DEFERRED, AR, AP, BANK, EXPENSE, INCOME, "Equity:Opening-Balances", "Expenses:Bad-Debt"):
            add_account(self.root, account, open_date="2000-01-01")
        self.entity = load_entity(self.root)
        self.store = LedgerStore(self.root / "ledger.sqlite")
        self.eid = self.evidence("default")

    def evidence(self, object_id):
        return intake_evidence(self.entity, {"schema_version": 1, "source": {
            "namespace": "example.synthetic", "account_id": "example-account", "object_id": object_id, "version": "1"},
            "currency": "USD", "observed_facts": {"description": "Synthetic evidence"}})["id"]

    def balances(self):
        totals = {}
        for e in self.store.load_entries():
            for p in e.postings:
                totals[p.account] = totals.get(p.account, Decimal(0)) + p.amount
        return totals

    def post(self, source_id, when, postings):
        entry = Entry(date.fromisoformat(when), "Synthetic", postings=tuple(Posting(a, Decimal(n)) for a, n in postings),
                      meta=(("source-id", source_id),))
        _atomic_ledger_write(self.entity, [], [entry], source_id, None, "Synthetic fixture")

    def schedule(self, **changes):
        request = {"operation_id": "schedule-create", "id": "annual", "kind": "prepaid", "account": PREPAID,
            "recognition_account": EXPENSE, "currency": "USD", "amount": "1200.00", "start_date": "2026-01-01",
            "end_date": "2027-01-01", "method": "equal_month", "origin": {
                "mode": "new", "source_id": "annual-payment", "funding_account": BANK}, "evidence_ids": [self.eid]}
        request.update(changes)
        return request


class ScheduleTests(AccrualFixture):
    def test_full_calendar_year_twelve_hundred_posts_twelve_hundreds_once(self):
        request = self.schedule()
        plan = preview_schedule(self.entity, request)
        before = self.store.content_digest()
        self.assertEqual(len(plan["entries"]), 1)
        self.assertEqual([row["amount"] for row in plan["details"]["installments"]], ["100.00"] * 12)
        self.assertEqual(plan["details"]["end_date_convention"], "exclusive")
        self.assertEqual(plan["details"]["remaining_amount"], "1200.00")
        self.assertEqual(self.store.content_digest(), before)
        receipt = apply_schedule(self.entity, plan)
        self.assertEqual(apply_schedule(self.entity, plan), receipt)
        due = preview_due(self.entity, "2026-12-31", operation_id="recognize-year")
        self.assertEqual([p["postings"][0]["amount"] for p in due["entries"]], ["100.00"] * 12)
        first = apply_due(self.entity, due)
        self.assertEqual(apply_due(self.entity, due), first)
        self.assertEqual(self.balances()[PREPAID], Decimal(0))
        self.assertEqual(self.balances()[EXPENSE], Decimal("1200"))
        self.assertEqual(self.balances()[BANK], Decimal("-1200"))
        self.assertEqual(list_schedules(self.entity)[0]["status"], "completed")
        empty = preview_due(self.entity, "2027-01-31", operation_id="recognize-again")
        self.assertEqual(empty["entries"], [])
        apply_due(self.entity, empty)
        self.assertEqual(self.store.counts().entries, 13)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_actual_day_crosses_leap_day_and_assigns_residual_to_final_period(self):
        record = {"start_date": "2023-12-15", "end_date": "2024-03-15", "remaining_amount": "100.00", "method": "actual_day"}
        rows = installments(record)
        self.assertEqual([r["date"] for r in rows], ["2023-12-31", "2024-01-31", "2024-02-29", "2024-03-14"])
        self.assertEqual([r["amount"] for r in rows], ["18.68", "34.06", "31.86", "15.40"])
        self.assertEqual(sum(Decimal(r["amount"]) for r in rows), Decimal("100.00"))
        thirds = installments({"start_date": "2026-01-01", "end_date": "2026-04-01", "remaining_amount": "100.00", "method": "equal_month"})
        self.assertEqual([r["amount"] for r in thirds], ["33.33", "33.33", "33.34"])

    def test_existing_expense_payment_is_reclassified_without_another_cash_effect(self):
        self.post("already-paid", "2026-01-01", [(BANK, "-1200"), (EXPENSE, "1200")])
        request = self.schedule(origin={"mode": "existing", "source_id": "already-paid", "reclass_account": EXPENSE})
        plan = preview_schedule(self.entity, request)
        self.assertFalse(any(p["account"] == BANK for p in plan["entries"][0]["postings"]))
        apply_schedule(self.entity, plan)
        self.assertEqual(self.balances()[BANK], Decimal("-1200"))
        self.assertEqual(self.balances()[EXPENSE], Decimal(0))
        apply_due(self.entity, preview_due(self.entity, "2026-01-31", operation_id="jan"))
        self.assertEqual(self.balances()[EXPENSE], Decimal("100"))

    def test_midyear_opening_schedule_recognizes_only_carried_remaining_balance(self):
        self.post("opening", "2026-06-30", [(PREPAID, "600"), ("Equity:Opening-Balances", "-600")])
        request = self.schedule(origin={"mode": "opening", "source_id": "opening"}, recognized_amount="600.00", recognition_start="2026-07-01")
        apply_schedule(self.entity, preview_schedule(self.entity, request))
        due = preview_due(self.entity, "2026-12-31", operation_id="remaining")
        self.assertEqual([e["postings"][0]["amount"] for e in due["entries"]], ["100.00"] * 6)
        apply_due(self.entity, due)
        self.assertEqual(self.balances()[PREPAID], Decimal(0))
        self.assertEqual(self.balances()[EXPENSE], Decimal("600"))
        self.assertNotIn(BANK, self.balances())

    def test_deferred_schedule_reclassifies_existing_income_and_recognizes_it(self):
        self.post("customer-payment", "2026-01-01", [(BANK, "1200"), (INCOME, "-1200")])
        request = self.schedule(kind="deferred", account=DEFERRED, recognition_account=INCOME,
                                origin={"mode": "existing", "source_id": "customer-payment", "reclass_account": INCOME})
        apply_schedule(self.entity, preview_schedule(self.entity, request))
        self.assertEqual(self.balances()[INCOME], Decimal(0))
        apply_due(self.entity, preview_due(self.entity, "2026-01-31", operation_id="jan"))
        self.assertEqual(self.balances()[DEFERRED], Decimal("-1100"))
        self.assertEqual(self.balances()[INCOME], Decimal("-100"))
        self.assertEqual(self.balances()[BANK], Decimal("1200"))

    def test_competing_due_previews_post_only_one_installment(self):
        apply_schedule(self.entity, preview_schedule(self.entity, self.schedule()))
        first = preview_due(self.entity, "2026-01-31", operation_id="first")
        competing = preview_due(self.entity, "2026-01-31", operation_id="competing")
        apply_due(self.entity, first)
        with self.assertRaisesRegex(ValueError, "preview changed"):
            apply_due(self.entity, competing)
        self.assertEqual(self.balances()[EXPENSE], Decimal("100"))
        self.assertEqual(len([r for r in list_records(self.store, "allocations") if r.get("kind") == "recognition"]), 1)

    def test_late_due_processing_reports_locked_period_without_partial_posts(self):
        apply_schedule(self.entity, preview_schedule(self.entity, self.schedule()))
        config = json.loads((self.root / "entity.json").read_text()); config["closed_periods"] = ["2026-01"]
        (self.root / "entity.json").write_text(json.dumps(config))
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "closed"):
            preview_due(load_entity(self.root), "2026-02-28", operation_id="late")
        self.assertEqual(self.store.content_digest(), before)

    def test_partial_refund_after_recognition_revises_remainder_preserving_history(self):
        apply_schedule(self.entity, preview_schedule(self.entity, self.schedule()))
        apply_due(self.entity, preview_due(self.entity, "2026-01-31", operation_id="jan"))
        original = self.store.load_entries()
        change = {"operation_id": "refund", "id": "annual", "action": "refund", "date": "2026-01-31",
                  "amount": "110.00", "offset_account": BANK, "reason": "Synthetic partial refund", "evidence_ids": [self.evidence("refund")]}
        plan = preview_change(self.entity, change)
        apply_change(self.entity, plan)
        self.assertEqual(self.store.load_entries()[:len(original)], original)
        self.assertEqual(load_record(self.store, "schedules", "annual")["remaining_amount"], "990.00")
        due = preview_due(self.entity, "2026-12-31", operation_id="rest")
        self.assertEqual([e["postings"][0]["amount"] for e in due["entries"]], ["90.00"] * 11)
        apply_due(self.entity, due)
        self.assertEqual(self.balances()[PREPAID], Decimal(0))
        self.assertEqual(self.balances()[EXPENSE], Decimal("1090"))
        self.assertEqual(self.balances()[BANK], Decimal("-1090"))

    def test_supersession_moves_only_remaining_support_without_financial_origin(self):
        apply_schedule(self.entity, preview_schedule(self.entity, self.schedule()))
        apply_due(self.entity, preview_due(self.entity, "2026-01-31", operation_id="jan"))
        change = {"operation_id": "revise", "id": "annual", "action": "supersede", "date": "2026-02-01", "reason": "Synthetic revised service",
                  "evidence_ids": [self.evidence("revision")], "replacement": {"id": "revised", "start_date": "2026-02-01", "end_date": "2026-08-01", "method": "equal_month"}}
        before = self.store.counts().entries
        apply_change(self.entity, preview_change(self.entity, change))
        self.assertEqual(self.store.counts().entries, before)
        self.assertEqual(load_record(self.store, "schedules", "annual")["status"], "superseded")
        self.assertEqual(load_record(self.store, "schedules", "revised")["remaining_amount"], "1100.00")
        apply_due(self.entity, preview_due(self.entity, "2026-08-31", operation_id="finish"))
        self.assertEqual(self.balances()[PREPAID], Decimal(0))
        self.assertEqual(self.balances()[EXPENSE], Decimal("1200"))

    def test_cancel_requires_explicit_balance_treatment_and_preserves_history(self):
        apply_schedule(self.entity, preview_schedule(self.entity, self.schedule()))
        change = {"operation_id": "cancel", "id": "annual", "action": "cancel", "date": "2026-01-10", "reason": "Service cancelled",
                  "evidence_ids": [self.evidence("cancellation")]}
        with self.assertRaises(ValueError):
            preview_change(self.entity, change)
        change["offset_account"] = BANK
        plan = preview_change(self.entity, change)
        receipt = apply_change(self.entity, plan)
        self.assertEqual(apply_change(self.entity, plan), receipt)
        self.assertEqual(self.balances()[PREPAID], Decimal(0))
        self.assertEqual(self.balances()[BANK], Decimal(0))
        self.assertEqual(list_schedules(self.entity)[0]["status"], "cancelled")

    def test_invalid_dates_precision_or_missing_facts_never_publish(self):
        for changes in ({"start_date": ""}, {"end_date": "2026-02-30"}, {"amount": "1.001"},
                        {"start_date": "2026-01-02"}, {"currency": "EUR"}, {"method": "guess"}):
            before = self.store.content_digest()
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                preview_schedule(self.entity, self.schedule(**changes))
            self.assertEqual(self.store.content_digest(), before)

    def test_activation_preview_installment_details_are_hash_bound(self):
        plan = preview_schedule(self.entity, self.schedule())
        plan["details"]["installments"][0]["amount"] = "0.01"
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "preview has been changed"):
            apply_schedule(self.entity, plan)
        self.assertEqual(self.store.content_digest(), before)

    def test_opening_principal_cannot_be_registered_again_after_recognition(self):
        self.post("opening", "2025-12-31", [(PREPAID, "1200"), ("Equity:Opening-Balances", "-1200")])
        request = self.schedule(origin={"mode": "opening", "source_id": "opening"})
        apply_schedule(self.entity, preview_schedule(self.entity, request))
        apply_due(self.entity, preview_due(self.entity, "2026-06-30", operation_id="half"))
        duplicate = self.schedule(id="duplicate", operation_id="duplicate", amount="600.00", origin={"mode": "opening", "source_id": "opening"})
        with self.assertRaisesRegex(ValueError, "exceed the linked"):
            preview_schedule(self.entity, duplicate)

    def test_explicit_refund_split_corrects_recognized_and_remaining_deferred_service_once(self):
        request = self.schedule(kind="deferred", account=DEFERRED, recognition_account=INCOME)
        apply_schedule(self.entity, preview_schedule(self.entity, request))
        apply_due(self.entity, preview_due(self.entity, "2026-04-30", operation_id="four-months"))
        history = self.store.load_entries()
        refund = {"operation_id": "large-refund", "id": "annual", "action": "refund", "date": "2026-04-30",
                  "reason": "Evidenced cancellation and recognized service refund", "evidence_ids": [self.evidence("large-refund")],
                  "amount": "1000.00", "offset_account": BANK}
        with self.assertRaisesRegex(ValueError, "explicitly split"):
            preview_change(self.entity, refund)
        refund.update(remaining_refund_amount="800.00", recognized_refund_amount="200.00", recognized_refund_account=INCOME)
        plan = preview_change(self.entity, refund)
        receipt = apply_change(self.entity, plan)
        self.assertEqual(apply_change(self.entity, plan), receipt)
        self.assertEqual(self.store.load_entries()[:len(history)], history)
        self.assertEqual(self.balances()[DEFERRED], Decimal(0))
        self.assertEqual(self.balances()[INCOME], Decimal("-200"))
        self.assertEqual(self.balances()[BANK], Decimal("200"))
        record = load_record(self.store, "schedules", "annual")
        self.assertEqual(record["recognized_amount"], "400.00")
        self.assertEqual(record["recognized_refund_amount"], "200.00")
        self.assertEqual(record["refunded_amount"], "800.00")
        self.assertEqual(record["history"][0]["record"]["remaining_amount"], "800.00")


if __name__ == "__main__":
    unittest.main()
