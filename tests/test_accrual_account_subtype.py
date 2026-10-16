"""Account subtype derivation in `books accrual accounts`."""
import unittest

from tests.test_accrual_schedules import AccrualFixture, EXPENSE
from bookkeeping.accrual import accounts as acct
from bookkeeping.accrual.items import apply_item, preview_item
from bookkeeping.entity import add_account, init_entity, load_entity


class SubtypeTests(AccrualFixture):
    def subtypes(self):
        return {a["name"]: a["subtype"] for a in acct.list_accounts(load_entity(self.root))["accounts"]}

    def test_starter_chart_subtypes_and_role_unchanged(self):
        root = self.root.parent / "starter"
        init_entity(root, name="Synthetic Starter", basis="accrual", cutover_date="2026-01-01")
        result = acct.list_accounts(load_entity(root))
        got = {a["name"]: a for a in result["accounts"]}
        expected = {
            "Assets:Bank:Checking": "bank", "Liabilities:CreditCard": "credit_card",
            "Assets:Receivable:Accounts-Receivable": "receivable", "Assets:Other:Prepaid-Expenses": "prepaid",
            "Assets:Fixed:Equipment": "fixed_asset", "Assets:Fixed:Accumulated-Depreciation": "accumulated_depreciation",
            "Liabilities:Payable:Accounts-Payable": "payable", "Liabilities:Other:Accrued-Liabilities": "accrued_liabilities",
            "Liabilities:Other:Deferred-Revenue": "deferred_revenue", "Liabilities:Other:Customer-Deposits": "customer_deposits",
            "Equity:Opening-Balances": "equity", "Income:Consulting": "income", "Expenses:Software": "expense",
        }
        for name, subtype in expected.items():
            self.assertEqual(got[name]["subtype"], subtype, name)
        self.assertEqual(got["Assets:Receivable:Accounts-Receivable"]["role"], "control")
        self.assertEqual(got["Assets:Bank:Checking"]["role"], "cash_or_bank")
        self.assertTrue(all(a["subtype"] in acct.SUBTYPES for a in result["accounts"]))
        self.assertIn("Subtype", acct.render_accounts(result))

    def test_custom_named_account_used_by_payable_items_is_payable(self):
        custom = "Liabilities:Trade:Vendor-Balances"
        add_account(self.root, custom, open_date="2000-01-01")
        self.assertEqual(self.subtypes()[custom], "other_liability")
        request = {"operation_id": "bill-create", "id": "bill-1", "kind": "payable", "account": custom, "currency": "USD",
                   "original_amount": "100.00", "as_of": "2026-01-01", "evidence_ids": [self.eid],
                   "origin": {"mode": "new", "source_id": "vendor-bill-1", "counter_account": EXPENSE}}
        apply_item(self.entity, preview_item(self.entity, request))
        self.assertEqual(self.subtypes()[custom], "payable")

    def test_prefix_fallback(self):
        names = {
            "Assets:Cash:Petty": "cash", "Assets:Bank:Savings": "bank", "Assets:Receivable:Other": "receivable",
            "Assets:Fixed:Vehicles": "fixed_asset", "Assets:Fixed:Accumulated-Vehicles": "accumulated_depreciation",
            "Assets:Misc": "other_asset", "Liabilities:CreditCard:Amex": "credit_card",
            "Liabilities:Payable:Other": "payable", "Liabilities:Loans": "other_liability",
            "Equity:Retained": "equity", "Income:Sales": "income", "Expenses:Rent": "expense",
        }
        for name, subtype in names.items():
            self.assertEqual(acct._subtype(name, {}), subtype, name)


if __name__ == "__main__":
    unittest.main()
