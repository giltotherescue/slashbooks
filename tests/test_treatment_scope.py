"""Which bank activity is held for accrual treatment, by discipline."""
from datetime import date
from decimal import Decimal
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_accrual_schedules import AccrualFixture, BANK, AP, AR, EXPENSE, INCOME, PREPAID
from bookkeeping.accrual import scope, treatment
from bookkeeping.accrual.items import apply_item, preview_item
from bookkeeping.accrual.schedules import apply_schedule, preview_schedule
from bookkeeping.entity import add_account, init_entity, load_entity
from bookkeeping.ledger.importer import _load_pending_categorization, import_transactions
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.queue import confirm, eligible_for_autopost, make_categorizer, propose
from bookkeeping.routine import routine_plan
from bookkeeping.stripe import stripe_plan

START = "2026-03-01"
MAPPINGS = {"checking": BANK}


def row(sid, description, amount, when="2026-03-10", **changes):
    return {"id": sid, "date": when, "amount": amount, "accountId": "checking", "accountName": "Checking",
            "accountType": "checking", "description": description, **changes}


def categorizer(txn):
    return (EXPENSE, "auto")


def set_config(root, **changes):
    path = root / "entity.json"
    config = json.loads(path.read_text())
    config.update(changes)
    if "bank_account_mappings" not in changes:
        config["bank_account_mappings"] = MAPPINGS
    path.write_text(json.dumps(config))
    return load_entity(root)


class CashCompanyTests(unittest.TestCase):
    def test_cash_company_never_needs_treatment_and_auto_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "co"
            init_entity(root, basis="cash")
            add_account(root, BANK, open_date="2000-01-01")
            add_account(root, EXPENSE, open_date="2000-01-01")
            entity = set_config(root)
            self.assertEqual(scope.discipline(entity), "off")
            self.assertFalse(treatment.needs_treatment(entity, row("a", "Acme Supplies", "-500.00")))
            result = import_transactions(entity, [row("a", "Acme Supplies", "-500.00")], "s", categorizer=categorizer)
            self.assertEqual((result.new_entries, result.pending_categorization), (1, 0))


class AccrualFixtureBase(AccrualFixture):
    def bill(self, amount="500.00", name="Acme Supplies", when="2026-03-01", ident="bill-1"):
        request = {"operation_id": ident, "id": ident, "kind": "payable", "account": AP, "original_amount": amount,
                   "as_of": when, "counterparty": name, "reference": "Invoice 7",
                   "origin": {"mode": "new", "source_id": "src-" + ident, "counter_account": EXPENSE},
                   "evidence_ids": [self.evidence("ev-" + ident)]}
        apply_item(self.entity, preview_item(self.entity, request))

    def reload(self, **changes):
        self.entity = set_config(self.root, **changes)
        return self.entity


class AllDisciplineTests(AccrualFixtureBase):
    def test_accrual_all_holds_everything_as_before(self):
        self.reload()
        self.assertEqual(scope.discipline(self.entity), "all")
        self.assertTrue(treatment.needs_treatment(self.entity, row("x", "Coffee Shop", "-4.50")))
        result = import_transactions(self.entity, [row("x", "Coffee Shop", "-4.50")], "s", categorizer=categorizer)
        self.assertEqual((result.new_entries, result.pending_categorization), (0, 1))
        self.assertTrue(_load_pending_categorization(self.entity)[0]["accrual_review_required"])
        self.assertFalse(eligible_for_autopost(self.entity, "COFFEE SHOP", row("x", "Coffee Shop", "-4.50")))
        with self.assertRaisesRegex(ValueError, "Cash-only Stripe"):
            stripe_plan(self.entity, {}, {})


class PerAccountTests(AccrualFixtureBase):
    def setUp(self):
        super().setUp()
        self.bill()
        self.reload(accrual_tracking_start=START)

    def test_discipline_is_per_account(self):
        self.assertEqual(scope.discipline(self.entity), "per_account")

    def test_payment_matching_open_bill_is_held_not_posted(self):
        for sid, description, amount in (("p1", "ACH PMT ACME SUPPLIES 4432", "-500.00"),
                                         ("p2", "Acme Supplies", "-125.00"),
                                         ("p3", "Mystery Vendor", "-500.00")):
            with self.subTest(sid=sid):
                self.assertTrue(treatment.needs_treatment(self.entity, row(sid, description, amount)))
        result = import_transactions(self.entity, [row("p1", "ACH PMT ACME SUPPLIES 4432", "-500.00")], "s",
                                     categorizer=categorizer)
        self.assertEqual((result.new_entries, result.pending_categorization), (0, 1))
        self.assertTrue(_load_pending_categorization(self.entity)[0]["accrual_review_required"])
        self.assertNotIn(Decimal("500.00"), [p.amount for e in self.store.load_entries() for p in e.postings
                                              if p.account == BANK])

    def test_unrelated_card_charge_auto_posts_like_cash(self):
        txn = row("c1", "Coffee Shop", "-4.50")
        self.assertFalse(treatment.needs_treatment(self.entity, txn))
        result = import_transactions(self.entity, [txn], "s", categorizer=categorizer)
        self.assertEqual((result.new_entries, result.pending_categorization), (1, 0))
        entry = next(e for e in self.store.load_entries() if e.source_id == "c1")
        self.assertNotIn("accrual-treatment", dict(entry.meta))

    def test_transaction_before_tracking_start_is_unaffected(self):
        txn = row("old", "ACME SUPPLIES", "-500.00", when="2026-02-27")
        self.assertFalse(treatment.needs_treatment(self.entity, txn))
        result = import_transactions(self.entity, [txn], "s", categorizer=categorizer)
        self.assertEqual((result.new_entries, result.pending_categorization), (1, 0))

    def test_settled_bill_no_longer_matches(self):
        from bookkeeping.accrual.items import apply_settlement, preview_settlement
        apply_settlement(self.entity, preview_settlement(self.entity, {
            "operation_id": "settle", "date": "2026-03-05", "source": {
                "mode": "new", "source_id": "pay-1", "bank_account": BANK, "amount": "-500.00"},
            "allocations": [{"item_id": "bill-1", "amount": "500.00"}], "evidence_ids": [self.evidence("pay")]}))
        self.assertFalse(treatment.needs_treatment(self.entity, row("later", "ACME SUPPLIES", "-500.00", "2026-03-20")))

    def test_source_owned_by_a_record_needs_treatment(self):
        self.assertTrue(treatment.needs_treatment(self.entity, row("src-bill-1", "Unrelated", "-1.00")))

    def test_schedule_funding_match_by_amount(self):
        request = self.schedule(origin={"mode": "new", "source_id": "annual-payment", "funding_account": BANK},
                                amount="1200.00", service_start="2026-03-01", service_end="2027-02-28")
        apply_schedule(self.entity, preview_schedule(self.entity, request))
        self.assertTrue(treatment.needs_treatment(self.entity, row("s1", "Software Annual", "-1200.00")))

    def test_autopost_gate_uses_transaction(self):
        propose_row = row("c2", "Coffee Shop", "-4.50")
        self.assertIsInstance(eligible_for_autopost(self.entity, "COFFEE SHOP", propose_row), bool)
        self.assertFalse(eligible_for_autopost(self.entity, "ACME SUPPLIES", row("c3", "Acme Supplies", "-9.00")))
        self.assertFalse(eligible_for_autopost(self.entity, "COFFEE SHOP"))  # no transaction: stays held
        self.assertEqual(make_categorizer(self.entity)(propose_row), ("", "queue"))

    def test_confirm_needs_explicit_treatment_only_for_matching_item(self):
        import_transactions(self.entity, [row("m1", "Acme Supplies", "-500.00"), row("m2", "Coffee Shop", "-4.50")],
                            "s", intake_only=True)
        propose(self.entity, "m2", EXPENSE, "ordinary spend")
        confirm(self.entity, "m2", "sess")
        entry = next(e for e in self.store.load_entries() if e.source_id == "m2")
        self.assertNotIn("accrual-treatment", dict(entry.meta))
        propose(self.entity, "m1", EXPENSE, "possible settlement")
        with self.assertRaisesRegex(ValueError, "explicit treatment"):
            confirm(self.entity, "m1", "sess")

    def test_routine_holds_matching_and_plans_unrelated(self):
        import_transactions(self.entity, [row("r1", "Acme Supplies", "-500.00"), row("r2", "Service 1234", "-12.00")],
                            "s", intake_only=True)
        policy = dict(version=1, enabled=True, rules=[
            dict(id="rule-a", source_account_id="checking", counterparty_key="ACME SUPPLIES", category=EXPENSE,
                 approved_ledger_account=BANK),
            dict(id="rule-b", source_account_id="checking", counterparty_key="SERVICE", category=EXPENSE,
                 approved_ledger_account=BANK)], review_categories=[EXPENSE], answer_roles=["client"])
        plan = routine_plan(self.entity, policy)
        self.assertEqual([a["source_id"] for a in plan["actions"]], ["r2"])
        self.assertEqual([(u["source_id"], u["reason"]) for u in plan["unresolved"]],
                         [("r1", "accrual_treatment_required")])


class StripePerAccountTests(AccrualFixtureBase):
    def test_stripe_charge_matching_open_invoice_is_held_other_planned(self):
        from tests.test_stripe import evidence, CLEARING, TRANSIT, FEES, REFUNDS, INCOME as S_INCOME
        for account in (CLEARING, TRANSIT, FEES, REFUNDS, S_INCOME):
            add_account(self.root, account, open_date="2000-01-01")
        policy = dict(version=1, enabled=True, stripe_account_id="acct_test", livemode=False, currency="USD",
            clearing_account=CLEARING, payout_transit_account=TRANSIT, fee_account=FEES, refund_account=REFUNDS,
            revenue_rules=[dict(id="services", match={"metadata": {"object": "charge", "key": "stream", "value": "service"}},
                                category=S_INCOME)])
        entity = set_config(self.root, accrual_tracking_start=START,
                            bank_account_mappings={"acct_test": CLEARING, "checking": BANK})
        raw = evidence()
        raw["balanceTransactions"][0]["created"] = 1773014400  # 2026-03-09
        plan = stripe_plan(entity, raw, policy)
        self.assertEqual(len(plan["actions"]), 1, plan["unresolved"])
        # An open 100.00 invoice makes the same charge a possible settlement.
        request = {"operation_id": "inv", "id": "inv", "kind": "receivable", "account": AR, "original_amount": "100.00",
                   "as_of": "2026-03-01", "counterparty": "Customer",
                   "origin": {"mode": "new", "source_id": "inv-src", "counter_account": INCOME},
                   "evidence_ids": [self.evidence("inv-ev")]}
        apply_item(self.entity, preview_item(self.entity, request))
        plan = stripe_plan(entity, raw, policy)
        self.assertEqual(plan["actions"], [])
        self.assertEqual([u["reason"] for u in plan["unresolved"]], ["accrual_treatment_required"])


if __name__ == "__main__":
    unittest.main()
