"""books ask routing on accrual books, true-up variance visibility and the mid-month refund error text."""
from datetime import date
import json
import unittest

from bookkeeping.accrual import items
from bookkeeping.accrual.schedules import apply_schedule, preview_change, preview_schedule
from bookkeeping.accrual.journals import JournalError
from bookkeeping.reports import statements
from tests.test_accrual_schedules import AP, AR, BANK, EXPENSE, INCOME, AccrualFixture

TODAY = date(2026, 3, 31)


class AskRoutingTests(AccrualFixture):
    def setUp(self):
        super().setUp()
        self.post("sale", "2026-01-10", [(AR, "500.00"), (INCOME, "-500.00")])
        self.post("cost", "2026-03-02", [(EXPENSE, "100.00"), (BANK, "-100.00")])
        apply_schedule(self.entity, preview_schedule(self.entity, self.schedule()))

    def ask(self, question):
        return statements.ask(self.root, question, today=TODAY)

    def test_help_examples_all_work(self):
        examples = ["What was Q1 revenue?", "How much did we earn in January?", "How much did we spend on software last quarter?",
                    "What were our total expenses this year?", "What were owner draws this month?",
                    "How much did we pay Acme last quarter?", "How are we doing this year?", "What is our net income for Q2?"]
        for question in examples:
            with self.subTest(question=question):
                result = self.ask(question)
                self.assertNotEqual(result.intent, "unknown")
                self.assertTrue(result.answer_text.startswith("Answer covers: accrual basis, from "))

    def test_earnings_and_profit_use_the_profit_and_loss(self):
        result = self.ask("How much did we earn in January?")
        self.assertIn("Recognized revenue", result.answer_text)
        self.assertEqual((result.data["from_date"], result.data["to_date"]), ("2026-01-01", "2026-01-31"))
        self.assertEqual(self.ask("What did we earn in March?").data["from_date"], "2026-03-01")

    def test_payables_and_receivables_answer_from_aging_totals(self):
        request = {"operation_id": "invoice-create", "id": "invoice-1", "kind": "receivable", "account": AR, "currency": "USD",
                   "original_amount": "1000.00", "as_of": "2026-01-01", "due_date": "2026-02-01", "counterparty": "Client",
                   "origin": {"mode": "new", "source_id": "inv-1", "counter_account": INCOME}, "evidence_ids": [self.evidence("inv-1")]}
        items.apply_item(self.entity, items.preview_item(self.entity, request))
        for question in ("Who owes us money?", "Show receivables aging", "What is owed to us?"):
            with self.subTest(question=question):
                result = self.ask(question)
                self.assertEqual(result.intent, "aging")
                self.assertIn("receivables", result.answer_text)
                self.assertEqual(result.data["totals"]["receivable"]["total"], "1000.00")
                self.assertNotIn("payable", result.data["totals"])
                self.assertTrue(result.answer_text.startswith("Answer covers: accrual basis, from 2026-03-31 to 2026-03-31."))
        for question in ("What do we owe?", "What are our unpaid bills?"):
            with self.subTest(question=question):
                result = self.ask(question)
                self.assertEqual(result.data["totals"]["payable"]["total"], "0.00")
                self.assertIn("payables", result.answer_text)
        self.assertEqual(set(self.ask("Show the aging").data["totals"]), {"payable", "receivable"})

    def test_prepaid_and_deferred_use_schedule_balances(self):
        prepaid = self.ask("How much is prepaid?")
        self.assertEqual(prepaid.intent, "schedules")
        self.assertIn("1,200.00", prepaid.answer_text)
        self.assertIn("Prepaid expenses", prepaid.answer_text)
        deferred = self.ask("What is our deferred revenue?")
        self.assertIn("Deferred revenue", deferred.answer_text)
        self.assertEqual(deferred.data["schedules"], [])

    def test_balance_sheet(self):
        result = self.ask("What is the balance sheet?")
        self.assertEqual(result.intent, "balance_sheet")
        self.assertIn("Balance sheet as of 2026-03-31", result.answer_text)
        self.assertTrue(result.answer_text.startswith("Answer covers: accrual basis"))

    def test_cash_flow_question_never_gets_an_accrual_figure(self):
        for question in ("How much cash did we spend on software in March?", "How much cash did we receive from customers in March?"):
            with self.subTest(question=question):
                result = self.ask(question)
                self.assertEqual(result.data["unavailable"], "basis_not_kept")
                self.assertIn("books report general-ledger", result.answer_text)
                self.assertNotIn("Recognized", result.answer_text)

    def test_unknown_question_lists_what_can_be_answered(self):
        result = self.ask("What is the meaning of life?")
        self.assertEqual(result.intent, "unknown")
        for text in ("What do we owe?", "Who owes us money?", "How much is prepaid?", "What is the balance sheet?", "How much did we earn in January?"):
            self.assertIn(text, result.answer_text)


class CashBooksAskTests(unittest.TestCase):
    def test_cash_books_keep_working(self):
        import tempfile
        from pathlib import Path
        from bookkeeping.entity import init_entity
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cash"
            init_entity(root, basis="cash")
            earn = statements.ask(root, "How much did we earn in January?", today=TODAY)
            self.assertEqual(earn.intent, "revenue")
            self.assertTrue(earn.answer_text.startswith("Answer covers: cash basis, from 2026-01-01 to 2026-01-31."))
            spent = statements.ask(root, "How much cash did we spend on software in March?", today=TODAY)
            self.assertNotEqual(spent.intent, "unavailable_basis")
            owe = statements.ask(root, "What do we owe?", today=TODAY)
            self.assertEqual(owe.data["unavailable"], "accrual_tracking_required")
            self.assertEqual(statements.ask(root, "What is the balance sheet?", today=TODAY).intent, "balance_sheet")


class MidMonthRefundTests(AccrualFixture):
    def test_error_explains_terminate_and_supersede_with_example(self):
        apply_schedule(self.entity, preview_schedule(self.entity, self.schedule()))
        change = {"operation_id": "refund", "id": "annual", "action": "refund", "date": "2026-03-15", "amount": "110.00",
                  "offset_account": BANK, "reason": "Synthetic", "evidence_ids": [self.evidence("refund")]}
        with self.assertRaises(JournalError) as caught:
            preview_change(self.entity, change)
        error = caught.exception
        text = str(error) + " " + str(getattr(error, "action", "")) + " " + " ".join(str(a) for a in error.args)
        self.assertIn("incomplete_calendar_month", repr(error) + text + getattr(error, "code", ""))
        for needle in ("terminate", "effective_date", "last service day", "supersede", "daily proration"):
            self.assertIn(needle, text)
        self.assertIn('"action": "terminate"', text)


class TrueUpVariancePreviewTests(AccrualFixture):
    def test_preview_and_receipt_show_estimate_actual_and_variance(self):
        from bookkeeping.accrual.store import operation_receipt
        accrued = "Liabilities:Accrued:Bonus"
        from bookkeeping.entity import add_account, load_entity
        add_account(self.root, accrued, open_date="2000-01-01")
        self.entity = load_entity(self.root)
        estimate = {"operation_id": "est", "id": "bonus-est", "kind": "accrual", "account": accrued, "currency": "USD",
                    "original_amount": "3000.00", "as_of": "2026-03-31", "due_date": "2026-04-30",
                    "origin": {"mode": "new", "source_id": "est-src", "counter_account": EXPENSE}, "evidence_ids": [self.evidence("est")],
                    "estimate_basis": "Synthetic plan"}
        actual = {"operation_id": "act", "id": "bonus-act", "kind": "payable", "account": AP,
                  "currency": "USD", "original_amount": "2700.00", "as_of": "2026-04-05", "due_date": "2026-05-05", "counterparty": "Staff",
                  "origin": {"mode": "new", "source_id": "act-src", "counter_account": EXPENSE}, "evidence_ids": [self.evidence("act")]}
        for request in (estimate, actual):
            items.apply_item(self.entity, items.preview_item(self.entity, request))
        adjust = {"operation_id": "trueup", "item_id": "bonus-est", "action": "true_up", "actual_item_id": "bonus-act",
                  "date": "2026-04-05", "counter_account": EXPENSE, "reason": "Bill approved"}
        plan = items.preview_adjustment(self.entity, adjust)
        fields = {"actual_item_id": "bonus-act", "estimate_amount": "3000.00", "actual_amount": "2700.00", "variance": "-300.00"}
        self.assertEqual(plan["details"]["true_up"], fields)
        self.assertNotIn("true_up_variance", [w["code"] for w in plan["warnings"]])
        receipt = items.apply_adjustment(self.entity, plan)
        self.assertEqual(receipt["details"]["true_up"], fields)
        self.assertNotIn("true_up_variance", [w["code"] for w in receipt["warnings"]])
        stored = operation_receipt(self.store, "trueup")["receipt"]
        self.assertEqual(stored, receipt)
        json.dumps(receipt)



if __name__ == "__main__":
    unittest.main()
