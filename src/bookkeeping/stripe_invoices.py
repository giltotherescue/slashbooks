"""Network-free, source-bound Stripe invoice accounting.

Provider facts are evidence. Account mappings are explicit reviewed input. This
adapter never calls Stripe, chooses a revenue category, or sends an invoice.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .accrual import items, journals, schedules
from .accrual.intake import canonical_hash, normalize_evidence, source_identity, facts_hash, evidence_hash
from .accrual.store import SupportingMutation, load_record, list_records, record_revision, projected_reads


class Held(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _hold(code):
    raise Held(code)


def _strict(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise journals.JournalError("invalid_import_request", "Supply only the documented invoice import fields.")
    return value


def _id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_]{1,200}", value):
        _hold("invalid_provider_identity")
    return value


def _ref(value):
    return value.get("id") if isinstance(value, dict) else value


def _minor(value):
    if type(value) is not int or abs(value) > 99999999999999:
        _hold("invalid_minor_amount")
    return value


def _usd(value):
    return f"{Decimal(value)/100:.2f}"


def _page(value, label):
    if (not isinstance(value, dict) or value.get("has_more") is not False
            or not isinstance(value.get("data"), list) or len(value["data"]) > 1000):
        _hold("incomplete_" + label)
    return value["data"]


def _objects(value, prefix):
    if not isinstance(value, list) or len(value) > 1000:
        _hold("incomplete_payment_sources")
    result = {}
    for row in value:
        if not isinstance(row, dict):
            _hold("invalid_provider_object")
        identity = _id(row.get("id"), prefix)
        if identity in result:
            _hold("duplicate_provider_object")
        result[identity] = row
    return result


def _rows(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 1000 or any(not isinstance(r, dict) for r in value):
        _hold("invalid_provider_array")
    return value


def _private_fields(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z]", "", key.lower())
            if (normalized in {"metadata", "clientsecret", "confirmationsecret", "taxid", "taxids", "customertaxids", "accounttaxids",
                               "taxiddata", "customfields", "invoicepdf", "hostedinvoiceurl"}
                    or normalized.endswith(("tin", "ssn", "ein", "taxidentifier", "taxpayerid"))):
                _hold("private_provider_fields_not_allowed")
            _private_fields(child)
    elif isinstance(value, list):
        for child in value:
            _private_fields(child)


def _billing_snapshot(invoice):
    from .business import _bounded, _reject_sensitive
    result = {}
    for key in ("name", "email"):
        value = invoice.get("customer_" + key)
        result[key] = None if value is None else _bounded(value, "billing." + key, 300)
    address = invoice.get("customer_address")
    if address is not None:
        _strict(address, {"line1", "line2", "city", "state", "postal_code", "country"})
        address = {k: None if v is None else _bounded(v, "billing.address." + k, 300) for k, v in address.items()}
    result["address"] = address
    _reject_sensitive(result)
    return result


def _day(value, zone):
    if type(value) is not int or value <= 0:
        _hold("missing_provider_date")
    try:
        return datetime.fromtimestamp(value, zone).date().isoformat()
    except (ValueError, OSError, OverflowError):
        _hold("invalid_provider_date")


def _evidence(namespace, account, identity, facts):
    envelope = normalize_evidence({"schema_version": 1,
        "source": {"namespace": namespace, "account_id": account, "object_id": identity,
                   "content_hash": canonical_hash(facts)}, "currency": "USD", "observed_facts": facts})
    eid = canonical_hash(envelope)
    return SupportingMutation("evidence", eid, {"id": eid, "envelope": envelope,
        "source_identity": source_identity(envelope), "facts_hash": facts_hash(envelope),
        "evidence_hash": evidence_hash(envelope)})


def _coalesce(mutations):
    result = {}
    for mutation in mutations:
        key = (mutation.table, mutation.record_id)
        previous = result.get(key)
        result[key] = replace(mutation, expected_revision=previous.expected_revision) if previous else mutation
    return list(result.values())


def _add(mutations, mutation, store):
    if mutation.table == "evidence" and load_record(store, "evidence", mutation.record_id):
        return
    mutations.append(mutation)


def _packet(store, source):
    record = journals._evidence(store, [source["evidence_id"]])[0]
    packet = record["envelope"].get("observed_facts", {}).get("stripe_invoice")
    if not isinstance(packet, dict) or canonical_hash(packet) != source["source_hash"]:
        _hold("provider_packet_hash_mismatch")
    _strict(packet, {"schema_version", "source", "api_version", "account", "invoice", "customer",
                    "credit_notes", "charges", "balance_transactions", "refunds", "payment_intents", "coverage"},
                   {"schema_version", "source", "api_version", "account", "invoice", "customer",
                    "credit_notes", "charges", "balance_transactions", "refunds", "payment_intents", "coverage"})
    _private_fields(packet)
    if type(packet["schema_version"]) is not int or packet["schema_version"] != 1 or packet["source"] != "stripe_invoices" or packet["api_version"] != "2025-06-30.basil":
        _hold("unsupported_provider_contract")
    account = packet["account"]
    if (not isinstance(account, dict) or account.get("id") != source["account_id"]
            or type(account.get("livemode")) is not bool or account["livemode"] != source["livemode"]):
        _hold("provider_scope_mismatch")
    coverage = packet["coverage"]
    expected = {"invoice_lines_complete", "invoice_payments_complete", "credit_notes_complete",
                "payment_sources_complete", "refund_sources_complete"}
    if not isinstance(coverage, dict) or set(coverage) != expected or any(coverage[k] is not True for k in expected):
        _hold("incomplete_provider_coverage")
    try:
        zone = ZoneInfo(account["timezone"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        _hold("invalid_account_timezone")
    if account.get("default_currency") != "usd":
        _hold("unsupported_currency")
    return packet, zone


def _invoice(packet, zone, request, store, entity):
    invoice = packet["invoice"]
    if (not isinstance(invoice, dict) or invoice.get("id") != request["source"]["invoice_id"]
            or invoice.get("object") != "invoice" or invoice.get("livemode") is not request["source"]["livemode"]):
        _hold("provider_invoice_identity_mismatch")
    if invoice.get("currency") != "usd":
        _hold("unsupported_currency")
    if invoice.get("status") not in {"open", "paid", "void"}:
        _hold("provider_" + str(invoice.get("status")) + "_requires_review")
    for key in ("starting_balance", "ending_balance", "amount_overpaid", "amount_shipping",
                "post_payment_credit_notes_amount"):
        if invoice.get(key) not in (None, 0):
            _hold("customer_balance_shipping_or_paid_credit_requires_review")
    if invoice.get("paid_out_of_band") or packet["refunds"]:
        _hold("out_of_band_or_refund_requires_explicit_source_review")
    customer = packet["customer"]
    if (not isinstance(customer, dict) or customer.get("deleted") or
            _id(customer.get("id"), "cus_") != _ref(invoice.get("customer"))):
        _hold("provider_customer_mismatch")
    transitions = invoice.get("status_transitions") or {}
    when = _day(invoice.get("effective_at") if invoice.get("effective_at") is not None
                else transitions.get("finalized_at"), zone)
    due = (_day(invoice["due_date"], zone) if invoice.get("due_date") is not None else when
           if invoice.get("collection_method") == "charge_automatically" else None)
    if not due or due < when:
        _hold("invalid_due_date")
    party = load_record(store, "business_parties", journals._id(request["party_id"]))
    if not party or party.get("status") != "active" or "customer" not in party.get("roles", []):
        _hold("customer_mapping_required")
    control = schedules._account(store, request["control_account"], "control_account", ("Assets:",))
    from .entity import flat_bank_account_mappings
    mapped_cash = set(flat_bank_account_mappings(entity.entity_config).values())
    csv_mappings = entity.entity_config.get("csv_account_mappings", {})
    if isinstance(csv_mappings, dict):
        mapped_cash.update(value for value in csv_mappings.values() if isinstance(value, str))
    if control in mapped_cash or any(control == prefix or control.startswith(prefix + ":")
                                     for prefix in ("Assets:Bank", "Assets:Cash", "Assets:Processors")):
        _hold("receivable_control_cannot_be_cash")
    mappings = {}
    if not isinstance(request["line_mappings"], list):
        _hold("line_mappings_required")
    for mapping in request["line_mappings"]:
        _strict(mapping, {"line_id", "account", "tax_account"}, {"line_id", "account"})
        identity = _id(mapping["line_id"], "il_")
        if identity in mappings:
            _hold("duplicate_line_mapping")
        mappings[identity] = mapping
    lines, subtotal, discounts, taxes, header_subtotal = [], 0, 0, 0, 0
    for raw in _page(invoice.get("lines"), "invoice_lines"):
        if not isinstance(raw, dict):
            _hold("invalid_invoice_line")
        identity = _id(raw.get("id"), "il_")
        if identity not in mappings or raw.get("currency") != "usd" or raw.get("livemode") is not request["source"]["livemode"]:
            _hold("line_mapping_or_scope_mismatch")
        if raw.get("invoice") not in (None, invoice["id"]):
            _hold("line_invoice_mismatch")
        mapping = mappings.pop(identity)
        amount = _minor(raw.get("amount"))
        parent = raw.get("parent") or {}
        if amount < 0 or any((parent.get(k) or {}).get("proration") for k in ("invoice_item_details", "subscription_item_details")):
            _hold("negative_or_proration_line_requires_review")
        credit_facts = _rows(raw.get("pretax_credit_amounts"))
        if any(row.get("type") != "discount" for row in credit_facts):
            _hold("customer_credit_requires_review")
        discount = sum(_minor(row.get("amount")) for row in _rows(raw.get("discount_amounts")))
        tax_rows = _rows(raw.get("taxes"))
        if any(row.get("tax_behavior") not in {"inclusive", "exclusive"} for row in tax_rows):
            _hold("unknown_tax_behavior")
        tax = sum(_minor(row.get("amount")) for row in tax_rows)
        inclusive = sum(_minor(row.get("amount")) for row in tax_rows if row["tax_behavior"] == "inclusive")
        if inclusive and discount:
            _hold("inclusive_discount_requires_net_breakdown")
        net = amount - discount - inclusive
        if min(discount, tax, net) < 0:
            _hold("invalid_line_amount_equation")
        account = schedules._account(store, mapping["account"], "line.account", ("Income:",))
        quantity = raw.get("quantity")
        if quantity is not None and (type(quantity) is not int or not 0 < quantity <= 999999999999):
            _hold("unsupported_line_quantity")
        unit = (raw.get("pricing") or {}).get("unit_amount_decimal")
        if unit is not None:
            try:
                if not isinstance(unit, str) or not re.fullmatch(r"[0-9]{1,14}(?:\.[0-9]{1,12})?", unit):
                    raise InvalidOperation
                unit_price = format(Decimal(unit)/100, "f")
                if (Decimal(unit) * Decimal(quantity or 1)).quantize(Decimal("1"), rounding=ROUND_HALF_UP) != amount:
                    _hold("unit_amount_line_mismatch")
            except InvalidOperation:
                _hold("unsupported_unit_amount")
        elif quantity in (None, 1):
            unit_price = _usd(amount)
        else:
            _hold("missing_precise_unit_price")
        line = {"source_line_id": identity, "description": raw.get("description") or "Stripe invoice line",
                "quantity": str(quantity or 1), "unit_price": unit_price, "account": account,
                "discount_amount": _usd(discount), "net_amount": _usd(net), "tax_amount": _usd(tax),
                "total_amount": _usd(net + tax), "calculation_basis": "provider_minor_units",
                "source_pricing": {"quantity": quantity, "unit_amount_decimal": unit}}
        from .business import _bounded, _reject_sensitive
        line["description"] = _bounded(line["description"], "description")
        _reject_sensitive(line["description"])
        if tax:
            line["tax_account"] = schedules._account(store, mapping.get("tax_account"), "tax_account", ("Liabilities:",))
        lines.append(line); subtotal += amount - inclusive; discounts += discount; taxes += tax
        item_discounts = {_ref(d) for d in raw.get("discounts") or []}
        header_subtotal += amount - sum(_minor(d["amount"]) for d in _rows(raw.get("discount_amounts")) if _ref(d.get("discount")) in item_discounts)
    if mappings or not 1 <= len(lines) <= 200:
        _hold("incomplete_or_extra_line_mapping")
    total = subtotal - discounts + taxes
    if total <= 0 or _minor(invoice.get("total")) != total or _minor(invoice.get("total_excluding_tax")) != total - taxes:
        _hold("invoice_line_totals_mismatch")
    if sum(_minor(r.get("amount")) for r in _rows(invoice.get("total_discount_amounts"))) != discounts:
        _hold("invoice_discount_mismatch")
    for field, expected in (("subtotal", header_subtotal), ("subtotal_excluding_tax", header_subtotal - sum(_minor(t["amount"]) for line in _page(invoice["lines"], "invoice_lines") for t in _rows(line.get("taxes")) if t["tax_behavior"] == "inclusive"))):
        if invoice.get(field) is not None and _minor(invoice[field]) != expected:
            _hold("invoice_subtotal_mismatch")
    if invoice.get("total_taxes") is not None and sum(_minor(t.get("amount")) for t in _rows(invoice["total_taxes"])) != taxes:
        _hold("invoice_tax_header_mismatch")
    number = invoice.get("number")
    if not isinstance(number, str) or not number or len(number) > 200:
        _hold("missing_invoice_number")
    doc = {"kind": "invoice", "party_id": party["id"], "party_snapshot": deepcopy(party),
           "provider_billing_snapshot": _billing_snapshot(invoice),
           "date": when, "due_date": due, "currency": "USD", "control_account": control,
           "lines": lines, "subtotal": _usd(subtotal), "discount_amount": _usd(discounts),
           "tax_amount": _usd(taxes), "total_amount": _usd(total), "number": number}
    return invoice, doc, total


def _payments(packet, invoice, source):
    charges = _objects(packet["charges"], "ch_")
    intents = _objects(packet["payment_intents"], "pi_")
    balances = _objects(packet["balance_transactions"], "txn_")
    paid, seen = [], set()
    for payment in _page(invoice.get("payments"), "invoice_payments"):
        identity = _id(payment.get("id"), "inpay_")
        if identity in seen:
            _hold("duplicate_invoice_payment")
        seen.add(identity)
        if payment.get("livemode") is not source["livemode"] or payment.get("currency") != "usd" or _ref(payment.get("invoice")) != invoice["id"]:
            _hold("invoice_payment_scope_mismatch")
        if payment.get("status") in {"open", "canceled"}:
            if payment.get("amount_paid") not in (None, 0):
                _hold("nonpaid_payment_has_money")
            continue
        if payment.get("status") != "paid":
            _hold("unsupported_invoice_payment")
        gross = _minor(payment.get("amount_paid"))
        ref = payment.get("payment") or {}
        if ref.get("type") == "payment_intent":
            intent = intents.get(_ref(ref.get("payment_intent")))
            if (not intent or intent.get("status") != "succeeded" or intent.get("currency") != "usd"
                    or intent.get("livemode") is not source["livemode"] or intent.get("amount_received") != gross
                    or _ref(intent.get("customer")) != _ref(invoice["customer"])):
                _hold("missing_or_shared_payment_intent")
            charge = charges.get(_ref(intent.get("latest_charge")))
            if charge and _ref(charge.get("payment_intent")) != intent["id"]:
                _hold("charge_payment_intent_mismatch")
        elif ref.get("type") == "charge":
            charge = charges.get(_ref(ref.get("charge")))
        else:
            _hold("unsupported_payment_reference")
        if (not charge or charge.get("currency") != "usd" or charge.get("livemode") is not source["livemode"]
                or charge.get("status") != "succeeded" or charge.get("paid") is not True or charge.get("captured") is not True
                or charge.get("amount") != gross or charge.get("amount_captured") != gross or gross <= 0
                or charge.get("amount_refunded") != 0 or _ref(charge.get("customer")) != _ref(invoice["customer"])):
            _hold("missing_or_shared_charge")
        balance = balances.get(_ref(charge.get("balance_transaction")))
        if (not balance or balance.get("type") not in {"charge", "payment"} or _ref(balance.get("source")) != charge["id"]
                or balance.get("currency") != "usd" or _minor(balance.get("amount")) != gross
                or _minor(balance.get("fee")) < 0 or gross - balance["fee"] != _minor(balance.get("net")) or balance["net"] <= 0):
            _hold("balance_transaction_mismatch")
        # Existing Stripe cash sources use the actual BT date in UTC, not the
        # invoice's paid_at marker. Preserve that source convention on linking.
        when = _day(balance.get("created"), timezone.utc)
        _day((payment.get("status_transitions") or {}).get("paid_at"), timezone.utc)
        paid.append({"id": identity, "date": when, "gross": gross, "fee": balance["fee"], "net": balance["net"], "balance": balance})
    if sum(p["gross"] for p in paid) != _minor(invoice.get("amount_paid")):
        _hold("incomplete_paid_payment_history")
    if len({p["balance"]["id"] for p in paid}) != len(paid):
        _hold("shared_balance_transaction_requires_allocation")
    return sorted(paid, key=lambda p: (p["date"], p["id"]))


def _credits(packet, invoice, normalized, zone, source):
    credits, seen, consumed = [], set(), {}
    originals = {line["source_line_id"]: line for line in normalized["lines"]}
    for note in _page(packet["credit_notes"], "credit_notes"):
        identity = _id(note.get("id"), "cn_")
        if identity in seen:
            _hold("duplicate_credit_note")
        seen.add(identity)
        if (note.get("status") != "issued" or note.get("currency") != "usd" or note.get("livemode") is not source["livemode"]
                or _ref(note.get("invoice")) != invoice["id"] or _ref(note.get("customer")) != _ref(invoice["customer"])):
            _hold("credit_note_scope_or_status_requires_review")
        amount = _minor(note.get("amount"))
        if (amount <= 0 or note.get("pre_payment_amount") != amount or note.get("post_payment_amount") != 0
                or note.get("out_of_band_amount") not in (None, 0) or note.get("customer_balance_transaction")
                or note.get("refunds") or note.get("amount_shipping") not in (None, 0)):
            _hold("paid_credit_or_refund_requires_explicit_review")
        counter, allocated = [], 0
        for line in _page(note.get("lines"), "credit_note_lines"):
            original = originals.get(line.get("invoice_line_item"))
            if not original or line.get("type") != "invoice_line_item" or line.get("livemode") is not source["livemode"]:
                _hold("credit_original_line_required")
            discount = _minor(line.get("discount_amount"))
            taxes = _rows(line.get("taxes"))
            if any(t.get("tax_behavior") not in {"inclusive", "exclusive"} for t in taxes):
                _hold("unknown_credit_tax_behavior")
            tax = sum(_minor(t["amount"]) for t in taxes)
            inclusive = sum(_minor(t["amount"]) for t in taxes if t["tax_behavior"] == "inclusive")
            net = _minor(line.get("amount")) - discount - inclusive
            if min(net, tax, discount) < 0:
                _hold("invalid_credit_line_equation")
            for key, account, value in (("net_amount", "account", net), ("tax_amount", "tax_account", tax)):
                used = (line["invoice_line_item"], key)
                consumed[used] = consumed.get(used, 0) + value
                if consumed[used] > int(Decimal(original[key]) * 100):
                    _hold("credit_exceeds_original_line")
                if value:
                    counter.append({"account": original[account], "amount": _usd(value)})
            allocated += net + tax
        if allocated != amount or note.get("total") != amount:
            _hold("credit_note_line_totals_mismatch")
        when = _day(note.get("effective_at") if note.get("effective_at") is not None else note.get("created"), zone)
        credits.append({"id": identity, "date": when, "amount": amount, "counter_lines": counter})
    if sum(c["amount"] for c in credits) != _minor(invoice.get("pre_payment_credit_notes_amount", 0)):
        _hold("incomplete_credit_note_history")
    return sorted(credits, key=lambda c: (c["date"], c["id"]))


def _existing_payment(payment, candidates, doc, bank, fee_account, store):
    """Prove an ordinary recorded settlement before attaching a provider ID."""
    if len(candidates) != 1:
        _hold("payment_source_already_has_other_facts")
    record = candidates[0]
    if any(p.get("refund_of") == record["id"] for p in list_records(store, "business_payments")):
        _hold("canonical_provider_refund_divergence")
    if (record.get("party_id") != doc["party_id"] or record.get("direction") != "receive" or record.get("refund_of")
            or record.get("currency") != "USD" or record.get("date") != payment["date"]
            or record.get("gross_amount") != _usd(payment["gross"]) or record.get("cash_amount") != _usd(payment["gross"])
            or record.get("fee_amount", "0.00") != _usd(payment["fee"]) or record.get("federal_withholding") != "0.00"
            or record["source"].get("bank_account") != bank or record["source"].get("amount") != _usd(payment["net"])):
        _hold("existing_payment_facts_mismatch")
    allocations = record.get("allocations", [])
    if (len(allocations) != 1 or allocations[0].get("item_id") != doc["item_id"]
            or allocations[0].get("amount") != _usd(payment["gross"])):
        _hold("existing_payment_item_mismatch")
    from .source_ids import source_id_variants
    aliases = source_id_variants(record["source"]["source_id"])
    settled = [r for r in list_records(store, "allocations") if r.get("kind") == "settlement" and r.get("source_id") in aliases]
    if (not settled or any(r.get("item_id") != doc["item_id"] or r.get("bank_account") != bank or r.get("bank_amount") != _usd(payment["net"]) for r in settled)
            or sum(Decimal(r["amount"]) for r in settled) != Decimal(_usd(payment["gross"]))):
        _hold("existing_payment_settlement_missing")
    item = load_record(store, "items", doc["item_id"])
    from .reports.cashview import _cumulative
    weights = [(r["account"], int(Decimal(r["amount"]) * 100)) for r in item["origin"].get("lines", [])]
    # Ordinary payment.record/link stores a per-payment recognition split.
    # Later allocations are already present here; cumulative report timing is
    # derived separately by cashview and must not rewrite these original facts.
    shares = _cumulative(weights, payment["gross"])
    expected = {}
    for (account, _), amount in zip(weights, shares):
        expected[account] = expected.get(account, Decimal(0)) + Decimal(amount)/100
    actual = {}
    for line in record.get("recognition_lines", []):
        actual[line["account"]] = actual.get(line["account"], Decimal(0)) + Decimal(line["amount"])
    if {a: n for a, n in actual.items() if n} != {a: n for a, n in expected.items() if n}:
        _hold("existing_payment_recognition_mismatch")
    original = items._find_live_entry(store, record["source"]["source_id"])
    remaining = list(original.postings)
    bank_rows = [p for p in remaining if p.account == bank and p.currency == "USD" and p.amount == Decimal(_usd(payment["net"]))]
    if original.date.isoformat() != payment["date"] or len(bank_rows) != 1:
        _hold("existing_payment_source_mismatch")
    remaining.remove(bank_rows[0])
    if payment["fee"]:
        fee = next((p for p in remaining if p.account == fee_account and p.currency == "USD" and p.amount == Decimal(_usd(payment["fee"]))), None)
        if fee is None:
            _hold("existing_payment_fee_mismatch")
        remaining.remove(fee)
    if not remaining or any(p.currency != "USD" or p.amount >= 0 for p in remaining) or sum(p.amount for p in remaining) != -Decimal(_usd(payment["gross"])):
        _hold("existing_payment_gross_mismatch")
    return record["id"]


def effect(entity, payload):
    """Compile one invoice and its complete proved settlement history atomically."""
    from .business import _store
    request = deepcopy(dict(payload))
    _strict(request, {"operation_id", "action", "source", "party_id", "control_account", "line_mappings",
                      "processor_account", "fee_account", "payment_sources", "match"},
                     {"operation_id", "action", "source", "party_id", "control_account", "line_mappings", "payment_sources"})
    source = _strict(request["source"], {"provider", "account_id", "livemode", "invoice_id", "evidence_id", "source_hash"},
                     {"provider", "account_id", "livemode", "invoice_id", "evidence_id", "source_hash"})
    store = _store(entity)
    details = {"status": "held", "blockers": [], "source_hash": source["source_hash"], "payment_matches": []}
    try:
        if source["provider"] != "stripe" or type(source["livemode"]) is not bool:
            _hold("invalid_provider_scope")
        _id(source["account_id"], "acct_"); _id(source["invoice_id"], "in_")
        identity = canonical_hash({"account_id": source["account_id"], "livemode": source["livemode"], "invoice_id": source["invoice_id"]})
        details["identity"] = identity
        packet, zone = _packet(store, source)
        invoice, normalized, total = _invoice(packet, zone, request, store, entity)
        paid = _payments(packet, invoice, source)
        credits = _credits(packet, invoice, normalized, zone, source)
        credited = sum(c["amount"] for c in credits)
        if credits and paid:
            _hold("mixed_credit_payment_requires_allocation_review")
        if _minor(invoice.get("amount_due")) != total - credited:
            _hold("customer_balance_or_adjusted_amount_due")
        remaining = total - credited - sum(p["gross"] for p in paid)
        if remaining < 0 or (invoice["status"] != "void" and _minor(invoice.get("amount_remaining")) != remaining):
            _hold("invoice_remaining_mismatch")
        if invoice["status"] == "paid" and remaining or invoice["status"] == "open" and remaining == 0:
            _hold("invoice_payment_status_mismatch")
        if invoice["status"] == "void" and paid:
            _hold("paid_invoice_void_requires_refund_review")
        claim_id = "stripe-invoice:" + identity
        claim = load_record(store, "intents", claim_id)
        match = request.get("match")
        if match:
            _strict(match, {"document_id", "expected_revision", "issued_snapshot_hash"}, {"document_id", "expected_revision", "issued_snapshot_hash"})
        doc_id = claim["document_id"] if claim else match["document_id"] if match else "stripe-invoice-" + identity
        old = load_record(store, "business_documents", doc_id)
        revision = record_revision(store, "business_documents", doc_id)
        financial = {**{k: v for k, v in normalized.items() if k != "party_snapshot"}, "provider_customer_id": _ref(invoice["customer"])}
        financial_hash = canonical_hash(financial)
        if claim and claim["financial_hash"] != financial_hash:
            _hold("issued_provider_financial_facts_changed")
        if claim and not old or match and not old:
            _hold("matched_document_missing")
        if match and (revision != match["expected_revision"] or old.get("issued_snapshot_hash") != match["issued_snapshot_hash"]):
            _hold("stale_document_match")
        if old and old.get("status") == "void" and invoice["status"] != "void":
            _hold("voided_invoice_cannot_reopen")
        known_payments = set((claim or {}).get("payment_ids", []))
        if not known_payments <= {p["id"] for p in paid}:
            _hold("provider_payment_history_regressed")
        if not set((claim or {}).get("credit_ids", [])) <= {c["id"] for c in credits}:
            _hold("provider_credit_history_regressed")
        if old and not claim:
            if not match or revision != match["expected_revision"] or old.get("issued_snapshot_hash") != match["issued_snapshot_hash"]:
                _hold("explicit_existing_document_match_required")
            for field in ("party_id", "date", "due_date", "currency", "control_account", "total_amount", "tax_amount", "number"):
                if old.get(field, "0.00" if field == "tax_amount" else None) != normalized[field]:
                    _hold("existing_document_financial_mismatch")
            def offsets(lines):
                result = {}
                for row in lines:
                    for key, account_key in (("net_amount", "account"), ("tax_amount", "tax_account")):
                        amount = Decimal(row.get(key, "0.00"))
                        if amount:
                            result[row[account_key]] = result.get(row[account_key], Decimal(0)) + amount
                return result
            if offsets(old["lines"]) != offsets(normalized["lines"]) or not old.get("item_id"):
                _hold("existing_document_line_mismatch")
        elif match and claim and match["document_id"] != doc_id:
            _hold("provider_identity_already_claimed")
        mutations, entries, requires_adjustments = [], [], False
        namespace = "stripe.live" if source["livemode"] else "stripe.test"
        party = normalized["party_snapshot"]
        customer_identity = {"namespace": namespace, "account_id": source["account_id"], "object_id": packet["customer"]["id"]}
        if any(customer_identity in p.get("external_ids", []) for p in list_records(store, "business_parties") if p["id"] != party["id"]):
            _hold("customer_identity_already_mapped")
        if customer_identity not in party.get("external_ids", []):
            party = {**party, "external_ids": [*party.get("external_ids", []), customer_identity]}
            mutations.append(SupportingMutation("business_parties", party["id"], party, record_revision(store, "business_parties", party["id"])))
        if not old:
            if any(d.get("number") == normalized["number"] for d in list_records(store, "business_documents")):
                _hold("invoice_number_already_recorded")
            evidence = _evidence(namespace + ".invoice", source["account_id"], source["invoice_id"], financial)
            _add(mutations, evidence, store)
            counter = [{"account": line[account], "amount": line[amount]} for line in normalized["lines"]
                       for amount, account in (("net_amount", "account"), ("tax_amount", "tax_account")) if Decimal(line[amount])]
            _, origin_entries, origin_mutations, replay = items._item_effect(entity, {"operation_id": request["operation_id"], "id": doc_id,
                "kind": "receivable", "account": normalized["control_account"], "original_amount": _usd(total), "currency": "USD",
                "as_of": normalized["date"], "due_date": normalized["due_date"], "counterparty": party["display_name"], "reference": normalized["number"],
                "origin": {"mode": "new", "source_id": claim_id, "lines": counter}, "evidence_ids": [evidence.record_id]}, pending_evidence=mutations)
            if replay:
                _hold("provider_origin_already_recorded")
            entries.extend(origin_entries)
            mutations.extend(replace(m, payload={**m.payload, "party_id": party["id"]}) if m.table == "items" else m for m in origin_mutations)
            doc = {**normalized, "id": doc_id, "party_snapshot": party, "item_id": doc_id, "status": "open",
                   "evidence_ids": [evidence.record_id], "issued_snapshot_hash": canonical_hash(normalized)}
        else:
            doc = deepcopy(old)
            if "provider_billing_snapshot" not in doc:
                doc["provider_billing_snapshot"] = normalized["provider_billing_snapshot"]
        for credit in credits:
            credit_id = "stripe-credit:" + canonical_hash({"account_id": source["account_id"], "livemode": source["livemode"], "credit_id": credit["id"]})
            previous = load_record(store, "intents", credit_id)
            digest = canonical_hash(credit)
            if previous:
                if previous["facts_hash"] != digest:
                    _hold("recorded_credit_facts_changed")
                continue
            evidence = _evidence(namespace + ".credit", source["account_id"], credit["id"], credit)
            _add(mutations, evidence, store)
            _, credit_entries, credit_mutations, replay = items._adjustment_effect(entity, {
                "operation_id": credit_id, "item_id": doc["item_id"], "action": "credit", "amount": _usd(credit["amount"]),
                "date": credit["date"], "reason": "Captured provider credit allocated to its original invoice lines",
                "source_id": credit_id, "counter_lines": credit["counter_lines"], "evidence_ids": [evidence.record_id]}, pending_mutations=mutations)
            if replay:
                _hold("provider_credit_already_claimed")
            entries.extend(credit_entries); mutations.extend(credit_mutations)
            requires_adjustments = old is not None
            mutations.append(SupportingMutation("intents", credit_id, {"id": credit_id, "kind": "stripe_invoice_credit",
                "facts_hash": digest, "document_id": doc_id}))
        sources = {}
        for row in request["payment_sources"]:
            _strict(row, {"payment_id", "source_id", "mode"}, {"payment_id", "source_id", "mode"})
            if row["payment_id"] in sources or row["mode"] not in {"new", "existing"}:
                _hold("invalid_payment_source_mapping")
            sources[row["payment_id"]] = row
        if set(sources) != {p["id"] for p in paid}:
            _hold("payment_source_mapping_required")
        from .stripe import _financial
        from .reports.cashview import _cumulative
        for payment in paid:
            mapping, balance = sources[payment["id"]], payment["balance"]
            sid = "stripe:" + source["account_id"] + ":" + balance["id"]
            if mapping["source_id"] != sid:
                _hold("canonical_payment_source_mismatch")
            payment_key = canonical_hash({"account_id": source["account_id"], "livemode": source["livemode"], "payment_id": payment["id"]})
            pid = "stripe-payment-" + payment_key
            balance_digest = canonical_hash(_financial(balance, source["account_id"], source["livemode"]))
            payment_facts = {"date": payment["date"], "gross_amount": _usd(payment["gross"]), "cash_amount": _usd(payment["gross"]),
                             "fee_amount": _usd(payment["fee"]), "source_id": sid, "balance_digest": balance_digest, "party_id": party["id"], "item_id": doc["item_id"]}
            payment_hash = canonical_hash(payment_facts)
            prior = load_record(store, "intents", "stripe-payment:" + payment_key)
            existing = load_record(store, "business_payments", (prior or {}).get("business_payment_id", pid))
            if prior:
                if prior["facts_hash"] != payment_hash or not existing:
                    _hold("recorded_payment_facts_changed")
                if any(p.get("refund_of") == existing["id"] for p in list_records(store, "business_payments")):
                    _hold("canonical_provider_refund_divergence")
                items._find_live_entry(store, sid)
                details["payment_matches"].append({"payment_id": payment["id"], "source_id": sid, "status": "matched"})
                continue
            bank = schedules._account(store, request.get("processor_account"), "processor_account", ("Assets:",))
            from .source_ids import source_id_variants
            candidates = [p for p in list_records(store, "business_payments") if source_id_variants(p["source"]["source_id"]) & source_id_variants(sid)]
            def claim_payment(business_payment_id):
                mutations.append(SupportingMutation("intents", "stripe-payment:" + payment_key,
                    {"id": "stripe-payment:" + payment_key, "kind": "stripe_invoice_payment", "facts_hash": payment_hash,
                     "document_id": doc_id, "payment_id": payment["id"], "business_payment_id": business_payment_id}))
                key = "stripe-balance:" + canonical_hash({"account": source["account_id"], "livemode": source["livemode"], "id": balance["id"]})
                value = {"id": key, "kind": "stripe_invoice_balance", "financial_digest": balance_digest, "source_id": sid, "document_id": doc_id}
                previous = load_record(store, "intents", key)
                if previous and previous != value:
                    _hold("balance_source_already_claimed")
                if previous is None:
                    mutations.append(SupportingMutation("intents", key, value))
            if candidates:
                original_id = _existing_payment(payment, candidates, doc, bank, request.get("fee_account"), store)
                claim_payment(original_id)
                details["payment_matches"].append({"payment_id": payment["id"], "source_id": sid, "status": "matched", "business_payment_id": original_id})
                continue
            evidence = _evidence(namespace + ".payment", source["account_id"], payment["id"], payment_facts)
            _add(mutations, evidence, store)
            settlement_source = {"mode": mapping["mode"], "source_id": sid, "bank_account": bank, "amount": _usd(payment["net"])}
            if payment["fee"]:
                settlement_source.update(fee_amount=_usd(payment["fee"]), fee_account=request.get("fee_account"))
            read, rows_for, _ = projected_reads(store, mutations)
            item = read("items", doc["item_id"])
            already = sum(Decimal(a["amount"]) for a in rows_for("allocations") if a.get("item_id") == item["id"] and a.get("kind") == "settlement")
            weights = [(r["account"], int(Decimal(r["amount"]) * 100)) for r in item["origin"].get("lines", [])]
            if not weights:
                _hold("existing_item_recognition_lines_required")
            before, after = _cumulative(weights, int(already * 100)), _cumulative(weights, int(already * 100) + payment["gross"])
            _, settlement_entries, settlement_mutations, replay = items._settlement_effect(entity, {
                "operation_id": "stripe-payment-" + payment_key, "date": payment["date"], "source": settlement_source,
                "allocations": [{"item_id": doc["item_id"], "amount": _usd(payment["gross"])}], "evidence_ids": [evidence.record_id]}, pending_mutations=mutations)
            if replay:
                _hold("payment_source_already_claimed")
            entries.extend(settlement_entries); mutations.extend(settlement_mutations)
            record = {"id": pid, "party_id": party["id"], "date": payment["date"], "currency": "USD", "direction": "receive",
                      "gross_amount": _usd(payment["gross"]), "cash_amount": _usd(payment["gross"]), "federal_withholding": "0.00",
                      "fee_amount": _usd(payment["fee"]), "payment_method": "unknown", "source": settlement_source,
                      "allocations": [{"item_id": doc["item_id"], "amount": _usd(payment["gross"]), "tax_category": "unknown", "classification_verified": False}],
                      "evidence_ids": [evidence.record_id], "recognition_lines": [{"account": account, "amount": _usd(new-old)} for (account, _), old, new in zip(weights, before, after) if new != old]}
            mutations.append(SupportingMutation("business_payments", pid, record))
            claim_payment(pid)
            details["payment_matches"].append({"payment_id": payment["id"], "source_id": sid, "status": "settled", "mode": mapping["mode"]})
        if invoice["status"] == "void" and doc.get("status") != "void":
            void_date = _day((invoice.get("status_transitions") or {}).get("voided_at"), zone)
            evidence = _evidence(namespace + ".invoice.void", source["account_id"], invoice["id"], {"date": void_date, "amount": _usd(total)})
            _add(mutations, evidence, store)
            lines = [{"account": l[a], "amount": l[k]} for l in doc["lines"] for k, a in (("net_amount", "account"), ("tax_amount", "tax_account")) if Decimal(l[k])]
            _, corrections, changes, _ = items._adjustment_effect(entity, {"operation_id": "stripe-void-" + identity,
                "item_id": doc["item_id"], "action": "credit", "amount": _usd(total), "date": void_date,
                "reason": "Provider void supported by the captured invoice", "source_id": "stripe-void-" + identity,
                "counter_lines": lines, "evidence_ids": [evidence.record_id]}, pending_mutations=mutations)
            entries.extend(corrections); mutations.extend(changes); doc["status"] = "void"
            requires_adjustments = old is not None
        read, rows_for, _ = projected_reads(store, mutations)
        actual_remaining = items._remaining(read("items", doc["item_id"]), rows_for("allocations"))
        if actual_remaining != Decimal(_usd(0 if doc["status"] == "void" else remaining)):
            _hold("canonical_provider_balance_divergence")
        external = {"provider": "stripe", "account_id": source["account_id"], "livemode": source["livemode"],
                    "invoice_id": source["invoice_id"], "source_hash": source["source_hash"]}
        doc.update(external_source=external)
        if not old:
            doc["provider_managed"] = True
        elif not claim:
            doc.setdefault("provider_managed", False)
        if not old or doc != old:
            mutations.append(SupportingMutation("business_documents", doc_id, doc, revision))
        updated_claim = {"id": claim_id, "kind": "stripe_invoice", "document_id": doc_id, "financial_hash": financial_hash,
                         "external_source": external, "payment_ids": sorted(p["id"] for p in paid), "credit_ids": sorted(c["id"] for c in credits)}
        if claim != updated_claim:
            mutations.append(SupportingMutation("intents", claim_id, updated_claim, record_revision(store, "intents", claim_id)))
        journals._check_dates(entity, store, [e.date.isoformat() for e in entries], "business_change")
        details.update(status="ready" if not old or entries else "matched", document_id=doc_id, party_id=party["id"], external_source=external,
                       requires_adjustments=requires_adjustments,
                       document={**doc, "remaining_amount": _usd(0 if doc["status"] == "void" else remaining)})
        return request, entries, _coalesce(mutations), details
    except Held as error:
        details["blockers"] = [{"code": error.code}]
        return request, [], [], details
    except journals.JournalError as error:
        details["blockers"] = [{"code": error.code}]
        return request, [], [], details
