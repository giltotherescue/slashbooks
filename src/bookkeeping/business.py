"""Provider-independent commercial records committed with the canonical ledger.

Drafts have no financial effect. Issuing a document creates an ordinary supported
item; settlement, credits and cash reports keep using the accounting engine.
Sensitive tax identity values never belong to this command or its receipts.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import csv
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import io
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .entity import Entity, load_entity
from .ledger.store import LedgerStore
from .ledger.importer import _entity_write_lock, check_store_integrity
from .accrual import items, journals, schedules
from .accrual.intake import canonical_hash, normalize_amount, normalize_evidence, source_identity, facts_hash, evidence_hash
from .accrual.store import SupportingMutation, load_record, list_records, record_revision

BUSINESS_TABLES = ("parties", "documents", "payments", "tax_preparations")
PAYMENT_METHODS = {"bank_transfer", "check", "cash", "card", "payment_network", "unknown"}
TAX_CATEGORIES = {"nonemployee_compensation", "legal_services", "medical_healthcare", "rent", "royalties", "other_income", "attorney_gross_proceeds", "not_reportable", "unknown"}
ACTIONS = ("party.save", "party.archive", "party.link_legacy", "party.import", "document.save", "document.issue", "document.void", "document.writeoff", "document.credit", "payment.record", "payment.link", "payment.refund", "payment.classify", "credit.apply", "tax.prepare", "tax.review", "tracking.enable")
SENSITIVE_KEYS = {"tin", "ssn", "ein", "tax_id", "taxpayer_identification_number", "full_tin"}


def _error(code: str, message: str) -> None:
    raise journals.JournalError(code, message)


def _bounded(value: Any, label: str, limit: int = 2000) -> str:
    value = journals._text(value, label)
    if len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        _error("invalid_text", f"{label} must be bounded text without control characters.")
    return value


def _reject_sensitive(value: Any) -> None:
    from .tax1099 import reject_protected_data
    reject_protected_data(value, allow_opaque_ids=True)


def _fields(payload: Any, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(payload, Mapping) or set(payload) - allowed or required - set(payload):
        _error("invalid_business_request", "Supply only the documented fields and all required facts.")
    _reject_sensitive(payload)
    return deepcopy(dict(payload))


def _money(value: Any, *, zero: bool = True) -> Decimal:
    number = Decimal(normalize_amount(value))
    if number < 0 or (not zero and number == 0):
        _error("invalid_amount", "Amount must be positive." if not zero else "Amount cannot be negative.")
    return number


def _table(resource: str) -> str:
    if resource not in BUSINESS_TABLES:
        _error("unknown_resource", "Unsupported business record resource.")
    return "business_" + resource


def _store(entity: Entity) -> LedgerStore:
    store = journals._store(entity)
    if store.schema_version() != 3:
        _error("business_tracking_required", "Start invoices and bills with business tracking.enable before saving commercial records.")
    return store


def _revision(store: LedgerStore, resource: str, identity: str, expected: Any) -> int:
    if type(expected) is not int or expected < 0:
        _error("invalid_revision", "expected_revision must be a nonnegative integer.")
    actual = record_revision(store, _table(resource), identity)
    if actual != expected:
        _error("stale_record", f"{resource}/{identity} changed; read its current revision and preview again.")
    return actual


def upgrade_business(store: LedgerStore, *, operation_id: str | None = None, plan_hash: str | None = None) -> Path | None:
    """Explicit sealed additive migration, without changing any historical entry."""
    with _entity_write_lock(store.path.parent):
        store.upgrade()
        if store.schema_version() == 3:
            return None
        integrity = check_store_integrity(store)
        if integrity.status != "ok":
            _error("ledger_integrity", "Cannot upgrade commercial storage while ledger integrity is invalid.")
        backup = store._backup("schema-v2")
        with store.transaction(immediate=True) as conn:
            prior = store.content_digest(conn)
            for name in BUSINESS_TABLES:
                conn.execute(f"CREATE TABLE business_{name} (id TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version=1), revision INTEGER NOT NULL CHECK(revision>0), payload_json TEXT NOT NULL)")
            store.set_meta("schema_version", "3", conn)
            if operation_id is not None:
                store.set_meta("business_setup_operation_id", operation_id, conn)
                store.set_meta("business_setup_plan_hash", plan_hash, conn)
            store.append_audit_event("schema-upgraded", {"from_version": 2, "to_version": 3, "prior_store_sha256": prior, "backup": backup.name}, conn)
            store.append_audit_event("ledger-store-sealed", {"session_id": "business-schema-upgrade", "entries": 0, "store_sha256": store.content_digest(conn)}, conn)
        return backup


def _enable_preview(entity: Entity, request: Mapping[str, Any]) -> dict:
    request = _fields(request, {"operation_id", "action", "tracking_start"}, {"operation_id", "action", "tracking_start"})
    request["operation_id"] = journals._id(request["operation_id"])
    request["tracking_start"] = journals._day(request["tracking_start"], "tracking_start").isoformat()
    entity = load_entity(entity.path)
    store = LedgerStore(entity.path / "ledger.sqlite")
    from .accrual.enable import preview_enable
    child = None
    if entity.entity_config.get("accrual_capability_version") != 1:
        child = preview_enable(entity, request["tracking_start"], request["operation_id"] + ":tracking")
    elif entity.entity_config.get("accrual_tracking_start") and entity.entity_config["accrual_tracking_start"] != request["tracking_start"]:
        _error("tracking_start_conflict", "The confirmed tracking start cannot change.")
    if check_store_integrity(store).status != "ok":
        _error("ledger_integrity", "Repair ledger integrity before starting commercial tracking.")
    body = {"schema_version": 1, "kind": "business_enable", "id": request["operation_id"], "request": request,
            "ledger_digest": store.content_digest(), "configuration_hash": canonical_hash(entity.entity_config),
            "effects": {"ledger_schema": {"from": store.schema_version(), "to": 3}, "basis": entity.entity_config.get("basis", "cash"), "entries_changed": 0, "tracking": child},
            "warnings": child["warnings"] if child else []}
    body["request_hash"] = canonical_hash({"kind": body["kind"], "request": request})
    body["plan_hash"] = canonical_hash(body)
    return body


def _enable_apply(entity: Entity, plan: Mapping[str, Any]) -> dict:
    from .accrual.enable import apply_enable
    from .accrual.store import apply_mutations
    plan = deepcopy(dict(plan))
    if canonical_hash({k: v for k, v in plan.items() if k != "plan_hash"}) != plan.get("plan_hash"):
        _error("plan_changed", "The business setup preview changed.")
    request = plan["request"]
    if canonical_hash({"kind": "business_enable", "request": request}) != plan.get("request_hash"):
        _error("plan_changed", "The business setup request changed.")
    with _entity_write_lock(entity.path):
        entity = load_entity(entity.path)
        store = LedgerStore(entity.path / "ledger.sqlite")
        if store.schema_version() in {2, 3}:
            prior = journals._existing_receipt(entity, request["operation_id"], plan["request_hash"]) if entity.entity_config.get("accrual_capability_version") == 1 else None
            if prior:
                return {**prior, "status": "already_applied"}
        child = plan["effects"]["tracking"]
        child_committed = load_record(store, "intents", "operation:" + request["operation_id"] + ":tracking") if store.schema_version() in {2, 3} else None
        migration_committed = store.schema_version() == 3 and store.get_meta("business_setup_operation_id") == request["operation_id"] and store.get_meta("business_setup_plan_hash") == plan["plan_hash"]
        if migration_committed and not child and canonical_hash(entity.entity_config) != plan["configuration_hash"]:
            _error("stale_preview", "Company configuration changed after the committed business storage upgrade.")
        if not child_committed and not migration_committed:
            fresh = _enable_preview(entity, request)
            if fresh["plan_hash"] != plan["plan_hash"]:
                _error("stale_preview", "Company state changed after the business setup preview.")
        if child:
            apply_enable(entity, child)
        backup = upgrade_business(store, operation_id=request["operation_id"], plan_hash=plan["plan_hash"])
        receipt = {"status": "applied", "id": request["operation_id"], "kind": "business_enable", "request_hash": plan["request_hash"], "plan_hash": plan["plan_hash"], "basis": plan["effects"]["basis"], "entries_changed": 0, "backup": backup.name if backup else None}
        record = {"id": "operation:" + receipt["id"], "kind": "business_enable", "request_hash": plan["request_hash"], "request": request, "plan": plan, "receipt": receipt}
        with store.transaction(immediate=True) as conn:
            apply_mutations(store, [SupportingMutation("intents", record["id"], record)], conn)
            store.append_audit_event("business-enabled", {"id": receipt["id"], "plan_hash": plan["plan_hash"]}, conn)
            store.append_audit_event("ledger-store-sealed", {"session_id": receipt["id"], "entries": 0, "store_sha256": store.content_digest(conn)}, conn)
        return receipt


def _address(value: Any) -> dict:
    row = _fields(value, {"line1", "line2", "city", "region", "postal_code", "country"})
    return {key: _bounded(text, "address." + key, 300) for key, text in row.items()}


def _party(entity: Entity, request: dict) -> list[SupportingMutation]:
    store = _store(entity)
    action = request["action"]
    allowed = {"operation_id", "action", "id", "expected_revision"}
    if action == "party.save":
        allowed |= {"display_name", "legal_name", "roles", "email", "address", "terms_days", "external_ids"}
    elif action == "party.link_legacy":
        allowed |= {"item_ids", "reason"}
    request.update(_fields(request, allowed, {"id", "expected_revision"}))
    identity = journals._id(request["id"])
    revision = _revision(store, "parties", identity, request["expected_revision"])
    old = load_record(store, "business_parties", identity)
    if action != "party.save" and not old:
        _error("missing_party", "Read an existing party before archiving or linking history.")
    if action == "party.archive":
        record = {**old, "status": "archived"}
    elif action == "party.link_legacy":
        ids = request.get("item_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or len(set(ids)) != len(ids):
            _error("invalid_legacy_links", "Supply unique legacy item IDs and a reason.")
        _bounded(request.get("reason"), "reason")
        mutations = []
        linked = list(old.get("legacy_item_ids", []))
        for item_id in ids:
            item = load_record(store, "items", journals._id(item_id))
            if not item or (item.get("party_id") and item["party_id"] != identity):
                _error("legacy_party_conflict", "Legacy item is missing or already linked to another party.")
            mutations.append(SupportingMutation("items", item_id, {**item, "party_id": identity}, record_revision(store, "items", item_id)))
            if item_id not in linked:
                linked.append(item_id)
        record = {**old, "legacy_item_ids": linked}
        return [SupportingMutation("business_parties", identity, record, revision), *mutations]
    else:
        roles = request.get("roles")
        if not isinstance(roles, list) or not roles or set(roles) - {"customer", "vendor"} or len(set(roles)) != len(roles):
            _error("invalid_roles", "A party needs customer and/or vendor roles.")
        if old:
            used = {"customer" if i["kind"] == "receivable" else "vendor" for i in list_records(store, "items") if i.get("party_id") == identity and i.get("kind") in {"receivable", "payable"}}
            if used - set(roles):
                _error("party_role_in_use", "A historical customer/vendor role cannot be removed.")
        record = {"id": identity, "display_name": _bounded(request.get("display_name"), "display_name", 300), "roles": sorted(roles), "status": "active"}
        for name in ("legal_name", "email"):
            if name in request:
                record[name] = _bounded(request[name], name, 300)
        if "email" in record and ("@" not in record["email"] or " " in record["email"]):
            _error("invalid_email", "Supply one valid billing email.")
        if "address" in request:
            record["address"] = _address(request["address"])
        if "terms_days" in request:
            if type(request["terms_days"]) is not int or not 0 <= request["terms_days"] <= 365:
                _error("invalid_terms", "terms_days must be an integer from zero to 365.")
            record["terms_days"] = request["terms_days"]
        if old and old.get("legacy_item_ids"):
            record["legacy_item_ids"] = old["legacy_item_ids"]
        externals = request.get("external_ids", [])
        if not isinstance(externals, list) or len(externals) > 100:
            _error("invalid_external_ids", "External identity mappings must be bounded.")
        record["external_ids"] = []
        for external in externals:
            external = _fields(external, {"namespace", "account_id", "object_id"}, {"namespace", "account_id", "object_id"})
            external = {k: _bounded(v, "external_id", 300) for k, v in external.items()}
            if any(external in p.get("external_ids", []) for p in list_records(store, "business_parties") if p["id"] != identity):
                _error("external_identity_conflict", "This external identity already belongs to another party.")
            if external in record["external_ids"]:
                _error("duplicate_external_identity", "External identities cannot repeat.")
            record["external_ids"].append(external)
    return [SupportingMutation("business_parties", identity, record, revision)]


def calculate_document(value: Mapping[str, Any]) -> dict:
    """Calculate explicit per-line discounts/tax; never determine a tax rate."""
    lines = value.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 200:
        _error("invalid_document_lines", "Supply one to 200 document lines.")
    output, subtotal, discount, taxes = [], Decimal(0), Decimal(0), Decimal(0)
    for row in lines:
        row = _fields(row, {"description", "quantity", "unit_price", "account", "discount_amount", "tax_amount", "tax_account", "tax_category", "classification_verified"}, {"description", "quantity", "unit_price", "account"})
        try:
            if not isinstance(row["quantity"], str) or not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,6})?", row["quantity"]):
                raise ValueError
            quantity = Decimal(row["quantity"])
            if quantity <= 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            _error("invalid_quantity", "quantity must be a positive decimal string with at most six fraction digits.")
        price = _money(row["unit_price"])
        base = (quantity * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cut = _money(row.get("discount_amount", "0.00"))
        tax = _money(row.get("tax_amount", "0.00"))
        if cut > base:
            _error("excess_discount", "A line discount cannot exceed its extended price.")
        if tax and not row.get("tax_account"):
            _error("tax_account_required", "An explicit tax amount needs its confirmed tax account.")
        line = {**row, "description": _bounded(row["description"], "description"), "unit_price": f"{price:.2f}", "discount_amount": f"{cut:.2f}", "tax_amount": f"{tax:.2f}", "net_amount": f"{base-cut:.2f}", "total_amount": f"{base-cut+tax:.2f}"}
        if "tax_category" in row and row["tax_category"] not in TAX_CATEGORIES:
            _error("invalid_tax_category", "Unsupported payment reporting category.")
        if "classification_verified" in row and type(row["classification_verified"]) is not bool:
            _error("invalid_classification", "classification_verified must be true or false.")
        output.append(line); subtotal += base; discount += cut; taxes += tax
    total = subtotal - discount + taxes
    if total <= 0:
        _error("invalid_document_total", "Document total must be positive.")
    return {"lines": output, "subtotal": f"{subtotal:.2f}", "discount_amount": f"{discount:.2f}", "tax_amount": f"{taxes:.2f}", "total_amount": f"{total:.2f}", "rounding": "ROUND_HALF_UP per line to USD cents"}


def _document_save(entity: Entity, request: dict):
    request = _fields(request, {"operation_id", "action", "id", "expected_revision", "kind", "party_id", "date", "due_date", "reference", "currency", "control_account", "lines", "evidence_ids"}, {"id", "expected_revision", "kind", "party_id", "date", "due_date", "currency", "control_account", "lines"})
    store = _store(entity)
    identity = journals._id(request["id"])
    revision = _revision(store, "documents", identity, request["expected_revision"])
    old = load_record(store, "business_documents", identity)
    if old and old["status"] != "draft":
        _error("immutable_document", "Issued documents are immutable; use an explicit credit or correction.")
    if request["kind"] not in {"invoice", "bill"} or request["currency"] != "USD" or entity.currency != "USD":
        _error("unsupported_document", "Commercial documents support USD invoices and bills only.")
    party = load_record(store, "business_parties", journals._id(request["party_id"]))
    required = "customer" if request["kind"] == "invoice" else "vendor"
    if not party or party.get("status") != "active" or required not in party["roles"]:
        _error("party_role_required", "Select an active party with the document's customer/vendor role.")
    when = journals._day(request["date"], "date").isoformat()
    due = journals._day(request["due_date"], "due_date").isoformat()
    if due < when:
        _error("invalid_due_date", "Due date cannot precede document date.")
    calculated = calculate_document(request)
    account = schedules._account(store, request["control_account"], "control_account", ("Assets:",) if request["kind"] == "invoice" else ("Liabilities:",))
    for line in calculated["lines"]:
        schedules._account(store, line["account"], "line.account", ("Income:",) if request["kind"] == "invoice" else ("Expenses:", "Assets:"))
        if Decimal(line["tax_amount"]):
            schedules._account(store, line["tax_account"], "tax_account", ("Liabilities:",) if request["kind"] == "invoice" else ("Expenses:", "Assets:"))
    ids = schedules._evidence_ids(request.get("evidence_ids", []))
    journals._evidence(store, ids)
    record = {"id": identity, "kind": request["kind"], "party_id": party["id"], "party_snapshot": deepcopy(party), "date": when, "due_date": due, "currency": "USD", "control_account": account, "status": "draft", "evidence_ids": ids, **calculated}
    if "reference" in request:
        record["reference"] = _bounded(request["reference"], "reference", 200)
    return request, [], [SupportingMutation("business_documents", identity, record, revision)], None


def _document_effect(entity: Entity, request: dict):
    request = _fields(request, {"operation_id", "action", "id", "expected_revision", "date", "amount", "counter_account", "reason", "evidence_ids", "number"}, {"id", "expected_revision"})
    store = _store(entity)
    identity = journals._id(request["id"])
    revision = _revision(store, "documents", identity, request["expected_revision"])
    doc = load_record(store, "business_documents", identity)
    if not doc:
        _error("missing_document", "Read an existing document first.")
    action = request["action"]
    if action == "document.issue":
        _fields(request, {"operation_id", "action", "id", "expected_revision", "number"})
        if doc["status"] != "draft":
            _error("immutable_document", "This document has already been issued or voided.")
        default_number = doc.get("reference", identity)
        if doc["kind"] == "invoice" and "reference" not in doc:
            previous_numbers = [int(d["number"][4:]) for d in list_records(store, "business_documents") if re.fullmatch(r"INV-[0-9]+", d.get("number", ""))]
            default_number = f"INV-{max(previous_numbers, default=0)+1:06d}"
        number = _bounded(request.get("number", default_number), "number", 200)
        if doc["kind"] == "invoice" and any(d.get("number") == number and d["id"] != identity and d["kind"] == "invoice" for d in list_records(store, "business_documents")):
            _error("duplicate_invoice_number", "Invoice number already belongs to another document.")
        lines = []
        for line in doc["lines"]:
            if Decimal(line["net_amount"]):
                lines.append({"account": line["account"], "amount": line["net_amount"]})
            if Decimal(line["tax_amount"]):
                lines.append({"account": line["tax_account"], "amount": line["tax_amount"]})
        # Stored self-authored invoice facts are supporting evidence, not an estimate.
        envelope = normalize_evidence({"schema_version": 1, "source": {"namespace": "slashbooks.business", "account_id": "local", "object_id": identity, "version": str(revision)}, "currency": "USD", "dates": {"document_date": doc["date"]}, "amounts": {"total": doc["total_amount"]}, "observed_facts": {"document": doc, "number": number}})
        evidence_id = canonical_hash(envelope)
        evidence = {"id": evidence_id, "envelope": envelope, "source_identity": source_identity(envelope), "facts_hash": facts_hash(envelope), "evidence_hash": evidence_hash(envelope)}
        pending = [SupportingMutation("evidence", evidence_id, evidence)]
        payload = {"operation_id": request["operation_id"], "id": identity, "kind": "receivable" if doc["kind"] == "invoice" else "payable", "account": doc["control_account"], "currency": "USD", "original_amount": doc["total_amount"], "as_of": doc["date"], "due_date": doc["due_date"], "counterparty": doc["party_snapshot"]["display_name"], "reference": number, "origin": {"mode": "new", "source_id": "business-document:" + identity, "lines": lines}, "evidence_ids": [*doc["evidence_ids"], evidence_id]}
        _, entries, mutations, replay = items._item_effect(entity, payload, pending_evidence=pending)
        if replay:
            _error("duplicate_document_origin", "This document evidence is already recorded; inspect the original receipt.")
        mutations = [SupportingMutation(m.table, m.record_id, {**m.payload, "party_id": doc["party_id"]}, m.expected_revision) if m.table == "items" else m for m in mutations]
        record = {**doc, "status": "open", "number": number, "item_id": identity, "issued_snapshot_hash": canonical_hash({**doc, "number": number})}
        return request, entries, [*pending, *mutations, SupportingMutation("business_documents", identity, record, revision)], None
    if doc["status"] == "draft":
        if action != "document.void":
            _error("draft_not_posted", "Drafts have no balance to credit or write off.")
        _bounded(request.get("reason"), "reason")
        return request, [], [SupportingMutation("business_documents", identity, {**doc, "status": "void", "reason": request["reason"]}, revision)], None
    item = load_record(store, "items", doc["item_id"])
    if not item or Decimal(item["remaining_amount"]) == 0:
        _error("inactive_document", "The document has no outstanding balance to adjust.")
    if action == "document.void" and Decimal(item["remaining_amount"]) != Decimal(item["original_amount"]):
        _error("paid_document_void", "A partly paid document needs a credit and an explicit payment refund.")
    when = journals._day(request.get("date"), "date").isoformat()
    amount = _money(request.get("amount", item["remaining_amount"]), zero=False)
    if action == "document.void" and amount != Decimal(item["remaining_amount"]):
        _error("partial_void_forbidden", "Voiding must reverse the whole unpaid document; use a credit for a partial reduction.")
    counter = request.get("counter_account")
    correction = {}
    indexed_lines = None
    if counter is not None:
        correction["counter_account"] = counter
    elif action == "document.writeoff":
        _error("writeoff_account_required", "Confirm the write-off account; tax liability remains as recorded.")
    else:
        weights, indices = [], []
        indexed_lines = [{"line_index": index, "net_amount": "0.00", "tax_amount": "0.00", "net_minor_units": "0", "tax_minor_units": "0"} for index in range(len(doc["lines"]))]
        for index, line in enumerate(doc["lines"]):
            for field, account_field in (("net", "account"), ("tax", "tax_account")):
                if Decimal(line[field + "_amount"]):
                    weights.append((line[account_field], int(Decimal(line[field + "_amount"]) * 100)))
                    indices.append((index, field))
        cents = int(amount * 100)
        # Credits have cumulative per-line rounding: repeated partial credits
        # cannot accumulate more tax/expense than the original document.
        already = sum((int(Decimal(a["amount"]) * 100) for a in list_records(store, "allocations") if a.get("item_id") == doc["item_id"] and a.get("kind") == "credit"), 0)
        from .reports.cashview import _cumulative
        before = _cumulative(weights, already)
        after = _cumulative(weights, already + cents)
        correction["counter_lines"] = [{"account": a, "amount": f"{Decimal(new-old)/100:.2f}"} for (a, _), old, new in zip(weights, before, after) if new != old]
        for (index, field), old, new in zip(indices, before, after):
            indexed_lines[index][field + "_amount"] = f"{Decimal(new-old)/100:.2f}"
            indexed_lines[index][field + "_minor_units"] = str(new-old)
    payload = {"operation_id": request["operation_id"], "item_id": doc["item_id"], "action": "writeoff" if action == "document.writeoff" else "credit", "amount": f"{amount:.2f}", "date": when, **correction, "evidence_ids": request.get("evidence_ids", []), "reason": request.get("reason"), "source_id": request["operation_id"]}
    _, entries, mutations, replay = items._adjustment_effect(entity, payload)
    if replay:
        _error("duplicate_adjustment", "This adjustment already has an accounting receipt.")
    state = "void" if action == "document.void" else "uncollectible" if action == "document.writeoff" and amount == Decimal(item["remaining_amount"]) else doc["status"]
    allocated = {"document_id": identity, "operation_id": request["operation_id"], "action": action.removeprefix("document."),
                 "document_revision": revision + 1, "issued_snapshot_hash": doc["issued_snapshot_hash"],
                 "amount": f"{amount:.2f}", "amount_minor_units": str(int(amount * 100)), "lines": indexed_lines}
    record = {**doc, "status": state, "last_adjustment_id": request["operation_id"], "last_adjustment_date": when, "last_adjustment_action": action,
              "last_adjustment": allocated}
    return request, entries, [*mutations, SupportingMutation("business_documents", identity, record, revision)], None


def _payment_source_fingerprint(payment: Mapping[str, Any]) -> str:
    """Bind immutable payment facts without scanning the ledger on every read."""
    facts = deepcopy(dict(payment))
    for field in ("payment_method", "network_qualifies_1099k"):
        facts.pop(field, None)
    facts["allocations"] = [{k: v for k, v in row.items() if k not in {"tax_category", "classification_verified"}} for row in facts["allocations"]]
    return canonical_hash(facts)


def _payment_classification_effect(entity: Entity, request: dict):
    request = _fields(request, {"operation_id", "action", "id", "party_id", "expected_revision", "expected_source_fingerprint", "classifications", "payment_method", "network_qualifies_1099k", "reason", "evidence_ids"},
                      {"id", "party_id", "expected_revision", "expected_source_fingerprint", "classifications", "reason"})
    from .preparation_runtime import is_preparation
    if is_preparation():
        _error("payment_classification_requires_person", "A person must confirm payment tax classifications.")
    store = _store(entity)
    identity = journals._id(request["id"])
    revision = _revision(store, "payments", identity, request["expected_revision"])
    payment = load_record(store, "business_payments", identity)
    if not payment or payment["party_id"] != journals._id(request["party_id"]):
        _error("payment_party_mismatch", "Read the payment for this exact stable party first.")
    fingerprint = _payment_source_fingerprint(payment)
    if request["expected_source_fingerprint"] != fingerprint:
        _error("payment_source_changed", "Payment source facts changed; read the payment and preview again.")
    source = payment["source"]
    entry = items._find_live_entry(store, source["source_id"])
    matching = [p for p in entry.postings if p.account == source["bank_account"] and p.currency == payment["currency"] and p.amount == Decimal(source["amount"])]
    if entry.date.isoformat() != payment["date"] or len(matching) != 1:
        _error("payment_source_changed", "The posted source no longer matches this payment's immutable facts.")
    rows = request["classifications"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= len(payment["allocations"]):
        _error("invalid_payment_classifications", "Supply exact indexed payment allocation classifications.")
    changed = deepcopy(payment)
    before, after, seen = [], [], set()
    for row in rows:
        row = _fields(row, {"allocation_index", "tax_category", "classification_verified"}, {"allocation_index", "tax_category", "classification_verified"})
        index = row["allocation_index"]
        if type(index) is not int or not 0 <= index < len(payment["allocations"]) or index in seen:
            _error("invalid_allocation_index", "Each classification must identify a different existing allocation index.")
        seen.add(index)
        if not isinstance(row["tax_category"], str) or row["tax_category"] not in TAX_CATEGORIES or type(row["classification_verified"]) is not bool or row["tax_category"] == "unknown" and row["classification_verified"]:
            _error("invalid_tax_classification", "Choose a supported category and its explicit verification state; unknown cannot be verified.")
        prior = payment["allocations"][index]
        before.append({"allocation_index": index, "tax_category": prior["tax_category"], "classification_verified": prior["classification_verified"]})
        after.append(row)
        changed["allocations"][index].update({k: row[k] for k in ("tax_category", "classification_verified")})
    if "payment_method" in request:
        if not isinstance(request["payment_method"], str) or request["payment_method"] not in PAYMENT_METHODS:
            _error("invalid_payment_facts", "Choose a supported payment method.")
        changed["payment_method"] = request["payment_method"]
        changed.pop("network_qualifies_1099k", None)
    if "network_qualifies_1099k" in request:
        if changed["payment_method"] != "payment_network" or type(request["network_qualifies_1099k"]) is not bool:
            _error("invalid_payment_network", "Only a payment-network method accepts an explicit 1099-K qualification fact.")
        changed["network_qualifies_1099k"] = request["network_qualifies_1099k"]
    _payment_method_source(entity, source["bank_account"], changed["payment_method"])
    request["reason"] = _bounded(request["reason"], "reason")
    request["evidence_ids"] = schedules._evidence_ids(request.get("evidence_ids", []))
    journals._evidence(store, request["evidence_ids"])
    details = {"payment_id": identity, "party_id": payment["party_id"], "source_fingerprint": fingerprint,
               "source_entry_hash": canonical_hash(journals._entry_document(entry)), "before": before, "after": after,
               "method_before": {k: payment[k] for k in ("payment_method", "network_qualifies_1099k") if k in payment},
               "method_after": {k: changed[k] for k in ("payment_method", "network_qualifies_1099k") if k in changed},
               "reason": request["reason"], "evidence_ids": request["evidence_ids"]}
    return request, [], [SupportingMutation("business_payments", identity, changed, revision)], details


def _payment_method_source(entity: Entity, account: str, method: str) -> None:
    """Reject a known credit-card source mislabeled as a direct bank payment."""
    from .entity import bank_mapping_account
    mappings = entity.entity_config.get("bank_account_mappings", {})
    card = account == "Liabilities:CreditCard" or account.startswith("Liabilities:CreditCard:")
    for mapping in mappings.values() if isinstance(mappings, dict) else ():
        if bank_mapping_account(mapping) == account and isinstance(mapping, dict) and mapping.get("account_type") in {"credit", "credit_card"}:
            card = True
    if card and method not in {"card", "unknown"}:
        _error("payment_method_source_mismatch", "This credit-card source requires card or an unresolved unknown payment method.")


def _payment_effect(entity: Entity, request: dict):
    request = _fields(request, {"operation_id", "action", "id", "party_id", "date", "currency", "direction", "gross_amount", "cash_amount", "federal_withholding", "payment_method", "network_qualifies_1099k", "source", "allocations", "evidence_ids", "refund_of", "fee_amount", "fee_account", "withholding_account", "counter_account", "excess"}, {"id", "party_id", "date", "currency", "direction", "gross_amount", "cash_amount", "federal_withholding", "payment_method", "source", "allocations"})
    store = _store(entity)
    identity = journals._id(request["id"])
    if load_record(store, "business_payments", identity):
        _error("identity_conflict", "Payment ID already exists; replay its operation or record an explicit refund.")
    party = load_record(store, "business_parties", journals._id(request["party_id"]))
    if not party:
        _error("missing_party", "Payment needs a stable customer/vendor party.")
    when = journals._day(request["date"], "date").isoformat()
    if request["currency"] != "USD" or request["direction"] not in {"pay", "receive"} or request["payment_method"] not in PAYMENT_METHODS:
        _error("invalid_payment_facts", "Supply USD payment direction and an explicit supported payment method.")
    role = "vendor" if request["direction"] == "pay" else "customer"
    if request["action"] == "payment.refund" and not request.get("refund_of"):
        _error("refund_origin_required", "A refund requires the original payment ID.")
    if request.get("refund_of"):
        original = load_record(store, "business_payments", journals._id(request["refund_of"]))
        if not original or original["party_id"] != party["id"] or original["direction"] == request["direction"]:
            _error("invalid_refund", "Refund must reverse a payment for the same party.")
        if when < original["date"]:
            _error("refund_precedes_payment", "Refund date cannot precede the original payment.")
        prior = sum((Decimal(p["gross_amount"]) for p in list_records(store, "business_payments") if p.get("refund_of") == original["id"]), Decimal(0))
        if prior + _money(request["gross_amount"]) > Decimal(original["gross_amount"]):
            _error("excess_refund", "Refunds cannot exceed the original payment.")
        role = "customer" if original["direction"] == "receive" else "vendor"
    if role not in party["roles"]:
        _error("party_role_required", "Payment direction needs the corresponding customer/vendor role.")
    gross = _money(request["gross_amount"], zero=False)
    cash = _money(request["cash_amount"], zero=False)
    withholding = _money(request["federal_withholding"])
    if cash + withholding != gross:
        _error("payment_total_mismatch", "Cash plus federal withholding must equal gross payment.")
    if withholding and request["direction"] != "pay":
        _error("withholding_direction", "Federal withholding is supported only on a vendor payment.")
    if "network_qualifies_1099k" in request and type(request["network_qualifies_1099k"]) is not bool:
        _error("invalid_payment_network", "network_qualifies_1099k must be true or false.")
    allocations = request["allocations"]
    if not isinstance(allocations, list) or not 1 <= len(allocations) <= 1000:
        _error("invalid_payment_allocations", "Supply one to 1000 exact payment allocations.")
    normalized, item_allocations, total = [], [], Decimal(0)
    for row in allocations:
        row = _fields(row, {"item_id", "amount", "tax_category", "classification_verified", "evidence_ids"}, {"amount"})
        amount = _money(row["amount"], zero=False)
        category = row.get("tax_category", "unknown")
        verified = row.get("classification_verified", False)
        if category not in TAX_CATEGORIES or type(verified) is not bool:
            _error("invalid_tax_classification", "Use a supported tax category and explicit verification state.")
        normal = {**row, "amount": f"{amount:.2f}", "tax_category": category, "classification_verified": verified}
        if "item_id" in row:
            item = load_record(store, "items", journals._id(row["item_id"]))
            if not item or item.get("party_id") != party["id"]:
                _error("payment_party_mismatch", "Every allocated item must belong to this stable party.")
            item_allocations.append({"item_id": item["id"], "amount": f"{amount:.2f}"})
        normalized.append(normal); total += amount
    if total != gross:
        _error("payment_allocation_mismatch", "Payment allocations must sum exactly to gross payment.")
    ids = schedules._evidence_ids(request.get("evidence_ids", []))
    journals._evidence(store, ids)
    source = _fields(request["source"], {"mode", "source_id", "bank_account", "amount"}, {"mode", "source_id", "bank_account", "amount"})
    if source["mode"] not in {"new", "existing"}:
        _error("invalid_payment_source", "Payment source mode must be new or existing.")
    source["source_id"] = journals._id(source["source_id"])
    bank_account = schedules._account(store, source["bank_account"], "bank_account", ("Assets:", "Liabilities:CreditCard"))
    from .entity import flat_bank_account_mappings
    mapped_banks = set(flat_bank_account_mappings(entity.entity_config).values())
    if not bank_account.startswith(("Assets:Bank:", "Assets:Cash", "Assets:Processors:", "Liabilities:CreditCard")) and bank_account not in mapped_banks:
        _error("invalid_bank_account", "Payment source must be a mapped bank, cash, processor or credit-card account.")
    source["bank_account"] = bank_account
    _payment_method_source(entity, bank_account, request["payment_method"])
    bank_amount = Decimal(normalize_amount(source["amount"]))
    sign = Decimal(-1 if request["direction"] == "pay" else 1)
    fee = _money(request.get("fee_amount", "0.00"))
    if bank_amount != sign * cash - fee:
        _error("payment_bank_mismatch", "Bank movement must equal signed cash less the explicit fee.")
    # A source is one cash event; a bank import and its bill settlement must not
    # be counted as two payments, regardless of their chosen payment IDs.
    from .source_ids import source_id_variants
    source_variants = set(source_id_variants(source["source_id"]))
    if any(source_variants & set(source_id_variants(p["source"]["source_id"])) for p in list_records(store, "business_payments")):
        _error("duplicate_payment_source", "This cash source already has payment facts; inspect or revise its original payment.")
    entries, mutations, replay = [], [], None
    if request["action"] == "payment.link":
        if source["mode"] != "existing":
            _error("existing_payment_required", "Linking facts requires an already recorded cash source.")
        original_entry = items._find_live_entry(store, source["source_id"])
        if original_entry.date.isoformat() != when or not any(p.account == source["bank_account"] and p.amount == bank_amount and p.currency == "USD" for p in original_entry.postings):
            _error("payment_source_mismatch", "Payment date, account and cash amount must match the posted source.")
        from .entity import flat_bank_account_mappings
        mapped = set(flat_bank_account_mappings(entity.entity_config).values())
        cashlike = lambda a: a in mapped or a.startswith(("Assets:Bank:", "Assets:Cash", "Assets:Processors:", "Liabilities:CreditCard"))
        if len([p for p in original_entry.postings if cashlike(p.account)]) != 1 or any(p.account.startswith("Equity:") for p in original_entry.postings):
            _error("noncommercial_payment_source", "Transfers and owner-equity movements are not vendor/customer payments.")
        remaining_postings = list(original_entry.postings)
        bank_matches = [p for p in remaining_postings if p.account == bank_account and p.amount == bank_amount and p.currency == "USD"]
        if len(bank_matches) != 1:
            _error("ambiguous_payment_source", "The source needs one exact supported cash movement.")
        remaining_postings.remove(bank_matches[0])
        for component, account_key, component_amount in ((fee, "fee_account", fee), (withholding, "withholding_account", -withholding)):
            if not component:
                continue
            account = schedules._account(store, request.get(account_key), account_key, ("Expenses:",) if account_key == "fee_account" else ("Liabilities:",))
            matching = next((p for p in remaining_postings if p.account == account and p.amount == component_amount and p.currency == "USD"), None)
            if matching is None:
                _error("payment_component_mismatch", "Fee or withholding facts must match their exact posted source components.")
            remaining_postings.remove(matching)
        if sum((p.amount for p in remaining_postings), Decimal(0)) != -sign*gross or any(p.currency != "USD" for p in remaining_postings):
            _error("payment_gross_mismatch", "Gross recipient payment does not reconcile to the source postings.")
        if request["direction"] == "receive" and not request.get("refund_of") and any(p.amount < 0 and p.account.startswith(("Expenses:", "Assets:")) and not any(i.get("account") == p.account and i.get("kind") == "receivable" for i in list_records(store, "items")) for p in remaining_postings):
            _error("refund_origin_required", "A vendor refund source must link its original vendor payment; it cannot be relabeled as a customer receipt.")
        if request["direction"] == "pay" and not request.get("refund_of") and any(p.account.startswith("Income:") for p in remaining_postings):
            _error("refund_origin_required", "A customer refund source must link its original customer payment.")
        for allocation in list_records(store, "allocations"):
            if allocation.get("source_id") == source["source_id"] and allocation.get("kind") == "settlement":
                linked_item = load_record(store, "items", allocation["item_id"])
                if linked_item and linked_item.get("party_id") != party["id"]:
                    _error("payment_party_mismatch", "A settled source cannot be reassigned to another or unconfirmed party.")
        if item_allocations:
            # Facts-only linking cannot pretend an item was actually settled.
            committed = list_records(store, "allocations")
            for allocation in item_allocations:
                actual = sum((Decimal(a["amount"]) for a in committed if a.get("kind") == "settlement" and a.get("source_id") == source["source_id"] and a.get("item_id") == allocation["item_id"]), Decimal(0))
                if actual != Decimal(allocation["amount"]):
                    _error("unsettled_payment_link", "Allocate the payment through payment.record before linking an item.")
        if not ids:
            _error("evidence_required", "Payment tax facts need evidence even when cash is already posted.")
    elif request.get("refund_of"):
        # Explicit refunds preserve the original recognition allocation, and
        # move cash only once. They do not silently reopen a paid document.
        if not ids:
            _error("evidence_required", "Refunds require supporting documents.")
        if item_allocations:
            _error("refund_allocation_required", "A refund uses direct account allocation, not a second item settlement.")
        original_lines = original.get("recognition_lines", [])
        if not original_lines:
            _error("refund_recognition_missing", "Original payment needs confirmed recognition lines before an exact refund.")
        if fee or withholding:
            _error("refund_components_unsupported", "Refund fee and withholding corrections need separate explicit supported payment records.")
        from .reports.cashview import _cumulative
        weights = [(line["account"], int(Decimal(line["amount"]) * 100)) for line in original_lines]
        before = _cumulative(weights, int(prior * 100))
        after = _cumulative(weights, int((prior + gross) * 100))
        reverse_lines = [(a, -sign * Decimal(new-old)/100) for (a, _), old, new in zip(weights, before, after) if new != old]
        if source["mode"] != "new" or store.source_exists(source["source_id"]):
            _error("duplicate_refund", "A recorded refund should use payment.link with its original source.")
        entries = [schedules._entry(date.fromisoformat(when), source["source_id"], "Refund: " + party["display_name"], [(source["bank_account"], bank_amount), *reverse_lines], currency="USD")]
        for line, old_share, new_share in zip(original_lines, before, after):
            if not line.get("unapplied_item_id") or new_share == old_share:
                continue
            credit_id = line["unapplied_item_id"]
            credit = load_record(store, "items", credit_id)
            moved = Decimal(new_share-old_share)/100
            if not credit or Decimal(credit["remaining_amount"]) < moved:
                _error("refund_credit_already_applied", "The original overpayment credit was already applied; correct its target document before refunding it.")
            remaining = Decimal(credit["remaining_amount"]) - moved
            mutations.append(SupportingMutation("items", credit_id, {**credit, "remaining_amount": f"{remaining:.2f}", "status": "settled" if not remaining else "open"}, record_revision(store, "items", credit_id)))
            allocation_id = "settlement:" + request["operation_id"] + ":" + credit_id
            mutations.append(SupportingMutation("allocations", allocation_id, {"id": allocation_id, "kind": "settlement", "item_id": credit_id, "amount": f"{moved:.2f}", "signed_amount": f"{sign*moved:.2f}", "date": when, "source_id": source["source_id"], "source_mode": "new", "bank_account": bank_account, "bank_amount": f"{bank_amount:.2f}", "evidence_ids": ids}))
    elif item_allocations:
        if len(item_allocations) != len(normalized) and not request.get("excess"):
            _error("mixed_payment_allocation", "An unallocated remainder needs an explicit excess credit.")
        extended_source = dict(source)
        if fee:
            extended_source.update(fee_amount=f"{fee:.2f}", fee_account=request.get("fee_account"))
        if withholding:
            extended_source.update(withholding_amount=f"{withholding:.2f}", withholding_account=request.get("withholding_account"))
        payload = {"operation_id": request["operation_id"], "date": when, "source": extended_source, "allocations": item_allocations, "evidence_ids": ids}
        if request.get("excess"):
            excess = _fields(request["excess"], {"id", "account"}, {"id", "account"})
            payload["excess"] = {**excess, "counterparty": party["display_name"]}
        _, entries, mutations, replay = items._settlement_effect(entity, payload)
        mutations = [SupportingMutation(m.table, m.record_id, {**m.payload, "party_id": party["id"]}, m.expected_revision) if m.table == "items" else m for m in mutations]
    else:
        if not ids:
            _error("evidence_required", "Direct payments require supporting documents.")
        if source["mode"] != "new" or store.source_exists(source["source_id"]):
            _error("existing_payment_required", "Use payment.link for a payment already posted.")
        counter = schedules._account(store, request.get("counter_account"), "counter_account", ("Income:",) if sign > 0 else ("Expenses:",))
        postings = [(source["bank_account"], bank_amount), (counter, -sign * gross)]
        if fee:
            postings.append((schedules._account(store, request.get("fee_account"), "fee_account", ("Expenses:",)), fee))
        if withholding:
            postings.append((schedules._account(store, request.get("withholding_account"), "withholding_account", ("Liabilities:",)), -withholding))
        entries = [schedules._entry(date.fromisoformat(when), source["source_id"], "Payment: " + party["display_name"], postings, currency="USD")]
    if entries and not item_allocations:
        economic = {"kind": "business_direct_payment", "date": when, "party_id": party["id"], "direction": request["direction"], "gross_amount": f"{gross:.2f}", "postings": [{"account": p.account, "amount": f"{p.amount:.2f}"} for e in entries for p in e.postings]}
        claims, prior_receipt = journals._source_claims(store, journals._evidence(store, ids), canonical_hash(economic), request["operation_id"])
        mutations.extend(claims)
        replay = replay or prior_receipt
    if replay:
        _error("duplicate_payment_evidence", "This payment evidence already has an accounting receipt; link the committed payment facts.")
    request.update(id=identity, date=when, gross_amount=f"{gross:.2f}", cash_amount=f"{cash:.2f}", federal_withholding=f"{withholding:.2f}", allocations=normalized, source={**source, "amount": f"{bank_amount:.2f}"}, evidence_ids=ids)
    record = {k: deepcopy(v) for k, v in request.items() if k not in {"operation_id", "action", "counter_account", "withholding_account", "fee_account"}}
    recognition = []
    if item_allocations:
        from .reports.cashview import _cumulative
        for allocation in item_allocations:
            item = load_record(store, "items", allocation["item_id"])
            origin = item.get("origin", {})
            weights = [(r["account"], int(Decimal(r["amount"]) * 100)) for r in origin.get("lines", [])]
            if not weights and origin.get("counter_account"):
                weights = [(origin["counter_account"], int(Decimal(item["original_amount"]) * 100))]
            if weights:
                shares = _cumulative(weights, int(Decimal(allocation["amount"]) * 100))
                recognition.extend({"account": a, "amount": f"{Decimal(n)/100:.2f}"} for (a, _), n in zip(weights, shares) if n)
    elif request.get("counter_account"):
        recognition = [{"account": request["counter_account"], "amount": f"{gross:.2f}"}]
    elif request["action"] == "payment.link":
        recognition = [{"account": p.account, "amount": f"{abs(p.amount):.2f}"} for p in remaining_postings if p.amount*sign < 0]
        if sum((Decimal(r["amount"]) for r in recognition), Decimal(0)) != gross:
            _error("ambiguous_payment_recognition", "Linking mixed-direction offsets needs a supported explicit accounting correction first.")
    if request.get("excess"):
        remainder = gross - sum((Decimal(a["amount"]) for a in item_allocations), Decimal(0))
        if remainder:
            recognition.append({"account": request["excess"]["account"], "amount": f"{remainder:.2f}", "unapplied_item_id": request["excess"]["id"]})
    record["recognition_lines"] = recognition
    return request, entries, [*mutations, SupportingMutation("business_payments", identity, record)], None



def payment_candidates(entity: Entity, tax_year: int | None = None) -> list[dict]:
    """Unlinked outgoing cash sources: visible work, never guessed vendors."""
    store = LedgerStore(entity.path / "ledger.sqlite")
    payments = list_records(store, "business_payments") if store.schema_version() == 3 else []
    from .source_ids import source_id_variants
    from .entity import flat_bank_account_mappings
    linked = {variant for p in payments for variant in source_id_variants(p["source"]["source_id"])}
    mapped = set(flat_bank_account_mappings(entity.entity_config).values())
    def cashlike(account):
        return account in mapped or account.startswith(("Assets:Bank:", "Assets:Cash", "Assets:Processors:", "Liabilities:CreditCard"))
    result = []
    receivable_accounts = set()
    if store.schema_version() in {2, 3}:
        receivable_accounts = {i["account"] for i in list_records(store, "items") if i.get("kind") == "receivable"}
    for entry in store.load_entries():
        if tax_year is not None and entry.date.year != tax_year:
            continue
        if entry.source_id in linked:
            continue
        noncash = [p for p in entry.postings if not cashlike(p.account)]
        # Pure transfers, equity contributions/distributions and liabilities
        # paid down are not vendor payments. Other unsupported outflows remain
        # candidates requiring an explicit classification decision.
        if not noncash or all(p.account.startswith("Equity:") for p in noncash):
            continue
        for posting in entry.postings:
            possible_refund = posting.amount > 0 and any(p.amount < 0 and p.account.startswith(("Expenses:", "Assets:")) and p.account not in receivable_accounts for p in noncash)
            if cashlike(posting.account) and (posting.amount < 0 or possible_refund):
                result.append({"id": entry.source_id, "source_id": entry.source_id, "date": entry.date.isoformat(), "bank_account": posting.account, "amount": f"{posting.amount:.2f}", "currency": posting.currency, "historical_payee": entry.payee, "counter_accounts": sorted({p.account for p in noncash}), "reason": "possible_vendor_refund_not_linked" if possible_refund else "payment_party_and_tax_facts_not_recorded"})
    return result


def _tax_effect(entity: Entity, request: dict):
    from .tax1099 import prepare_1099, seal_preparation, verify_preparation
    store = _store(entity)
    action = request["action"]
    if action == "tax.review":
        request = _fields(request, {"operation_id", "action", "id", "expected_revision", "preparation_hash"}, {"id", "expected_revision", "preparation_hash"})
        from .preparation_runtime import is_preparation
        if is_preparation():
            _error("tax_review_requires_person", "A person must review a prepared tax package.")
        identity = journals._id(request["id"])
        revision = _revision(store, "tax_preparations", identity, request["expected_revision"])
        record = load_record(store, "business_tax_preparations", identity)
        if not record or record["preparation"]["content_hash"] != request["preparation_hash"]:
            _error("tax_preparation_changed", "Review the exact prepared version and its current hash.")
        verify_preparation(record["preparation"])
        if not record["preparation"]["ready"]:
            _error("tax_preparation_blocked", "Resolve the package's incomplete facts before review.")
        current = canonical_hash(list_records(store, "business_payments"))
        if record["source_payments_hash"] != current or payment_candidates(entity, record["tax_year"]):
            _error("tax_sources_changed", "Payments changed after preparation; create a new preparation version.")
        record = {**record, "status": "reviewed", "preparation": {**record["preparation"], "status": "reviewed"}, "reviewed_operation_id": request["operation_id"]}
        return request, [], [SupportingMutation("business_tax_preparations", identity, record, revision)], None
    request = _fields(request, {"operation_id", "action", "id", "tax_year", "payer_profile", "recipients", "coverage", "coverage_evidence_ids", "rule_version", "supersedes"}, {"id", "tax_year", "payer_profile", "recipients", "coverage", "coverage_evidence_ids"})
    identity = journals._id(request["id"])
    _revision(store, "tax_preparations", identity, 0)
    year = request["tax_year"]
    if type(year) is not int or year not in {2025, 2026}:
        _error("unsupported_tax_year", "Only explicitly supported tax years may be prepared.")
    coverage_ids = schedules._evidence_ids(request["coverage_evidence_ids"])
    journals._evidence(store, coverage_ids)
    recipients = request["recipients"]
    if not isinstance(recipients, list) or len(recipients) > 10000:
        _error("invalid_recipients", "Supply bounded confirmed recipient profiles.")
    for recipient in recipients:
        party = load_record(store, "business_parties", journals._id(recipient.get("party_id")))
        if not party or "vendor" not in party["roles"]:
            _error("recipient_party_required", "Every recipient needs a stable vendor party.")
    payments = list_records(store, "business_payments")
    # Payee text is untrusted source data and may contain protected identity.
    # The source remains available through its ordinary permissioned records;
    # tax preparations need only the stable source references and amounts.
    unmatched = [{k: v for k, v in candidate.items() if k != "historical_payee"} for candidate in payment_candidates(entity, year)]
    coverage = deepcopy(request["coverage"])
    if not isinstance(coverage, dict):
        _error("invalid_coverage", "Calendar-year coverage must be confirmed with supporting evidence.")
    if unmatched or not coverage_ids:
        coverage["complete"] = False
    by_id = {payment["id"]: payment for payment in payments}
    vendor_payments = [payment for payment in payments if (payment["direction"] == "pay" and not payment.get("refund_of")) or (payment.get("refund_of") and by_id.get(payment["refund_of"], {}).get("direction") == "pay")]
    effective_payments, method_conflicts = [], []
    for payment in vendor_payments:
        try:
            if payment["date"][:4] == str(year):
                _payment_method_source(entity, payment["source"]["bank_account"], payment["payment_method"])
        except journals.JournalError as exc:
            if exc.code != "payment_method_source_mismatch":
                raise
            # Older recorded facts remain immutable until a person corrects
            # them. A conflicting rail cannot make the current tax year ready.
            payment = {**payment, "payment_method": "unknown"}
            method_conflicts.append({"code": "payment_method_source_mismatch", "payment_id": payment["id"], "party_id": payment["party_id"]})
        effective_payments.append(payment)
    payload = {"tax_year": year, "payer_profile": request["payer_profile"], "recipients": recipients, "payments": effective_payments, "coverage": coverage}
    if "rule_version" in request:
        payload["rule_version"] = request["rule_version"]
    preparation = prepare_1099(payload)
    preparation["issues"].extend(method_conflicts)
    preparation["unmatched_sources"] = unmatched
    preparation["coverage_evidence_ids"] = coverage_ids
    preparation["source_payments_hash"] = canonical_hash(payments)
    if unmatched:
        preparation["issues"].append({"code": "unclassified_recorded_cash_sources", "source_ids": sorted({p["source_id"] for p in unmatched})})
        preparation["ready"] = False
    seal_preparation(preparation)
    record = {"id": identity, "tax_year": year, "status": "prepared", "source_payments_hash": canonical_hash(payments), "preparation": preparation}
    if request.get("supersedes"):
        previous = load_record(store, "business_tax_preparations", journals._id(request["supersedes"]))
        if not previous or previous["tax_year"] != year:
            _error("invalid_superseded_preparation", "A revision must name a prior preparation for this tax year.")
        record["supersedes"] = previous["id"]
        record["change_summary"] = {"previous_hash": previous["preparation"]["content_hash"], "current_hash": preparation["content_hash"], "previous_totals": previous["preparation"]["totals"], "current_totals": preparation["totals"]}
    return request, [], [SupportingMutation("business_tax_preparations", identity, record)], None


def _credit_effect(entity: Entity, request: dict):
    request = _fields(request, {"operation_id", "action", "date", "credit_item_id", "target_item_id", "amount", "reason", "evidence_ids"}, {"date", "credit_item_id", "target_item_id", "amount", "reason"})
    store = _store(entity)
    credit = load_record(store, "items", request["credit_item_id"])
    target = load_record(store, "items", request["target_item_id"])
    if not credit or not target or not credit.get("party_id") or credit.get("party_id") != target.get("party_id"):
        _error("credit_party_mismatch", "Credit application needs matching stable party identities.")
    payload = {k: v for k, v in request.items() if k != "action"}
    _, entries, mutations, replay = items._offset_effect(entity, payload)
    if replay:
        _error("duplicate_credit_application", "Inspect the existing credit application receipt.")
    return request, entries, mutations, None

def _effect(entity: Entity, payload: Mapping[str, Any]):
    _reject_sensitive(payload)
    if not isinstance(payload, Mapping) or payload.get("action") not in ACTIONS:
        _error("unknown_business_action", "Choose a published business action.")
    request = deepcopy(dict(payload))
    request["operation_id"] = journals._id(request.get("operation_id"))
    action = request["action"]
    if action in {"party.save", "party.archive", "party.link_legacy"}:
        return request, [], _party(entity, request), None
    if action == "party.import":
        request = _fields(request, {"operation_id", "action", "rows"}, {"rows"})
        if not isinstance(request["rows"], list) or not 1 <= len(request["rows"]) <= 1000:
            _error("invalid_party_import", "Supply one to 1000 explicit party rows.")
        mutations = []
        for row in request["rows"]:
            row = _fields(row, {"id", "expected_revision", "display_name", "legal_name", "roles", "email", "address", "terms_days", "external_ids"})
            mutations.extend(_party(entity, {**row, "operation_id": request["operation_id"], "action": "party.save"}))
        identities = [m.record_id for m in mutations]
        if len(identities) != len(set(identities)):
            _error("duplicate_party_import", "An import cannot repeat a stable party ID.")
        # Across-batch provider mappings must be unique too.
        external_ids = [canonical_hash(e) for m in mutations for e in m.payload.get("external_ids", [])]
        if len(external_ids) != len(set(external_ids)):
            _error("duplicate_external_identity", "Import rows cannot share an external identity.")
        return request, [], mutations, None
    if action == "document.save":
        return _document_save(entity, request)
    if action.startswith("document."):
        return _document_effect(entity, request)
    if action == "payment.classify":
        return _payment_classification_effect(entity, request)
    if action.startswith("payment."):
        return _payment_effect(entity, request)
    if action.startswith("tax."):
        return _tax_effect(entity, request)
    if action == "credit.apply":
        return _credit_effect(entity, request)
    _error("invalid_business_action", "Tracking activation uses its dedicated preview/apply plan.")


def preview(entity: Entity, payload: Mapping[str, Any]) -> dict:
    if payload.get("action") == "tracking.enable":
        return _enable_preview(entity, payload)
    request, entries, mutations, classification = _effect(entity, payload)
    ids = sorted({e for m in mutations if m.table == "business_payments" for e in m.payload.get("evidence_ids", [])} | {m.record_id for m in mutations if m.table == "evidence"})
    ids = sorted(set(ids) | set(request.get("evidence_ids", [])) | set(request.get("coverage_evidence_ids", [])))
    dates = sorted({e.date.isoformat() for e in entries})
    # A draft's date is editable commercial context, not a journal posting.
    details = {"action": request["action"], "records": [{"resource": m.table, "id": m.record_id, "revision": m.expected_revision + 1} for m in mutations]}
    if request["action"] == "payment.classify":
        details["payment_classification"] = classification
    for mutation in mutations:
        if mutation.table == "business_documents" and mutation.payload.get("last_adjustment_id") == request["operation_id"]:
            details["business_correction"] = mutation.payload["last_adjustment"]
    if request["action"] in {"document.save", "document.issue"}:
        doc = next(m.payload for m in mutations if m.table == "business_documents")
        details["document"] = doc
    return journals._preview_operation(entity, operation_id=request["operation_id"], kind="business_change", request=request, entries=entries, mutations=mutations, evidence_ids=ids, affected_dates=dates, details=details)


def apply(entity: Entity, plan: Mapping[str, Any]) -> dict:
    if plan.get("kind") == "business_enable":
        return _enable_apply(entity, plan)
    with _entity_write_lock(entity.path):
        receipt = schedules._apply(entity, plan, "business_change", preview, _effect)
        if plan["request"]["action"] == "payment.record" and plan["request"].get("allocations") and any(a.get("item_id") for a in plan["request"]["allocations"]):
            journals._complete_linked_source_review(entity, plan["request"]["source"]["source_id"], receipt, "settlement")
        return receipt


def _records_all(entity: Entity, resource: str, *, record_id: str | None = None, party_id: str | None = None, as_of: str | None = None) -> dict:
    store = LedgerStore(entity.path / "ledger.sqlite")
    if resource == "payment_candidates":
        return {"schema_version": 1, "resource": resource, "records": payment_candidates(entity), "ledger_digest": store.content_digest()}
    if resource in {"aging", "outstanding"}:
        if store.schema_version() == 1:
            return {"schema_version": 1, "resource": resource, "records": [], "tracking_required": True}
        from .accrual.reports import report
        value = report(entity, resource, as_of=as_of or date.today().isoformat())
        return {**value, "resource": resource, "records": [row for row in value["rows"] if party_id is None or row.get("party_id") == party_id]}
    _table(resource)
    if store.schema_version() != 3:
        return {"schema_version": 1, "resource": resource, "records": [], "tracking_required": True}
    if as_of is not None:
        as_of = journals._day(as_of, "as_of").isoformat()
    values = []
    all_parties = list_records(store, "business_parties") if resource == "parties" else []
    supported = []
    if resource in {"parties", "documents"}:
        from .accrual.reports import report
        supported = report(entity, "outstanding", as_of=as_of or date.today().isoformat())["rows"]
    supported_by_id = {item["id"]: item for item in supported}
    credits = {}
    if resource == "documents":
        for allocation in list_records(store, "allocations"):
            if allocation.get("kind") == "credit" and allocation.get("date", "") <= (as_of or date.today().isoformat()):
                item_id = allocation.get("item_id")
                credits[item_id] = credits.get(item_id, Decimal(0)) + Decimal(allocation["amount"])
    by_party = {}
    for item in supported:
        by_party.setdefault(item.get("party_id"), []).append(item)
    names, emails = {}, {}
    for party in all_parties:
        names.setdefault(party["display_name"].casefold(), []).append(party["id"])
        if party.get("email"):
            emails.setdefault(party["email"].casefold(), []).append(party["id"])
    for value in (all_parties if resource == "parties" else list_records(store, _table(resource))):
        if record_id is not None and value["id"] != record_id:
            continue
        if party_id is not None and value.get("party_id") != party_id:
            continue
        if as_of is not None and resource in {"documents", "payments"} and value.get("date", "") > as_of:
            continue
        row = {**value, "revision": record_revision(store, _table(resource), value["id"])}
        if resource == "payments":
            row["source_fingerprint"] = _payment_source_fingerprint(value)
        if resource == "documents" and value.get("item_id"):
            item = supported_by_id.get(value["item_id"])
            remaining = item["remaining_amount"] if item else "0.00"
            row["remaining_amount"] = remaining
            if as_of and row.get("last_adjustment_date", "") > as_of:
                row["status"] = "open"
            if row["status"] not in {"void", "uncollectible"}:
                fully_credited = credits.get(value["item_id"], Decimal(0)) == Decimal(row["total_amount"])
                row["status"] = "credited" if Decimal(remaining) == 0 and fully_credited else "paid" if Decimal(remaining) == 0 else "partially_paid" if Decimal(remaining) < Decimal(row["total_amount"]) else "open"
        if resource == "parties":
            row["balances"] = {"receivable": "0.00", "payable": "0.00", "customer_credit": "0.00", "vendor_credit": "0.00"}
            for item in by_party.get(row["id"], []):
                key = item["kind"] if item["kind"] in {"receivable", "payable"} else "customer_credit" if item.get("direction") == "credit" else "vendor_credit" if item.get("kind") == "credit" else None
                if key:
                    row["balances"][key] = f"{Decimal(row['balances'][key]) + Decimal(item['remaining_amount']):.2f}"
            row["balances"]["net_receivable"] = f"{Decimal(row['balances']['receivable']) - Decimal(row['balances']['customer_credit']):.2f}"
            row["balances"]["net_payable"] = f"{Decimal(row['balances']['payable']) - Decimal(row['balances']['vendor_credit']):.2f}"
            row["duplicate_suggestions"] = sorted((set(names.get(row["display_name"].casefold(), [])) | set(emails.get(row.get("email", "").casefold(), []))) - {row["id"]})
        values.append(row)
    return {"schema_version": 1, "resource": resource, "records": values, "ledger_digest": store.content_digest()}



def records(entity: Entity, resource: str, *, record_id: str | None = None, party_id: str | None = None, as_of: str | None = None, limit: int = 100, cursor: str | None = None) -> dict:
    """Bounded pages are tied to exact state and filters, never silently skip rows."""
    if type(limit) is not int or not 1 <= limit <= 250:
        _error("invalid_page_limit", "Record page limit must be one to 250.")
    with _entity_write_lock(entity.path):
        result = _records_all(entity, resource, record_id=record_id, party_id=party_id, as_of=as_of)
        digest = LedgerStore(entity.path / "ledger.sqlite").content_digest()
        query_hash = canonical_hash({"resource": resource, "id": record_id, "party_id": party_id, "as_of": as_of, "ledger_digest": digest})
        offset = 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 1000:
                    raise ValueError
                position = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if position["query_hash"] != query_hash or type(position["offset"]) is not int or position["offset"] < 0:
                    raise ValueError
                offset = position["offset"]
            except (ValueError, KeyError, TypeError):
                _error("stale_record_cursor", "Record state or filters changed; start a new page sequence.")
        all_rows = result["records"]
        end = offset + limit
        next_cursor = base64.urlsafe_b64encode(json.dumps({"query_hash": query_hash, "offset": end}, sort_keys=True).encode()).decode() if end < len(all_rows) else None
        return {**result, "records": all_rows[offset:end], "total": len(all_rows), "next_cursor": next_cursor, "ledger_digest": digest}

def import_csv(text: str, operation_id: str) -> dict:
    """Translate an explicit portable party CSV into the ordinary preview request."""
    if len(text.encode("utf-8")) > 2 * 1024 * 1024:
        _error("party_csv_too_large", "Party CSV exceeds two MiB.")
    reader = csv.DictReader(io.StringIO(text))
    allowed = {"id", "display_name", "legal_name", "roles", "email", "terms_days"}
    if not reader.fieldnames or set(reader.fieldnames) - allowed or not {"id", "display_name", "roles"} <= set(reader.fieldnames):
        _error("invalid_party_csv", "CSV needs id, display_name and roles, with only documented optional columns.")
    rows = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            _error("invalid_party_csv", "CSV rows must match the header.")
        parsed = {key: value for key, value in row.items() if value != ""}
        parsed["roles"] = parsed["roles"].split("|")
        parsed["expected_revision"] = 0
        if "terms_days" in parsed:
            try:
                parsed["terms_days"] = int(parsed["terms_days"])
            except ValueError:
                _error("invalid_terms", "CSV terms_days must be an integer.")
        rows.append(parsed)
    return {"operation_id": operation_id, "action": "party.import", "rows": rows}


def add_parser(subcommands) -> None:
    parser = subcommands.add_parser("business", help="Customers, vendors, invoices, bills and payment facts")
    commands = parser.add_subparsers(dest="business_command", required=True)
    for name in ("schema", "records", "preview", "apply", "receipt", "import-preview", "tax-preview", "tax-export", "tax-recipient"):
        child = commands.add_parser(name)
        if name not in {"schema", "tax-preview", "tax-export", "tax-recipient"}:
            child.add_argument("--entity", required=True, type=Path)
        if name in {"preview", "apply", "tax-preview", "tax-export", "tax-recipient"}:
            child.add_argument("--input", required=True, type=Path)
        if name == "records":
            child.add_argument("--resource", required=True, choices=(*BUSINESS_TABLES, "payment_candidates", "aging", "outstanding"))
            child.add_argument("--id", default=None)
            child.add_argument("--party-id", default=None)
            child.add_argument("--as-of", default=None)
            child.add_argument("--limit", type=int, default=100)
            child.add_argument("--cursor", default=None)
        if name == "receipt":
            child.add_argument("--id", required=True)
        if name == "import-preview":
            child.add_argument("--file", required=True, type=Path)
            child.add_argument("--operation-id", required=True)
        if name in {"tax-export", "tax-recipient"}:
            child.add_argument("--output", required=True, type=Path)
        if name == "tax-recipient":
            child.add_argument("--id", required=True)


def run(args: argparse.Namespace) -> int:
    try:
        command = args.business_command
        entity = load_entity(args.entity) if hasattr(args, "entity") else None
        if command == "schema":
            result = {"schema_version": 1, "ledger_schema_version": 3, "actions": list(ACTIONS), "resources": list(BUSINESS_TABLES), "commands": ["business schema", "business records --entity PATH --resource RESOURCE [--id ID] [--party-id ID] [--as-of DATE]", "business preview|apply --entity PATH --input FILE", "business receipt --entity PATH --id ID", "business import-preview --entity PATH --file CSV --operation-id ID", "business tax-preview --input FILE", "business tax-export --input FILE --output DIR", "business tax-recipient --input FILE --id PARTY --output PDF"], "sensitive_tax_identity": "protected export only; never in business requests or plans"}
        elif command == "records":
            result = records(entity, args.resource, record_id=args.id, party_id=args.party_id, as_of=args.as_of, limit=args.limit, cursor=args.cursor)
        elif command == "receipt":
            result = load_record(LedgerStore(entity.path / "ledger.sqlite"), "intents", "operation:" + journals._id(args.id))
            result = result["receipt"] if result else {"status": "not_found", "id": args.id}
        elif command == "import-preview":
            result = preview(entity, import_csv(args.file.read_text(encoding="utf-8-sig"), args.operation_id))
        else:
            if args.input.stat().st_size > 2 * 1024 * 1024:
                _error("input_too_large", "Business input exceeds two MiB.")
            payload = json.loads(args.input.read_text())
            if not isinstance(payload, dict):
                _error("invalid_input", "Input must contain one JSON object.")
            if command == "preview":
                result = preview(entity, payload)
            elif command == "apply":
                result = apply(entity, payload)
            elif command == "tax-preview":
                from .tax1099 import prepare_1099
                result = prepare_1099(payload)
            elif command == "tax-export":
                from .tax1099 import export_cpa_package
                result = export_cpa_package(payload, args.output)
            else:
                # The regular remote bridge never accepts privileged tax profiles.
                # Protected export requires a separate explicit host operation.
                from .tax1099_pdf import render_recipient_pdf
                result = render_recipient_pdf(payload["preparation"], args.id, payload["protected_profile"], args.output)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        result = exc.as_dict() if isinstance(exc, journals.JournalError) else {"status": "blocked", "errors": [{"code": "invalid_business_input", "message": str(exc)}]}
        print(json.dumps(result, sort_keys=True))
        return 1
