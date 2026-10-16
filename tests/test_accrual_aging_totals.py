import unittest
from datetime import date

from bookkeeping.accrual import reports
from bookkeeping.accrual.render import render_report


def item(id, kind, account, remaining, due, signed=None):
    row = {"id": id, "kind": kind, "account": account, "currency": "USD",
           "remaining_amount": remaining, "counterparty": "Acme", "signed_balance": signed}
    if due:
        row["due_date"] = due
    return row


AR, AP = "Assets:Receivable", "Liabilities:Payable"
ITEMS = [
    item("r1", "receivable", AR, "100.00", "2026-06-30"),   # current (due today)
    item("r2", "receivable", AR, "50.50", "2026-06-20"),    # 10 days: 1_30
    item("r3", "receivable", AR, "200.00", "2026-04-15"),   # 76 days: 61_90
    item("r4", "receivable", AR, "25.00", None),            # unknown
    item("c1", "credit", AR, "30.00", None, signed="30.00"),
    item("p1", "payable", AP, "300.00", "2026-07-15"),      # current
    item("p2", "payable", AP, "75.25", "2026-05-20"),       # 41 days: 31_60
    item("p3", "payable", AP, "10.00", "2026-01-01"),       # over_90
]


class AgingTotalsTest(unittest.TestCase):
    def setUp(self):
        self.rows, self.summary = reports.aging(ITEMS, date(2026, 6, 30))
        self.totals = reports.aging_totals(self.rows)

    def test_receivable(self):
        r = self.totals["receivable"]
        self.assertEqual((r["total"], r["overdue_total"], r["count"], r["overdue_count"]),
                         ("375.50", "250.50", 4, 2))
        self.assertEqual(r["buckets"], {"current": "100.00", "1_30": "50.50", "31_60": "0.00",
                                        "61_90": "200.00", "over_90": "0.00", "unknown_due_date": "25.00"})
        self.assertEqual(r["unapplied_credits"], {"total": "30.00", "count": 1})

    def test_payable(self):
        p = self.totals["payable"]
        self.assertEqual((p["total"], p["overdue_total"], p["count"], p["overdue_count"]),
                         ("385.25", "85.25", 3, 2))
        self.assertEqual(p["buckets"], {"current": "300.00", "1_30": "0.00", "31_60": "75.25",
                                        "61_90": "0.00", "over_90": "10.00", "unknown_due_date": "0.00"})
        self.assertEqual(p["unapplied_credits"], {"total": "0.00", "count": 0})

    def test_text_lines(self):
        doc = {"kind": "aging", "as_of": "2026-06-30", "currency": "USD", "rows": self.rows,
               "summary": self.summary, "totals": self.totals, "issues": [],
               "bucket_labels": dict(reports.AGING_BUCKETS)}
        text = render_report(doc)
        self.assertIn("Total receivable | 375.50 | Overdue 250.50 | Items 4 | Overdue items 2 | Unapplied credits 30.00 (separate)", text)
        self.assertIn("Total payable | 385.25 | Overdue 85.25 | Items 3 | Overdue items 2", text)


class CreditSideTest(unittest.TestCase):
    def test_credit_side_follows_direction_not_account(self):
        deposit = item("d1", "credit", "Liabilities:Other:Customer-Deposits", "40.00", None, signed="-40.00")
        deposit["direction"] = "credit"
        prepay = item("v1", "credit", "Assets:Prepaid:Vendor", "15.00", None, signed="15.00")
        prepay["direction"] = "debit"
        rows, _ = reports.aging([deposit, prepay], date(2026, 6, 30))
        totals = reports.aging_totals(rows)
        self.assertEqual(totals["receivable"]["unapplied_credits"], {"total": "40.00", "count": 1})
        self.assertEqual(totals["payable"]["unapplied_credits"], {"total": "15.00", "count": 1})
        self.assertEqual(totals["receivable"]["total"], "0.00")


if __name__ == "__main__":
    unittest.main()
