"""Accrual import ownership, explicit treatment and unchanged cash regression."""
from decimal import Decimal
import json
import unittest
from unittest.mock import Mock

from tests.test_accrual_schedules import AccrualFixture, BANK, AR, EXPENSE, INCOME
from bookkeeping.accrual.items import preview_item, apply_item, preview_settlement, apply_settlement
from bookkeeping.accrual.store import SupportingMutation
from bookkeeping.entity import load_entity
from bookkeeping.ledger.importer import import_transactions, _atomic_ledger_write, _load_pending_categorization
from bookkeeping.queue import propose, confirm, eligible_for_autopost
from bookkeeping.routine import routine_plan
from bookkeeping.stripe import stripe_plan, stripe_apply


def bank_row(source_id="bank-row", **changes):
    row = {"id": source_id, "date": "2026-01-15", "amount": "-100.00", "accountId": "checking",
           "accountName": "Checking", "accountType": "checking", "description": "Synthetic software"}
    row.update(changes)
    return row


class AccrualImportTests(AccrualFixture):
    def test_learned_categorizer_cannot_post_accrual_activity_or_trust_provider_treatment(self):
        categorizer = Mock(return_value=(EXPENSE, "high"))
        row = bank_row(accrual_treatment="new_cash_event", proposed_treatment="new income", authorized=True)
        result = import_transactions(self.entity, [row], "automatic", categorizer)
        self.assertEqual((result.new_entries, result.pending_categorization), (0, 1))
        categorizer.assert_not_called()
        self.assertTrue(_load_pending_categorization(self.entity)[0]["accrual_review_required"])
        self.assertFalse(eligible_for_autopost(self.entity, "SYNTHETIC SOFTWARE"))

    def test_explicit_current_period_cash_treatment_remains_available_and_recorded(self):
        import_transactions(self.entity, [bank_row()], "intake")
        propose(self.entity, "bank-row", EXPENSE, "Explicitly reviewed current-period service")
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "explicit treatment"):
            confirm(self.entity, "bank-row", "implicit")
        self.assertEqual(self.store.content_digest(), before)
        confirm(self.entity, "bank-row", "reviewed", accrual_treatment="new_cash_event")
        self.assertEqual(self.balances()[EXPENSE], Decimal("100"))
        self.assertEqual(self.balances()[BANK], Decimal("-100"))
        self.assertEqual(dict(self.store.load_entries()[-1].meta)["accrual-treatment"], "new_cash_event")
        self.assertEqual(import_transactions(self.entity, [bank_row()], "retry").skipped_duplicate, 1)

    def test_bank_intake_settlement_uses_staged_source_once_without_revenue(self):
        self.post("opening", "2025-12-31", [(AR, "1000"), ("Equity:Opening-Balances", "-1000")])
        item = {"operation_id": "opening-item", "id": "invoice", "kind": "receivable", "account": AR, "original_amount": "1000.00",
                "as_of": "2026-01-01", "origin": {"mode": "opening", "source_id": "opening"}, "settlement_strategy": "direct"}
        apply_item(self.entity, preview_item(self.entity, item))
        row = bank_row(amount="400.00")
        import_transactions(self.entity, [row], "intake", lambda _: (INCOME, "high"))
        settlement = {"operation_id": "link-payment", "date": row["date"], "source": {
            "mode": "new", "source_id": row["id"], "bank_account": BANK, "amount": row["amount"]},
            "allocations": [{"item_id": "invoice", "amount": "400.00"}], "evidence_ids": [self.evidence("payment")]}
        changed = {**settlement, "source": {**settlement["source"], "amount": "401.00"}, "allocations": [{"item_id": "invoice", "amount": "401.00"}]}
        with self.assertRaisesRegex(ValueError, "staged source"):
            preview_settlement(self.entity, changed)
        plan = preview_settlement(self.entity, settlement)
        apply_settlement(self.entity, plan)
        self.assertEqual(_load_pending_categorization(self.entity), [])
        self.assertEqual(import_transactions(self.entity, [row], "retry").skipped_duplicate, 1)
        self.assertNotIn(INCOME, self.balances())
        self.assertEqual(self.balances()[AR], Decimal("600"))

    def test_ordinary_confirm_cannot_cross_migrated_coverage_or_closed_period(self):
        _atomic_ledger_write(self.entity, [], [], "coverage", None, "Synthetic coverage",
            supporting_mutations=[SupportingMutation("coverage", "opening", {"id": "opening", "cutover_date": "2026-02-01"})])
        import_transactions(self.entity, [bank_row()], "stage")
        propose(self.entity, "bank-row", EXPENSE, "Review old historical bank evidence")
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "precedes the supported cutover"):
            confirm(self.entity, "bank-row", "old", accrual_treatment="new_cash_event")
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(len(_load_pending_categorization(self.entity)), 1)

    def test_routine_cash_categories_are_held_until_explicit_accrual_treatment(self):
        config = json.loads((self.root / "entity.json").read_text()); config["bank_account_mappings"] = {"checking": BANK}
        (self.root / "entity.json").write_text(json.dumps(config)); self.entity = load_entity(self.root)
        import_transactions(self.entity, [bank_row()], "intake")
        policy = {"version": 1, "enabled": True, "rules": [{"id": "learned", "source_account_id": "checking",
            "counterparty_key": "SYNTHETIC SOFTWARE", "category": EXPENSE, "approved_ledger_account": BANK}],
            "review_categories": [EXPENSE], "answer_roles": ["client"]}
        plan = routine_plan(self.entity, policy)
        self.assertEqual(plan["actions"], [])
        self.assertEqual(plan["unresolved"][0]["reason"], "accrual_treatment_required")

    def test_cash_only_stripe_plan_and_apply_are_rejected_with_action(self):
        for call in (lambda: stripe_plan(self.entity, {}, {}),
                     lambda: stripe_apply(self.entity, {}, {}, expected_plan_digest="irrelevant")):
            before = self.store.content_digest()
            with self.assertRaisesRegex(ValueError, "books accrual intake"):
                call()
            self.assertEqual(self.store.content_digest(), before)


if __name__ == "__main__":
    unittest.main()
