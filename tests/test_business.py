"""Synthetic commercial workflows and unchanged cash/accrual boundaries."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from bookkeeping import business
from bookkeeping.accrual import reports
from bookkeeping.accrual.intake import canonical_hash
from bookkeeping.entity import add_account, init_entity, load_entity
from bookkeeping.ledger.importer import check_integrity
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.reports.cashview import compute
from bookkeeping.tax1099 import reject_protected_data
from tests.test_accrual_schedules import AccrualFixture, AR, AP, BANK, INCOME, EXPENSE


class BusinessTests(AccrualFixture):
    def setUp(self):
        super().setUp()
        business.apply(self.entity, business.preview(self.entity, {"operation_id": "enable-business", "action": "tracking.enable", "tracking_start": "2026-01-01"}))
        for account in ("Liabilities:Sales-Tax", "Expenses:Payment-Fees", "Liabilities:Backup-Withholding"):
            add_account(self.root, account, open_date="2000-01-01")
        self.entity = load_entity(self.root)
        self.change({"operation_id": "party-create", "action": "party.save", "id": "party-1", "expected_revision": 0, "display_name": "Synthetic Party", "roles": ["customer", "vendor"]})

    def change(self, payload):
        plan = business.preview(self.entity, payload)
        result = business.apply(self.entity, plan)
        before = self.store.content_digest()
        self.assertEqual(business.apply(self.entity, plan), {**result, "status": "already_applied"})
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        return result

    def document(self, *, kind="invoice", tax="0.00", amount="1000.00", identity="doc-1"):
        payload = {"operation_id": identity + "-draft", "action": "document.save", "id": identity, "expected_revision": 0, "kind": kind, "party_id": "party-1", "date": "2026-01-01", "due_date": "2026-02-01", "currency": "USD", "control_account": AR if kind == "invoice" else AP, "lines": [{"description": "Synthetic services", "quantity": "1", "unit_price": amount, "account": INCOME if kind == "invoice" else EXPENSE, "tax_amount": tax, "tax_account": "Liabilities:Sales-Tax"}]}
        entries_before = self.store.counts().entries
        self.change(payload)
        self.assertEqual(self.store.counts().entries, entries_before)
        self.change({"operation_id": identity + "-issue", "action": "document.issue", "id": identity, "expected_revision": 1})
        return payload

    def payment(self, *, gross="500.00", cash=None, direction="receive", identity="payment-1", allocations=None, source=None, **extra):
        return {"operation_id": identity, "action": "payment.record", "id": identity, "party_id": "party-1", "date": "2026-01-15", "currency": "USD", "direction": direction, "gross_amount": gross, "cash_amount": cash or gross, "federal_withholding": "0.00", "payment_method": "bank_transfer", "source": source or {"mode": "new", "source_id": identity + "-source", "bank_account": BANK, "amount": ("-" if direction == "pay" else "") + (cash or gross)}, "allocations": allocations or [{"item_id": "doc-1", "amount": gross, "tax_category": "nonemployee_compensation", "classification_verified": True}], "evidence_ids": [self.evidence(identity)], **extra}

    def test_taxed_invoice_partial_payment_cash_recognition(self):
        self.document(tax="100.00")
        self.change(self.payment(gross="550.00"))
        with self.store.connection() as conn:
            cash = compute(conn, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(cash["cash_net_income"], Decimal("500.00"))
        self.assertEqual(cash["accrual_net_income"], Decimal("1000.00"))
        self.assertNotIn("Liabilities:Sales-Tax", cash["balances"])
        self.assertEqual(self.balances()[AR], Decimal("550.00"))
        self.assertTrue(cash["tie_out"]["ties"])

    def test_issue_preview_with_digit_run_reference_passes_routine_privacy_guard(self):
        identity = "invoice-12345678-abcd-4abc-8abc-a123456789bc"
        self.change({"operation_id": "save-digit-run", "action": "document.save", "id": identity,
                     "expected_revision": 0, "kind": "invoice", "party_id": "party-1", "date": "2026-01-01",
                     "due_date": "2026-02-01", "currency": "USD", "control_account": AR,
                     "lines": [{"description": "Synthetic service", "quantity": "1", "unit_price": "1000.00", "account": INCOME}]})
        plan = business.preview(self.entity, {"operation_id": "issue-87654321-abcd-4abc-8abc-b987654321cd", "action": "document.issue",
                                              "id": identity, "expected_revision": 1})
        reject_protected_data(plan, allow_opaque_ids=True)
        tampered = deepcopy(plan)
        tampered["entries"][0]["meta"]["origin-source-id"] = "business-document:other"
        tampered["plan_hash"] = canonical_hash({key: value for key, value in tampered.items() if key != "plan_hash"})
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "changed"):
            business.apply(self.entity, tampered)
        self.assertEqual(self.store.content_digest(), before)
        receipt = business.apply(self.entity, plan)
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual(self.store.counts().entries, 1)
        self.assertEqual(business.apply(self.entity, plan)["status"], "already_applied")
        self.assertEqual(self.store.counts().entries, 1)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_bill_partial_payment(self):
        self.document(kind="bill", amount="1200.00")
        self.change(self.payment(gross="600.00", direction="pay"))
        with self.store.connection() as conn:
            cash = compute(conn, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(cash["cash_net_income"], Decimal("-600.00"))
        self.assertEqual(cash["accrual_net_income"], Decimal("-1200.00"))

    def test_processor_payment_fee_new_and_existing(self):
        self.document()
        source = {"mode": "new", "source_id": "processor-charge", "bank_account": BANK, "amount": "97.00"}
        self.change(self.payment(gross="100.00", source=source, fee_amount="3.00", fee_account="Expenses:Payment-Fees"))
        self.assertEqual(self.balances()[BANK], Decimal("97.00"))
        with self.store.connection() as conn:
            cash = compute(conn, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(cash["cash_net_income"], Decimal("97.00"))
        self.post("existing-gross", "2026-01-15", [(BANK, "97.00"), ("Expenses:Payment-Fees", "3.00"), (INCOME, "-100.00")])
        self.change(self.payment(identity="existing-payment", gross="100.00", source={**source, "mode": "existing", "source_id": "existing-gross"}, fee_amount="3.00", fee_account="Expenses:Payment-Fees"))
        self.assertEqual(self.balances()[BANK], Decimal("194.00"))
        self.assertEqual(self.balances()[INCOME], Decimal("-1000.00"))
        with self.store.connection() as conn:
            cash = compute(conn, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(cash["cash_net_income"], Decimal("194.00"))

    def test_stale_party_preview_and_sensitive_values_fail(self):
        payload = {"operation_id": "party-update", "action": "party.save", "id": "party-1", "expected_revision": 1, "display_name": "Renamed Party", "roles": ["customer", "vendor"]}
        plan = business.preview(self.entity, payload)
        self.change({**payload, "operation_id": "party-other", "display_name": "Other Party"})
        with self.assertRaisesRegex(ValueError, "changed|preview"):
            business.apply(self.entity, plan)
        with self.assertRaisesRegex(ValueError, "tax identifier|tax identifier|Protected tax|Taxpayer"):
            business.preview(self.entity, {**payload, "tin": "000-00-0000"})

    def test_party_csv_duplicates_are_suggestions_not_merge(self):
        request = business.import_csv("id,display_name,roles\np2,Synthetic Party,vendor\np3,Synthetic Party,customer\n", "import-parties")
        self.change(request)
        parties = business.records(self.entity, "parties")["records"]
        self.assertEqual(len(parties), 3)
        self.assertEqual(len(parties[0]["duplicate_suggestions"]), 2)

    def test_document_issued_snapshot_and_closed_period(self):
        self.document()
        with self.assertRaisesRegex(ValueError, "immutable"):
            business.preview(self.entity, {**self.document_payload(), "expected_revision": 2})
        config = json.loads((self.root / "entity.json").read_text())
        config["closed_through"] = "2026-01-31"
        (self.root / "entity.json").write_text(json.dumps(config))
        self.entity = load_entity(self.root)
        with self.assertRaisesRegex(ValueError, "closed"):
            business.preview(self.entity, self.payment())

    def document_payload(self):
        return {"operation_id": "edit-doc", "action": "document.save", "id": "doc-1", "expected_revision": 0, "kind": "invoice", "party_id": "party-1", "date": "2026-01-01", "due_date": "2026-02-01", "currency": "USD", "control_account": AR, "lines": [{"description": "Synthetic", "quantity": "1", "unit_price": "1000", "account": INCOME}]}


class CashBusinessSetupTests(unittest.TestCase):
    def test_v1_cash_setup_keeps_view_and_history_and_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "company"
            init_entity(root, basis="cash")
            entity = load_entity(root)
            store = LedgerStore(root / "ledger.sqlite")
            self.assertEqual(store.schema_version(), 1)
            request = {"operation_id": "business-enable", "action": "tracking.enable", "tracking_start": "2026-01-01"}
            plan = business.preview(entity, request)
            self.assertEqual(store.schema_version(), 1)
            result = business.apply(entity, plan)
            current = load_entity(root)
            self.assertEqual(current.entity_config["basis"], "cash")
            self.assertEqual(current.entity_config["accrual_tracking_start"], "2026-01-01")
            self.assertEqual(store.schema_version(), 3)
            self.assertEqual(store.counts().entries, 0)
            self.assertEqual(check_integrity(current).status, "ok")
            self.assertEqual(business.apply(current, plan), {**result, "status": "already_applied"})
            self.assertTrue(list(root.glob("*.schema-v1.*")) or list(root.rglob("*schema-v1*")))

class BusinessLifecycleTests(BusinessTests):
    def test_taxed_credit_and_refund_keep_both_views_balanced(self):
        self.document(tax="100.00")
        self.change(self.payment(gross="550.00"))
        self.change({"operation_id": "credit-unpaid", "action": "document.credit", "id": "doc-1", "expected_revision": 2, "date": "2026-01-20", "amount": "550.00", "reason": "Synthetic cancellation", "evidence_ids": [self.evidence("credit")]})
        refund = self.payment(identity="refund", direction="pay", gross="550.00", allocations=[{"amount": "550.00", "tax_category": "not_reportable", "classification_verified": True}])
        refund.update(action="payment.refund", refund_of="payment-1")
        self.change(refund)
        with self.store.connection() as conn:
            result = compute(conn, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(result["cash_net_income"], Decimal("0.00"))
        self.assertEqual(result["accrual_net_income"], Decimal("0.00"))
        self.assertEqual(self.balances()[BANK], Decimal("0.00"))
        self.assertEqual(self.balances()["Liabilities:Sales-Tax"], Decimal("0.00"))

    def test_writeoff_does_not_create_cash_expense(self):
        self.document()
        self.change(self.payment(gross="500.00"))
        self.change({"operation_id": "writeoff", "action": "document.writeoff", "id": "doc-1", "expected_revision": 2, "date": "2026-01-20", "amount": "500.00", "counter_account": "Expenses:Bad-Debt", "reason": "Synthetic uncollectible", "evidence_ids": [self.evidence("writeoff")]})
        with self.store.connection() as conn:
            result = compute(conn, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(result["cash_net_income"], Decimal("500.00"))
        self.assertEqual(result["accrual_net_income"], Decimal("500.00"))

    def test_overpayment_is_linked_credit_and_applies_by_party_id(self):
        self.document()
        request = self.payment(gross="1200.00", allocations=[{"item_id": "doc-1", "amount": "1000.00"}, {"amount": "200.00"}], excess={"id": "customer-credit", "account": AR})
        self.change(request)
        from bookkeeping.accrual.store import load_record
        credit = load_record(self.store, "items", "customer-credit")
        self.assertEqual(credit["party_id"], "party-1")
        self.document(identity="doc-2", amount="200.00")
        self.change({"operation_id": "apply-credit", "action": "credit.apply", "credit_item_id": "customer-credit", "target_item_id": "doc-2", "date": "2026-01-20", "amount": "200.00", "reason": "Apply customer overpayment"})
        self.assertEqual(load_record(self.store, "items", "doc-2")["remaining_amount"], "0.00")
        with self.store.connection() as conn:
            result = compute(conn, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(result["cash_net_income"], Decimal("1200.00"))

    def test_non_cash_payment_source_and_duplicate_source_fail(self):
        self.post("cash-source", "2026-01-15", [(BANK, "500.00"), (INCOME, "-500.00")])
        fake = self.payment(direction="pay", allocations=[{"amount": "500.00"}], source={"mode": "existing", "source_id": "cash-source", "bank_account": INCOME, "amount": "-500.00"})
        fake["action"] = "payment.link"
        with self.assertRaisesRegex(ValueError, "bank|account"):
            business.preview(self.entity, fake)
        with self.assertRaises(ValueError):
            business.preview(self.entity, {"operation_id": "tin-name", "action": "party.save", "id": "p-tin", "expected_revision": 0, "display_name": "Synthetic 000-00-0000", "roles": ["vendor"]})

    def test_existing_paid_vendor_candidate_and_link(self):
        self.post("old-vendor-payment", "2026-01-15", [(BANK, "-800.00"), (EXPENSE, "800.00")])
        self.assertEqual(len(business.payment_candidates(self.entity, 2026)), 1)
        request = self.payment(direction="pay", gross="800.00", allocations=[{"amount": "800.00", "tax_category": "nonemployee_compensation", "classification_verified": True}], source={"mode": "existing", "source_id": "old-vendor-payment", "bank_account": BANK, "amount": "-800.00"})
        request["action"] = "payment.link"
        count = self.store.counts().entries
        self.change(request)
        self.assertEqual(self.store.counts().entries, count)
        self.assertEqual(business.payment_candidates(self.entity, 2026), [])

class BusinessIntegrityTests(BusinessTests):
    def test_issue_failure_rolls_back_entries_evidence_item_document(self):
        from unittest.mock import patch
        from bookkeeping.accrual import store as supporting
        payload = self.document_payload()
        self.change(payload)
        plan = business.preview(self.entity, {"operation_id": "issue-rollback", "action": "document.issue", "id": "doc-1", "expected_revision": 1})
        before = self.store.content_digest()
        original = supporting.apply_mutations
        def fail_after_support(store, mutations, conn):
            original(store, mutations, conn)
            raise ValueError("Synthetic write interruption")
        with patch.object(supporting, "apply_mutations", side_effect=fail_after_support):
            with self.assertRaisesRegex(ValueError, "Synthetic"):
                business.apply(self.entity, plan)
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(self.store.counts().entries, 0)
        self.assertIsNone(supporting.load_record(self.store, "items", "doc-1"))
        self.assertEqual(supporting.load_record(self.store, "business_documents", "doc-1")["status"], "draft")
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        self.change(plan["request"])

    def test_withholding_must_exist_in_linked_bank_source(self):
        self.post("paid-eight-hundred", "2026-01-15", [(BANK, "-800.00"), (EXPENSE, "800.00")])
        request = self.payment(direction="pay", gross="1000.00", cash="800.00", allocations=[{"amount": "1000.00"}], source={"mode": "existing", "source_id": "paid-eight-hundred", "bank_account": BANK, "amount": "-800.00"})
        request.update(action="payment.link", federal_withholding="200.00", withholding_account="Liabilities:Backup-Withholding")
        with self.assertRaisesRegex(ValueError, "component"):
            business.preview(self.entity, request)
        self.assertEqual(len(business.records(self.entity, "payments")["records"]), 0)

    def test_historical_vendor_refund_remains_unresolved_until_linked(self):
        self.post("vendor-refund-history", "2026-01-15", [(BANK, "500.00"), (EXPENSE, "-500.00")])
        candidates = business.payment_candidates(self.entity, 2026)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["reason"], "possible_vendor_refund_not_linked")
        request = self.payment(gross="500.00", allocations=[{"amount": "500.00"}], source={"mode": "existing", "source_id": "vendor-refund-history", "bank_account": BANK, "amount": "500.00"})
        request["action"] = "payment.link"
        with self.assertRaisesRegex(ValueError, "original vendor payment"):
            business.preview(self.entity, request)

    def test_credit_refund_reduces_unapplied_balance(self):
        self.document()
        self.change(self.payment(gross="1200.00", allocations=[{"item_id": "doc-1", "amount": "1000.00"}, {"amount": "200.00"}], excess={"id": "extra-credit", "account": AR}))
        refund = self.payment(identity="refund-overpay", direction="pay", gross="1200.00", allocations=[{"amount": "1200.00"}])
        refund.update(action="payment.refund", refund_of="payment-1")
        self.change(refund)
        from bookkeeping.accrual.store import load_record
        self.assertEqual(load_record(self.store, "items", "extra-credit")["remaining_amount"], "0.00")
        with self.store.connection() as conn:
            result = compute(conn, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(result["cash_net_income"], Decimal("0.00"))

    def test_schema3_reopens_and_retains_sealed_backup(self):
        from bookkeeping.ledger.importer import check_store_integrity
        reopened = LedgerStore(self.root / "ledger.sqlite")
        self.assertEqual(reopened.schema_version(), 3)
        backups = list((self.root / "backups").glob("*schema-v2*"))
        self.assertTrue(backups)
        backup = LedgerStore(backups[-1])
        self.assertEqual(backup.schema_version(), 2)
        self.assertEqual(check_store_integrity(backup).status, "ok")

class BusinessSetupRecoveryTests(unittest.TestCase):
    def test_accrual_setup_recovers_after_schema_commit_before_receipt(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "company"
            init_entity(root, basis="accrual")
            entity = load_entity(root)
            store = LedgerStore(root / "ledger.sqlite")
            request = {"operation_id": "recovery-setup", "action": "tracking.enable", "tracking_start": "2026-01-01"}
            plan = business.preview(entity, request)
            original = business.upgrade_business
            def interrupted(*args, **kwargs):
                original(*args, **kwargs)
                raise ValueError("Synthetic interruption after schema commit")
            with patch.object(business, "upgrade_business", side_effect=interrupted):
                with self.assertRaisesRegex(ValueError, "Synthetic interruption"):
                    business.apply(entity, plan)
            self.assertEqual(store.schema_version(), 3)
            self.assertEqual(check_integrity(load_entity(root)).status, "ok")
            result = business.apply(entity, plan)
            self.assertEqual(result["status"], "applied")
            self.assertEqual(business.apply(entity, plan)["status"], "already_applied")
            self.assertEqual(store.counts().entries, 0)

class StablePartyIdentityTests(BusinessTests):
    def test_generic_accrual_credit_cannot_cross_same_name_parties(self):
        self.document()
        self.change(self.payment(gross="1200.00", allocations=[{"item_id": "doc-1", "amount": "1000.00"}, {"amount": "200.00"}], excess={"id": "party-a-credit", "account": AR}))
        self.change({"operation_id": "create-party-b", "action": "party.save", "id": "party-b", "expected_revision": 0, "display_name": "Synthetic Party", "roles": ["customer"]})
        draft = self.document_payload()
        draft.update(operation_id="party-b-draft", id="party-b-invoice", party_id="party-b")
        self.change(draft)
        self.change({"operation_id": "party-b-issue", "action": "document.issue", "id": "party-b-invoice", "expected_revision": 1})
        from bookkeeping.accrual.items import preview_offset
        with self.assertRaisesRegex(ValueError, "stable party"):
            preview_offset(self.entity, {"operation_id": "cross-party-credit", "date": "2026-01-20", "credit_item_id": "party-a-credit", "target_item_id": "party-b-invoice", "amount": "200.00", "reason": "Synthetic incorrect association"})
