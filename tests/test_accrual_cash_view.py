"""Golden tests for the cash-basis view derived from accrual books.

Every expected figure is hand-computed in the comments next to it. No test copies
engine output. Every test also asserts the built-in tie-out: accrual net income
minus cash net income equals the reconciling items, to the cent.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bookkeeping import cli
from bookkeeping.accrual import items, journals, opening, schedules
from bookkeeping.accrual.reports import CoverageError
from bookkeeping.entity import add_account, init_entity, load_entity
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.reports import cashview, statements

BANK = "Assets:Bank:Checking"
CARD = "Liabilities:CreditCard"
AR = "Assets:Receivable:Accounts-Receivable"
AP = "Liabilities:Payable:Accounts-Payable"
PREPAID = "Assets:Other:Prepaid-Expenses"
EQUIPMENT = "Assets:Fixed:Equipment"
ACCUM = "Assets:Fixed:Accumulated-Depreciation"
ACCRUED = "Liabilities:Other:Accrued-Liabilities"
DEFERRED = "Liabilities:Other:Deferred-Revenue"
DEPOSITS = "Liabilities:Other:Customer-Deposits"
SERVICES, SUBSCRIPTIONS, SUPPORT = "Income:Services", "Income:Subscriptions", "Income:Support"
UTILITIES, BONUSES, BAD_DEBT = "Expenses:Utilities", "Expenses:Bonuses", "Expenses:Bad-Debt"
INSURANCE, COMMISSIONS, INTEREST = "Expenses:Insurance", "Expenses:Commissions", "Expenses:Interest"
SOFTWARE, DEPRECIATION = "Expenses:Software", "Expenses:Depreciation"
LOAN, ACCRUED_INTEREST, SECURITY = "Liabilities:Other:Loan-Payable", "Liabilities:Other:Accrued-Interest", "Assets:Other:Security-Deposits"
D = Decimal


def label(account: str) -> str:
    return account.replace(":", " › ")


class CashViewFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "acme"
        init_entity(self.root, name="Acme", basis="accrual", cutover_date="2026-01-01")
        for account in (SERVICES, SUBSCRIPTIONS, SUPPORT, UTILITIES, BONUSES, BAD_DEBT, INSURANCE, COMMISSIONS, INTEREST,
                        LOAN, ACCRUED_INTEREST, SECURITY):
            add_account(self.root, account, open_date="2025-12-31")
        self.entity = load_entity(self.root)
        self.store = LedgerStore(self.root / "ledger.sqlite")

    # ---- posting helpers -------------------------------------------------
    def ev(self, name: str) -> str:
        return journals.intake_evidence(self.entity, {"schema_version": 1, "source": {
            "namespace": "example.synthetic", "account_id": "acme", "object_id": name, "version": "1"},
            "currency": "USD", "observed_facts": {"document": name}})["id"]

    def item(self, item_id: str, kind: str, account: str, amount: str, when: str, who: str, *, counter: str | None = None,
             lines: list | None = None, **extra):
        origin = {"mode": "new", "source_id": item_id + "-src"}
        if lines:
            origin["lines"] = [{"account": a, "amount": n} for a, n in lines]
        else:
            origin["counter_account"] = counter
        request = {"operation_id": "create-" + item_id, "id": item_id, "kind": kind, "account": account, "currency": "USD",
                   "original_amount": amount, "as_of": when, "counterparty": who, "origin": origin, "evidence_ids": [self.ev(item_id)], **extra}
        return items.apply_item(self.entity, items.preview_item(self.entity, request))

    def settle(self, op: str, when: str, amount: str, allocations: list, *, bank: str = BANK, excess: dict | None = None):
        request = {"operation_id": op, "date": when, "source": {"mode": "new", "source_id": op, "bank_account": bank, "amount": amount},
                   "allocations": [{"item_id": i, "amount": a} for i, a in allocations], "evidence_ids": [self.ev(op)]}
        if excess:
            request["excess"] = excess
        return items.apply_settlement(self.entity, items.preview_settlement(self.entity, request))

    def adjust(self, op: str, item_id: str, action: str, amount: str, when: str, counter: str, **extra):
        request = {"operation_id": op, "item_id": item_id, "action": action, "amount": amount, "date": when,
                   "counter_account": counter, "reason": "Synthetic reason", "evidence_ids": [self.ev(op)], **extra}
        return items.apply_adjustment(self.entity, items.preview_adjustment(self.entity, request))

    def offset(self, op: str, when: str, credit: str, target: str, amount: str):
        request = {"operation_id": op, "date": when, "credit_item_id": credit, "target_item_id": target, "amount": amount,
                   "reason": "Apply deposit", "evidence_ids": [self.ev(op)]}
        return items.apply_offset(self.entity, items.preview_offset(self.entity, request))

    def schedule(self, sid: str, kind: str, account: str, recognition: str, amount: str, start: str, end: str, method: str,
                 funding: str, paid: str | None = None, **extra):
        origin = {"mode": "new", "source_id": sid + "-payment", "funding_account": funding}
        if paid:
            origin["date"] = paid
        request = {"operation_id": "create-" + sid, "id": sid, "kind": kind, "account": account, "recognition_account": recognition,
                   "currency": "USD", "amount": amount, "service_start": start, "service_end": end, "method": method,
                   "origin": origin, "evidence_ids": [self.ev(sid)], **extra}
        return schedules.apply_schedule(self.entity, schedules.preview_schedule(self.entity, request))

    def due(self, through: str):
        return schedules.apply_due(self.entity, schedules.preview_due(self.entity, through, operation_id="due-" + through))

    def change(self, op: str, sid: str, action: str, when: str, **extra):
        request = {"operation_id": op, "id": sid, "action": action, "date": when, "reason": "Synthetic change",
                   "evidence_ids": [self.ev(op)], **extra}
        return schedules.apply_change(self.entity, schedules.preview_change(self.entity, request))

    def journal(self, jid: str, when: str, postings: list, **extra):
        request = {"id": jid, "date": when, "description": "Synthetic " + jid,
                   "postings": [{"account": a, "amount": n} for a, n in postings], **extra}
        if "estimate_basis" not in extra:
            request["evidence_ids"] = [self.ev(jid)]
        return journals.apply_journal(self.entity, journals.preview_journal(self.entity, request))

    def open_books(self):
        (self.root / "evidence").mkdir(exist_ok=True)
        (self.root / "evidence/open-items.pdf").write_bytes(b"%PDF synthetic opening items")
        request = {"operation_id": "opening", "cutover": "2026-01-01", "evidence_refs": ["evidence/open-items.pdf"],
                   "lines": [{"account": BANK, "amount": "30000.00"}, {"account": AR, "amount": "300.00"},
                             {"account": AP, "amount": "-100.00"}, {"account": "Equity:Opening-Balances", "amount": "-30200.00"}],
                   "items": [{"id": "open-inv", "kind": "receivable", "account": AR, "counterparty": "Delta", "reference": "INV-1",
                              "due_date": "2025-12-15", "open_amount": "300.00"},
                             {"id": "open-bill", "kind": "payable", "account": AP, "counterparty": "Hosting Co", "reference": "BILL-9",
                              "due_date": "2026-01-10", "open_amount": "100.00"}]}
        plan = opening.preview_opening(self.entity, request)
        self.assertEqual(plan["status"], "ready", plan)
        opening.apply_opening(self.entity, plan)
        self.entity = load_entity(self.root)

    # ---- assertions ------------------------------------------------------
    def cash(self, start: str, end: str):
        """The cash view, after asserting its tie-out and that the accrual side equals the accrual report."""
        result = statements.profit_and_loss(self.root, date.fromisoformat(start), date.fromisoformat(end), basis="cash")
        tie = result.meta["tie_out"]
        self.assertTrue(tie["ties"])
        self.assertEqual(D(tie["accrual_net_income"]) - D(tie["cash_net_income"]), D(tie["reconciling_total"]))
        self.assertEqual(D(tie["difference"]), D(tie["reconciling_total"]))
        self.assertEqual(D(tie["cash_net_income"]), result.totals["net_income"])
        accrual = statements.profit_and_loss(self.root, date.fromisoformat(start), date.fromisoformat(end))
        self.assertEqual(D(tie["accrual_net_income"]), accrual.totals["net_income"])
        self.assertEqual(result.meta["view_label"], "Cash basis (derived from accrual books)")
        return result

    def groups(self, result) -> dict[str, Decimal]:
        return {r["key"]: D(r["amount"]) for r in result.meta["tie_out"]["reconciling_items"] if D(r["amount"])}

    def rows(self, result, section: int) -> dict[str, Decimal]:
        return {r["label"]: r["amount"] for r in result.sections[section]["rows"]}

    def revenue(self, result) -> dict[str, Decimal]:
        return self.rows(result, 0)

    def expenses(self, result) -> dict[str, Decimal]:
        return self.rows(result, 1)

    def accrual_net(self, start: str, end: str) -> Decimal:
        return statements.profit_and_loss(self.root, date.fromisoformat(start), date.fromisoformat(end)).totals["net_income"]


class InvoiceAndBillTests(CashViewFixture):
    def test_split_invoice_paid_in_parts_recognizes_each_line_pro_rata_and_ends_exact(self):
        # Invoice 1,000.00: Services 700.00, Support 300.00. Cents: 70000 and 30000 of 100000.
        self.item("inv", "receivable", AR, "1000.00", "2026-01-05", "Cust", lines=[(SERVICES, "700.00"), (SUPPORT, "300.00")])
        # Payment 1 on Jan 20: 333.33 = 33333 cents. Services floor(70000*33333/100000)=floor(23333.1)=23333; Support = 33333-23333 = 10000.
        self.settle("pay1", "2026-01-20", "333.33", [("inv", "333.33")])
        # Payment 2 on Feb 10: 333.33. Cumulative 66666: Services floor(46666.2)=46666, Support 66666-46666=20000.
        # This month: Services 46666-23333 = 23333; Support 20000-10000 = 10000.
        self.settle("pay2", "2026-02-10", "333.33", [("inv", "333.33")])
        # Payment 3 on Mar 3: 333.34, fully settled. Cumulative 100000: Services 70000, Support 30000.
        # This month: Services 70000-46666 = 23334; Support 30000-20000 = 10000.
        self.settle("pay3", "2026-03-03", "333.34", [("inv", "333.34")])
        jan, feb, mar = self.cash("2026-01-01", "2026-01-31"), self.cash("2026-02-01", "2026-02-28"), self.cash("2026-03-01", "2026-03-31")
        self.assertEqual(self.revenue(jan), {label(SERVICES): D("233.33"), label(SUPPORT): D("100.00")})
        self.assertEqual(self.revenue(feb), {label(SERVICES): D("233.33"), label(SUPPORT): D("100.00")})
        self.assertEqual(self.revenue(mar), {label(SERVICES): D("233.34"), label(SUPPORT): D("100.00")})
        quarter = self.cash("2026-01-01", "2026-03-31")
        # Cumulative per line equals the invoice line exactly: 233.33+233.33+233.34 = 700.00; 100*3 = 300.00.
        self.assertEqual(self.revenue(quarter), {label(SERVICES): D("700.00"), label(SUPPORT): D("300.00")})
        # January: accrual income 1,000.00, cash 333.33; the invoice raised receivables by 1,000.00 - 333.33 = 666.67.
        self.assertEqual((D(jan.meta["tie_out"]["accrual_net_income"]), jan.totals["net_income"]), (D("1000.00"), D("333.33")))
        self.assertEqual(self.groups(jan), {"receivables": D("666.67")})
        # Whole quarter: the invoice is fully collected, so accrual and cash agree and nothing reconciles.
        self.assertEqual(self.groups(quarter), {})

    def test_residual_cent_lands_on_the_last_line(self):
        # Invoice 100.00 over three lines: 33.34, 33.33, 33.33 (3334, 3333, 3333 cents of 10000).
        for account in ("Income:Alpha", "Income:Beta", "Income:Gamma"):
            add_account(self.root, account, open_date="2025-12-31")
        self.item("thirds", "receivable", AR, "100.00", "2026-01-02", "Cust",
                  lines=[("Income:Alpha", "33.34"), ("Income:Beta", "33.33"), ("Income:Gamma", "33.33")])
        # Payment 10.00 = 1000 cents. Alpha floor(3334*1000/10000)=floor(333.4)=333; Beta floor(333.3)=333; Gamma 1000-333-333 = 334.
        self.settle("part", "2026-01-15", "10.00", [("thirds", "10.00")])
        result = self.cash("2026-01-01", "2026-01-31")
        self.assertEqual(self.revenue(result), {label("Income:Alpha"): D("3.33"), label("Income:Beta"): D("3.33"), label("Income:Gamma"): D("3.34")})
        self.assertEqual(result.sections[0]["total"], D("10.00"))

    def test_one_payment_for_two_invoices_and_a_bill_with_two_expense_lines(self):
        self.item("inv-a", "receivable", AR, "600.00", "2026-01-03", "Cust", counter=SERVICES)
        self.item("inv-b", "receivable", AR, "400.00", "2026-01-04", "Cust", counter=SUPPORT)
        self.item("bill", "payable", AP, "250.00", "2026-01-06", "Vendor", lines=[(SOFTWARE, "100.00"), (UTILITIES, "150.00")])
        self.settle("receipt", "2026-01-12", "1000.00", [("inv-a", "600.00"), ("inv-b", "400.00")])
        # Bill 250: half paid on Feb 2 (125.00 = 12500 of 25000): Software floor(10000*12500/25000)=5000; Utilities 12500-5000=7500.
        self.settle("half", "2026-02-02", "-125.00", [("bill", "125.00")])
        self.settle("rest", "2026-02-20", "-125.00", [("bill", "125.00")])
        jan = self.cash("2026-01-01", "2026-01-31")
        self.assertEqual(self.revenue(jan), {label(SERVICES): D("600.00"), label(SUPPORT): D("400.00")})
        # The unpaid bill is a payable of 250.00: accrual net income 1000-250 = 750, cash 1000, difference -250.
        self.assertEqual(self.expenses(jan), {})
        self.assertEqual(self.groups(jan), {"payables": D("-250.00")})
        feb = self.cash("2026-02-01", "2026-02-28")
        # Second payment: cumulative 25000: Software 10000, Utilities 15000. Month total = 250.00: Software 100.00, Utilities 150.00.
        self.assertEqual(self.expenses(feb), {label(SOFTWARE): D("100.00"), label(UTILITIES): D("150.00")})
        self.assertEqual(self.groups(feb), {"payables": D("250.00")})

    def test_invoice_posted_as_an_existing_entry_uses_the_lines_of_that_entry(self):
        add_account(self.root, "Income:Extra", open_date="2025-12-31")
        journals_request = [(AR, "1000.00"), (SERVICES, "-600.00"), ("Income:Extra", "-400.00")]
        self.journal("plain-invoice", "2026-01-04", journals_request)
        request = {"operation_id": "reg", "id": "plain-inv", "kind": "receivable", "account": AR, "currency": "USD", "original_amount": "1000.00",
                   "as_of": "2026-01-04", "counterparty": "Cust", "origin": {"mode": "existing", "source_id": "accrual:plain-invoice"},
                   "evidence_ids": [self.ev("reg")]}
        items.apply_item(self.entity, items.preview_item(self.entity, request))
        self.settle("pay", "2026-02-01", "1000.00", [("plain-inv", "1000.00")])
        jan, feb = self.cash("2026-01-01", "2026-01-31"), self.cash("2026-02-01", "2026-02-28")
        self.assertEqual(self.revenue(jan), {})
        self.assertEqual(self.groups(jan), {"receivables": D("1000.00")})
        # The entry's own lines are 600 and 400 of 1000.
        self.assertEqual(self.revenue(feb), {label(SERVICES): D("600.00"), label("Income:Extra"): D("400.00")})


class OpeningItemTests(CashViewFixture):
    def test_opening_receivable_and_payable_show_on_their_own_lines(self):
        self.open_books()
        self.settle("collect", "2026-01-10", "300.00", [("open-inv", "300.00")])
        self.settle("pay-bill", "2026-01-20", "-100.00", [("open-bill", "100.00")])
        result = self.cash("2026-01-01", "2026-01-31")
        # Revenue 300.00 (no category), expense 100.00 (no category), cash net income 200.00. Accrual income and expense are 0.
        self.assertEqual(self.revenue(result), {cashview.OPENING_RECEIVABLES: D("300.00")})
        self.assertEqual(self.expenses(result), {cashview.OPENING_PAYABLES: D("100.00")})
        self.assertEqual(result.totals["net_income"], D("200.00"))
        self.assertEqual(D(result.meta["tie_out"]["accrual_net_income"]), D("0.00"))
        # Accrual 0 minus cash 200 = -200 = -300 (receivable collected) + 100 (payable paid), shown as one opening line.
        self.assertEqual(self.groups(result), {"opening": D("-200.00")})
        self.assertEqual([r["special"] for r in result.sections[0]["rows"]], ["opening_receivables"])
        self.assertIn("Collected from opening receivables", result.to_text())

    def test_partial_opening_collection_is_shown_once_per_payment(self):
        self.open_books()
        self.settle("c1", "2026-01-10", "100.00", [("open-inv", "100.00")])
        self.settle("c2", "2026-02-10", "200.00", [("open-inv", "200.00")])
        jan, feb = self.cash("2026-01-01", "2026-01-31"), self.cash("2026-02-01", "2026-02-28")
        self.assertEqual(self.revenue(jan), {cashview.OPENING_RECEIVABLES: D("100.00")})
        self.assertEqual(self.revenue(feb), {cashview.OPENING_RECEIVABLES: D("200.00")})
        self.assertEqual(self.groups(feb), {"opening": D("-200.00")})


class AdjustmentTests(CashViewFixture):
    def test_write_off_has_no_cash_view_effect(self):
        # Ridge invoice 300 on Jan 10, written off Mar 15 to bad debt.
        self.item("ridge", "receivable", AR, "300.00", "2026-01-10", "Ridge", counter=SERVICES)
        self.adjust("wo", "ridge", "writeoff", "300.00", "2026-03-15", BAD_DEBT)
        jan, mar = self.cash("2026-01-01", "2026-01-31"), self.cash("2026-03-01", "2026-03-31")
        for result in (jan, mar):
            self.assertEqual((self.revenue(result), self.expenses(result), result.totals["net_income"]), ({}, {}, D("0.00")))
        # January: accrual income 300, cash 0, receivables +300. March: accrual bad debt -300, cash 0, write-off line -300.
        self.assertEqual(self.groups(jan), {"receivables": D("300.00")})
        self.assertEqual(self.groups(mar), {"writeoffs": D("-300.00")})
        # Over Jan to Mar the two lines cancel: receivable +300 when invoiced, write-off -300.
        whole = self.cash("2026-01-01", "2026-03-31")
        self.assertEqual(self.groups(whole), {"receivables": D("300.00"), "writeoffs": D("-300.00")})
        self.assertEqual(D(whole.meta["tie_out"]["reconciling_total"]), D("0.00"))

    def test_credit_memo_applied_to_an_invoice_reduces_cash_income_only_through_the_payment(self):
        # Invoice 1,000. Credit memo 100 (a credit item with an income counter) applied; the customer pays the remaining 900.
        self.item("inv", "receivable", AR, "1000.00", "2026-01-05", "Cust", counter=SERVICES)
        self.item("memo", "credit", DEPOSITS, "100.00", "2026-01-10", "Cust", counter=SERVICES, direction="credit")
        self.offset("apply-memo", "2026-01-11", "memo", "inv", "100.00")
        self.settle("pay", "2026-01-20", "900.00", [("inv", "900.00")])
        result = self.cash("2026-01-01", "2026-01-31")
        # Cash: 900 received, all Services. Accrual: 1000 - 100 = 900. They agree.
        self.assertEqual(self.revenue(result), {label(SERVICES): D("900.00")})
        # Receivable: +1000 invoice - 900 payment = +100. The memo is a credit of 100 (deposits group -100); applying it nets to 0.
        self.assertEqual(self.groups(result), {"receivables": D("100.00"), "customer_deposits": D("-100.00")})
        self.assertEqual(D(result.meta["tie_out"]["difference"]), D("0.00"))

    def test_true_up_of_three_estimates_against_one_actual_bill(self):
        # Three accrual items of 1,000 for bonuses (Jan 31, Feb 28, Mar 31). The actual bill is 2,700 on Mar 31, paid Apr 10.
        for index, when in enumerate(("2026-01-31", "2026-02-28", "2026-03-31"), start=1):
            self.item(f"est{index}", "accrual", ACCRUED, "1000.00", when, "Staff", counter=BONUSES, estimate_basis="Plan 1,000 a month", evidence_ids=[])
        self.item("actual", "payable", AP, "2700.00", "2026-03-31", "Payroll provider", counter=BONUSES)
        for index in (1, 2, 3):
            self.adjust(f"trueup{index}", f"est{index}", "true_up", "1000.00", "2026-03-31", BONUSES, actual_item_id="actual")
        self.settle("bonus-paid", "2026-04-10", "-2700.00", [("actual", "2700.00")])
        quarter = self.cash("2026-01-01", "2026-03-31")
        # Cash view Q1: no bonus is paid, so no expense. Accrual Q1: 1000+1000+1000-3000+2700 = 2,700 expense.
        self.assertEqual((self.expenses(quarter), quarter.totals["net_income"]), ({}, D("0.00")))
        self.assertEqual(D(quarter.meta["tie_out"]["accrual_net_income"]), D("-2700.00"))
        # Reconciling: accrued -3000 (estimates), true-ups +3000, payable -2700.
        self.assertEqual(self.groups(quarter), {"accrued": D("-3000.00"), "writeoffs": D("3000.00"), "payables": D("-2700.00")})
        april = self.cash("2026-04-01", "2026-04-30")
        self.assertEqual(self.expenses(april), {label(BONUSES): D("2700.00")})
        self.assertEqual(self.groups(april), {"payables": D("2700.00")})


class EstimateTests(CashViewFixture):
    def test_utility_accrual_reversed_by_the_real_bill_follows_the_bill_rule(self):
        # Jan 31: accrue 450, reversing Feb 1. Feb 10: real bill 480, paid Feb 25. Feb 28: accrue 450 again, reversing Mar 1.
        # Mar 10: real bill 460, paid Apr 6.
        self.journal("util-jan", "2026-01-31", [(UTILITIES, "450.00"), (ACCRUED, "-450.00")], estimate_basis="Average of last 3 bills", reversal_date="2026-02-01")
        journals.due_reversals(self.entity, "2026-02-01", apply=True)
        self.item("bill-feb", "payable", AP, "480.00", "2026-02-10", "Power Co", counter=UTILITIES)
        self.settle("pay-feb", "2026-02-25", "-480.00", [("bill-feb", "480.00")])
        self.journal("util-feb", "2026-02-28", [(UTILITIES, "450.00"), (ACCRUED, "-450.00")], estimate_basis="Average of last 3 bills", reversal_date="2026-03-01")
        journals.due_reversals(self.entity, "2026-03-01", apply=True)
        self.item("bill-mar", "payable", AP, "460.00", "2026-03-10", "Power Co", counter=UTILITIES)
        self.settle("pay-mar", "2026-04-06", "-460.00", [("bill-mar", "460.00")])
        jan, feb, mar, apr = (self.cash(a, b) for a, b in (("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28"),
                                                           ("2026-03-01", "2026-03-31"), ("2026-04-01", "2026-04-30")))
        # Cash view: estimates and their reversals are excluded; the real bill is expense when paid.
        self.assertEqual(self.expenses(jan), {})
        self.assertEqual(self.expenses(feb), {label(UTILITIES): D("480.00")})
        self.assertEqual(self.expenses(mar), {})
        self.assertEqual(self.expenses(apr), {label(UTILITIES): D("460.00")})
        # Jan accrual -450, cash 0: accrued -450.
        self.assertEqual(self.groups(jan), {"accrued": D("-450.00")})
        # Feb accrual: reversal +450, bill -480, new estimate -450 = -480; cash -480; no difference (payable +480-480, accrued +450-450).
        self.assertEqual(D(feb.meta["tie_out"]["accrual_net_income"]), D("-480.00"))
        self.assertEqual(self.groups(feb), {})
        # Mar accrual: reversal +450, bill -460 = -10; cash 0; payable -460, accrued +450.
        self.assertEqual(self.groups(mar), {"payables": D("-460.00"), "accrued": D("450.00")})
        self.assertEqual(self.groups(apr), {"payables": D("460.00")})
        # Estimate journals are excluded by the cash view, so nothing is flagged as possibly non-cash.
        for result in (jan, feb, mar, apr):
            self.assertEqual([i for i in result.meta["unresolved_issues"] if i["code"] == "cash_view_noncash_entry"], [])
            self.assertNotIn("may not be cash", result.to_text())

    def test_hand_built_accrual_in_a_ledger_only_account_is_shown_as_recorded(self):
        # Loan 50,000 at 6%: interest 250 a month into Accrued-Interest (no item, schedule or estimate journal), paid in March.
        self.journal("loan", "2026-01-05", [(BANK, "50000.00"), (LOAN, "-50000.00")])
        for index, when in enumerate(("2026-01-31", "2026-02-28", "2026-03-31"), start=1):
            self.journal(f"int{index}", when, [(INTEREST, "250.00"), (ACCRUED_INTEREST, "-250.00")])
        self.journal("int-paid", "2026-03-31", [(ACCRUED_INTEREST, "750.00"), (BANK, "-750.00")])
        result = self.cash("2026-01-01", "2026-03-31")
        # Documented limit: 3 x 250 = 750 of interest, as recorded; the loan proceeds are not income.
        self.assertEqual(self.expenses(result), {label(INTEREST): D("750.00")})
        self.assertEqual(self.revenue(result), {})
        self.assertEqual(self.groups(result), {})
        # Each of the three accrual entries is flagged: 3 issues of 250.00, 750.00 in all. The payment entry has no income or expense.
        flagged = [i for i in result.meta["unresolved_issues"] if i["code"] == "cash_view_noncash_entry"]
        self.assertEqual([(i["date"], i["amount"]) for i in flagged],
                         [("2026-01-31", "250.00"), ("2026-02-28", "250.00"), ("2026-03-31", "250.00")])
        self.assertEqual(flagged[0]["accounts"], [INTEREST, ACCRUED_INTEREST])
        self.assertNotIn("refs", flagged[0])
        self.assertEqual(result.meta["noncash_entries_total"], "750.00")
        text = result.to_text()
        self.assertIn("Includes $750.00 of entries that may not be cash - see the list", text)
        self.assertIn("Entries that may not be cash", text)
        # Books ask mentions it.
        answer = statements.ask(self.root, "What was our cash-basis profit for Q1 2026?", today=date(2026, 3, 31))
        self.assertIn("includes $750.00 of entries that may not be cash", answer.answer_text)
        # A period with no such entry has no note.
        quiet = statements.ask(self.root, "What was our cash-basis profit for April 2026?", today=date(2026, 4, 30))
        self.assertNotIn("may not be cash", quiet.answer_text)

    def test_security_deposit_asset_never_reaches_the_profit_and_loss(self):
        self.journal("sec", "2026-01-15", [(SECURITY, "3000.00"), (BANK, "-3000.00")])
        self.journal("sec-back", "2026-04-30", [(BANK, "3000.00"), (SECURITY, "-3000.00")])
        result = self.cash("2026-01-01", "2026-04-30")
        self.assertEqual((self.revenue(result), self.expenses(result), result.totals["net_income"]), ({}, {}, D("0.00")))
        self.assertEqual(self.groups(result), {})


class ScheduleTests(CashViewFixture):
    def test_deferred_revenue_is_income_on_the_receipt_date(self):
        # BigCo pays 12,000 on Jan 15 for Jan 15 2026 to Jan 14 2027 (365 days), actual_day.
        self.schedule("bigco", "deferred", DEFERRED, SUBSCRIPTIONS, "12000.00", "2026-01-15", "2027-01-14", "actual_day", BANK)
        self.due("2026-03-31")
        jan, feb, mar = self.cash("2026-01-01", "2026-01-31"), self.cash("2026-02-01", "2026-02-28"), self.cash("2026-03-01", "2026-03-31")
        # Cash: the whole 12,000.00 in January; later months have no income.
        self.assertEqual(self.revenue(jan), {label(SUBSCRIPTIONS): D("12000.00")})
        self.assertEqual((self.revenue(feb), self.revenue(mar)), ({}, {}))
        # Accrual January: 12000*17/365 = 558.904 -> 558.90 (17 days, Jan 15 to Jan 31). Difference 558.90 - 12000 = -11,441.10 deferred.
        self.assertEqual(D(jan.meta["tie_out"]["accrual_net_income"]), D("558.90"))
        self.assertEqual(self.groups(jan), {"deferred_revenue": D("-11441.10")})
        # Accrual February: 12000*28/365 = 920.547 -> 920.54 earned, so deferred falls 920.54.
        self.assertEqual(self.groups(feb), {"deferred_revenue": D("920.54")})

    def test_milestone_custom_schedule_is_income_when_paid(self):
        # Customer pays 9,000 on Feb 1; milestones 2,700 (Feb 20), 2,700 (Mar 20), 3,600 (Apr 20).
        self.schedule("milestones", "deferred", DEFERRED, SERVICES, "9000.00", "2026-02-01", "2026-04-30", "custom", BANK,
                      installments=[{"date": "2026-02-20", "amount": "2700.00"}, {"date": "2026-03-20", "amount": "2700.00"},
                                    {"date": "2026-04-20", "amount": "3600.00"}])
        self.due("2026-04-30")
        feb, mar, apr = (self.cash(a, b) for a, b in (("2026-02-01", "2026-02-28"), ("2026-03-01", "2026-03-31"), ("2026-04-01", "2026-04-30")))
        self.assertEqual(self.revenue(feb), {label(SERVICES): D("9000.00")})
        self.assertEqual((self.revenue(mar), self.revenue(apr)), ({}, {}))
        # Accrual: Feb 2700, Mar 2700, Apr 3600. Deferred changes: Feb -9000+2700, Mar +2700, Apr +3600.
        self.assertEqual(self.groups(feb), {"deferred_revenue": D("-6300.00")})
        self.assertEqual(self.groups(mar), {"deferred_revenue": D("2700.00")})
        self.assertEqual(self.groups(apr), {"deferred_revenue": D("3600.00")})

    def test_retainer_refund_reverses_income_on_the_refund_date(self):
        # Nova pays 3,000 on Feb 1 for Feb to Apr (equal_month, 1,000 a month). February is recognized; the March month is refunded Mar 31.
        self.schedule("nova", "deferred", DEFERRED, SERVICES, "3000.00", "2026-02-01", "2026-04-30", "equal_month", BANK)
        self.due("2026-02-28")
        self.change("nova-refund", "nova", "refund", "2026-03-31", amount="1000.00", offset_account=BANK)
        self.due("2026-04-30")
        feb, mar, apr = (self.cash(a, b) for a, b in (("2026-02-01", "2026-02-28"), ("2026-03-01", "2026-03-31"), ("2026-04-01", "2026-04-30")))
        self.assertEqual(self.revenue(feb), {label(SERVICES): D("3000.00")})
        # Refund of 1,000 in cash on Mar 31 is -1,000 income. Net cash income Feb to Apr = 3000 - 1000 = 2000.
        self.assertEqual(self.revenue(mar), {label(SERVICES): D("-1000.00")})
        self.assertEqual(self.revenue(apr), {})
        self.assertEqual(self.revenue(self.cash("2026-02-01", "2026-04-30")), {label(SERVICES): D("2000.00")})
        # Accrual: Feb 1000, Mar 0, Apr 1000 = 2000 total, so over the whole period nothing is left to reconcile.
        self.assertEqual(self.groups(self.cash("2026-02-01", "2026-04-30")), {})

    def test_card_funded_software_cancelled_with_refund(self):
        # Annual software 1,200 on the card Jan 1 for calendar 2026 (100 a month). Service ends Apr 30; 800 is refunded to the card.
        self.schedule("saas", "prepaid", PREPAID, SOFTWARE, "1200.00", "2026-01-01", "2026-12-31", "equal_month", CARD)
        self.due("2026-03-31")
        self.change("saas-end", "saas", "terminate", "2026-04-30", effective_date="2026-04-30", offset_account=CARD)
        jan, apr = self.cash("2026-01-01", "2026-01-31"), self.cash("2026-04-01", "2026-04-30")
        # Cash view: the 1,200 charge is expense on Jan 1; the 800 refund reverses it on Apr 30.
        self.assertEqual(self.expenses(jan), {label(SOFTWARE): D("1200.00")})
        self.assertEqual(self.expenses(apr), {label(SOFTWARE): D("-800.00")})
        # Jan: accrual -100, cash -1200: prepaid +1200-100 = +1100. Apr: accrual -100, cash +800: prepaid -100-800 = -900.
        self.assertEqual(self.groups(jan), {"prepaid": D("1100.00")})
        self.assertEqual(self.groups(apr), {"prepaid": D("-900.00")})
        whole = self.cash("2026-01-01", "2026-04-30")
        # Four months of 100 = 400 on both bases.
        self.assertEqual(self.expenses(whole), {label(SOFTWARE): D("400.00")})
        self.assertEqual(self.groups(whole), {})

    def test_insurance_with_a_rate_increase_is_expense_when_paid(self):
        # Q1 policy 1,500 paid Jan 2 (Jan to Mar, equal_month). Endorsement 100 paid Feb 16 (Feb 16 to Mar 31, actual_day).
        # Q2 policy 1,800 paid Apr 1 (Apr to Jun).
        self.schedule("q1", "prepaid", PREPAID, INSURANCE, "1500.00", "2026-01-01", "2026-03-31", "equal_month", BANK, paid="2026-01-02")
        self.schedule("endorse", "prepaid", PREPAID, INSURANCE, "100.00", "2026-02-16", "2026-03-31", "actual_day", BANK)
        self.schedule("q2", "prepaid", PREPAID, INSURANCE, "1800.00", "2026-04-01", "2026-06-30", "equal_month", BANK)
        self.due("2026-04-30")
        months = [self.cash(a, b) for a, b in (("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28"),
                                               ("2026-03-01", "2026-03-31"), ("2026-04-01", "2026-04-30"))]
        self.assertEqual([self.expenses(m) for m in months],
                         [{label(INSURANCE): D("1500.00")}, {label(INSURANCE): D("100.00")}, {}, {label(INSURANCE): D("1800.00")}])
        # Accrual: Jan 500; Feb 500 + floor(10000*13/44)=2954 cents = 529.54; Mar 500 + (10000-2954) = 570.46; Apr 600.
        self.assertEqual([D(m.meta["tie_out"]["accrual_net_income"]) for m in months],
                         [D("-500.00"), D("-529.54"), D("-570.46"), D("-600.00")])
        # Prepaid changes: Jan +1500-500; Feb +100-529.54; Mar -570.46; Apr +1800-600.
        self.assertEqual([self.groups(m) for m in months],
                         [{"prepaid": D("1000.00")}, {"prepaid": D("-429.54")}, {"prepaid": D("-570.46")}, {"prepaid": D("1200.00")}])

    def test_laptop_depreciation_stays_as_recorded_and_the_purchase_is_not_expense(self):
        # Laptop 2,400 paid Jan 15; 24 months from Feb 1: 100 a month.
        self.schedule("laptop", "depreciation", EQUIPMENT, DEPRECIATION, "2400.00", "2026-02-01", "2028-01-31", "equal_month", BANK,
                      paid="2026-01-15", contra_account=ACCUM)
        self.due("2026-04-30")
        jan = self.cash("2026-01-01", "2026-01-31")
        self.assertEqual((self.expenses(jan), jan.totals["net_income"]), ({}, D("0.00")))
        quarter = self.cash("2026-02-01", "2026-04-30")
        # Convention: depreciation as recorded, 3 x 100.00 = 300.00. Equipment stays an asset.
        self.assertEqual(self.expenses(quarter), {label(DEPRECIATION): D("300.00")})
        self.assertEqual(self.groups(quarter), {})


class DepositTests(CashViewFixture):
    def test_deposit_then_invoice_offset_and_partial_payment(self):
        # Feb 10: Vega pays a 500 deposit. Mar 1: invoice 2,000. Mar 5: the deposit is applied. Apr 10: Vega pays 1,000.
        self.settle("deposit", "2026-02-10", "500.00", [], excess={"id": "dep", "account": DEPOSITS, "counterparty": "Vega"})
        self.item("vega-inv", "receivable", AR, "2000.00", "2026-03-01", "Vega", counter=SERVICES)
        self.offset("apply-dep", "2026-03-05", "dep", "vega-inv", "500.00")
        self.settle("vega-pay", "2026-04-10", "1000.00", [("vega-inv", "1000.00")])
        feb, mar, apr = (self.cash(a, b) for a, b in (("2026-02-01", "2026-02-28"), ("2026-03-01", "2026-03-31"), ("2026-04-01", "2026-04-30")))
        # Feb: 500 received, income on the "not yet applied" line. Mar: 500 moves to Services (500 - 500 = 0 total).
        # Apr: cash 1,000 = 1000/2000 of the line, but the 500 deposit already counted: cumulative settled 500+1000 = 1500; Apr gets 1000.
        self.assertEqual(self.revenue(feb), {cashview.UNAPPLIED_CUSTOMER: D("500.00")})
        self.assertEqual(self.revenue(mar), {label(SERVICES): D("500.00"), cashview.UNAPPLIED_CUSTOMER: D("-500.00")})
        self.assertEqual(mar.totals["net_income"], D("0.00"))
        self.assertEqual(self.revenue(apr), {label(SERVICES): D("1000.00")})
        # Cash received 500 + 1000 = 1,500 in total; Services income 500 + 1000 = 1,500.
        whole = self.cash("2026-02-01", "2026-04-30")
        self.assertEqual(self.revenue(whole), {label(SERVICES): D("1500.00")})
        # Feb: deposit liability +500 (credit): -500. Mar: invoice +2000 receivable; the application nets to 0 across its two accounts.
        # Apr: receivable -1000.
        self.assertEqual(self.groups(feb), {"customer_deposits": D("-500.00")})
        self.assertEqual(self.groups(mar), {"receivables": D("2000.00")})
        self.assertEqual(self.groups(apr), {"receivables": D("-1000.00")})
        # Over the whole period: accrual 2,000, cash 1,500, difference 500. Receivables +2000-1000 = +1000, deposit credit -500.
        self.assertEqual(self.groups(whole), {"receivables": D("1000.00"), "customer_deposits": D("-500.00")})
        self.assertEqual(D(whole.meta["tie_out"]["difference"]), D("500.00"))

    def test_unapplied_overpayment_is_income_on_the_unapplied_line_and_a_refund_reverses_it(self):
        # Customer overpays 250 on Jan 8 (no invoice). It is refunded Jan 28 (a payment against the credit item).
        self.settle("over", "2026-01-08", "250.00", [], excess={"id": "over-credit", "account": DEPOSITS, "counterparty": "Cust"})
        self.settle("refund", "2026-01-28", "-250.00", [("over-credit", "250.00")])
        january = self.cash("2026-01-01", "2026-01-31")
        self.assertEqual((self.revenue(january), january.totals["net_income"]), ({}, D("0.00")))
        first_half = self.cash("2026-01-01", "2026-01-20")
        self.assertEqual(self.revenue(first_half), {cashview.UNAPPLIED_CUSTOMER: D("250.00")})
        self.assertEqual(self.groups(first_half), {"customer_deposits": D("-250.00")})

    def test_supplier_prepayment_applied_to_a_bill(self):
        # Pay a supplier 400 in advance Jan 5, bill 1,000 arrives Jan 20 and the prepayment is applied Jan 21; the rest is paid Feb 3.
        self.settle("prepay", "2026-01-05", "-400.00", [], excess={"id": "prepay-credit", "account": AP, "counterparty": "Vendor"})
        self.item("v-bill", "payable", AP, "1000.00", "2026-01-20", "Vendor", counter=SOFTWARE)
        self.offset("apply-prepay", "2026-01-21", "prepay-credit", "v-bill", "400.00")
        self.settle("v-pay", "2026-02-03", "-600.00", [("v-bill", "600.00")])
        jan, feb = self.cash("2026-01-01", "2026-01-31"), self.cash("2026-02-01", "2026-02-28")
        # January: 400 paid then applied: expense Software 400, "not yet applied" line nets to 0. Cash expense 400.
        self.assertEqual(self.expenses(jan), {label(SOFTWARE): D("400.00")})
        self.assertEqual(self.expenses(feb), {label(SOFTWARE): D("600.00")})
        # Accrual January: bill 1000 expense, cash 400: payable 1000 - 400 prepayment effect = -600 net.
        self.assertEqual(self.groups(jan), {"payables": D("-600.00")})
        self.assertEqual(self.groups(feb), {"payables": D("600.00")})


class UnclassifiedAndFailureTests(CashViewFixture):
    def test_cash_through_a_control_account_with_nothing_to_explain_it_is_flagged_not_hidden(self):
        # A deposits account is registered by a credit item, then a plain journal moves 100 of cash through it with no item.
        self.settle("dep", "2026-01-05", "50.00", [], excess={"id": "dep", "account": DEPOSITS, "counterparty": "Cust"})
        self.journal("stray", "2026-01-12", [(BANK, "100.00"), (DEPOSITS, "-100.00")])
        result = self.cash("2026-01-01", "2026-01-31")
        self.assertEqual(result.sections[2]["rows"][0]["amount"], D("100.00"))
        self.assertEqual(result.sections[2]["rows"][0]["label"], cashview.UNCLASSIFIED)
        # Net income = 50.00 on the "not yet applied" line + 100.00 unclassified; accrual 0; deposits -150.
        self.assertEqual(result.totals["net_income"], D("150.00"))
        self.assertEqual(self.groups(result), {"customer_deposits": D("-150.00")})
        self.assertTrue([i for i in result.meta["unresolved_issues"] if i.get("code") == "cash_view_unclassified"])
        self.assertIn(cashview.UNCLASSIFIED, result.to_text())

    def test_a_view_that_does_not_tie_fails_with_no_figures(self):
        self.settle("deposit", "2026-02-10", "500.00", [], excess={"id": "dep", "account": DEPOSITS, "counterparty": "Vega"})
        self.item("vega-inv", "receivable", AR, "2000.00", "2026-03-01", "Vega", counter=SERVICES)
        self.offset("apply-dep", "2026-03-05", "dep", "vega-inv", "500.00")
        real = cashview._cumulative
        # A bug that drops one cent from the line shares of an application must be caught by the tie-out.
        broken = lambda weights, settled: [x - (1 if settled and i == 0 else 0) for i, x in enumerate(real(weights, settled))]
        with patch.object(cashview, "_cumulative", broken):
            with self.assertRaisesRegex(cashview.CashViewError, "does not tie"):
                statements.profit_and_loss(self.root, date(2026, 3, 1), date(2026, 3, 31), basis="cash")
            err, out = StringIO(), StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                rc = cli.main(["report", "pnl", "--entity", str(self.root), "--from", "2026-03-01", "--to", "2026-03-31", "--basis", "cash"])
        self.assertEqual(rc, 1)
        self.assertIn("No cash-basis figures are shown", err.getvalue())
        self.assertEqual(out.getvalue(), "")


class CoverageAndOutputTests(CashViewFixture):
    def post_sale(self):
        self.item("inv", "receivable", AR, "500.00", "2026-01-10", "Cust", counter=SERVICES)
        self.settle("pay", "2026-02-05", "500.00", [("inv", "500.00")])

    def run_cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["report", "pnl", "--entity", str(self.root), *args])
        return rc, out.getvalue(), err.getvalue()

    def test_range_before_the_cutover_names_the_first_available_date(self):
        self.post_sale()
        with self.assertRaises(CoverageError) as caught:
            statements.profit_and_loss(self.root, date(2025, 12, 1), date(2026, 1, 31), basis="cash")
        self.assertEqual((caught.exception.first_date, caught.exception.field), ("2026-01-01", "from"))
        rc, out, err = self.run_cli("--from", "2025-12-01", "--to", "2026-01-31", "--basis", "cash")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("The first available date is 2026-01-01", err)
        self.assertIn("--basis cash", err)
        self.assertIn("--from 2026-01-01", err)

    def test_text_json_and_file_export_all_say_cash_basis_derived_from_accrual(self):
        self.post_sale()
        args = ("--from", "2026-01-01", "--to", "2026-02-28", "--basis", "cash")
        rc, text, _ = self.run_cli(*args)
        self.assertEqual(rc, 0)
        self.assertIn("Profit and Loss - Cash basis (derived from accrual books)", text.splitlines()[0])
        self.assertIn("Tie-out to the accrual books", text)
        self.assertIn("A credit card charge counts as paid on the charge date (cash-basis tax rule). Paying the card balance later is not an expense.", text)
        self.assertIn("ties to the cent", text)
        rc, raw, _ = self.run_cli(*args, "--format", "json")
        document = json.loads(raw)
        self.assertEqual(document["meta"]["view_label"], "Cash basis (derived from accrual books)")
        self.assertTrue(document["meta"]["tie_out"]["ties"])
        self.assertEqual(document["totals"]["net_income"], "500.00")
        ids = [rule["id"] for rule in document["meta"]["method"]["rules"]]
        for rule in ("invoices_and_bills", "rounding", "deposits", "prepaid", "deferred", "depreciation", "estimates", "opening_items", "credit_card"):
            self.assertIn(rule, ids)
        target = Path(self.tmp.name) / "cash-pnl.txt"
        rc, _, _ = self.run_cli(*args, "--output", str(target))
        self.assertIn("Cash basis (derived from accrual books)", target.read_text())

    def test_workbook_statement_metadata_names_the_cash_view(self):
        from bookkeeping.reports import workbook
        self.post_sale()
        result = self.cash("2026-01-01", "2026-02-28")
        rows = workbook._with_statement_metadata([], result)
        self.assertIn(["Report metadata", "Basis", "Cash basis (derived from accrual books)"], rows)

    def test_basis_accrual_and_default_show_the_accrual_report_unchanged(self):
        self.post_sale()
        _, default, _ = self.run_cli("--from", "2026-01-01", "--to", "2026-01-31", "--format", "json")
        _, explicit, _ = self.run_cli("--from", "2026-01-01", "--to", "2026-01-31", "--format", "json", "--basis", "accrual")
        self.assertEqual(default, explicit)
        self.assertEqual(json.loads(default)["totals"]["net_income"], "500.00")
        self.assertNotIn("view_label", json.loads(default)["meta"])

    def test_the_cash_view_writes_nothing(self):
        self.post_sale()
        before = self.store.content_digest()
        self.cash("2026-01-01", "2026-02-28")
        self.assertEqual(self.store.content_digest(), before)

    def test_cash_books_keep_their_report_and_refuse_an_accrual_view(self):
        root = Path(self.tmp.name) / "cashco"
        init_entity(root, name="Cash Co", basis="cash")
        default = statements.profit_and_loss(root, date(2026, 1, 1), date(2026, 1, 31))
        explicit = statements.profit_and_loss(root, date(2026, 1, 1), date(2026, 1, 31), basis="cash")
        self.assertEqual(default.to_json(), explicit.to_json())
        with self.assertRaisesRegex(ValueError, "Cash books keep no data for an accrual view"):
            statements.profit_and_loss(root, date(2026, 1, 1), date(2026, 1, 31), basis="accrual")
        err, out = StringIO(), StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            rc = cli.main(["report", "pnl", "--entity", str(root), "--from", "2026-01-01", "--to", "2026-01-31", "--basis", "accrual"])
        self.assertEqual((rc, out.getvalue()), (1, ""))
        self.assertIn("Cash books keep no data for an accrual view", err.getvalue())


class AskCashViewTests(CashViewFixture):
    def test_cash_question_on_accrual_books_is_answered_from_the_cash_view(self):
        # Invoice 500 Jan 10, paid Feb 5. Bill 200 Jan 12 unpaid.
        self.item("inv", "receivable", AR, "500.00", "2026-01-10", "Cust", counter=SERVICES)
        self.item("bill", "payable", AP, "200.00", "2026-01-12", "Vendor", counter=SOFTWARE)
        self.settle("pay", "2026-02-05", "500.00", [("inv", "500.00")])
        today = date(2026, 2, 28)
        january = statements.ask(self.root, "What was our cash-basis profit for January 2026?", today=today)
        # January cash: nothing received or paid.
        self.assertTrue(january.answer_text.startswith("Answer covers: cash basis (derived from accrual books), from 2026-01-01 to 2026-01-31."))
        self.assertEqual(january.data["net_income"], "0.00")
        self.assertEqual((january.data["basis"], january.data["books_basis"]), ("cash", "accrual"))
        february = statements.ask(self.root, "How much cash did we receive from customers in February 2026?", today=today)
        self.assertEqual(february.data["total_revenue"], "500.00")
        self.assertIn("Revenue received (cash basis)", february.answer_text)
        spent = statements.ask(self.root, "How much cash did we spend on software in January 2026?", today=today)
        self.assertEqual(spent.data["total_expenses"], "0.00")
        accrual = statements.ask(self.root, "What was our net income for January 2026?", today=today)
        self.assertEqual(accrual.data["net_income"], "300.00")
        self.assertTrue(accrual.answer_text.startswith("Answer covers: accrual basis, from 2026-01-01"))

    def test_cash_question_before_the_cutover_raises_the_coverage_error(self):
        with self.assertRaises(CoverageError):
            statements.ask(self.root, "What was our cash profit for December 2025?", today=date(2026, 2, 28))

    def test_balance_style_cash_questions_are_not_answered_and_cash_books_refuse_accrual(self):
        refused = statements.ask(self.root, "What do we owe on a cash basis?", today=date(2026, 2, 28))
        self.assertEqual(refused.data["unavailable"], "cash_view_profit_and_loss_only")
        root = Path(self.tmp.name) / "cashco"
        init_entity(root, name="Cash Co", basis="cash")
        no = statements.ask(root, "What is our accrual basis profit for January 2026?", today=date(2026, 2, 28))
        self.assertEqual(no.data["unavailable"], "basis_not_kept")
        self.assertIn("Cash books keep no invoices, bills, prepaid or deferred records", no.answer_text)


if __name__ == "__main__":
    unittest.main()
