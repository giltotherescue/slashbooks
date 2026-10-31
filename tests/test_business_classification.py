"""Synthetic payment-fact corrections preserve accounting and tax history."""
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from bookkeeping import business
from bookkeeping.accrual import journals
from bookkeeping.accrual.intake import canonical_hash
from bookkeeping.accrual.store import load_record
from bookkeeping.entity import add_account, load_entity, map_bank_account
from bookkeeping.ledger.importer import _atomic_ledger_write, check_integrity, reverse_entry
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.reports.cashview import compute
from bookkeeping.tax1099 import verify_preparation
from tests import test_business as fixtures
from tests.test_accrual_schedules import BANK, EXPENSE
from tests.test_tax1099 import payload as tax_payload


class PaymentClassificationTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.BusinessTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def payment(self, **changes):
        request = self.fx.payment(gross="2500.00", direction="pay", payment_method="unknown",
                                  allocations=[{"amount": "2500.00"}], counter_account=EXPENSE)
        request.update(changes)
        self.fx.change(request)

    def classify(self, **changes):
        payment = business.records(self.fx.entity, "payments")["records"][0]
        request = {"operation_id": "classify-payment", "action": "payment.classify", "id": payment["id"],
                   "party_id": payment["party_id"], "expected_revision": payment["revision"],
                   "expected_source_fingerprint": payment["source_fingerprint"], "payment_method": "check",
                   "classifications": [{"allocation_index": 0, "tax_category": "nonemployee_compensation", "classification_verified": True}],
                   "reason": "Reviewed payment and vendor tax classification", "evidence_ids": [self.fx.evidence("classification")]}
        return {**request, **changes}

    def prepare(self, identity):
        tax = tax_payload()
        tax["recipients"][0]["party_id"] = "party-1"
        request = {"operation_id": identity, "action": "tax.prepare", "id": identity,
                   **{k: tax[k] for k in ("tax_year", "payer_profile", "recipients", "coverage")},
                   "coverage_evidence_ids": [self.fx.evidence(identity)]}
        self.fx.change(request)
        return load_record(self.fx.store, "business_tax_preparations", identity)

    def test_unknown_to_verified_preserves_ledger_reports_and_replays(self):
        self.payment()
        previous = self.prepare("tax-before")
        self.assertFalse(previous["preparation"]["ready"])
        entries = self.fx.store.load_entries()
        with self.fx.store.connection() as conn:
            before_report = compute(conn, date(2026, 1, 1), date(2026, 12, 31))
        request = self.classify()
        plan = business.preview(self.fx.entity, request)
        self.assertEqual(plan["entries"], [])
        self.assertEqual(plan["affected_dates"], [])
        receipt = self.fx.change(request)
        self.assertEqual(receipt["entry_source_ids"], [])
        self.assertEqual(receipt["details"]["payment_classification"]["before"][0]["tax_category"], "unknown")
        current = business.records(self.fx.entity, "payments")["records"][0]
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["source_fingerprint"], request["expected_source_fingerprint"])
        self.assertEqual(self.fx.store.load_entries(), entries)
        with self.fx.store.connection() as conn:
            self.assertEqual(compute(conn, date(2026, 1, 1), date(2026, 12, 31)), before_report)
        self.assertEqual(load_record(self.fx.store, "business_tax_preparations", "tax-before"), previous)
        prepared = self.prepare("tax-after")
        self.assertTrue(prepared["preparation"]["ready"])
        self.assertEqual(prepared["preparation"]["forms"][0]["boxes"], {"1a": "2500.00"})
        self.fx.change({"operation_id": "review-after", "action": "tax.review", "id": "tax-after", "expected_revision": 1, "preparation_hash": prepared["preparation"]["content_hash"]})

    def test_closed_period_correction_invalidates_prepared_review_without_rewriting_snapshot(self):
        self.payment(payment_method="check", allocations=[{"amount": "2500.00", "tax_category": "nonemployee_compensation", "classification_verified": True}])
        previous = self.prepare("tax-before")
        config = json.loads((self.fx.root / "entity.json").read_text())
        config["closed_through"] = "2026-12-31"
        (self.fx.root / "entity.json").write_text(json.dumps(config))
        self.fx.entity = load_entity(self.fx.root)
        request = self.classify(classifications=[{"allocation_index": 0, "tax_category": "rent", "classification_verified": True}])
        self.fx.change(request)
        self.assertEqual(load_record(self.fx.store, "business_tax_preparations", "tax-before"), previous)
        with self.assertRaisesRegex(ValueError, "Payments changed"):
            business.preview(self.fx.entity, {"operation_id": "stale-review", "action": "tax.review", "id": "tax-before", "expected_revision": 1, "preparation_hash": previous["preparation"]["content_hash"]})
        self.assertEqual(check_integrity(self.fx.entity).status, "ok")

    def test_revision_source_and_party_guards(self):
        self.payment()
        request = self.classify()
        for change, code in [({"expected_revision": 0}, "stale_record"), ({"expected_source_fingerprint": "0" * 64}, "payment_source_changed"), ({"party_id": "other"}, "payment_party_mismatch")]:
            with self.subTest(change=change), self.assertRaises(journals.JournalError) as raised:
                business.preview(self.fx.entity, {**request, **change})
            self.assertEqual(raised.exception.code, code)
        plan = business.preview(self.fx.entity, request)
        entry = self.fx.store.load_entries()[0]
        with patch("bookkeeping.accrual.items._find_live_entry", return_value=replace(entry, date=date(2026, 1, 16))), self.assertRaisesRegex(ValueError, "source no longer matches"):
            business.preview(self.fx.entity, request)
        # Ordinary writes cannot silently reverse managed payment lineage.
        with self.assertRaisesRegex(ValueError, "managed accrual"):
            reverse_entry(self.fx.entity, "payment-1-source", "synthetic-reversal")
        self.fx.post("subsequent-source", "2026-01-16", [(BANK, "-1.00"), (EXPENSE, "1.00")])
        with self.assertRaises(ValueError):
            business.apply(self.fx.entity, plan)
        self.assertEqual(business.records(self.fx.entity, "payments")["records"][0]["revision"], 1)

    def test_strict_indexes_immutability_and_automation_guard(self):
        self.payment()
        request = self.classify()
        row = request["classifications"][0]
        for changes in ({"date": "2026-02-01"}, {"gross_amount": "1.00"}, {"classifications": [row, row]}, {"classifications": [{**row, "allocation_index": True}]}, {"classifications": [{**row, "allocation_index": 1}]}, {"classifications": [{**row, "tax_category": "unknown"}]}, {"classifications": [{**row, "amount": "1.00"}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                business.preview(self.fx.entity, {**request, **changes})
        with patch("bookkeeping.preparation_runtime.is_preparation", return_value=True), self.assertRaisesRegex(ValueError, "person"):
            business.preview(self.fx.entity, request)
        plan = business.preview(self.fx.entity, request)
        forged = deepcopy(plan)
        forged["details"]["payment_classification"]["after"][0]["tax_category"] = "rent"
        forged["plan_hash"] = canonical_hash({k: v for k, v in forged.items() if k != "plan_hash"})
        with self.assertRaises(ValueError):
            business.apply(self.fx.entity, forged)

    def test_reversed_replaced_source_same_bank_and_date_is_not_reclassified(self):
        self.payment()
        request = self.classify()
        original = self.fx.store.load_entries()[0]
        reversal = Entry(original.date, "Synthetic audited reversal", postings=tuple(replace(p, amount=-p.amount) for p in original.postings),
                         meta=(("source-id", "reverse-original"), ("reverses", original.source_id)))
        replacement = Entry(original.date, "Synthetic replacement with different offsets", postings=(Posting(BANK, Decimal("-2500.00")), Posting("Expenses:Payment-Fees", Decimal("2500.00"))),
                            meta=(("source-id", "replacement-source"), ("supersedes", original.source_id)))
        _atomic_ledger_write(self.fx.entity, [], [reversal, replacement], "synthetic-reviewed-correction", None, "Synthetic corrected lineage")
        self.assertEqual(check_integrity(self.fx.entity).status, "ok")
        self.assertEqual(replacement.date, original.date)
        self.assertEqual(replacement.postings[0], original.postings[0])
        with self.assertRaisesRegex(ValueError, "already been reversed"):
            business.preview(self.fx.entity, request)
        self.assertEqual(business.records(self.fx.entity, "payments")["records"][0]["revision"], 1)

    def test_method_correction_is_reviewable_and_known_card_source_is_consistent(self):
        card = "Liabilities:CreditCard:Synthetic"
        add_account(self.fx.root, card, open_date="2000-01-01")
        self.fx.entity = load_entity(self.fx.root)
        for method in ("bank_transfer", "check", "cash", "payment_network"):
            request = self.fx.payment(direction="pay", source={"mode": "new", "source_id": "card-source", "bank_account": card, "amount": "-2500.00"}, gross="2500.00", payment_method=method, allocations=[{"amount": "2500.00"}], counter_account=EXPENSE)
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, "credit-card"):
                business.preview(self.fx.entity, request)
        self.payment(source={"mode": "new", "source_id": "card-source", "bank_account": card, "amount": "-2500.00"})
        with self.assertRaisesRegex(ValueError, "credit-card"):
            business.preview(self.fx.entity, self.classify(payment_method="check"))
        self.fx.change(self.classify(payment_method="card"))
        preparation = self.prepare("tax-card")["preparation"]
        self.assertTrue(preparation["ready"])
        self.assertEqual(preparation["forms"], [])
        self.assertEqual(preparation["payments"][0]["status"], "excluded")

    def test_rich_credit_mapping_does_not_guess_processor_rail(self):
        map_bank_account(self.fx.root, "synthetic-card", BANK, account_type="credit_card")
        self.fx.entity = load_entity(self.fx.root)
        with self.assertRaisesRegex(ValueError, "credit-card"):
            self.payment(payment_method="check")
        map_bank_account(self.fx.root, "synthetic-card", BANK, account_type="checking")
        self.fx.entity = load_entity(self.fx.root)
        self.payment()
        self.fx.change(self.classify(payment_method="payment_network", network_qualifies_1099k=False))
        self.assertTrue(self.prepare("tax-network")["preparation"]["ready"])

    def test_historical_card_method_conflict_blocks_until_reviewed_correction(self):
        card = "Liabilities:CreditCard:Synthetic"
        add_account(self.fx.root, card, open_date="2000-01-01")
        self.fx.entity = load_entity(self.fx.root)
        # Model a valid sealed payment recorded before source/method validation.
        with patch("bookkeeping.business._payment_method_source"):
            self.payment(source={"mode": "new", "source_id": "legacy-card", "bank_account": card, "amount": "-2500.00"}, payment_method="bank_transfer",
                         allocations=[{"amount": "2500.00", "tax_category": "nonemployee_compensation", "classification_verified": True}])
        original = load_record(self.fx.store, "business_payments", "payment-1")
        preparation = self.prepare("tax-legacy-card")["preparation"]
        self.assertFalse(preparation["ready"])
        self.assertIn("payment_method_source_mismatch", {issue["code"] for issue in preparation["issues"]})
        self.assertEqual(preparation["payments"][0]["status"], "blocked")
        self.assertEqual(load_record(self.fx.store, "business_payments", "payment-1"), original)
        self.fx.change(self.classify(payment_method="card"))
        corrected = self.prepare("tax-corrected-card")["preparation"]
        self.assertTrue(corrected["ready"])
        self.assertEqual(corrected["forms"], [])
        self.assertEqual(corrected["payments"][0]["status"], "excluded")

    def test_untrusted_historical_payee_does_not_enter_preparation(self):
        entry = Entry(date(2026, 1, 15), "Synthetic outflow", payee="Synthetic Payee 123456789", postings=(Posting(BANK, Decimal("-2500.00")), Posting(EXPENSE, Decimal("2500.00"))), meta=(("source-id", "unmatched"),))
        _atomic_ledger_write(self.fx.entity, [], [entry], "unmatched", None, "Synthetic fixture")
        preparation = self.prepare("tax-unmatched")["preparation"]
        self.assertFalse(preparation["ready"])
        self.assertNotIn("historical_payee", preparation["unmatched_sources"][0])
        verify_preparation(preparation)
        self.assertEqual(self.fx.store.load_entries()[0].payee, entry.payee)

    def test_unicode_custom_number_and_fully_credited_status(self):
        draft = {**self.fx.document_payload(), "reference": "INV-²"}
        self.fx.change(draft)
        self.fx.change({"operation_id": "issue-custom", "action": "document.issue", "id": "doc-1", "expected_revision": 1})
        self.fx.document(identity="doc-2")
        self.assertEqual(business.records(self.fx.entity, "documents", record_id="doc-2")["records"][0]["number"], "INV-000001")
        self.fx.change({"operation_id": "credit-all", "action": "document.credit", "id": "doc-1", "expected_revision": 2, "date": "2026-01-20", "amount": "1000.00", "reason": "Synthetic cancellation", "evidence_ids": [self.fx.evidence("credit")]})
        self.assertEqual(business.records(self.fx.entity, "documents", record_id="doc-1")["records"][0]["status"], "credited")
        self.assertEqual(business.records(self.fx.entity, "documents", record_id="doc-1", as_of="2026-01-10")["records"][0]["status"], "open")

