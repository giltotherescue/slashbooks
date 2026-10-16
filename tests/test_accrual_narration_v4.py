"""Remaining generated accrual narration is plain text: no engine verbs, no repeated counterparty."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from bookkeeping.accrual.items import apply_item, preview_item, preview_offset
from bookkeeping.accrual.schedules import apply_schedule, preview_schedule
from tests.test_accrual_items_v2 import DEPOSITS, ItemsV2Tests
from tests.test_accrual_narration_v3 import NarrationMixin
from tests.test_accrual_schedules import AP, AR, BANK, EXPENSE, INCOME
from tests.test_accrual_schedules_v2 import V2Fixture

JARGON = ("Record ", "Fund ", "accrued balance", "evidence", "Reclassify")


class ItemOriginNarrationTests(NarrationMixin, ItemsV2Tests):
    def origin(self, n, **fields):
        base = {"operation_id": f"op-7c1e44aa-{n}", "id": f"inv-7c1e44aa-{n}", "kind": "receivable", "account": AR, "currency": "USD",
                "original_amount": "10.00", "as_of": "2026-03-01",
                "origin": {"mode": "new", "source_id": f"bank-7c1e44aa-{n}", "counter_account": INCOME},
                "evidence_ids": [self.evidence(f"doc-{n}")]}
        base.update(fields)
        return self.descriptions(preview_item(self.entity, base))[0]

    def test_each_kind(self):
        self.assertEqual(self.origin(1, counterparty="Orbit", reference="INV-9"), "Invoice: Orbit INV-9")
        self.assertEqual(self.origin(2, kind="payable", account=AP, counterparty="Acme", reference="B-4",
                                     origin={"mode": "new", "source_id": "bank-7c1e44aa-2", "counter_account": EXPENSE}), "Bill: Acme B-4")
        self.assertEqual(self.origin(3, kind="accrual", account=AP, reference="January hosting",
                                     estimate_basis="Last month", evidence_ids=[],
                                     origin={"mode": "new", "source_id": "bank-7c1e44aa-3", "counter_account": EXPENSE}),
                         "Estimate: January hosting")
        self.assertEqual(self.origin(4, kind="accrual", account=AP, estimate_basis="Hosting usage estimate", evidence_ids=[],
                                     origin={"mode": "new", "source_id": "bank-7c1e44aa-4", "counter_account": EXPENSE}),
                         "Estimate: Hosting usage estimate")
        self.assertEqual(self.origin(5, kind="credit", account=AR, direction="credit", counterparty="Orbit", reference="Deposit D-7"),
                         "Customer credit: Orbit Deposit D-7")

    def test_no_repeated_counterparty(self):
        self.assertEqual(self.origin(6, counterparty="Harbor Design", reference="harbor design"), "Invoice: Harbor Design")
        self.assertEqual(self.origin(7, counterparty="Harbor Design"), "Invoice: Harbor Design")
        self.assertEqual(self.origin(8, reference="INV-3"), "Invoice: INV-3")
        self.assertEqual(self.origin(9), "Invoice")

    def test_credit_applied_does_not_repeat_counterparty(self):
        self.invoice("inv-7c1e44aa", "1000.00", reference="INV-100", counterparty="Harbor Design")
        self.receipt("deposit", "500.00", [], when="2026-03-11", excess={
            "id": "credit-7c1e44aa", "account": DEPOSITS, "counterparty": "Harbor Design"})
        offset = preview_offset(self.entity, {"operation_id": "op-7c1e44aa-offset", "date": "2026-03-15", "credit_item_id": "credit-7c1e44aa",
            "target_item_id": "inv-7c1e44aa", "amount": "300.00", "reason": "Apply deposit"})
        self.assertEqual(self.descriptions(offset), ["Credit applied: Harbor Design to INV-100"])


class ScheduleNarrationV4Tests(NarrationMixin, V2Fixture):
    def funding(self, **changes):
        base = {"operation_id": "op-7c1e44aa-create", "id": "sched-7c1e44aa", "reference": "Annual software #104",
                "origin": {"mode": "new", "source_id": "bank-7c1e44aa", "funding_account": BANK}}
        base.update(changes)
        return self.descriptions(preview_schedule(self.entity, self.schedule(**base)))[0]

    def test_funding_per_kind(self):
        self.assertEqual(self.funding(), "Prepaid expense: Annual software #104")
        self.assertEqual(self.funding(kind="deferred", account="Liabilities:Deferred:Revenue", recognition_account=INCOME,
                                      reference="Retainer for Orbit", evidence_ids=[self.evidence("def")]),
                         "Deferred revenue received: Retainer for Orbit")
        laptop = self.descriptions(preview_schedule(self.entity, self.laptop(reference="Laptop 16 inch",
            origin={"mode": "new", "source_id": "lap-bank-7c1e44aa", "date": "2026-01-05", "funding_account": BANK},
            evidence_ids=[self.evidence("lap")])))
        self.assertEqual(laptop[0], "Equipment purchase: Laptop 16 inch")

    def test_replay_of_committed_legacy_funding(self):
        with patch("bookkeeping.accrual.schedules._funding_narration", return_value="Fund prepaid expense: Old"):
            plan = preview_schedule(self.entity, self.schedule(reference="Old"))
            receipt = apply_schedule(self.entity, plan)
        before = self.store.content_digest()
        self.assertEqual(apply_schedule(self.entity, plan), {**receipt, "status": "already_applied"})
        self.assertEqual(self.store.content_digest(), before)


class OpeningNarrationTests(unittest.TestCase):
    def test_opening_narration_has_date_only(self):
        import inspect
        from bookkeeping.accrual import migration, opening
        for module in (opening, migration):
            self.assertIn('narration=f"Opening balances (', inspect.getsource(module))
            self.assertNotIn("Reconciled accrual", inspect.getsource(module).replace('"Reconciled accrual opening balances from QuickBooks")', ""))


if __name__ == "__main__":
    unittest.main()
