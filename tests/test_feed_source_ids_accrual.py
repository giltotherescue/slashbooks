"""An accrual-held payment posts once whichever spelling of its BankSync ID arrives."""
from __future__ import annotations

import unittest

from tests.test_accrual_schedules import AccrualFixture, BANK, AR, INCOME
from bookkeeping.accrual.items import apply_item, apply_settlement, preview_item, preview_settlement
from bookkeeping.accrual.treatment import treatment_checker
from bookkeeping.ledger.importer import _load_pending_categorization, import_transactions
from bookkeeping.entity import load_entity

from tests.test_accrual_imports import bank_row


class FeedIdAliasAccrualTests(AccrualFixture):
    def _invoice(self, source_id: str = "provider-invoice") -> None:
        apply_item(self.entity, preview_item(self.entity, {
            "operation_id": "invoice", "id": "invoice", "kind": "receivable", "account": AR,
            "original_amount": "1000.00", "as_of": "2026-01-01",
            "origin": {"mode": "new", "source_id": source_id, "counter_account": INCOME},
            "evidence_ids": [self.eid]}))

    def _settle(self, source_id: str, amount: str = "400.00") -> None:
        apply_settlement(self.entity, preview_settlement(self.entity, {
            "operation_id": "link-" + source_id.replace(":", "-"), "date": "2026-01-15", "source": {
                "mode": "new", "source_id": source_id, "bank_account": BANK, "amount": amount},
            "allocations": [{"item_id": "invoice", "amount": amount}],
            "evidence_ids": [self.evidence("pay-" + source_id.replace(":", "-"))]}))

    def test_payment_held_and_settled_under_the_old_id_is_not_posted_again_under_the_prefix(self) -> None:
        self._invoice()
        old = bank_row("bill-pay", amount="400.00")
        import_transactions(self.entity, [old], "old")            # held for accrual treatment
        self.assertEqual([i["id"] for i in _load_pending_categorization(self.entity)], ["bill-pay"])
        self._settle("bill-pay")
        entries = len(self.store.load_entries())
        balances = self.balances()
        again = import_transactions(self.entity, [bank_row("banksync:bill-pay", amount="400.00")], "new",
                                    lambda _: (INCOME, "high"))
        self.assertEqual((again.new_entries, again.skipped_duplicate), (0, 1))
        self.assertEqual(len(self.store.load_entries()), entries)
        self.assertEqual(self.balances(), balances)
        self.assertEqual(_load_pending_categorization(self.entity), [])

    def test_payment_held_under_the_prefix_is_not_posted_again_under_the_old_id(self) -> None:
        self._invoice()
        import_transactions(self.entity, [bank_row("banksync:bill-pay", amount="400.00")], "new")
        self._settle("banksync:bill-pay")
        entries = len(self.store.load_entries())
        again = import_transactions(self.entity, [bank_row("bill-pay", amount="400.00")], "old",
                                    lambda _: (INCOME, "high"))
        self.assertEqual((again.new_entries, again.skipped_duplicate), (0, 1))
        self.assertEqual(len(self.store.load_entries()), entries)

    def test_held_payment_is_not_queued_twice_under_the_two_spellings(self) -> None:
        import_transactions(self.entity, [bank_row("bill-pay", amount="400.00")], "old")
        again = import_transactions(self.entity, [bank_row("banksync:bill-pay", amount="400.00")], "new")
        self.assertEqual(again.pending_categorization, 0)
        self.assertEqual(len(_load_pending_categorization(self.entity)), 1)

    def test_a_record_that_names_the_old_id_owns_the_prefixed_row(self) -> None:
        # Per-account discipline holds a row only when a record names it or its words/amount match.
        self._invoice(source_id="owned-id")
        entity = load_entity(self.root)
        entity.entity_config["accrual_tracking_start"] = "2026-01-01"
        row = {"id": "banksync:owned-id", "date": "2026-02-01", "amount": "-7.00", "description": "unrelated"}
        self.assertTrue(treatment_checker(entity)(row))
        self.assertTrue(treatment_checker(entity)({**row, "id": "owned-id"}))
        self.assertFalse(treatment_checker(entity)({**row, "id": "other-id"}))

    def test_a_record_that_names_the_prefixed_id_owns_the_old_spelling(self) -> None:
        self._invoice(source_id="banksync:owned-id")
        entity = load_entity(self.root)
        entity.entity_config["accrual_tracking_start"] = "2026-01-01"
        row = {"id": "owned-id", "date": "2026-02-01", "amount": "-7.00", "description": "unrelated"}
        self.assertTrue(treatment_checker(entity)(row))
        self.assertFalse(treatment_checker(entity)({**row, "id": "other-id"}))

    def _settlement(self, source_id: str, amount: str) -> dict:
        return {"operation_id": "wrong-" + source_id.replace(":", "-"), "date": "2026-01-15", "source": {
            "mode": "new", "source_id": source_id, "bank_account": BANK, "amount": amount},
            "allocations": [{"item_id": "invoice", "amount": amount}],
            "evidence_ids": [self.evidence("wrong-" + source_id.replace(":", "-"))]}

    def test_staged_prefixed_row_still_checks_a_settlement_that_uses_the_old_id(self) -> None:
        self._invoice()
        import_transactions(self.entity, [bank_row("banksync:abc", amount="400.00")], "new")
        with self.assertRaises(Exception) as caught:
            preview_settlement(self.entity, self._settlement("abc", "300.00"))
        self.assertEqual(getattr(caught.exception, "code", ""), "staged_payment_mismatch")

    def test_staged_old_row_still_checks_a_settlement_that_uses_the_prefixed_id(self) -> None:
        self._invoice()
        import_transactions(self.entity, [bank_row("abc", amount="400.00")], "old")
        with self.assertRaises(Exception) as caught:
            preview_settlement(self.entity, self._settlement("banksync:abc", "300.00"))
        self.assertEqual(getattr(caught.exception, "code", ""), "staged_payment_mismatch")

    def test_settlement_under_the_other_spelling_is_refused_and_cash_is_recorded_once(self) -> None:
        self._invoice()
        import_transactions(self.entity, [bank_row("banksync:abc", amount="400.00")], "new")
        with self.assertRaises(Exception) as caught:
            apply_settlement(self.entity, preview_settlement(self.entity, {
                **self._settlement("abc", "400.00"), "operation_id": "other-spelling"}))
        self.assertEqual(getattr(caught.exception, "code", ""), "staged_source_id_differs")
        self.assertIn("banksync:abc", str(caught.exception))
        # Settle with the staged ID, then confirm the queue item: the cash is counted once.
        self._settle("banksync:abc")
        before = self.balances()
        self.assertEqual(before[BANK], 400)
        self.assertEqual(_load_pending_categorization(self.entity), [])
        again = import_transactions(self.entity, [bank_row("abc", amount="400.00")], "again")
        self.assertEqual((again.new_entries, again.pending_categorization, again.skipped_duplicate), (0, 0, 1))
        self.assertEqual(self.balances(), before)

    def test_confirm_checks_every_spelling_for_an_already_posted_source(self) -> None:
        from bookkeeping.queue import _recover_finalization
        self.post("abc", "2026-01-15", [(BANK, "400"), (INCOME, "-400")])
        item = {"source_id": "banksync:abc"}
        with self.assertRaises(ValueError):  # posted under the other spelling: not a clean first confirm
            _recover_finalization(self.entity, item, ["banksync:abc"], "s", "confirmed", INCOME)

    def test_correct_amount_previews_with_the_staged_spelling(self) -> None:
        self._invoice()
        import_transactions(self.entity, [bank_row("banksync:abc", amount="400.00")], "new")
        preview_settlement(self.entity, self._settlement("banksync:abc", "400.00"))

    def test_reversal_and_correction_find_a_row_by_either_spelling(self) -> None:
        from bookkeeping.ledger.importer import _find_live_entry
        self.post("banksync:abc", "2026-01-15", [(BANK, "5"), (INCOME, "-5")])
        self.assertEqual(_find_live_entry(self.store, "abc").source_id, "banksync:abc")
        self.post("old2", "2026-01-16", [(BANK, "6"), (INCOME, "-6")])
        self.assertEqual(_find_live_entry(self.store, "banksync:old2").source_id, "old2")


if __name__ == "__main__":
    unittest.main()
