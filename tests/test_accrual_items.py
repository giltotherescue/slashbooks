"""Synthetic opening, settlement, credit and alternative reversal proofs."""
from copy import deepcopy
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from tests.test_accrual_schedules import AccrualFixture, BANK, AR, AP, EXPENSE, INCOME
from bookkeeping.accrual.items import (apply_adjustment, apply_item, apply_settlement, list_items,
    preview_adjustment, preview_item, preview_settlement)
from bookkeeping.accrual.journals import due_reversals, intake_evidence
from bookkeeping.accrual.store import load_record
from bookkeeping.entity import load_entity
from bookkeeping.ledger.importer import check_integrity, import_transactions


class ItemTests(AccrualFixture):
    def item(self, **changes):
        request = {"operation_id": "invoice-create", "id": "invoice-1", "kind": "receivable", "account": AR,
            "currency": "USD", "original_amount": "1000.00", "as_of": "2026-01-01",
            "origin": {"mode": "new", "source_id": "provider-invoice-1", "counter_account": INCOME},
            "evidence_ids": [self.eid]}
        request.update(changes)
        return request

    def settlement(self, **changes):
        request = {"operation_id": "settle-1", "date": "2026-01-15", "source": {
            "mode": "new", "source_id": "bank-payment-1", "bank_account": BANK, "amount": "400.00"},
            "allocations": [{"item_id": "invoice-1", "amount": "400.00"}], "evidence_ids": [self.evidence("payment")]}
        request.update(changes)
        return request

    def create(self, **changes):
        return apply_item(self.entity, preview_item(self.entity, self.item(**changes)))

    def test_item_narration_uses_human_reference_then_counterparty(self):
        request = self.item(kind="payable", account=AP, id="item-machine-id",
            reference="Invoice #104", counterparty="Synthetic Vendor",
            origin={"mode": "new", "source_id": "vendor-invoice", "counter_account": EXPENSE})
        plan = preview_item(self.entity, request)
        self.assertEqual(plan["entries"][0]["description"], "Record payable: Invoice #104")
        self.assertEqual(dict(plan["entries"][0]["meta"])["item-id"], "item-machine-id")
        receipt = apply_item(self.entity, plan)
        before = self.store.content_digest()
        self.assertEqual(apply_item(self.entity, plan), receipt)
        self.assertEqual(self.store.content_digest(), before)
        for number, (labels, expected) in enumerate((
                ({"counterparty": "Synthetic Customer"}, "Record receivable: Synthetic Customer"),
                ({}, "Record receivable"))):
            with self.subTest(labels=labels):
                preview = preview_item(self.entity, self.item(operation_id="other-create-" + str(number),
                    evidence_ids=[self.evidence("other-invoice")], **labels))
                self.assertEqual(preview["entries"][0]["description"], expected)

    def test_committed_legacy_item_narration_replays_without_rewriting(self):
        with patch("bookkeeping.accrual.items._narration", return_value="Record receivable invoice-1"):
            plan = preview_item(self.entity, self.item(reference="Invoice #104"))
            receipt = apply_item(self.entity, plan)
        before = self.store.content_digest()
        self.assertEqual(apply_item(self.entity, plan), receipt)
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(self.store.load_entries()[0].narration, "Record receivable invoice-1")

    def test_partial_migrated_receivable_settlement_does_not_create_revenue(self):
        self.post("opening", "2025-12-31", [(AR, "1000.00"), ("Equity:Opening-Balances", "-1000.00")])
        self.create(origin={"mode": "opening", "source_id": "opening"})
        plan = preview_settlement(self.entity, self.settlement())
        receipt = apply_settlement(self.entity, plan)
        self.assertEqual(apply_settlement(self.entity, plan), receipt)
        self.assertEqual(load_record(self.store, "items", "invoice-1")["remaining_amount"], "600.00")
        self.assertEqual(self.balances()[AR], Decimal("600"))
        self.assertEqual(self.balances()[BANK], Decimal("400"))
        self.assertNotIn(INCOME, self.balances())

    def test_one_payment_settles_several_items_and_one_item_several_payments(self):
        self.create(original_amount="600.00")
        self.create(operation_id="invoice-2-create", id="invoice-2", original_amount="400.00",
                    origin={"mode": "new", "source_id": "provider-invoice-2", "counter_account": INCOME},
                    evidence_ids=[self.evidence("invoice-2")])
        first = self.settlement(allocations=[{"item_id": "invoice-1", "amount": "300.00"}, {"item_id": "invoice-2", "amount": "100.00"}])
        apply_settlement(self.entity, preview_settlement(self.entity, first))
        second = self.settlement(operation_id="settle-2", source={"mode": "new", "source_id": "bank-payment-2", "bank_account": BANK, "amount": "600.00"},
            allocations=[{"item_id": "invoice-1", "amount": "300.00"}, {"item_id": "invoice-2", "amount": "300.00"}], evidence_ids=[self.evidence("payment-2")])
        apply_settlement(self.entity, preview_settlement(self.entity, second))
        self.assertEqual({i["status"] for i in list_items(self.entity)}, {"settled"})
        self.assertEqual(self.balances()[AR], Decimal(0))
        self.assertEqual(self.balances()[BANK], Decimal("1000"))
        self.assertEqual(self.balances()[INCOME], Decimal("-1000"))

    def test_same_payment_evidence_through_another_transport_returns_original_receipt(self):
        self.create()
        request = self.settlement()
        original = apply_settlement(self.entity, preview_settlement(self.entity, request))
        alternate = intake_evidence(self.entity, {"schema_version": 1, "source": {
            "namespace": "example.synthetic", "account_id": "example-account", "object_id": "payment", "version": "1"},
            "currency": "USD", "observed_facts": {"description": "Synthetic evidence"}, "provenance": [{"transport": "mcp"}]})["id"]
        second = {**request, "operation_id": "second-payment-transport", "evidence_ids": [alternate],
                  "source": {**request["source"], "source_id": "second-api-payment"}}
        receipt = apply_settlement(self.entity, preview_settlement(self.entity, second))
        self.assertEqual(receipt, original)
        self.assertEqual(self.store.counts().entries, 2)
        self.assertEqual(self.balances()[BANK], Decimal("400"))
        self.assertEqual(self.balances()[AR], Decimal("600"))
        self.assertEqual(load_record(self.store, "items", "invoice-1")["remaining_amount"], "600.00")
        changed = {**second, "operation_id": "changed-payment-treatment", "allocations": [{"item_id": "invoice-1", "amount": "300.00"}]}
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "different financial treatment"):
            preview_settlement(self.entity, changed)
        self.assertEqual(self.store.content_digest(), before)

    def test_existing_payment_evidence_cannot_be_reassigned_to_another_bank_entry(self):
        self.create()
        self.post("first-bank", "2026-01-15", [(BANK, "400.00"), (INCOME, "-400.00")])
        self.post("other-bank", "2026-01-15", [(BANK, "400.00"), (INCOME, "-400.00")])
        request = self.settlement(source={"mode": "existing", "source_id": "first-bank", "bank_account": BANK, "amount": "400.00"})
        apply_settlement(self.entity, preview_settlement(self.entity, request))
        other = {**request, "operation_id": "other-bank-allocation", "source": {**request["source"], "source_id": "other-bank"}}
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "different payment"):
            preview_settlement(self.entity, other)
        self.assertEqual(self.store.content_digest(), before)
        from bookkeeping.ledger.importer import reverse_entry
        with self.assertRaisesRegex(ValueError, "managed accrual"):
            reverse_entry(self.entity, "first-bank", "legacy-payment-reversal")
        self.assertEqual(self.store.content_digest(), before)

    def test_payment_and_item_overallocations_fail_without_mutation(self):
        self.create(original_amount="300.00")
        first = self.settlement()
        second = self.settlement(source={"mode": "new", "source_id": "too-small", "bank_account": BANK, "amount": "100.00"},
                allocations=[{"item_id": "invoice-1", "amount": "200.00"}])
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "remaining item"):
            preview_settlement(self.entity, first)
        with self.assertRaisesRegex(ValueError, "available payment"):
            preview_settlement(self.entity, second)
        self.assertEqual(self.store.content_digest(), before)

    def test_existing_bank_income_is_reclassified_without_second_cash_or_revenue(self):
        self.create()
        self.post("already-bank", "2026-01-15", [(BANK, "400.00"), (INCOME, "-400.00")])
        before = self.balances()[BANK]
        request = self.settlement(source={"mode": "existing", "source_id": "already-bank", "bank_account": BANK, "amount": "400.00"})
        plan = preview_settlement(self.entity, request)
        self.assertFalse(any(p["account"] == BANK for e in plan["entries"] for p in e["postings"]))
        apply_settlement(self.entity, plan)
        self.assertEqual(self.balances()[BANK], before)
        self.assertEqual(self.balances()[INCOME], Decimal("-1000"))
        self.assertEqual(self.balances()[AR], Decimal("600"))

    def test_new_bank_after_journal_and_existing_bank_before_journal_have_equal_balances(self):
        self.post("already-bank", "2026-01-15", [(BANK, "400.00"), (INCOME, "-400.00")])
        self.create()
        apply_settlement(self.entity, preview_settlement(self.entity, self.settlement(
            source={"mode": "existing", "source_id": "already-bank", "bank_account": BANK, "amount": "400.00"})))
        self.assertEqual(self.balances(), {BANK: Decimal("400"), INCOME: Decimal("-1000"), AR: Decimal("600")})

    def test_linking_already_posted_control_settlement_has_no_second_financial_effect(self):
        self.create()
        self.post("control-payment", "2026-01-15", [(BANK, "400.00"), (AR, "-400.00")])
        count = self.store.counts().entries
        request = self.settlement(source={"mode": "existing", "source_id": "control-payment", "bank_account": BANK, "amount": "400.00"})
        apply_settlement(self.entity, preview_settlement(self.entity, request))
        self.assertEqual(self.store.counts().entries, count)
        self.assertEqual(load_record(self.store, "items", "invoice-1")["remaining_amount"], "600.00")

    def test_one_existing_payment_can_be_allocated_in_separate_reviewed_batches(self):
        self.create()
        self.post("large-payment", "2026-01-15", [(BANK, "1000.00"), (INCOME, "-1000.00")])
        first = self.settlement(source={"mode": "existing", "source_id": "large-payment", "bank_account": BANK, "amount": "1000.00"})
        apply_settlement(self.entity, preview_settlement(self.entity, first))
        second = {**first, "operation_id": "remaining-payment", "allocations": [{"item_id": "invoice-1", "amount": "600.00"}]}
        apply_settlement(self.entity, preview_settlement(self.entity, second))
        self.assertEqual(self.balances()[BANK], Decimal("1000"))
        self.assertEqual(self.balances()[INCOME], Decimal("-1000"))
        self.assertEqual(self.balances()[AR], Decimal(0))

    def test_competing_settlement_previews_cannot_allocate_twice(self):
        self.create()
        first_request = self.settlement()
        competing_request = {**first_request, "operation_id": "competing-payment"}
        first = preview_settlement(self.entity, first_request)
        competing = preview_settlement(self.entity, competing_request)
        apply_settlement(self.entity, first)
        with self.assertRaises(ValueError):
            apply_settlement(self.entity, competing)
        self.assertEqual(self.balances()[BANK], Decimal("400"))
        self.assertEqual(load_record(self.store, "items", "invoice-1")["remaining_amount"], "600.00")

    def test_payable_partial_settlement_carries_expense_once(self):
        self.create(kind="payable", account=AP, origin={"mode": "new", "source_id": "vendor-bill", "counter_account": EXPENSE})
        request = self.settlement(source={"mode": "new", "source_id": "vendor-payment", "bank_account": BANK, "amount": "-400.00"})
        apply_settlement(self.entity, preview_settlement(self.entity, request))
        self.assertEqual(self.balances()[EXPENSE], Decimal("1000"))
        self.assertEqual(self.balances()[AP], Decimal("-600"))
        self.assertEqual(self.balances()[BANK], Decimal("-400"))

    def test_credit_note_and_writeoff_reduce_support_and_control_exactly_once(self):
        self.create()
        request = {"operation_id": "credit-1", "item_id": "invoice-1", "action": "credit", "amount": "200.00",
            "date": "2026-01-10", "counter_account": INCOME, "source_id": "credit-note-1", "reason": "Synthetic credit note", "evidence_ids": [self.evidence("credit-note")]}
        plan = preview_adjustment(self.entity, request)
        receipt = apply_adjustment(self.entity, plan)
        self.assertEqual(apply_adjustment(self.entity, plan), receipt)
        writeoff = {**request, "operation_id": "writeoff", "action": "writeoff", "source_id": "writeoff-1", "counter_account": "Expenses:Bad-Debt",
                    "amount": "100.00", "evidence_ids": [self.evidence("writeoff")]}
        apply_adjustment(self.entity, preview_adjustment(self.entity, writeoff))
        self.assertEqual(load_record(self.store, "items", "invoice-1")["remaining_amount"], "700.00")
        self.assertEqual(self.balances()[AR], Decimal("700"))
        self.assertEqual(self.balances()[INCOME], Decimal("-800"))
        self.assertEqual(self.balances()["Expenses:Bad-Debt"], Decimal("100"))
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_second_transport_retains_provenance_without_another_invoice_effect(self):
        self.create()
        record = load_record(self.store, "evidence", self.eid)
        envelope = deepcopy(record["envelope"]); envelope["provenance"] = [{"transport": "another-api"}]
        from bookkeeping.accrual.journals import intake_evidence
        alternate = intake_evidence(self.entity, envelope)["id"]
        before = self.store.counts().entries
        plan = preview_item(self.entity, self.item(operation_id="second-transport", id="other-id", evidence_ids=[alternate]))
        receipt = apply_item(self.entity, plan)
        self.assertEqual(receipt["id"], "invoice-create")
        self.assertEqual(self.store.counts().entries, before)
        self.assertEqual(len(list_items(self.entity)), 1)
        self.assertNotEqual(alternate, self.eid)

    def test_opening_reservation_does_not_reuse_balance_after_partial_settlement(self):
        self.post("opening", "2025-12-31", [(AR, "600.00"), ("Equity:Opening-Balances", "-600.00")])
        self.create(original_amount="1000.00", remaining_amount="600.00", origin={"mode": "opening", "source_id": "opening"})
        apply_settlement(self.entity, preview_settlement(self.entity, self.settlement()))
        self.assertEqual(load_record(self.store, "items", "invoice-1")["remaining_amount"], "200.00")
        with self.assertRaisesRegex(ValueError, "exceeds the linked"):
            preview_item(self.entity, self.item(id="duplicate", operation_id="duplicate", original_amount="400.00", origin={"mode": "opening", "source_id": "opening"}))

    def test_closed_period_settlement_and_stale_preview_are_rejected(self):
        self.create()
        plan = preview_settlement(self.entity, self.settlement())
        config = json.loads((self.root / "entity.json").read_text()); config["closed_periods"] = ["2026-01"]
        (self.root / "entity.json").write_text(json.dumps(config)); self.entity = load_entity(self.root)
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "closed"):
            apply_settlement(self.entity, plan)
        self.assertEqual(self.store.content_digest(), before)


if __name__ == "__main__":
    unittest.main()
