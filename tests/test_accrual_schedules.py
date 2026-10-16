"""Synthetic independent recognition, origin-linkage and lifecycle proofs."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
            "recognition_account": EXPENSE, "currency": "USD", "amount": "1200.00", "service_start": "2026-01-01",
            "service_end": "2026-12-31", "method": "equal_month", "origin": {
                "mode": "new", "source_id": "annual-payment", "funding_account": BANK}, "evidence_ids": [self.eid]}
        request.update(changes)
        return request


class ScheduleTests(AccrualFixture):
    def test_new_prepaid_card_funding_posts_card_once_without_cash_and_replays(self):
        for card in ("Liabilities:CreditCard", "Liabilities:CreditCard:Synthetic"):
            with self.subTest(card=card):
                add_account(self.root, card, open_date="2000-01-01")
                identity = "card-" + card.rsplit(":", 1)[-1]
                request = self.schedule(id=identity, operation_id=identity,
                    origin={"mode": "new", "source_id": identity + "-purchase", "funding_account": card},
                    evidence_ids=[self.evidence(identity)])
                plan = preview_schedule(self.entity, request)
                self.assertEqual(plan["entries"][0]["postings"], [
                    {"account": PREPAID, "amount": "1200.00", "currency": "USD", "debit": "1200.00", "credit": "0.00"},
                    {"account": card, "amount": "-1200.00", "currency": "USD", "debit": "0.00", "credit": "1200.00"}])
                receipt = apply_schedule(self.entity, plan)
                before = self.store.content_digest()
                self.assertEqual(apply_schedule(self.entity, plan), {**receipt, "status": "already_applied"})
                self.assertEqual(self.store.content_digest(), before)
                self.assertEqual(self.balances()[card], Decimal("-1200.00"))
                self.assertEqual(self.balances().get(BANK, Decimal(0)), Decimal(0))
                duplicate = {**request, "id": identity + "-duplicate", "operation_id": identity + "-duplicate",
                             "evidence_ids": [self.evidence(identity + "-duplicate")]}
                before = self.store.content_digest()
                with self.assertRaisesRegex(ValueError, "already posted"):
                    preview_schedule(self.entity, duplicate)
                self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(self.store.counts().entries, 2)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_existing_card_expense_reclassifies_without_another_card_or_cash_effect(self):
        card = "Liabilities:CreditCard:Synthetic"
        add_account(self.root, card, open_date="2000-01-01")
        self.post("existing-card-purchase", "2026-01-01", [(EXPENSE, "1200.00"), (card, "-1200.00")])
        request = self.schedule(origin={"mode": "existing", "source_id": "existing-card-purchase", "reclass_account": EXPENSE})
        plan = preview_schedule(self.entity, request)
        self.assertTrue(all(posting["account"] not in {card, BANK} for entry in plan["entries"] for posting in entry["postings"]))
        receipt = apply_schedule(self.entity, plan)
        self.assertEqual(apply_schedule(self.entity, plan), {**receipt, "status": "already_applied"})
        self.assertEqual(self.balances()[card], Decimal("-1200.00"))
        self.assertEqual(self.balances()[PREPAID], Decimal("1200.00"))
        self.assertEqual(self.balances()[EXPENSE], Decimal(0))
        self.assertEqual(self.balances().get(BANK, Decimal(0)), Decimal(0))

    def test_card_cannot_fund_deferred_revenue_and_other_liabilities_do_not_fund_prepaids(self):
        for account in ("Liabilities:CreditCard", "Liabilities:CreditCard:Synthetic", "Liabilities:CreditCardboard", AP):
            add_account(self.root, account, open_date="2000-01-01")
        before = self.store.content_digest()
        for kind, funding in (("deferred", "Liabilities:CreditCard"), ("deferred", "Liabilities:CreditCard:Synthetic"),
                              ("prepaid", AP), ("prepaid", "Liabilities:CreditCardboard")):
            with self.subTest(kind=kind, funding=funding), self.assertRaisesRegex(ValueError, "funding requires"):
                preview_schedule(self.entity, self.schedule(kind=kind, account=DEFERRED if kind == "deferred" else PREPAID,
                    recognition_account=INCOME if kind == "deferred" else EXPENSE,
                    origin={"mode": "new", "source_id": "invalid-funding", "funding_account": funding}))
            self.assertEqual(self.store.content_digest(), before)

    def test_refund_evidence_cannot_post_cash_again_with_another_operation_id(self):
        apply_schedule(self.entity, preview_schedule(self.entity, self.schedule()))
        request = {"operation_id": "refund-one", "id": "annual", "action": "refund", "date": "2026-01-31",
            "reason": "Same synthetic refund", "evidence_ids": [self.evidence("same-refund")], "amount": "100.00", "offset_account": BANK}
        receipt = apply_change(self.entity, preview_change(self.entity, request))
        repeated = {**request, "operation_id": "refund-another-transport"}
        self.assertEqual(apply_change(self.entity, preview_change(self.entity, repeated)), receipt)
        self.assertEqual(self.balances()[BANK], Decimal("-1100"))
        self.assertEqual(self.balances()[PREPAID], Decimal("1100"))
        self.assertEqual(self.store.counts().entries, 2)
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "different financial treatment"):
            preview_change(self.entity, {**repeated, "operation_id": "different-refund", "amount": "200.00"})
        self.assertEqual(self.store.content_digest(), before)

    def test_cancel_and_split_refund_replay_before_inactive_schedule_checks(self):
        for action in ("cancel", "refund"):
            with self.subTest(action=action):
                self.setUp()
                apply_schedule(self.entity, preview_schedule(self.entity, self.schedule()))
                apply_due(self.entity, preview_due(self.entity, "2026-01-31", operation_id="recognize-january"))
                request = {"operation_id": "end-service", "id": "annual", "action": action, "date": "2026-01-31",
                    "reason": "Synthetic complete service termination", "evidence_ids": [self.evidence("termination")], "offset_account": BANK}
                if action == "refund":
                    request.update(amount="1200.00", remaining_refund_amount="1100.00", recognized_refund_amount="100.00", recognized_refund_account=EXPENSE)
                receipt = apply_change(self.entity, preview_change(self.entity, request))
                before = self.balances()
                self.assertEqual(apply_change(self.entity, preview_change(self.entity, {**request, "operation_id": "second-termination"})), receipt)
                self.assertEqual(self.balances(), before)

    def test_changes_cannot_precede_actual_funding_origin(self):
        apply_schedule(self.entity, preview_schedule(self.entity, self.schedule()))
        for action in ("refund", "cancel", "supersede"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "cannot precede its financial origin"):
                preview_change(self.entity, {"operation_id": "impossible-" + action, "id": "annual", "action": action,
                    "date": "2025-12-31", "reason": "Synthetic pre-origin change", "evidence_ids": [self.evidence("pre-origin-" + action)],
                    "amount": "100.00", "offset_account": BANK})
        self.assertEqual(self.balances(), {PREPAID: Decimal("1200"), BANK: Decimal("-1200")})
        from bookkeeping.ledger.importer import reverse_entry
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "managed accrual"):
            reverse_entry(self.entity, "annual-payment", "legacy-funding-reversal")
        self.assertEqual(self.store.content_digest(), before)

    def test_human_reference_is_validated_stored_reported_and_replay_safe(self):
        from bookkeeping.accrual.reports import report
        from bookkeeping.accrual.render import render_report
        from bookkeeping.accrual.cli import request_schemas
        reference = "Annual software subscription #104"
        plan = preview_schedule(self.entity, self.schedule(reference=reference))
        self.assertEqual(plan["request"]["reference"], reference)
        self.assertEqual(plan["request"]["id"], "annual")
        self.assertEqual(plan["entries"][0]["description"], "Fund prepaid expense: " + reference)
        self.assertEqual(dict(plan["entries"][0]["meta"])["schedule-id"], "annual")
        receipt = apply_schedule(self.entity, plan)
        before = self.store.content_digest()
        self.assertEqual(apply_schedule(self.entity, plan), {**receipt, "status": "already_applied"})
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(list_schedules(self.entity)[0]["reference"], reference)
        due = preview_due(self.entity, "2026-01-31", operation_id="recognize-reference")
        self.assertEqual(due["entries"][0]["description"], "Prepaid expense used: " + reference + " (Jan 2026)")
        document = report(self.entity, "schedules", as_of="2026-01-31")
        self.assertEqual(document["rows"][0]["reference"], reference)
        self.assertIn(reference, render_report(document))
        self.assertEqual(request_schemas()["schedule"]["properties"]["reference"]["maxLength"], 2000)
        self.assertEqual(check_integrity(self.entity).status, "ok")
        for invalid in ("", "  ", None, 104, "x" * 2001):
            with self.subTest(reference=invalid), self.assertRaises(ValueError):
                preview_schedule(self.entity, self.schedule(id="another", operation_id="another-create", reference=invalid))
            self.assertEqual(self.store.content_digest(), before)

    def test_committed_legacy_schedule_narrations_replay_without_rewriting(self):
        with patch("bookkeeping.accrual.schedules._narration", return_value="Fund prepaid schedule annual"):
            plan = preview_schedule(self.entity, self.schedule(reference="Annual software subscription #104"))
            receipt = apply_schedule(self.entity, plan)
        with patch("bookkeeping.accrual.schedules._recognition_narration", return_value="Recognize prepaid schedule annual"):
            due = preview_due(self.entity, "2026-01-31", operation_id="legacy-recognition")
            due_receipt = apply_due(self.entity, due)
        before = self.store.content_digest()
        self.assertEqual(apply_schedule(self.entity, plan), {**receipt, "status": "already_applied"})
        self.assertEqual(apply_due(self.entity, due), {**due_receipt, "status": "already_applied"})
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual([e.narration for e in self.store.load_entries()],
                         ["Fund prepaid schedule annual", "Recognize prepaid schedule annual"])

    def test_full_calendar_year_twelve_hundred_posts_twelve_hundreds_once(self):
        request = self.schedule()
        plan = preview_schedule(self.entity, request)
        before = self.store.content_digest()
        self.assertEqual(len(plan["entries"]), 1)
        self.assertEqual(plan["entries"][0]["description"], "Fund prepaid expense")
        self.assertEqual([row["amount"] for row in plan["details"]["installments"]], ["100.00"] * 12)
        self.assertIn("inclusive", plan["details"]["service_dates"])
        self.assertEqual(plan["details"]["remaining_amount"], "1200.00")
        self.assertEqual(self.store.content_digest(), before)
        receipt = apply_schedule(self.entity, plan)
        self.assertEqual(apply_schedule(self.entity, plan), {**receipt, "status": "already_applied"})
        due = preview_due(self.entity, "2026-12-31", operation_id="recognize-year")
        self.assertEqual([p["postings"][0]["amount"] for p in due["entries"]], ["100.00"] * 12)
        self.assertEqual({p["description"] for p in due["entries"]}, {f"Prepaid expense used: dated 2026-01-01 ({m} 2026)" for m in "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()})
        first = apply_due(self.entity, due)
        self.assertEqual(apply_due(self.entity, due), {**first, "status": "already_applied"})
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
        record = {"service_start": "2023-12-15", "service_end": "2024-03-14", "remaining_amount": "100.00", "method": "actual_day"}
        rows = installments(record)
        self.assertEqual([r["date"] for r in rows], ["2023-12-31", "2024-01-31", "2024-02-29", "2024-03-14"])
        self.assertEqual([r["amount"] for r in rows], ["18.68", "34.06", "31.86", "15.40"])
        self.assertEqual(sum(Decimal(r["amount"]) for r in rows), Decimal("100.00"))
        thirds = installments({"service_start": "2026-01-01", "service_end": "2026-03-31", "remaining_amount": "100.00", "method": "equal_month"})
        self.assertEqual([r["amount"] for r in thirds], ["33.33", "33.33", "33.34"])

    def test_existing_expense_payment_is_reclassified_without_another_cash_effect(self):
        self.post("already-paid", "2026-01-01", [(BANK, "-1200"), (EXPENSE, "1200")])
        request = self.schedule(reference="Annual software subscription #104",
            origin={"mode": "existing", "source_id": "already-paid", "reclass_account": EXPENSE})
        plan = preview_schedule(self.entity, request)
        self.assertEqual(plan["entries"][0]["description"],
                         "Reclassify funding for prepaid expense: Annual software subscription #104")
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
        plan = preview_schedule(self.entity, request)
        self.assertEqual(plan["entries"][0]["description"], "Reclassify funding for deferred revenue")
        apply_schedule(self.entity, plan)
        self.assertEqual(self.balances()[INCOME], Decimal(0))
        due = preview_due(self.entity, "2026-01-31", operation_id="jan")
        self.assertEqual(due["entries"][0]["description"], "Deferred revenue earned: dated 2026-01-01 (Jan 2026)")
        apply_due(self.entity, due)
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
                  "evidence_ids": [self.evidence("revision")], "replacement": {"id": "revised", "service_start": "2026-02-01", "service_end": "2026-07-31", "method": "equal_month"}}
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
        self.assertEqual(apply_change(self.entity, plan), {**receipt, "status": "already_applied"})
        self.assertEqual(self.balances()[PREPAID], Decimal(0))
        self.assertEqual(self.balances()[BANK], Decimal(0))
        self.assertEqual(list_schedules(self.entity)[0]["status"], "cancelled")

    def test_invalid_dates_precision_or_missing_facts_never_publish(self):
        for changes in ({"service_start": ""}, {"service_end": "2026-02-30"}, {"amount": "1.001"},
                        {"service_start": "2026-01-02"}, {"currency": "EUR"}, {"method": "guess"}):
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
        self.assertEqual(apply_change(self.entity, plan), {**receipt, "status": "already_applied"})
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
