"""Synthetic proofs for credit application, unapplied cash, estimate reversal, true-up and split bills."""
from decimal import Decimal
import unittest

from tests.test_accrual_schedules import AccrualFixture, BANK, AR, AP, EXPENSE, INCOME
from bookkeeping.accrual import cli, reports
from bookkeeping.accrual.items import (apply_adjustment, apply_item, apply_offset, apply_settlement, list_items,
    preview_adjustment, preview_item, preview_offset, preview_settlement)
from bookkeeping.accrual.journals import JournalError, apply_journal, due_reversals, preview_journal
from bookkeeping.accrual.store import list_records, load_record
from bookkeeping.entity import add_account

DEPOSITS, PREPAY, ACCRUED, EXPENSE2 = ("Liabilities:CustomerDeposits", "Assets:VendorPrepayments",
                                       "Liabilities:Accrued:Bonus", "Expenses:Utilities")


class ItemsV2Tests(AccrualFixture):
    def setUp(self):
        super().setUp()
        for account in (DEPOSITS, PREPAY, ACCRUED, EXPENSE2):
            add_account(self.root, account, open_date="2000-01-01")

    def invoice(self, item_id, amount, **changes):
        request = {"operation_id": item_id + "-create", "id": item_id, "kind": "receivable", "account": AR, "currency": "USD",
            "original_amount": amount, "as_of": "2026-03-01", "due_date": "2026-03-01", "counterparty": "Orbit",
            "origin": {"mode": "new", "source_id": item_id + "-src", "counter_account": INCOME},
            "evidence_ids": [self.evidence(item_id)]}
        request.update(changes)
        return apply_item(self.entity, preview_item(self.entity, request))

    def receipt(self, name, cash, allocations, when="2026-03-10", **changes):
        request = {"operation_id": name, "date": when, "evidence_ids": [self.evidence(name)],
            "source": {"mode": "new", "source_id": name + "-bank", "bank_account": BANK, "amount": cash},
            "allocations": [{"item_id": i, "amount": a} for i, a in allocations]}
        request.update(changes)
        return apply_settlement(self.entity, preview_settlement(self.entity, request))

    def offset(self, name, credit, target, amount, when="2026-03-15"):
        return apply_offset(self.entity, preview_offset(self.entity, {"operation_id": name, "date": when,
            "credit_item_id": credit, "target_item_id": target, "amount": amount, "reason": "Synthetic application"}))

    def remaining(self, item_id):
        return load_record(self.store, "items", item_id)["remaining_amount"]

    def test_deposit_applied_to_invoice_then_partial_payment_ages_at_500(self):
        # Deposit received before the invoice: no allocations, whole receipt is a customer credit.
        self.receipt("deposit", "500.00", [], when="2026-02-03", excess={"id": "deposit-credit", "account": DEPOSITS, "counterparty": "Orbit"})
        self.assertEqual(self.balances()[DEPOSITS], Decimal("-500"))
        self.invoice("invoice-1", "2000.00")
        plan = preview_offset(self.entity, {"operation_id": "apply-deposit", "date": "2026-03-15", "credit_item_id": "deposit-credit",
            "target_item_id": "invoice-1", "amount": "500.00", "reason": "Apply deposit"})
        self.assertEqual({p["account"]: p["amount"] for p in plan["entries"][0]["postings"]}, {DEPOSITS: "500.00", AR: "-500.00"})
        receipt = apply_offset(self.entity, plan)
        before = self.store.content_digest()
        self.assertEqual(apply_offset(self.entity, plan), receipt)
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual((self.remaining("invoice-1"), self.remaining("deposit-credit")), ("1500.00", "0.00"))
        self.assertEqual(self.balances()[DEPOSITS], Decimal(0))
        self.assertEqual(self.balances()[AR], Decimal("1500"))
        self.receipt("part-payment", "1000.00", [("invoice-1", "1000.00")], when="2026-04-01")
        report = reports.report(self.entity, "aging", as_of="2026-05-31")
        orbit, = report["summary"]
        self.assertEqual((orbit["total"], orbit["buckets"]["unapplied_credit"]), ("500.00", "0.00"))
        self.assertEqual([r["aging_amount"] for r in report["rows"]], ["500.00"])
        self.assertEqual(report["rows"][0]["bucket"], "over_90")
        # Historical aging before the application still shows the deposit and the whole invoice.
        earlier = reports.report(self.entity, "aging", as_of="2026-03-10")
        self.assertEqual(sorted((r["bucket"], r["aging_amount"]) for r in earlier["rows"]), [("1_30", "2000.00"), ("unapplied_credit", "500.00")])
        self.assertTrue(all(r["difference"] == "0.00" for r in reports.report(self.entity, "reconciliation", as_of="2026-05-31")["rows"]))

    def test_overpayment_becomes_credit_and_applies_in_same_control_account_by_link_only(self):
        self.invoice("invoice-1", "1000.00")
        self.receipt("overpay", "1200.00", [("invoice-1", "1000.00")],
                     excess={"id": "overpay-credit", "account": AR, "reference": "Overpayment"})
        credit = load_record(self.store, "items", "overpay-credit")
        self.assertEqual((credit["kind"], credit["direction"], credit["counterparty"], credit["remaining_amount"]),
                         ("credit", "credit", "Orbit", "200.00"))
        self.assertEqual(self.balances()[BANK], Decimal("1200"))
        self.assertEqual(self.balances()[AR], Decimal("-200"))
        self.assertEqual(reports.report(self.entity, "aging", as_of="2026-03-31")["rows"][0]["bucket"], "unapplied_credit")
        self.invoice("invoice-2", "500.00", as_of="2026-03-20")
        plan = preview_offset(self.entity, {"operation_id": "apply-overpay", "date": "2026-03-21", "credit_item_id": "overpay-credit",
            "target_item_id": "invoice-2", "amount": "200.00", "reason": "Apply overpayment"})
        self.assertEqual(plan["entries"], [])
        apply_offset(self.entity, plan)
        self.assertEqual(self.balances()[AR], Decimal("300"))
        self.assertEqual((self.remaining("invoice-2"), self.remaining("overpay-credit")), ("300.00", "0.00"))
        self.assertTrue(all(r["difference"] == "0.00" for r in reports.report(self.entity, "reconciliation", as_of="2026-03-31")["rows"]))

    def test_unallocated_new_payment_error_names_the_excess_field(self):
        self.invoice("invoice-1", "1000.00")
        with self.assertRaises(JournalError) as raised:
            self.receipt("short", "1200.00", [("invoice-1", "1000.00")])
        self.assertEqual(raised.exception.code, "unallocated_payment")
        self.assertIn("excess", raised.exception.action)

    def test_vendor_prepayment_applies_to_bill(self):
        self.invoice("bill-1", "300.00", kind="payable", account=AP, counterparty="Vendor",
                     origin={"mode": "new", "source_id": "bill-1-src", "counter_account": EXPENSE})
        self.receipt("supplier-payment", "-400.00", [("bill-1", "300.00")], excess={"id": "prepay-credit", "account": PREPAY})
        credit = load_record(self.store, "items", "prepay-credit")
        self.assertEqual((credit["direction"], credit["counterparty"]), ("debit", "Vendor"))
        self.assertEqual(self.balances()[PREPAY], Decimal("100"))
        self.invoice("bill-2", "250.00", kind="payable", account=AP, counterparty="Vendor", as_of="2026-03-20",
                     origin={"mode": "new", "source_id": "bill-2-src", "counter_account": EXPENSE})
        plan = preview_offset(self.entity, {"operation_id": "apply-prepay", "date": "2026-03-21", "credit_item_id": "prepay-credit",
            "target_item_id": "bill-2", "amount": "100.00", "reason": "Apply prepayment"})
        self.assertEqual({p["account"]: p["amount"] for p in plan["entries"][0]["postings"]}, {AP: "100.00", PREPAY: "-100.00"})
        apply_offset(self.entity, plan)
        self.assertEqual((self.balances()[PREPAY], self.balances()[AP]), (Decimal(0), Decimal("-150")))
        self.assertEqual(self.remaining("bill-2"), "150.00")

    def test_offset_rejects_wrong_counterparty_side_and_overamount(self):
        self.receipt("deposit", "500.00", [], excess={"id": "deposit-credit", "account": DEPOSITS, "counterparty": "Orbit"})
        self.invoice("other-customer", "900.00", counterparty="Delta")
        self.invoice("invoice-1", "900.00")
        base = {"operation_id": "bad", "date": "2026-03-15", "credit_item_id": "deposit-credit", "target_item_id": "invoice-1",
                "amount": "500.00", "reason": "Synthetic"}
        for changes, code in (({"target_item_id": "other-customer"}, "counterparty_mismatch"),
                              ({"amount": "600.00"}, "overallocated_item"),
                              ({"credit_item_id": "invoice-1", "target_item_id": "other-customer"}, "invalid_offset_pair")):
            with self.subTest(code=code), self.assertRaises(JournalError) as raised:
                preview_offset(self.entity, {**base, **changes})
            self.assertEqual(raised.exception.code, code)

    def test_accrued_estimate_has_one_path_the_journal_reversal_date(self):
        journal = {"id": "utility-accrual", "date": "2026-01-31", "description": "Estimated January utilities",
            "estimate_basis": "Synthetic last-three-month average", "reversal_date": "2026-02-01",
            "postings": [{"account": EXPENSE2, "amount": "450.00"}, {"account": "Liabilities:Accrued:Bonus", "amount": "-450.00"}]}
        plan = preview_journal(self.entity, journal)
        self.assertEqual(plan["warnings"], [])
        apply_journal(self.entity, plan)
        self.assertEqual(due_reversals(self.entity, "2026-01-31")["results"], [])
        result = due_reversals(self.entity, "2026-02-01", apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual((self.balances()[EXPENSE2], self.balances()[ACCRUED]), (Decimal(0), Decimal(0)))
        self.assertEqual(list_items(self.entity), [])
        self.invoice("util-bill", "480.00", kind="payable", account=AP, counterparty="Utility", as_of="2026-02-10",
                     origin={"mode": "new", "source_id": "util-src", "counter_account": EXPENSE2})
        self.assertEqual(self.balances()[EXPENSE2], Decimal("480"))
        # The cut concepts are rejected, not silently ignored.
        for removed in ({"reversal_strategy": "reverse"}, {"obligation_ids": []}):
            with self.assertRaises(JournalError):
                preview_journal(self.entity, {**journal, "id": "again", **removed})
        for removed in ({"settlement_strategy": "direct"}, {"reversal_date": "2026-02-01"}, {"obligation_ids": []}):
            with self.assertRaises(JournalError):
                preview_item(self.entity, {"operation_id": "x", "id": "x", "kind": "payable", "account": AP, "currency": "USD",
                    "original_amount": "1.00", "as_of": "2026-01-01", "origin": {"mode": "opening", "source_id": "o"}, **removed})

    def test_true_up_links_estimate_to_actual_and_records_variance(self):
        self.invoice("bonus-estimate", "1000.00", kind="accrual", account=ACCRUED, as_of="2026-03-31", evidence_ids=[],
                     estimate_basis="Synthetic target bonus pool", due_date="2026-04-15",
                     origin={"mode": "new", "source_id": "bonus-est-src", "counter_account": EXPENSE2})
        self.assertEqual(self.balances()[ACCRUED], Decimal("-1000"))
        self.invoice("bonus-actual", "700.00", kind="payable", account=AP, as_of="2026-04-10", counterparty="Staff",
                     origin={"mode": "new", "source_id": "bonus-actual-src", "counter_account": EXPENSE2})
        plan = preview_adjustment(self.entity, {"operation_id": "bonus-true-up", "item_id": "bonus-estimate", "action": "true_up",
            "actual_item_id": "bonus-actual", "date": "2026-04-10", "counter_account": EXPENSE2, "reason": "Actual bonus approved"})
        apply_adjustment(self.entity, plan)
        estimate = load_record(self.store, "items", "bonus-estimate")
        self.assertEqual((estimate["status"], estimate["actual_item_id"], estimate["remaining_amount"]), ("settled", "bonus-actual", "0.00"))
        allocation, = [a for a in list_records(self.store, "allocations") if a.get("actual_item_id")]
        self.assertEqual((allocation["action"], allocation["estimate_amount"], allocation["actual_amount"], allocation["variance"]),
                         ("true_up", "1000.00", "700.00", "-300.00"))
        self.assertEqual((self.balances()[ACCRUED], self.balances()[EXPENSE2]), (Decimal(0), Decimal("700")))
        with self.assertRaises(JournalError) as raised:
            preview_adjustment(self.entity, {"operation_id": "no-link", "item_id": "bonus-actual", "action": "true_up", "date": "2026-04-11",
                "counter_account": EXPENSE2, "reason": "x", "source_id": "s", "evidence_ids": [self.eid], "amount": "1.00"})
        self.assertEqual(raised.exception.code, "actual_item_required")

    def test_split_bill_is_one_operation_with_one_control_balance(self):
        plan = preview_item(self.entity, {"operation_id": "split-create", "id": "split", "kind": "payable", "account": AP, "currency": "USD",
            "original_amount": "100.00", "as_of": "2026-03-01", "evidence_ids": [self.eid], "counterparty": "Vendor",
            "origin": {"mode": "new", "source_id": "split-src", "lines": [
                {"account": EXPENSE, "amount": "60.00"}, {"account": EXPENSE2, "amount": "40.00"}]}})
        self.assertEqual([(p["account"], p["amount"]) for p in plan["entries"][0]["postings"]],
                         [(AP, "-100.00"), (EXPENSE, "60.00"), (EXPENSE2, "40.00")])
        apply_item(self.entity, plan)
        self.assertEqual(self.balances()[AP], Decimal("-100"))
        self.receipt("split-payment", "-100.00", [("split", "100.00")])
        self.assertEqual(self.remaining("split"), "0.00")
        base = {"operation_id": "bad-split", "id": "bad-split", "kind": "payable", "account": AP, "currency": "USD",
                "original_amount": "100.00", "as_of": "2026-03-01", "evidence_ids": [self.eid]}
        for origin, code in (({"mode": "new", "source_id": "b1", "lines": [{"account": EXPENSE, "amount": "99.00"}]}, "lines_total_mismatch"),
                             ({"mode": "new", "source_id": "b2", "counter_account": EXPENSE, "lines": [{"account": EXPENSE, "amount": "100.00"}]}, "invalid_origin")):
            with self.subTest(code=code), self.assertRaises(JournalError) as raised:
                preview_item(self.entity, {**base, "origin": origin})
            self.assertEqual(raised.exception.code, code)

    def test_schema_documents_new_contracts_without_cut_fields(self):
        requests = cli.request_schemas()
        item = requests["item"]["properties"]
        self.assertNotIn("settlement_strategy", item)
        self.assertNotIn("obligation_ids", item)
        self.assertIn("estimate_basis", item)
        self.assertIn("lines", item["origin"]["properties"])
        self.assertIn("true_up", requests["item_adjustment"]["properties"]["action"]["enum"])
        self.assertTrue(requests["item_offset"]["examples"] and requests["settlement"]["examples"])
        self.assertNotIn("item_reversal", requests)
        self.assertNotIn("reversal_strategy", requests["journal"]["properties"])
        self.assertIn("description", requests["journal"]["properties"]["reversal_date"])


if __name__ == "__main__":
    unittest.main()
