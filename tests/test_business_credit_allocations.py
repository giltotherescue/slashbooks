"""Exact provider credit allocations retain original document line identity."""
from decimal import Decimal
import unittest

from bookkeeping import business
from bookkeeping.accrual.store import load_record
from tests.test_business import BusinessTests
from tests.test_accrual_schedules import AR, INCOME


class IndexedCreditTests(BusinessTests):
    def test_repeated_accounts_and_tiny_partial_credits_preserve_each_line(self):
        request = self.document_payload()
        request["lines"] = [
            {"description": "Synthetic first", "quantity": "1", "unit_price": "1.00", "account": INCOME,
             "tax_amount": "0.10", "tax_account": "Liabilities:Sales-Tax"},
            {"description": "Synthetic second", "quantity": "1", "unit_price": "2.00", "account": INCOME,
             "tax_amount": "0.20", "tax_account": "Liabilities:Sales-Tax"},
        ]
        self.change(request)
        self.change({"operation_id": "issue-indexed", "action": "document.issue", "id": "doc-1", "expected_revision": 1})
        totals = {(index, field): Decimal(0) for index in range(2) for field in ("net", "tax")}
        for sequence, amount in enumerate(("0.01", "0.02", "3.27")):
            payload = {"operation_id": "indexed-credit-" + str(sequence), "action": "document.credit", "id": "doc-1",
                       "expected_revision": 2 + sequence, "date": "2026-01-20", "amount": amount,
                       "reason": "Synthetic allocated credit", "evidence_ids": [self.evidence("indexed-credit-" + str(sequence))]}
            plan = business.preview(self.entity, payload)
            correction = plan["details"]["business_correction"]
            self.assertEqual(correction["document_id"], "doc-1")
            self.assertEqual(correction["issued_snapshot_hash"], load_record(self.store, "business_documents", "doc-1")["issued_snapshot_hash"])
            self.assertEqual(correction["document_revision"], 3 + sequence)
            self.assertEqual(correction["amount_minor_units"], str(int(Decimal(amount) * 100)))
            self.assertEqual([row["line_index"] for row in correction["lines"]], [0, 1])
            self.assertEqual(sum((Decimal(row["net_amount"]) + Decimal(row["tax_amount"]) for row in correction["lines"]), Decimal(0)), Decimal(amount))
            if sequence == 0:
                self.assertEqual(correction["lines"][0]["net_amount"], "0.00")
                self.assertEqual(correction["lines"][1]["tax_amount"], "0.01")
            receipt = business.apply(self.entity, plan)
            self.assertEqual(receipt["details"]["business_correction"], correction)
            self.assertEqual(business.apply(self.entity, plan)["details"]["business_correction"], correction)
            for row in correction["lines"]:
                for field in ("net", "tax"):
                    self.assertEqual(row[field + "_minor_units"], str(int(Decimal(row[field + "_amount"]) * 100)))
                    totals[(row["line_index"], field)] += Decimal(row[field + "_amount"])
        self.assertEqual(totals, {(0, "net"): Decimal("1.00"), (0, "tax"): Decimal("0.10"), (1, "net"): Decimal("2.00"), (1, "tax"): Decimal("0.20")})
        self.assertEqual(self.balances()[AR], Decimal(0))

    def test_explicit_counter_account_does_not_invent_invoice_line_allocation(self):
        self.document()
        plan = business.preview(self.entity, {"operation_id": "explicit-writeoff", "action": "document.writeoff", "id": "doc-1",
            "expected_revision": 2, "date": "2026-01-20", "amount": "1000.00", "counter_account": "Expenses:Bad-Debt",
            "reason": "Synthetic writeoff", "evidence_ids": [self.evidence("indexed-writeoff")]})
        self.assertIsNone(plan["details"]["business_correction"]["lines"])


if __name__ == "__main__":
    unittest.main()
