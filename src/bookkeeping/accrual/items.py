"""Outstanding balances and explicit, source-linked settlements without invoicing."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from ..entity import Entity, flat_bank_account_mappings
from ..source_ids import source_id_variants
from ..ledger.importer import _find_live_entry, _load_pending_categorization, _ledger_account_for_txn
from .intake import canonical_hash, normalize_amount
from .journals import JournalError, _day, _evidence, _existing_receipt, _id, _preview_operation, _source_claims, _store, _text
from .schedules import _account, _apply, _entry, _evidence_ids, _money, _request, _item_ref, _human, _party_ref, _labelled
from .store import SupportingMutation, list_records, load_record, record_revision


def item_sign(record: Mapping[str, Any]) -> Decimal:
    """Return the debit-positive sign of the item's remaining control balance."""
    if record.get("kind") == "credit":
        if record.get("direction") not in {"debit", "credit"}:
            raise JournalError("credit_direction_required", "Credit support requires explicit debit or credit direction.")
        return Decimal(1 if record["direction"] == "debit" else -1)
    if record["account"].startswith("Assets:"):
        return Decimal(1)
    if record["account"].startswith("Liabilities:"):
        return Decimal(-1)
    raise JournalError("invalid_control_account", "Outstanding support needs an asset or liability control account.")


def validate_due_date(record: Mapping[str, Any]) -> str | None:
    """Validate an explicit payment due date without inferring it from book dates."""
    return _day(record["due_date"], "due_date").isoformat() if "due_date" in record else None


def _item_effect(entity: Entity, payload: Mapping[str, Any]):
    request = _request(payload, {"operation_id", "id", "kind", "account", "currency", "original_amount", "as_of",
                                "origin", "evidence_ids", "direction", "counterparty", "reference", "estimate_basis", "remaining_amount", "due_date"})
    operation_id, item_id = _id(request.get("operation_id")), _id(request.get("id"))
    store = _store(entity)
    kind = request.get("kind")
    if kind not in {"receivable", "payable", "accrual", "credit"} or request.get("currency", "USD") != "USD":
        raise JournalError("invalid_item", "Outstanding items support receivable, payable, accrual, or credit in USD.")
    if kind != "credit" and "direction" in request:
        raise JournalError("invalid_direction", "Only a credit item accepts an explicit debit/credit direction.")
    roots = ("Assets:",) if kind == "receivable" else ("Liabilities:",) if kind == "payable" else ("Assets:", "Liabilities:")
    account = _account(store, request.get("account"), "account", roots)
    amount = _money(request.get("original_amount"), "original_amount")
    when = _day(request.get("as_of"), "as_of")
    due_date = validate_due_date(request)
    if due_date is not None:
        request["due_date"] = due_date
    origin = request.get("origin")
    if not isinstance(origin, Mapping) or set(origin) - {"mode", "source_id", "counter_account", "lines"} or origin.get("mode") not in {"new", "existing", "opening"}:
        raise JournalError("invalid_origin", "Supply a bounded new, existing, or opening origin.",
                           "Set origin to {mode: new|existing|opening, source_id, counter_account} or, for a split bill, origin.lines.")
    if "estimate_basis" in request:
        request["estimate_basis"] = _text(request["estimate_basis"], "estimate_basis")
    _id(origin.get("source_id"))
    remaining = _money(request.get("remaining_amount", f"{amount:.2f}"), "remaining_amount")
    if remaining > amount or (remaining != amount and origin["mode"] != "opening"):
        raise JournalError("invalid_carried_balance", "Only an opening origin may carry a partly settled original amount.")
    ids = _evidence_ids(request.get("evidence_ids", []))
    if not ids and origin["mode"] == "new" and "estimate_basis" not in request:
        raise JournalError("evidence_required", "A new outstanding balance requires stored supporting documents or an estimate basis.",
                           "Set `evidence_ids` to stored supporting document IDs, or set `estimate_basis` to a short text that explains the estimate.")
    request.update(operation_id=operation_id, id=item_id, account=account, original_amount=f"{amount:.2f}",
                   currency="USD", evidence_ids=ids)
    lines = None
    if "lines" in origin:
        if "counter_account" in origin or origin["mode"] != "new":
            raise JournalError("invalid_origin", "origin.lines replaces origin.counter_account and applies only to a new origin.",
                               "Remove origin.counter_account, or remove origin.lines.")
        raw = origin["lines"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 200 or any(not isinstance(r, Mapping) or set(r) != {"account", "amount"} for r in raw):
            raise JournalError("invalid_lines", "origin.lines needs 1 to 200 rows of {account, amount}.")
        lines = [(_account(store, r["account"], "origin.lines.account"), _money(r["amount"], "origin.lines.amount")) for r in raw]
        if sum((a for _, a in lines), Decimal(0)) != amount:
            raise JournalError("lines_total_mismatch", "origin.lines amounts must add up exactly to original_amount.",
                               f"Change the origin.lines amounts so their sum is {amount:.2f}.")
        request["origin"] = {**origin, "lines": [{"account": a, "amount": f"{n:.2f}"} for a, n in lines]}
    record = {k: deepcopy(v) for k, v in request.items() if k != "operation_id"}
    record.update(remaining_amount=f"{remaining:.2f}", base_remaining_amount=f"{remaining:.2f}", status="open", created_operation_id=operation_id)
    sign = item_sign(record)
    claims, replay = [], None
    if origin["mode"] == "new":
        logical = {k: v for k, v in request.items() if k not in {"operation_id", "id", "evidence_ids"}}
        claims, replay = _source_claims(store, _evidence(store, ids), canonical_hash(logical), operation_id)
    if replay:
        return request, [], [], replay
    if load_record(store, "items", item_id):
        raise JournalError("identity_conflict", "Item already exists; use an explicit adjustment.")
    entries = []
    if origin["mode"] == "new":
        spellings = source_id_variants(origin["source_id"])
        if any(store.source_exists(s) for s in spellings) or any(i.get("origin", {}).get("source_id") in spellings
                                                                 for i in list_records(store, "items")):
            raise JournalError("duplicate_origin", "The source already has a financial origin; link existing support or correct it.")
        if lines is None:
            counter = _account(store, origin.get("counter_account"), "origin.counter_account")
            lines = [(counter, amount)]
        for counter, _ in lines:
            if counter == account or counter.startswith(("Assets:Bank:", "Assets:Cash")):
                raise JournalError("invalid_counter_account", "An outstanding origin cannot create cash or use its control as its offset.")
        meta = {"estimate_basis": request["estimate_basis"]} if "estimate_basis" in request else {}
        entries.append(_entry(when, "accrual:" + operation_id, _origin_narration(record),
                              [(account, sign * amount), *[(c, -sign * n) for c, n in lines]], item_id=item_id,
                              origin_source_id=origin["source_id"], **meta))
    else:
        original = _find_live_entry(store, origin["source_id"])
        if original.date > when:
            raise JournalError("invalid_origin_date", "Supporting balance cannot precede its origin journal.")
        available = sum((p.amount for p in original.postings if p.account == account), Decimal(0))
        reserved = sum((item_sign(i) * _money(i.get("base_remaining_amount", i["remaining_amount"]) if origin["mode"] == "opening" else i["original_amount"])
                        for i in list_records(store, "items")
                        if i.get("origin", {}).get("source_id") == origin["source_id"] and i.get("account") == account), Decimal(0))
        reserve_amount = remaining if origin["mode"] == "opening" else amount
        if sign * (reserved + sign * reserve_amount) > sign * available:
            raise JournalError("origin_support_exceeds_balance", "Item support exceeds the linked control-account journal.")
    mutations = [SupportingMutation("items", item_id, record), *claims]
    return request, entries, mutations, None


def preview_item(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    request, entries, mutations, replay = _item_effect(entity, payload)
    plan = _preview_operation(entity, operation_id=request["operation_id"], kind="item_create", request=request,
                              entries=entries, mutations=mutations, evidence_ids=request["evidence_ids"],
                              affected_dates=[request["as_of"]])
    if replay:
        plan["existing_receipt"] = replay
    return plan


def apply_item(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    return _apply(entity, plan, "item_create", preview_item, _item_effect)


def _remaining(item: Mapping[str, Any], allocations: list[dict[str, Any]]) -> Decimal:
    """Reconcile the stored remainder against immutable allocation history."""
    relevant = [a for a in allocations if a.get("item_id") == item["id"] and a.get("kind") in {"settlement", "credit", "writeoff", "reversal", "application"}]
    already_allocated = sum((_money(a["amount"]) for a in relevant), Decimal(0))
    base = _money(item.get("base_remaining_amount", item["remaining_amount"]), positive=False)
    expected = base - already_allocated
    remaining = _money(item["remaining_amount"], positive=False)
    if "base_remaining_amount" not in item:
        # Imported support starts at its carried remaining amount. The first
        # allocation freezes that base before it changes the current remainder.
        expected = remaining if not relevant else _money(item["original_amount"]) - already_allocated
    if expected != remaining or expected < 0:
        raise JournalError("item_allocation_conflict", "Supporting allocation history does not reconcile to the remaining item balance.")
    return remaining


def _origin_narration(record: Mapping[str, Any]) -> str:
    kind = record["kind"]
    if kind == "accrual":
        return _labelled("Estimate", _human(record.get("reference"), 1900) or _human(record.get("counterparty"), 1900)
                         or _human(record.get("estimate_basis"), 1900))
    label = {"receivable": "Invoice", "payable": "Bill", "credit": "Customer credit" if record.get("direction") == "credit" else "Supplier credit"}[kind]
    return _labelled(label, _party_ref(record))


def _offset_narration(credit: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    who = _human(credit.get("counterparty"))
    ref = lambda item: _human(item.get("reference")) if _human(item.get("reference")).casefold() != who.casefold() else ""
    applied = " ".join(part for part in (who, ref(credit)) if part)
    return f"Credit applied: {applied} to {ref(target) or ('invoice' if target['kind'] == 'receivable' else 'bill')}"[:2000]


def _payment_narration(cash: Decimal, counterparties: set, refs: list) -> str:
    names = sorted({_human(c) for c in counterparties if _human(c)})
    head = ("Payment from " if cash > 0 else "Payment to ") + names[0] if len(names) == 1 else ("Payment received" if cash > 0 else "Payment made")
    unique = [r for r in dict.fromkeys(refs) if len(names) != 1 or r.casefold() != names[0].casefold()]
    return (head + (": " + ", ".join(unique) if unique else ""))[:2000]


def _settlement_effect(entity: Entity, payload: Mapping[str, Any]):
    request = _request(payload, {"operation_id", "date", "source", "allocations", "evidence_ids", "excess"})
    operation_id, when = _id(request.get("operation_id")), _day(request.get("date"), "date")
    store = _store(entity)
    source = request.get("source")
    if not isinstance(source, Mapping) or set(source) != {"mode", "source_id", "bank_account", "amount"} or source["mode"] not in {"new", "existing"}:
        raise JournalError("invalid_settlement_source", "Settlement source needs mode, source_id, bank_account and signed amount.")
    source_id = _id(source["source_id"])
    bank = _account(store, source["bank_account"], "bank_account", ("Assets:", "Liabilities:CreditCard"))
    mapped_banks = set(flat_bank_account_mappings(entity.entity_config).values())
    if not bank.startswith(("Assets:Bank:", "Assets:Cash", "Assets:Processors:", "Liabilities:CreditCard")) and bank not in mapped_banks:
        raise JournalError("invalid_bank_account", "Settlement source must be an explicitly mapped bank, cash, processor, or credit-card account.")
    cash = Decimal(normalize_amount(source["amount"]))
    if cash == 0:
        raise JournalError("invalid_amount", "Settlement cash movement must be nonzero.")
    ids = _evidence_ids(request.get("evidence_ids", []))
    if source["mode"] == "new" and not ids:
        raise JournalError("evidence_required", "A new payment requires a stored supporting document.")
    excess = request.get("excess")
    if excess is not None:
        if not isinstance(excess, Mapping) or set(excess) - {"id", "account", "counterparty", "reference"} or not {"id", "account"} <= set(excess):
            raise JournalError("invalid_excess", "excess needs id and account, and may add counterparty and reference.",
                               "Set excess to {id, account, counterparty}: id is the new credit item, account is the control account that holds it.")
        excess = {**excess, "id": _id(excess["id"]), "account": _account(store, excess["account"], "excess.account", ("Assets:", "Liabilities:"))}
        for label in ("counterparty", "reference"):
            if label in excess:
                excess[label] = _text(excess[label], "excess." + label)
        if excess["account"] == bank or excess["account"].startswith(("Assets:Bank:", "Assets:Cash")):
            raise JournalError("invalid_excess", "excess.account must be a receivable, payable, deposit or prepayment account, not cash.")
        request["excess"] = excess
    rows = request.get("allocations")
    if not isinstance(rows, list) or not (0 if excess else 1) <= len(rows) <= 1000:
        raise JournalError("invalid_allocations", "Supply 1 to 1000 exact item allocations (0 when excess records the whole payment).")
    normalized_rows, seen = [], set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"item_id", "amount"}:
            raise JournalError("invalid_allocation", "Each allocation contains only item_id and amount.")
        item_id, amount = _id(row["item_id"]), _money(row["amount"])
        if item_id in seen:
            raise JournalError("duplicate_allocation", "Combine repeated item allocations into one exact amount.")
        seen.add(item_id)
        normalized_rows.append({"item_id": item_id, "amount": f"{amount:.2f}"})
    request.update(operation_id=operation_id, source={**source, "amount": f"{cash:.2f}"}, allocations=normalized_rows, evidence_ids=ids)
    records = _evidence(store, ids)
    claims = []
    if source["mode"] == "new":
        staged = next((t for t in _load_pending_categorization(entity) if t.get("id") in source_id_variants(source_id)), None)
        if staged:
            staged_amount = Decimal(normalize_amount(staged.get("amount")))
            staged_bank = _ledger_account_for_txn(staged, entity.entity_config.get("bank_account_mappings"), entity.entity_config.get("csv_account_mappings"))
            if staged_amount != cash or str(staged.get("date"))[:10] != when.isoformat() or staged_bank != bank:
                raise JournalError("staged_payment_mismatch", "Selected bank facts differ from the staged source.")
            if str(staged.get("id")) != source_id:
                # The same row under its other spelling: settling it here would leave the staged
                # row open, and a later confirm would record the cash again.
                raise JournalError("staged_source_id_differs",
                                   f"The staged row is {staged.get('id')}; settle it with that source_id.",
                                   f"Use source_id {staged.get('id')} and create a new preview.")
        # Provider/transport references cannot manufacture a second cash event.
        financial = {"kind": "item_settlement", "date": when.isoformat(), "bank_account": bank,
                     "amount": f"{cash:.2f}", "allocations": sorted(normalized_rows, key=lambda row: row["item_id"])}
        if excess:
            financial["excess"] = excess
        claims, replay = _source_claims(store, records, canonical_hash(financial), operation_id)
        if replay:
            return request, [], [], replay
    else:
        # Several reviewed batches may allocate ONE independently posted bank
        # movement. Bind evidence to that original, not to an individual batch.
        financial_hash = canonical_hash({"kind": "existing_payment", "source_id": source_id,
            "date": when.isoformat(), "bank_account": bank, "amount": f"{cash:.2f}"})
        by_source = {}
        for record in records:
            identity = record["source_identity"]
            if identity in by_source:
                if by_source[identity] != record["facts_hash"]:
                    raise JournalError("source_conflict", "Payment supporting documents contain conflicting facts for one source.")
                continue
            by_source[identity] = record["facts_hash"]
            claim_id = "source:" + identity
            prior = load_record(store, "intents", claim_id)
            if prior:
                owner = load_record(store, "intents", "operation:" + prior["operation_id"])
                owned_sources = (owner or {}).get("receipt", {}).get("entry_source_ids", [])
                if prior["facts_hash"] != record["facts_hash"] or (prior["financial_hash"] != financial_hash and source_id not in owned_sources):
                    raise JournalError("source_conflict", "This supporting document belongs to a different payment or financial treatment.")
            else:
                claims.append(SupportingMutation("intents", claim_id, {"id": claim_id, "kind": "source_claim",
                    "source_identity": identity, "facts_hash": record["facts_hash"], "financial_hash": financial_hash,
                    "operation_id": operation_id}))
    allocations = list_records(store, "allocations")
    existing_sources = [a for a in allocations if a.get("kind") in {"settlement", "unapplied_receipt"} and a.get("source_id") == source_id]
    if any(a.get("bank_account") != bank or a.get("bank_amount") != f"{cash:.2f}" for a in existing_sources):
        raise JournalError("source_conflict", "The payment identity already has different bank facts.")
    used = sum((Decimal(a["signed_amount"]) for a in existing_sources), Decimal(0))
    entries, mutations, control_postings = [], list(claims), []
    total, counterparties, refs = Decimal(0), set(), []
    for row in normalized_rows:
        item_id, amount = _id(row["item_id"]), _money(row["amount"])
        item = load_record(store, "items", item_id)
        if not item or item.get("status", "open") in {"settled", "closed", "reversed"}:
            raise JournalError("inactive_item", "The selected item is absent or already extinguished.")
        if when.isoformat() < item["as_of"]:
            raise JournalError("settlement_precedes_origin", "Settlement cannot precede the supported outstanding balance.")
        remaining = _remaining(item, allocations)
        if amount > remaining:
            raise JournalError("overallocated_item", f"Allocation exceeds remaining item balance for {item_id}.")
        sign = item_sign(item)
        if item["account"] == bank:
            raise JournalError("invalid_bank_account", "A payment account cannot also be the outstanding control account being settled.")
        signed = sign * amount
        if signed * cash < 0:
            raise JournalError("wrong_payment_direction", "Allocation direction conflicts with the signed payment.",
                               "Allocate a receipt only to receivables and a payment only to payables. Apply a credit to an invoice or bill with `books accrual item offset-preview`.")
        total += signed
        counterparties.add(item.get("counterparty") or "")
        refs.append(_item_ref(item))
        control_postings.append((item["account"], -signed))
        changed = {**item, "base_remaining_amount": item.get("base_remaining_amount", item["remaining_amount"]),
                   "remaining_amount": f"{remaining - amount:.2f}", "status": "settled" if remaining == amount else "open"}
        mutations.append(SupportingMutation("items", item_id, changed, record_revision(store, "items", item_id)))
        allocation_id = "settlement:" + operation_id + ":" + item_id
        allocation = {"id": allocation_id, "kind": "settlement", "item_id": item_id, "amount": f"{amount:.2f}",
                      "signed_amount": f"{signed:.2f}", "date": when.isoformat(), "source_id": source_id,
                      "source_mode": source["mode"], "bank_account": bank, "bank_amount": f"{cash:.2f}", "evidence_ids": ids}
        mutations.append(SupportingMutation("allocations", allocation_id, allocation))
    if abs(used + total) > abs(cash) or (used + total) * cash < 0:
        raise JournalError("overallocated_payment", "Allocations exceed the available payment amount.")
    excess_cash = Decimal(0)
    if excess:
        excess_cash = cash - used - total
        if excess_cash == 0:
            raise JournalError("no_excess", "The allocations use the whole payment, so excess has nothing to record.",
                               "Remove the excess field, or lower an allocation amount.")
        if load_record(store, "items", excess["id"]):
            raise JournalError("identity_conflict", "excess.id already names an item.", "Choose a new excess.id.")
        who = excess.get("counterparty") or (next(iter(counterparties)) if len(counterparties) == 1 else "")
        if not who:
            raise JournalError("counterparty_required", "The excess credit needs a counterparty so it can be applied to an invoice or bill later.",
                               "Set excess.counterparty to the same text as the invoice or bill counterparty.")
        direction = "credit" if cash > 0 else "debit"
        counterparties.add(who)
        refs.append(_human(excess.get("reference")) or "unapplied credit")
        credit = {"id": excess["id"], "kind": "credit", "account": excess["account"], "currency": "USD",
                  "original_amount": f"{abs(excess_cash):.2f}", "as_of": when.isoformat(), "direction": direction,
                  "counterparty": who, "origin": {"mode": source["mode"], "source_id": source_id}, "evidence_ids": ids,
                  "remaining_amount": f"{abs(excess_cash):.2f}", "base_remaining_amount": f"{abs(excess_cash):.2f}",
                  "status": "open", "created_operation_id": operation_id}
        if "reference" in excess:
            credit["reference"] = excess["reference"]
        control_postings.append((excess["account"], -excess_cash))
        mutations.append(SupportingMutation("items", excess["id"], credit))
        allocation_id = "unapplied:" + operation_id + ":" + excess["id"]
        mutations.append(SupportingMutation("allocations", allocation_id, {
            "id": allocation_id, "kind": "unapplied_receipt", "item_id": excess["id"], "amount": f"{abs(excess_cash):.2f}",
            "signed_amount": f"{excess_cash:.2f}", "date": when.isoformat(), "source_id": source_id,
            "source_mode": source["mode"], "bank_account": bank, "bank_amount": f"{cash:.2f}", "evidence_ids": ids}))
    if source["mode"] == "new":
        if any(store.source_exists(s) for s in source_id_variants(source_id)) or existing_sources:
            raise JournalError("duplicate_payment", "The payment is already posted; select existing mode to allocate its remaining amount.")
        if total + excess_cash != cash:
            raise JournalError("unallocated_payment", "A new payment must be fully allocated or its remainder recorded as a credit.",
                               "Add excess {id, account, counterparty} to the settlement request to record the remainder as an open customer or supplier credit, or raise the allocation amounts.")
        entries.append(_entry(when, source_id, _payment_narration(cash, counterparties, refs), [(bank, cash), *control_postings], settlement_id=operation_id))
    else:
        original = _find_live_entry(store, source_id)
        if original.date != when or len(original.postings) != 2:
            raise JournalError("ambiguous_payment", "Existing settlement needs the exact date and a two-posting source.")
        bank_postings = [p for p in original.postings if p.account == bank and p.amount == cash and p.currency == "USD"]
        if len(bank_postings) != 1:
            raise JournalError("payment_mismatch", "Bank account or amount differs from the original payment.")
        other = next(p for p in original.postings if p.account != bank)
        grouped = {}
        for account, amount in control_postings:
            grouped[account] = grouped.get(account, Decimal(0)) + amount
        if set(grouped) == {other.account}:
            if other.amount != -cash:
                raise JournalError("payment_mismatch", "Existing control payment is not balanced to its bank movement.")
            # It already reduced this control. This operation supplies support,
            # not another financial or cash effect.
        else:
            expected_root = "Income:" if cash > 0 else "Expenses:"
            if not other.account.startswith(expected_root):
                raise JournalError("ambiguous_existing_treatment", "Existing payment is neither the exact control settlement nor a simple cash income/expense needing reclassification.")
            entries.append(_entry(when, "settlement:" + operation_id + ":reclass", "Reclassified: " + _payment_narration(cash, counterparties, refs),
                                  [(other.account, total + excess_cash), *control_postings], origin_source_id=source_id, settlement_id=operation_id))
    return request, entries, mutations, None


def preview_settlement(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    request, entries, mutations, replay = _settlement_effect(entity, payload)
    plan = _preview_operation(entity, operation_id=request["operation_id"], kind="item_settlement", request=request,
                              entries=entries, mutations=mutations, evidence_ids=request["evidence_ids"], affected_dates=[request["date"]])
    if replay:
        plan["existing_receipt"] = replay
    return plan


def apply_settlement(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    from ..ledger.importer import _entity_write_lock
    from .journals import _complete_linked_source_review
    with _entity_write_lock(entity.path):
        receipt = _apply(entity, plan, "item_settlement", preview_settlement, _settlement_effect)
        _complete_linked_source_review(entity, plan["request"]["source"]["source_id"], receipt, "settlement")
        return receipt


def _adjustment_effect(entity: Entity, payload: Mapping[str, Any]):
    request = _request(payload, {"operation_id", "item_id", "action", "amount", "date", "counter_account", "source_id", "evidence_ids", "reason", "actual_item_id"})
    operation_id, item_id = _id(request.get("operation_id")), _id(request.get("item_id"))
    when = _day(request.get("date"), "date")
    action = request.get("action")
    if action not in {"credit", "writeoff", "true_up"}:
        raise JournalError("invalid_adjustment", "action must be credit, writeoff or true_up.",
                           "Set action to credit (reduce the item and the income or expense), writeoff (bad debt or forgiven balance) or true_up (settle an accrued estimate against its actual item).")
    reason = request.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise JournalError("missing_reason", "Credit, writeoff and true-up need a disclosed reason.", "Set reason to a short text.")
    store = _store(entity)
    item = load_record(store, "items", item_id)
    actual = None
    if action == "true_up":
        if "actual_item_id" not in request:
            raise JournalError("actual_item_required", "A true-up needs the actual item that replaces the estimate.",
                               "Set actual_item_id to the item ID of the real bill or invoice, and item_id to the accrual item.")
        actual = load_record(store, "items", _id(request["actual_item_id"]))
        if not actual or not item or actual["id"] == item["id"] or item.get("kind") != "accrual" or actual.get("kind") not in {"receivable", "payable"} or item_sign(actual) != item_sign(item):
            raise JournalError("invalid_true_up", "A true-up links an accrual item to an actual payable (or receivable) item on the same side.",
                               "Set item_id to a kind accrual item and actual_item_id to a kind payable or receivable item.")
        if "amount" not in request and item.get("status", "open") not in {"settled", "closed"}:
            request["amount"] = f"{_remaining(item, list_records(store, 'allocations')):.2f}"
    elif "actual_item_id" in request:
        raise JournalError("invalid_adjustment", "actual_item_id applies only to action true_up.")
    request.setdefault("source_id", operation_id)
    source_id = _id(request["source_id"])
    ids = _evidence_ids(request.get("evidence_ids", []))
    if not ids and action != "true_up":
        raise JournalError("evidence_required", "Credits and writeoffs require stored supporting documents.",
                           "Set `evidence_ids` to stored supporting document IDs.")
    amount = _money(request.get("amount"))
    counter = _account(store, request.get("counter_account"), "counter_account", ("Income:", "Expenses:"))
    request.update(operation_id=operation_id, item_id=item_id, amount=f"{amount:.2f}", evidence_ids=ids, counter_account=counter)
    logical = {k: v for k, v in request.items() if k not in {"operation_id", "evidence_ids"}}
    claims, replay = _source_claims(store, _evidence(store, ids), canonical_hash(logical), operation_id)
    if replay:
        return request, [], [], replay
    if not item or item.get("status", "open") in {"settled", "closed", "reversed"}:
        raise JournalError("inactive_item", "Adjustment needs an open item.")
    if when.isoformat() < item["as_of"]:
        raise JournalError("adjustment_precedes_origin", "Adjustment cannot precede the outstanding balance.")
    remaining = _remaining(item, list_records(store, "allocations"))
    if amount > remaining:
        raise JournalError("overallocated_item", "Credit/writeoff exceeds the supported remaining amount.")
    sign = item_sign(item)
    label = {"credit": "Credit issued", "writeoff": "Written off", "true_up": "Estimate settled"}[action]
    detail = _item_ref(item) + (" (actual: " + _item_ref(actual) + ")" if actual else "") + " - " + _human(reason)
    entry = _entry(when, "adjustment:" + operation_id, label + ": " + detail,
                   [(item["account"], -sign * amount), (counter, sign * amount)], item_id=item_id, origin_source_id=source_id)
    changed = {**item, "base_remaining_amount": item.get("base_remaining_amount", item["remaining_amount"]),
               "remaining_amount": f"{remaining - amount:.2f}", "status": "settled" if amount == remaining else "open"}
    allocation_id = "adjustment:" + source_id
    allocation = {"id": allocation_id, "kind": "credit" if action == "true_up" else action, "action": action, "item_id": item_id,
                  "amount": f"{amount:.2f}", "date": when.isoformat(), "source_id": source_id, "operation_id": operation_id,
                  "counter_account": counter, "evidence_ids": ids, "reason": reason}
    if actual:
        variance = _money(actual["original_amount"]) - amount
        allocation.update(actual_item_id=actual["id"], estimate_amount=f"{amount:.2f}",
                          actual_amount=actual["original_amount"], variance=f"{variance:.2f}")
        changed["actual_item_id"] = actual["id"]
    if load_record(store, "allocations", allocation_id):
        raise JournalError("source_conflict", "This adjustment source is already allocated; use its original receipt or a correction.")
    return request, [entry], [SupportingMutation("items", item_id, changed, record_revision(store, "items", item_id)),
                             SupportingMutation("allocations", allocation_id, allocation), *claims], None


def preview_adjustment(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    request, entries, mutations, replay = _adjustment_effect(entity, payload)
    fields = {}
    for mutation in mutations:
        record = mutation.payload
        if mutation.table == "allocations" and "variance" in record:
            fields = {k: record[k] for k in ("actual_item_id", "estimate_amount", "actual_amount", "variance")}
    plan = _preview_operation(entity, operation_id=request["operation_id"], kind="item_adjustment", request=request,
                              entries=entries, mutations=mutations, evidence_ids=request["evidence_ids"],
                              details={"true_up": fields} if fields else None)
    if replay:
        plan["existing_receipt"] = replay
    return plan


def apply_adjustment(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    return _apply(entity, plan, "item_adjustment", preview_adjustment, _adjustment_effect)


def _offset_effect(entity: Entity, payload: Mapping[str, Any]):
    """Apply an open credit item (deposit, credit memo, overpayment, prepayment) to an open invoice or bill."""
    request = _request(payload, {"operation_id", "date", "credit_item_id", "target_item_id", "amount", "reason", "evidence_ids"})
    operation_id, when = _id(request.get("operation_id")), _day(request.get("date"), "date")
    credit_id, target_id = _id(request.get("credit_item_id")), _id(request.get("target_item_id"))
    amount, reason = _money(request.get("amount")), _text(request.get("reason"), "reason")
    ids = _evidence_ids(request.get("evidence_ids", []))
    request.update(operation_id=operation_id, date=when.isoformat(), credit_item_id=credit_id, target_item_id=target_id,
                   amount=f"{amount:.2f}", reason=reason, evidence_ids=ids)
    store = _store(entity)
    replay = _existing_receipt(entity, operation_id, canonical_hash({"kind": "item_offset", "request": request}))
    if replay:
        return request, [], [], replay
    credit, target = load_record(store, "items", credit_id), load_record(store, "items", target_id)
    if (not credit or not target or credit_id == target_id or credit.get("kind") != "credit"
            or target.get("kind") not in {"receivable", "payable"}):
        raise JournalError("invalid_offset_pair", "An offset applies one credit item to one receivable or payable item.",
                           "Set credit_item_id to a kind credit item and target_item_id to a kind receivable or payable item.")
    for item in (credit, target):
        if item.get("status", "open") in {"settled", "closed", "reversed"}:
            raise JournalError("inactive_item", f"Item {item['id']} is already settled.")
        if when.isoformat() < item["as_of"]:
            raise JournalError("offset_precedes_origin", f"The offset date precedes item {item['id']}.",
                               "Set date on or after the later as_of of the two items.")
    who = credit.get("counterparty")
    if not who or who != target.get("counterparty"):
        raise JournalError("counterparty_mismatch", "The credit and the invoice or bill must have the same non-empty counterparty.",
                           "Create or record both items with an identical counterparty field.")
    sign = item_sign(target)
    if item_sign(credit) != -sign:
        raise JournalError("wrong_credit_side", "This credit is on the same side as the target item, so it cannot reduce it.",
                           "Apply a customer credit (credit balance) to a receivable, or a supplier prepayment (debit balance) to a payable.")
    records = _evidence(store, ids)
    allocations = list_records(store, "allocations")
    for item in (credit, target):
        if amount > _remaining(item, allocations):
            raise JournalError("overallocated_item", f"The amount exceeds the open balance of item {item['id']}.")
    entries = []
    if credit["account"] != target["account"]:
        # Same control account: the two open balances already net in the ledger, so only link them.
        entries.append(_entry(when, "offset:" + operation_id,
                              _offset_narration(credit, target),
                              [(target["account"], -sign * amount), (credit["account"], sign * amount)],
                              item_id=target_id, credit_item_id=credit_id))
    mutations = []
    for item, role, other in ((credit, "credit", target_id), (target, "target", credit_id)):
        remaining = _remaining(item, allocations)
        mutations.append(SupportingMutation("items", item["id"], {**item,
            "base_remaining_amount": item.get("base_remaining_amount", item["remaining_amount"]),
            "remaining_amount": f"{remaining - amount:.2f}", "status": "settled" if remaining == amount else "open"},
            record_revision(store, "items", item["id"])))
        allocation_id = "application:" + operation_id + ":" + item["id"]
        mutations.append(SupportingMutation("allocations", allocation_id, {
            "id": allocation_id, "kind": "application", "role": role, "item_id": item["id"], "linked_item_id": other,
            "amount": f"{amount:.2f}", "date": when.isoformat(), "operation_id": operation_id, "counterparty": who,
            "reason": reason, "evidence_ids": [r["id"] for r in records]}))
    return request, entries, mutations, None


def preview_offset(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    request, entries, mutations, replay = _offset_effect(entity, payload)
    plan = _preview_operation(entity, operation_id=request["operation_id"], kind="item_offset", request=request,
                              entries=entries, mutations=mutations, evidence_ids=request["evidence_ids"], affected_dates=[request["date"]])
    if replay:
        plan["existing_receipt"] = replay
    return plan


def apply_offset(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    return _apply(entity, plan, "item_offset", preview_offset, _offset_effect)


def list_items(entity: Entity) -> list[dict[str, Any]]:
    from .journals import _read_store
    return list_records(_read_store(entity), "items")
