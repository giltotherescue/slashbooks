"""Synthetic, network-free invoice origin/settlement acceptance in both views."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from bookkeeping import business
from bookkeeping.accrual.intake import canonical_hash
from bookkeeping.accrual.journals import intake_evidence
from bookkeeping.accrual.store import load_record
from bookkeeping.entity import init_entity, load_entity, add_account
from bookkeeping.ledger.importer import _atomic_ledger_write, check_integrity
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.reports.cashview import compute

AR = "Assets:Receivable:Trade"
BANK = "Assets:Processors:Synthetic"
INCOME = "Income:Reviewed-Service"
OTHER = "Income:Reviewed-Other"
FEE = "Expenses:Reviewed-Fees"
TAX = "Liabilities:Reviewed-Sales-Tax"


def stamp(day):
    return int(datetime.fromisoformat(day + "T12:00:00+00:00").timestamp())


class StripeInvoiceTests(unittest.TestCase):
    def setUp(self):
        self.company("accrual")

    def company(self, basis):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "books"
        init_entity(self.root, basis=basis)
        self.entity = load_entity(self.root)
        business.apply(self.entity, business.preview(self.entity, {"action": "tracking.enable", "operation_id": "tracking", "tracking_start": "2026-01-01"}))
        self.store = LedgerStore(self.root / "ledger.sqlite")
        for account in (AR, BANK, INCOME, OTHER, FEE, TAX):
            add_account(self.root, account, open_date="2000-01-01")
        self.entity = load_entity(self.root)
        business.apply(self.entity, business.preview(self.entity, {"action": "party.save", "operation_id": "customer", "id": "customer",
            "expected_revision": 0, "display_name": "Synthetic Customer", "roles": ["customer"]}))

    def packet(self, *, amount=10000, discount=0, tax=0, payments=(), status=None, identity="in_synthetic"):
        total = amount - discount + tax
        lines = [{"id": "il_synthetic", "object": "line_item", "invoice": identity, "livemode": False, "currency": "usd",
                  "amount": amount, "quantity": 1, "description": "Synthetic services", "pricing": {"unit_amount_decimal": str(amount)},
                  "discount_amounts": [{"amount": discount, "discount": "di_synthetic"}] if discount else [],
                  "taxes": [{"amount": tax, "tax_behavior": "exclusive"}] if tax else []}]
        paid = sum(p[0] for p in payments)
        invoice = {"id": identity, "object": "invoice", "livemode": False, "currency": "usd", "customer": "cus_synthetic",
                   "status": status or ("paid" if paid == total else "open"), "number": "SYN-100", "created": stamp("2025-12-01"),
                   "effective_at": stamp("2026-01-10"), "due_date": stamp("2026-02-10"), "status_transitions": {"finalized_at": stamp("2026-01-10")},
                   "collection_method": "send_invoice", "lines": {"data": lines, "has_more": False}, "payments": {"data": [], "has_more": False},
                   "subtotal": amount, "total": total, "total_excluding_tax": amount-discount, "amount_due": total, "amount_paid": paid,
                   "amount_remaining": total-paid, "total_discount_amounts": [{"amount": discount}] if discount else [],
                   "pre_payment_credit_notes_amount": 0, "post_payment_credit_notes_amount": 0}
        packet = {"schema_version": 1, "source": "stripe_invoices", "api_version": "2025-06-30.basil",
                  "account": {"id": "acct_synthetic", "livemode": False, "default_currency": "usd", "timezone": "UTC"},
                  "invoice": invoice, "customer": {"id": "cus_synthetic", "object": "customer"}, "credit_notes": {"data": [], "has_more": False},
                  "charges": [], "balance_transactions": [], "refunds": [], "payment_intents": [],
                  "coverage": {k: True for k in ("invoice_lines_complete", "invoice_payments_complete", "credit_notes_complete", "payment_sources_complete", "refund_sources_complete")}}
        for index, (gross, fee) in enumerate(payments):
            pi, charge, balance = "pi_synthetic" + str(index), "ch_synthetic" + str(index), "txn_synthetic" + str(index)
            invoice["payments"]["data"].append({"id": "inpay_synthetic" + str(index), "object": "invoice_payment", "invoice": identity,
                "currency": "usd", "livemode": False, "status": "paid", "amount_paid": gross, "amount_requested": gross,
                "payment": {"type": "payment_intent", "payment_intent": pi}, "status_transitions": {"paid_at": stamp("2026-01-15")}})
            packet["payment_intents"].append({"id": pi, "status": "succeeded", "currency": "usd", "livemode": False,
                "customer": "cus_synthetic", "amount_received": gross, "latest_charge": charge})
            packet["charges"].append({"id": charge, "currency": "usd", "livemode": False, "customer": "cus_synthetic",
                "payment_intent": pi, "balance_transaction": balance, "status": "succeeded", "paid": True, "captured": True,
                "amount": gross, "amount_captured": gross, "amount_refunded": 0})
            packet["balance_transactions"].append({"id": balance, "type": "charge", "source": charge, "currency": "usd",
                "amount": gross, "fee": fee, "net": gross-fee, "created": stamp("2026-01-15"), "status": "available"})
        return packet

    def request(self, packet, *, operation="import-invoice", mode="new", **changes):
        digest = canonical_hash(packet)
        evidence = intake_evidence(self.entity, {"schema_version": 1, "source": {"namespace": "stripe.test.capture", "account_id": packet["account"]["id"],
            "object_id": packet["invoice"]["id"], "content_hash": digest}, "currency": "USD", "observed_facts": {"stripe_invoice": packet}})
        return {"operation_id": operation, "action": "document.import", "source": {"provider": "stripe", "account_id": packet["account"]["id"],
            "livemode": packet["account"]["livemode"], "invoice_id": packet["invoice"]["id"], "evidence_id": evidence["id"], "source_hash": digest},
            "party_id": "customer", "control_account": AR, "line_mappings": [{"line_id": line["id"], "account": INCOME, "tax_account": TAX} for line in packet["invoice"]["lines"]["data"]],
            "processor_account": BANK, "fee_account": FEE,
            "payment_sources": [{"payment_id": p["id"], "source_id": "stripe:" + packet["account"]["id"] + ":" + bt["id"], "mode": mode}
                                for p, bt in zip(packet["invoice"]["payments"]["data"], packet["balance_transactions"])], **changes}

    def apply(self, request):
        before = self.store.content_digest()
        plan = business.preview(self.entity, request)
        self.assertEqual(self.store.content_digest(), before)
        self.assertNotEqual(plan["details"]["document_import"]["status"], "held", plan["details"])
        receipt = business.apply(self.entity, plan)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        return plan, receipt

    def totals(self):
        balances = {}
        for entry in self.store.load_entries():
            for p in entry.postings:
                balances[p.account] = balances.get(p.account, Decimal(0)) + p.amount
        return balances

    def cash(self):
        with self.store.connection() as conn:
            return compute(conn, date(2026, 1, 1), date(2026, 1, 31))

    def post(self, sid, amounts):
        entry = Entry(date(2026, 1, 15), "Synthetic source", postings=tuple(Posting(a, Decimal(n)) for a, n in amounts), meta=(("source-id", sid),))
        _atomic_ledger_write(self.entity, [], [entry], sid, None, "Synthetic source fixture")

    def held(self, packet, **changes):
        request = self.request(packet, **changes)
        before, count = self.store.content_digest(), self.store.counts().entries
        plan = business.preview(self.entity, request)
        self.assertEqual(plan["details"]["document_import"]["status"], "held")
        self.assertEqual(plan["entries"], [])
        with self.assertRaisesRegex(ValueError, "blockers"):
            business.apply(self.entity, plan)
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(self.store.counts().entries, count)
        return plan["details"]["document_import"]["blockers"][0]["code"]

    def test_open_cash_and_accrual_preserve_default_basis(self):
        for basis in ("cash", "accrual"):
            with self.subTest(basis=basis):
                self.company(basis)
                plan, receipt = self.apply(self.request(self.packet()))
                self.assertEqual(load_entity(self.root).basis, basis)
                self.assertEqual(self.totals()[AR], Decimal("100.00"))
                self.assertEqual(self.totals()[INCOME], Decimal("-100.00"))
                self.assertEqual(self.cash()["cash_net_income"], Decimal(0))
                self.assertEqual(self.cash()["accrual_net_income"], Decimal("100.00"))
                doc = business.records(self.entity, "documents")["records"][0]
                self.assertTrue(doc["provider_managed"])
                self.assertEqual(doc["remaining_amount"], "100.00")
                self.assertEqual(receipt["details"]["document_import"]["status"], "imported")

    def test_paid_new_source_is_one_cash_fee_and_income_in_both_bases(self):
        for basis in ("cash", "accrual"):
            with self.subTest(basis=basis):
                self.company(basis)
                request = self.request(self.packet(payments=[(10000, 300)]))
                plan, receipt = self.apply(request)
                self.assertEqual(self.totals()[BANK], Decimal("97.00")); self.assertEqual(self.totals()[FEE], Decimal("3.00"))
                self.assertEqual(self.totals()[AR], Decimal(0)); self.assertEqual(self.totals()[INCOME], Decimal("-100.00"))
                self.assertEqual(self.cash()["cash_net_income"], Decimal("97.00"))
                self.assertEqual(business.apply(self.entity, plan)["status"], "already_applied")
                self.apply({**request, "operation_id": "another-request"})
                self.assertEqual(self.store.counts().entries, 2)

    def test_already_posted_split_income_reclasses_without_touching_cash_fee_or_source(self):
        self.post("stripe:acct_synthetic:txn_synthetic0", [(BANK, "97.00"), (FEE, "3.00"), (INCOME, "-60.00"), (OTHER, "-40.00")])
        original = self.store.load_entries()[0]
        self.apply(self.request(self.packet(payments=[(10000, 300)]), mode="existing"))
        self.assertEqual(next(e for e in self.store.load_entries() if e.source_id == original.source_id), original)
        totals = self.totals()
        self.assertEqual(totals[BANK], Decimal("97.00")); self.assertEqual(totals[FEE], Decimal("3.00"))
        self.assertEqual(totals[AR], Decimal(0)); self.assertEqual(totals[INCOME], Decimal("-100.00")); self.assertEqual(totals[OTHER], Decimal(0))
        self.assertEqual(self.cash()["cash_net_income"], Decimal("97.00"))

    def test_discount_tax_partial_and_later_payment_are_exact(self):
        packet = self.packet(amount=110000, discount=10000, tax=10000, payments=[(55000, 300)])
        self.apply(self.request(packet))
        self.assertEqual(self.totals()[AR], Decimal("550.00"))
        self.assertEqual(self.cash()["cash_net_income"], Decimal("497.00"))
        self.assertEqual(self.cash()["accrual_net_income"], Decimal("997.00"))
        later = self.packet(amount=110000, discount=10000, tax=10000, payments=[(55000, 300), (55000, 100)])
        self.apply(self.request(later, operation="later-payment"))
        self.assertEqual(self.totals()[BANK], Decimal("1096.00")); self.assertEqual(self.totals()[FEE], Decimal("4.00"))
        self.assertEqual(self.totals()[AR], Decimal(0)); self.assertEqual(self.totals()[TAX], Decimal("-100.00"))
        self.assertEqual(self.store.counts().entries, 3)

    def test_discovery_date_is_not_recognition_and_timezone_is_verified(self):
        packet = self.packet()
        packet["invoice"]["effective_at"] = None
        packet["invoice"]["status_transitions"]["finalized_at"] = int(datetime(2026, 1, 11, 1, tzinfo=timezone.utc).timestamp())
        packet["account"]["timezone"] = "America/Chicago"
        self.apply(self.request(packet))
        self.assertEqual(self.store.load_entries()[0].date, date(2026, 1, 10))

    def test_void_keeps_original_provider_remaining_but_extinguishes_canonical_balance(self):
        packet = self.packet(status="void")
        packet["invoice"]["status_transitions"]["voided_at"] = stamp("2026-01-20")
        self.apply(self.request(packet))
        self.assertEqual(self.totals()[AR], Decimal(0)); self.assertEqual(self.totals()[INCOME], Decimal(0))
        self.assertEqual(business.records(self.entity, "documents")["records"][0]["status"], "void")

    def test_utc_cash_date_before_local_issue_date_holds_without_shifting_dates(self):
        packet = self.packet(payments=[(10000, 300)])
        packet["account"]["timezone"] = "Pacific/Kiritimati"
        instant = stamp("2026-01-15")
        packet["invoice"]["effective_at"] = instant
        packet["invoice"]["status_transitions"]["finalized_at"] = instant
        original = deepcopy(packet)
        self.assertEqual(self.held(packet), "settlement_precedes_origin")
        self.assertEqual(packet, original)
        self.assertEqual(self.store.counts().entries, 0)

    def test_incomplete_ambiguous_and_unmapped_packets_hold_without_effects(self):
        for change in (lambda p: p["invoice"]["lines"].update(has_more=True),
                       lambda p: p["account"].update(timezone="Unknown/Zone"),
                       lambda p: p["invoice"].update(currency="eur"),
                       lambda p: p["invoice"].update(starting_balance=100),
                       lambda p: p["invoice"].update(total=1),
                       lambda p: p["coverage"].update(invoice_lines_complete=1)):
            with self.subTest(change=change):
                packet = self.packet(); change(packet)
                self.held(packet)
        self.held(self.packet(), line_mappings=[])

    def test_closed_period_and_stale_preview_have_no_partial_origin(self):
        packet = self.packet(payments=[(10000, 300)])
        request = self.request(packet)
        plan = business.preview(self.entity, request)
        self.post("unrelated-source", [(BANK, "1.00"), (OTHER, "-1.00")])
        count = self.store.counts().entries
        with self.assertRaisesRegex(ValueError, "preview"):
            business.apply(self.entity, plan)
        self.assertEqual(self.store.counts().entries, count)
        config = json.loads((self.root / "entity.json").read_text()); config["closed_through"] = "2026-01-31"
        (self.root / "entity.json").write_text(json.dumps(config)); self.entity = load_entity(self.root)
        self.assertEqual(self.held(packet), "closed_period")

    def ordinary_invoice_and_payment(self, *, gross=10000):
        draft = {"operation_id": "ordinary-draft", "action": "document.save", "id": "ordinary-invoice", "expected_revision": 0,
            "kind": "invoice", "party_id": "customer", "date": "2026-01-10", "due_date": "2026-02-10", "currency": "USD", "control_account": AR,
            "lines": [{"description": "Reviewed service", "quantity": "1", "unit_price": "100.00", "account": INCOME}]}
        business.apply(self.entity, business.preview(self.entity, draft))
        business.apply(self.entity, business.preview(self.entity, {"operation_id": "ordinary-issue", "action": "document.issue", "id": "ordinary-invoice", "expected_revision": 1, "number": "SYN-100"}))
        if gross == 0:
            return load_record(self.store, "business_documents", "ordinary-invoice")
        evidence = intake_evidence(self.entity, {"schema_version": 1, "source": {"namespace": "example.synthetic", "account_id": "synthetic", "object_id": "ordinary-payment", "version": "1"}, "currency": "USD", "observed_facts": {"description": "Reviewed processor source"}})
        amount = f"{Decimal(gross)/100:.2f}"
        payment = {"operation_id": "ordinary-payment", "action": "payment.record", "id": "ordinary-payment", "party_id": "customer", "date": "2026-01-15",
            "direction": "receive", "currency": "USD", "gross_amount": amount, "cash_amount": amount, "federal_withholding": "0.00", "payment_method": "unknown",
            "source": {"mode": "new", "source_id": "stripe:acct_synthetic:txn_synthetic0", "bank_account": BANK, "amount": f"{Decimal(gross-300)/100:.2f}"},
            "allocations": [{"item_id": "ordinary-invoice", "amount": amount}], "fee_amount": "3.00", "fee_account": FEE, "evidence_ids": [evidence["id"]]}
        business.apply(self.entity, business.preview(self.entity, payment))
        return load_record(self.store, "business_documents", "ordinary-invoice")

    def test_explicit_outbound_match_reuses_ordinary_payment_and_original_financial_history(self):
        original = self.ordinary_invoice_and_payment()
        entries = self.store.load_entries()
        request = self.request(self.packet(payments=[(10000, 300)]), mode="existing", match={"document_id": original["id"], "expected_revision": 2, "issued_snapshot_hash": original["issued_snapshot_hash"]})
        plan, receipt = self.apply(request)
        self.assertEqual(plan["entries"], [])
        self.assertEqual(self.store.load_entries(), entries)
        self.assertEqual(receipt["details"]["document_import"]["payment_matches"][0]["status"], "matched")
        self.assertEqual(receipt["details"]["document_import"]["payment_matches"][0]["business_payment_id"], "ordinary-payment")
        imported = load_record(self.store, "business_documents", original["id"])
        self.assertEqual(imported["issued_snapshot_hash"], original["issued_snapshot_hash"])
        self.assertEqual(imported["party_snapshot"], original["party_snapshot"])
        self.assertIs(imported["provider_managed"], False)
        self.assertEqual(len(business.records(self.entity, "payments")["records"]), 1)

    def test_existing_ordinary_payment_with_different_actual_money_is_held(self):
        original = self.ordinary_invoice_and_payment(gross=9900)
        self.assertEqual(self.held(self.packet(payments=[(10000, 300)]), mode="existing", match={"document_id": original["id"], "expected_revision": 2, "issued_snapshot_hash": original["issued_snapshot_hash"]}), "existing_payment_facts_mismatch")

    def test_billing_snapshot_uses_issued_invoice_not_mutable_customer(self):
        packet = self.packet()
        packet["invoice"].update(customer_name="Issued Customer", customer_email="issued@example.invalid", customer_address={"line1": "1 Synthetic Road", "city": "Example", "country": "US"})
        packet["customer"].update(name="Current Customer", email="current@example.invalid")
        self.apply(self.request(packet))
        stored = business.records(self.entity, "documents")["records"][0]
        self.assertEqual(stored["provider_billing_snapshot"]["name"], "Issued Customer")
        self.assertEqual(stored["provider_billing_snapshot"]["email"], "issued@example.invalid")
        packet["customer"].update(name="Another Current Name")
        self.apply(self.request(packet, operation="fresh-customer-capture"))
        self.assertEqual(self.store.counts().entries, 1)
        packet["invoice"]["customer_name"] = "Changed Issued Fact"
        self.assertEqual(self.held(packet, operation="changed-billing"), "issued_provider_financial_facts_changed")

    def test_complete_unpaid_credit_reduces_original_net_and_tax_once(self):
        packet = self.packet(tax=1000)
        packet["invoice"].update(pre_payment_credit_notes_amount=2200, amount_due=8800, amount_remaining=8800)
        packet["credit_notes"]["data"] = [{"id": "cn_synthetic", "object": "credit_note", "currency": "usd", "livemode": False,
            "invoice": "in_synthetic", "customer": "cus_synthetic", "status": "issued", "amount": 2200, "total": 2200,
            "pre_payment_amount": 2200, "post_payment_amount": 0, "created": stamp("2026-01-20"),
            "lines": {"has_more": False, "data": [{"id": "cnli_synthetic", "type": "invoice_line_item", "invoice_line_item": "il_synthetic",
                "livemode": False, "amount": 2000, "discount_amount": 0, "taxes": [{"amount": 200, "tax_behavior": "exclusive"}]}]}}]
        plan, receipt = self.apply(self.request(packet))
        self.assertIs(plan["details"]["document_import"]["requires_adjustments"], False)
        self.assertIs(receipt["details"]["document_import"]["requires_adjustments"], False)
        self.assertEqual(self.totals()[AR], Decimal("88.00")); self.assertEqual(self.totals()[INCOME], Decimal("-80.00")); self.assertEqual(self.totals()[TAX], Decimal("-8.00"))
        self.apply(self.request(packet, operation="credit-replay")); self.assertEqual(self.store.counts().entries, 2)

    def test_fractional_price_requires_exact_provider_cent_reconciliation(self):
        packet = self.packet(amount=100)
        packet["invoice"]["lines"]["data"][0].update(quantity=3, pricing={"unit_amount_decimal": "33.333333333333"})
        self.apply(self.request(packet))
        stored = business.records(self.entity, "documents")["records"][0]
        self.assertEqual(stored["lines"][0]["source_pricing"]["unit_amount_decimal"], "33.333333333333")
        packet["invoice"]["lines"]["data"][0]["pricing"]["unit_amount_decimal"] = "32"
        self.assertEqual(self.held(packet, operation="bad-unit-price"), "unit_amount_line_mismatch")

    def test_payment_history_cannot_regress_or_move_to_another_customer(self):
        self.apply(self.request(self.packet(payments=[(5000, 100)])))
        self.assertEqual(self.held(self.packet(), operation="lost-payment"), "provider_payment_history_regressed")
        packet = self.packet(payments=[(5000, 100)])
        packet["customer"]["id"] = packet["invoice"]["customer"] = "cus_other"
        for charge in packet["charges"]:
            charge["customer"] = "cus_other"
        for intent in packet["payment_intents"]:
            intent["customer"] = "cus_other"
        self.assertEqual(self.held(packet, operation="moved-customer"), "issued_provider_financial_facts_changed")

    def test_header_subtotal_tax_and_inclusive_facts_are_reconciled(self):
        for key, value in (("subtotal", -1), ("subtotal_excluding_tax", 9999), ("total_taxes", [{"amount": 999}])):
            packet = self.packet(tax=1000); packet["invoice"][key] = value
            self.assertIn(self.held(packet), {"invoice_subtotal_mismatch", "invoice_tax_header_mismatch"})
        packet = self.packet(amount=11000)
        invoice = packet["invoice"]
        invoice["lines"]["data"][0]["taxes"] = [{"amount": 1000, "tax_behavior": "inclusive"}]
        invoice.update(total_excluding_tax=10000, subtotal_excluding_tax=10000, total_taxes=[{"amount": 1000}])
        self.apply(self.request(packet))
        self.assertEqual(self.totals()[INCOME], Decimal("-100.00")); self.assertEqual(self.totals()[TAX], Decimal("-10.00"))

    def test_typed_provider_ids_with_digit_runs_keep_free_text_and_tax_ids_protected(self):
        from bookkeeping.tax1099 import reject_protected_data
        packet = self.packet(identity="in_abc123456789def")
        packet["invoice"]["lines"]["data"][0]["id"] = "il_abc987654321def"
        plan, _ = self.apply(self.request(packet))
        reject_protected_data(plan, allow_opaque_ids=True)
        for value in ("123456789", "000-00-0000", "00-0000000"):
            with self.assertRaises(ValueError):
                reject_protected_data({"invoice_id": value}, allow_opaque_ids=True)
        with self.assertRaises(ValueError):
            reject_protected_data({"description": "in_abc123456789def"}, allow_opaque_ids=True)

    def test_import_sidecar_failure_replays_original_committed_plan_without_duplicate_cash(self):
        request = self.request(self.packet(payments=[(10000, 300)]))
        plan = business.preview(self.entity, request)
        with patch("bookkeeping.business.journals._complete_linked_source_review", side_effect=ValueError("Synthetic cleanup interruption")):
            with self.assertRaisesRegex(ValueError, "cleanup interruption"):
                business.apply(self.entity, plan)
        self.assertEqual(self.store.counts().entries, 2)
        self.assertEqual(business.apply(self.entity, plan)["status"], "already_applied")
        self.assertEqual(self.store.counts().entries, 2)
        self.assertEqual(self.totals()[BANK], Decimal("97.00"))

    def test_exact_stripe_cash_claim_skips_owned_payment_and_holds_changed_facts(self):
        from bookkeeping.stripe import stripe_plan
        self.company("cash")
        packet = self.packet(payments=[(10000, 300)])
        self.apply(self.request(packet))
        transit, refunds = "Assets:Transfers:Synthetic", "Income:Reviewed-Refunds"
        for account in (transit, refunds):
            add_account(self.root, account, open_date="2000-01-01")
        config = json.loads((self.root / "entity.json").read_text())
        config.setdefault("bank_account_mappings", {})["acct_synthetic"] = BANK
        (self.root / "entity.json").write_text(json.dumps(config)); self.entity = load_entity(self.root)
        policy = {"version": 1, "enabled": True, "stripe_account_id": "acct_synthetic", "livemode": False, "currency": "USD",
            "clearing_account": BANK, "payout_transit_account": transit, "fee_account": FEE, "refund_account": refunds, "revenue_rules": []}
        captured = {"source": "stripe", "account": {"id": "acct_synthetic", "livemode": False}, "coverage": {"complete": True},
            "balanceTransactions": deepcopy(packet["balance_transactions"]), "charges": packet["charges"]}
        before = self.store.counts().entries
        planned = stripe_plan(self.entity, captured, policy)
        self.assertEqual(planned["actions"], [])
        self.assertEqual(planned["skipped"][0]["reason"], "invoice_settlement_recorded")
        self.assertEqual(self.store.counts().entries, before)
        captured["balanceTransactions"][0].update(amount=10100, net=9800)
        changed = stripe_plan(self.entity, captured, policy)
        self.assertEqual(changed["actions"], [])
        self.assertEqual(changed["unresolved"][0]["reason"], "changed_invoice_payment_source")

    def test_local_credit_absent_from_provider_packet_cannot_restore_collectible_balance(self):
        original = self.ordinary_invoice_and_payment(gross=0)
        source = self.request(self.packet())
        business.apply(self.entity, business.preview(self.entity, {"operation_id": "local-credit", "action": "document.credit", "id": original["id"],
            "expected_revision": 2, "date": "2026-01-20", "amount": "50.00", "reason": "Reviewed local correction",
            "evidence_ids": [source["source"]["evidence_id"]]}))
        self.assertEqual(self.held(self.packet(), match={"document_id": original["id"], "expected_revision": 3, "issued_snapshot_hash": original["issued_snapshot_hash"]}), "canonical_provider_balance_divergence")
        self.assertEqual(self.totals()[AR], Decimal("50.00"))

    def test_local_customer_refund_without_provider_refund_evidence_stays_held(self):
        original = self.ordinary_invoice_and_payment()
        source = self.request(self.packet(payments=[(10000, 300)]))
        refund = {"operation_id": "local-refund", "action": "payment.refund", "id": "local-refund", "party_id": "customer", "date": "2026-01-20",
            "direction": "pay", "currency": "USD", "gross_amount": "10.00", "cash_amount": "10.00", "federal_withholding": "0.00", "payment_method": "unknown",
            "source": {"mode": "new", "source_id": "local-refund-source", "bank_account": BANK, "amount": "-10.00"},
            "allocations": [{"amount": "10.00"}], "refund_of": "ordinary-payment", "evidence_ids": [source["source"]["evidence_id"]]}
        business.apply(self.entity, business.preview(self.entity, refund))
        self.assertEqual(self.held(self.packet(payments=[(10000, 300)]), mode="existing", match={"document_id": original["id"], "expected_revision": 2, "issued_snapshot_hash": original["issued_snapshot_hash"]}), "canonical_provider_refund_divergence")

    def test_receivable_control_cannot_create_cash_or_use_a_mapped_cash_asset(self):
        checking, cash, mapped = "Assets:Bank:Import-Test", "Assets:Cash:Import-Test", "Assets:Other:Mapped-Money"
        for account in (checking, cash, mapped):
            add_account(self.root, account, open_date="2000-01-01")
        config = json.loads((self.root / "entity.json").read_text())
        config.setdefault("bank_account_mappings", {})["synthetic-bank"] = mapped
        (self.root / "entity.json").write_text(json.dumps(config)); self.entity = load_entity(self.root)
        for control in (checking, cash, BANK, mapped):
            with self.subTest(control=control):
                self.assertEqual(self.held(self.packet(), control_account=control), "receivable_control_cannot_be_cash")
        self.assertEqual(self.store.counts().entries, 0)

    def test_new_credit_on_existing_doc_seals_adjustment_requirement_and_preserves_replay(self):
        self.apply(self.request(self.packet()))
        packet = self.packet()
        packet["invoice"].update(pre_payment_credit_notes_amount=2000, amount_due=8000, amount_remaining=8000)
        packet["credit_notes"]["data"] = [{"id": "cn_later", "object": "credit_note", "currency": "usd", "livemode": False,
            "invoice": "in_synthetic", "customer": "cus_synthetic", "status": "issued", "amount": 2000, "total": 2000,
            "pre_payment_amount": 2000, "post_payment_amount": 0, "created": stamp("2026-01-20"),
            "lines": {"has_more": False, "data": [{"id": "cnli_later", "type": "invoice_line_item", "invoice_line_item": "il_synthetic",
                "livemode": False, "amount": 2000, "discount_amount": 0, "taxes": []}]}}]
        request = self.request(packet, operation="new-credit")
        plan = business.preview(self.entity, request)
        self.assertIs(plan["details"]["document_import"]["requires_adjustments"], True)
        forged = deepcopy(plan)
        forged["details"]["document_import"]["requires_adjustments"] = False
        forged["plan_hash"] = canonical_hash({k: v for k, v in forged.items() if k != "plan_hash"})
        with self.assertRaisesRegex(ValueError, "preview"):
            business.apply(self.entity, forged)
        self.assertEqual(self.store.counts().entries, 1)
        receipt = business.apply(self.entity, plan)
        self.assertIs(receipt["details"]["document_import"]["requires_adjustments"], True)
        self.assertIs(business.apply(self.entity, plan)["details"]["document_import"]["requires_adjustments"], True)
        matched, _ = self.apply(self.request(packet, operation="already-credited-match"))
        self.assertIs(matched["details"]["document_import"]["requires_adjustments"], False)
        self.assertEqual(self.store.counts().entries, 2)
