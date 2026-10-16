"""Generated ledger narration is human text: a human reference, never an internal ID or engine jargon."""
from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from bookkeeping.accrual import accounts as acct
from bookkeeping.accrual.items import (apply_adjustment, apply_item, apply_offset, apply_settlement, preview_adjustment,
                                       preview_item, preview_offset, preview_settlement)
from bookkeeping.accrual.journals import apply_journal, due_reversals, preview_journal
from bookkeeping.accrual.schedules import apply_change, apply_due, apply_schedule, preview_change, preview_due, preview_schedule
from bookkeeping.entity import add_account, init_entity, load_entity
from tests.test_accrual_items_v2 import ACCRUED, DEPOSITS, EXPENSE2, ItemsV2Tests
from tests.test_accrual_schedules import AR, AP, BANK, EXPENSE, INCOME
from tests.test_accrual_schedules_v2 import V2Fixture

# Every identity below is deliberately ID-like so a leak into narration is detectable.
IDS = ("sched-7c1e44aa", "laptop-7c1e44aa", "inv-7c1e44aa", "credit-7c1e44aa", "journal-7c1e44aa", "op-7c1e44aa", "bank-7c1e44aa")
BANNED = re.compile(r"\b(recogni[sz]e|recognition|evidence|draft-|reversal of draft)\b|[0-9a-f]{8}|\w+:\w+:|-src\b", re.I)


class NarrationMixin:
    def assertHuman(self, text, *human):
        self.assertIsNone(BANNED.search(text), text)
        for identity in IDS:
            self.assertNotIn(identity, text)
        for part in human:
            self.assertIn(part, text)

    def descriptions(self, plan):
        return [row["description"] for row in plan["entries"]]


class ScheduleNarrationTests(NarrationMixin, V2Fixture):
    def make(self, **changes):
        base = {"operation_id": "op-7c1e44aa-create", "id": "sched-7c1e44aa", "reference": "Annual software subscription #104",
                "origin": {"mode": "new", "source_id": "bank-7c1e44aa", "funding_account": BANK}}
        base.update(changes)
        self.create(self.schedule(**base))

    def test_prepaid_deferred_and_depreciation_recognition(self):
        self.make()
        due = preview_due(self.entity, "2026-02-28", operation_id="op-7c1e44aa-due")
        self.assertEqual(self.descriptions(due), ["Prepaid expense used: Annual software subscription #104 (Jan 2026)",
                                                  "Prepaid expense used: Annual software subscription #104 (Feb 2026)"])
        for text in self.descriptions(due):
            self.assertHuman(text, "Annual software subscription #104")
        deferred = self.schedule(operation_id="def-create", id="def-7c1e44aa", kind="deferred", account="Liabilities:Deferred:Revenue",
                                 recognition_account=INCOME, reference="Retainer for Orbit", service_end="2026-03-31", amount="300.00",
                                 origin={"mode": "new", "source_id": "def-bank-7c1e44aa", "funding_account": BANK},
                                 evidence_ids=[self.evidence("deferred-doc")])
        self.create(deferred)
        laptop = self.laptop(operation_id="lap-create", id="laptop-7c1e44aa", reference="Laptop 16 inch",
                             origin={"mode": "new", "source_id": "laptop-bank-7c1e44aa", "date": "2026-01-05", "funding_account": BANK},
                             evidence_ids=[self.evidence("laptop-doc")])
        self.create(laptop)
        texts = self.descriptions(preview_due(self.entity, "2026-01-31", operation_id="all-due"))
        self.assertIn("Deferred revenue earned: Retainer for Orbit (Jan 2026)", texts)
        self.assertIn("Depreciation: Laptop 16 inch (Jan 2026)", texts)
        for text in texts:
            self.assertHuman(text)

    def test_terminate_and_refund(self):
        self.make()
        plan = preview_change(self.entity, {"operation_id": "op-7c1e44aa-end", "id": "sched-7c1e44aa", "action": "terminate",
            "date": "2026-05-10", "effective_date": "2026-04-30", "reason": "Cancelled", "offset_account": BANK,
            "evidence_ids": [self.evidence("end")]})
        last = self.descriptions(plan)[-1]
        self.assertEqual(last, "Ended early: Annual software subscription #104")
        for text in self.descriptions(plan):
            self.assertHuman(text, "Annual software subscription #104")
        apply_change(self.entity, plan)

    def test_refund(self):
        self.make()
        plan = preview_change(self.entity, {"operation_id": "op-7c1e44aa-refund", "id": "sched-7c1e44aa", "action": "refund",
            "date": "2026-01-31", "reason": "Refund", "evidence_ids": [self.evidence("refund")], "amount": "100.00", "offset_account": BANK})
        self.assertEqual(self.descriptions(plan), ["Refund: Annual software subscription #104"])

    def test_missing_reference_falls_back_to_counterparty_or_date_never_id(self):
        self.create(self.schedule(operation_id="plain-create", id="sched-7c1e44aa", origin={
            "mode": "new", "source_id": "bank-7c1e44aa", "funding_account": BANK}))
        text, = self.descriptions(preview_due(self.entity, "2026-01-31", operation_id="plain-due"))
        self.assertEqual(text, "Prepaid expense used: dated 2026-01-01 (Jan 2026)")
        self.assertHuman(text)

    def test_committed_legacy_recognition_replays_and_is_not_rewritten(self):
        self.make()
        with patch("bookkeeping.accrual.schedules._recognition_narration", return_value="Recognize prepaid expense: legacy"):
            due = preview_due(self.entity, "2026-01-31", operation_id="legacy-due")
            receipt = apply_due(self.entity, due)
        before = self.store.content_digest()
        self.assertEqual(apply_due(self.entity, due), {**receipt, "status": "already_applied"})
        again = preview_due(self.entity, "2026-01-31", operation_id="legacy-due")
        self.assertEqual(apply_due(self.entity, again)["status"], "already_applied")
        self.assertEqual(self.store.content_digest(), before)
        self.assertIn("Recognize prepaid expense: legacy", [e.narration for e in self.store.load_entries()])
        later = preview_due(self.entity, "2026-02-28", operation_id="new-due")
        self.assertEqual(self.descriptions(later), ["Prepaid expense used: Annual software subscription #104 (Feb 2026)"])


class ItemNarrationTests(NarrationMixin, ItemsV2Tests):
    def test_settlement_offset_and_adjustment(self):
        self.invoice("inv-7c1e44aa", "1000.00", reference="INV-1042")
        plan = preview_settlement(self.entity, {"operation_id": "op-7c1e44aa-pay", "date": "2026-03-10", "evidence_ids": [self.evidence("pay")],
            "source": {"mode": "new", "source_id": "bank-7c1e44aa", "bank_account": BANK, "amount": "400.00"},
            "allocations": [{"item_id": "inv-7c1e44aa", "amount": "400.00"}]})
        self.assertEqual(self.descriptions(plan), ["Payment from Orbit: INV-1042"])
        self.assertHuman(self.descriptions(plan)[0])
        apply_settlement(self.entity, plan)
        self.receipt("deposit", "500.00", [], when="2026-03-11", excess={
            "id": "credit-7c1e44aa", "account": DEPOSITS, "counterparty": "Orbit", "reference": "Deposit D-7"})
        offset = preview_offset(self.entity, {"operation_id": "op-7c1e44aa-offset", "date": "2026-03-15", "credit_item_id": "credit-7c1e44aa",
            "target_item_id": "inv-7c1e44aa", "amount": "300.00", "reason": "Apply deposit"})
        self.assertEqual(self.descriptions(offset), ["Credit applied: Orbit Deposit D-7 to INV-1042"])
        self.assertHuman(self.descriptions(offset)[0])
        adjust = preview_adjustment(self.entity, {"operation_id": "op-7c1e44aa-wo", "item_id": "inv-7c1e44aa", "action": "writeoff",
            "amount": "50.00", "date": "2026-03-20", "counter_account": "Expenses:Bad-Debt", "reason": "Uncollectable",
            "evidence_ids": [self.evidence("wo")]})
        self.assertEqual(self.descriptions(adjust), ["Written off: INV-1042 - Uncollectable"])

    def test_payment_to_supplier_and_true_up(self):
        self.invoice("bonus-est", "1000.00", kind="accrual", account=ACCRUED, as_of="2026-03-31", evidence_ids=[], reference="Bonus pool estimate",
                     estimate_basis="Synthetic pool", origin={"mode": "new", "source_id": "est-src-7c1e44aa", "counter_account": EXPENSE2})
        self.invoice("bonus-act", "700.00", kind="payable", account=AP, as_of="2026-04-10", counterparty="Staff", reference="Bonus run April",
                     origin={"mode": "new", "source_id": "act-src-7c1e44aa", "counter_account": EXPENSE2})
        plan = preview_adjustment(self.entity, {"operation_id": "op-7c1e44aa-tu", "item_id": "bonus-est", "action": "true_up",
            "actual_item_id": "bonus-act", "date": "2026-04-10", "counter_account": EXPENSE2, "reason": "Actual approved"})
        self.assertEqual(self.descriptions(plan), ["Estimate settled: Bonus pool estimate (actual: Bonus run April) - Actual approved"])
        pay = preview_settlement(self.entity, {"operation_id": "op-7c1e44aa-spay", "date": "2026-04-12", "evidence_ids": [self.evidence("spay")],
            "source": {"mode": "new", "source_id": "bank-7c1e44aa", "bank_account": BANK, "amount": "-700.00"},
            "allocations": [{"item_id": "bonus-act", "amount": "700.00"}]})
        self.assertEqual(self.descriptions(pay), ["Payment to Staff: Bonus run April"])

    def test_accrued_balance_origin_has_no_id(self):
        plan = preview_item(self.entity, {"operation_id": "op-7c1e44aa-acc", "id": "inv-7c1e44aa", "kind": "receivable", "account": AR,
            "currency": "USD", "original_amount": "10.00", "as_of": "2026-03-01", "counterparty": "Orbit", "reference": "INV-9",
            "origin": {"mode": "new", "source_id": "bank-7c1e44aa", "counter_account": INCOME}, "evidence_ids": [self.evidence("acc")]})
        self.assertEqual(self.descriptions(plan), ["Invoice: Orbit INV-9"])


class ReversalNarrationTests(NarrationMixin, V2Fixture):
    def test_scheduled_reversal_uses_original_description_not_its_id(self):
        request = {"id": "journal-7c1e44aa", "date": "2026-01-31", "description": "Accrued January hosting",
                   "estimate_basis": "Synthetic", "reversal_date": "2026-02-01", "postings": [
                       {"account": EXPENSE, "amount": "10.00"}, {"account": BANK, "amount": "-10.00"}]}
        apply_journal(self.entity, preview_journal(self.entity, request))
        preview = due_reversals(self.entity, "2026-02-28")
        self.assertEqual(self.descriptions(preview["results"][0]), ["Reversal of: Accrued January hosting"])
        due_reversals(self.entity, "2026-02-28", apply=True)
        self.assertIn("Reversal of: Accrued January hosting", [e.narration for e in self.store.load_entries()])
        self.assertEqual(due_reversals(self.entity, "2026-03-31", apply=True)["results"], [])
        self.assertTrue(all("journal-7c1e44aa" not in e.narration for e in self.store.load_entries()))


class StarterChartTests(unittest.TestCase):
    def test_starter_accrual_chart_has_depreciation_expense(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "starter"
            init_entity(root, name="Synthetic Starter", basis="accrual", cutover_date="2026-01-01")
            got = {a["name"]: a["subtype"] for a in acct.list_accounts(load_entity(root))["accounts"]}
            self.assertEqual(got["Expenses:Depreciation"], "expense")
            cash = Path(tmp) / "cash"
            init_entity(cash, name="Synthetic Cash")
            self.assertNotIn("Expenses:Depreciation", {a["name"] for a in acct.list_accounts(load_entity(cash))["accounts"]})


if __name__ == "__main__":
    unittest.main()
