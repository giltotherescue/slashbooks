"""Outstanding balances and explicit, source-linked settlements without invoicing."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from ..entity import Entity
from ..ledger.importer import _find_live_entry, _load_pending_categorization, _ledger_account_for_txn
from .intake import canonical_hash, normalize_amount
from .journals import JournalError, _day, _evidence, _id, _preview_operation, _source_claims, _store
from .schedules import _account, _apply, _entry, _evidence_ids, _money, _request
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


def _item_effect(entity: Entity, payload: Mapping[str, Any]):
    request = _request(payload, {"operation_id", "id", "kind", "account", "currency", "original_amount", "as_of",
                                "origin", "evidence_ids", "settlement_strategy", "direction", "counterparty", "reference", "obligation_ids", "reversal_date", "remaining_amount"})
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
    strategy = request.get("settlement_strategy", "direct")
    if strategy not in {"direct", "reverse"}:
        raise JournalError("invalid_strategy", "Use exactly one settlement strategy: direct or reverse.")
    origin = request.get("origin")
    if not isinstance(origin, Mapping) or set(origin) - {"mode", "source_id", "counter_account"} or origin.get("mode") not in {"new", "existing", "opening"}:
        raise JournalError("invalid_origin", "Supply a bounded new, existing, or opening origin.")
    _id(origin.get("source_id"))
    remaining = _money(request.get("remaining_amount", f"{amount:.2f}"), "remaining_amount")
    if remaining > amount or (remaining != amount and origin["mode"] != "opening"):
        raise JournalError("invalid_carried_balance", "Only an opening origin may carry a partly settled original amount.")
    ids = _evidence_ids(request.get("evidence_ids", []))
    if not ids and origin["mode"] == "new":
        raise JournalError("evidence_required", "A new outstanding balance requires stored evidence.")
    request.update(operation_id=operation_id, id=item_id, account=account, original_amount=f"{amount:.2f}",
                   currency="USD", evidence_ids=ids, settlement_strategy=strategy)
    if strategy == "reverse":
        if "obligation_ids" in request and request["obligation_ids"] != [item_id]:
            raise JournalError("invalid_obligation", "A reversal item can link only its own obligation.")
        request["obligation_ids"] = [item_id]
        if "reversal_date" in request and (_day(request["reversal_date"], "reversal_date") <= when or origin["mode"] != "new"):
            raise JournalError("invalid_reversal_date", "A scheduled item reversal must follow its new financial origin.")
    elif "obligation_ids" in request:
        raise JournalError("invalid_obligation", "Direct items cannot carry a scheduled-reversal claim.")
    if "reversal_date" in request and strategy != "reverse":
        raise JournalError("invalid_strategy", "Only a reverse-strategy item may schedule a reversal.")
    record = {k: deepcopy(v) for k, v in request.items() if k not in {"operation_id", "obligation_ids"}}
    record.update(remaining_amount=f"{remaining:.2f}", base_remaining_amount=f"{remaining:.2f}", status="open", created_operation_id=operation_id)
    sign = item_sign(record)
    claims, replay = [], None
    if origin["mode"] == "new":
        logical = {k: v for k, v in request.items() if k not in {"operation_id", "id", "evidence_ids", "obligation_ids"}}
        claims, replay = _source_claims(store, _evidence(store, ids), canonical_hash(logical), operation_id)
    if replay:
        return request, [], [], replay
    if load_record(store, "items", item_id):
        raise JournalError("identity_conflict", "Item already exists; use an explicit adjustment.")
    entries = []
    if origin["mode"] == "new":
        if store.source_exists(origin["source_id"]) or any(i.get("origin", {}).get("source_id") == origin["source_id"]
                                                         for i in list_records(store, "items")):
            raise JournalError("duplicate_origin", "The source already has a financial origin; link existing support or correct it.")
        counter = _account(store, origin.get("counter_account"), "origin.counter_account")
        if counter == account or counter.startswith(("Assets:Bank:", "Assets:Cash")):
            raise JournalError("invalid_counter_account", "An outstanding origin cannot create cash or use its control as its offset.")
        entries.append(_entry(when, "accrual:" + operation_id, "Record " + kind + " " + item_id,
                              [(account, sign * amount), (counter, -sign * amount)], item_id=item_id,
                              origin_source_id=origin["source_id"]))
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
    if "reversal_date" in request:
        reversal_id = "reversal:" + operation_id
        mutations.append(SupportingMutation("intents", reversal_id, {
            "id": reversal_id, "kind": "scheduled_reversal", "status": "pending", "date": request["reversal_date"],
            "original_id": operation_id, "strategy": "reverse", "obligation_ids": [item_id],
        }))
    return request, entries, mutations, None


def preview_item(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    request, entries, mutations, replay = _item_effect(entity, payload)
    plan = _preview_operation(entity, operation_id=request["operation_id"], kind="item_create", request=request,
                              entries=entries, mutations=mutations, evidence_ids=request["evidence_ids"],
                              affected_dates=[request["as_of"], *([request["reversal_date"]] if "reversal_date" in request else [])])
    if replay:
        plan["existing_receipt"] = replay
    return plan


def apply_item(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    return _apply(entity, plan, "item_create", preview_item, _item_effect)


def _remaining(item: Mapping[str, Any], allocations: list[dict[str, Any]]) -> Decimal:
    """Reconcile the stored remainder against immutable allocation history."""
    relevant = [a for a in allocations if a.get("item_id") == item["id"] and a.get("kind") in {"settlement", "credit", "writeoff", "reversal"}]
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


def _settlement_effect(entity: Entity, payload: Mapping[str, Any]):
    request = _request(payload, {"operation_id", "date", "source", "allocations", "evidence_ids"})
    operation_id, when = _id(request.get("operation_id")), _day(request.get("date"), "date")
    store = _store(entity)
    source = request.get("source")
    if not isinstance(source, Mapping) or set(source) != {"mode", "source_id", "bank_account", "amount"} or source["mode"] not in {"new", "existing"}:
        raise JournalError("invalid_settlement_source", "Settlement source needs mode, source_id, bank_account and signed amount.")
    source_id = _id(source["source_id"])
    bank = _account(store, source["bank_account"], "bank_account", ("Assets:", "Liabilities:CreditCard"))
    mapped_banks = {v for v in (entity.entity_config.get("bank_account_mappings") or {}).values() if isinstance(v, str)}
    if not bank.startswith(("Assets:Bank:", "Assets:Cash", "Assets:Processors:", "Liabilities:CreditCard")) and bank not in mapped_banks:
        raise JournalError("invalid_bank_account", "Settlement source must be an explicitly mapped bank, cash, processor, or credit-card account.")
    cash = Decimal(normalize_amount(source["amount"]))
    if cash == 0:
        raise JournalError("invalid_amount", "Settlement cash movement must be nonzero.")
    ids = _evidence_ids(request.get("evidence_ids", []))
    if source["mode"] == "new" and not ids:
        raise JournalError("evidence_required", "A new payment requires stored evidence.")
    rows = request.get("allocations")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise JournalError("invalid_allocations", "Supply 1 to 1000 exact item allocations.")
    allocations = list_records(store, "allocations")
    existing_sources = [a for a in allocations if a.get("kind") == "settlement" and a.get("source_id") == source_id]
    if any(a.get("bank_account") != bank or a.get("bank_amount") != f"{cash:.2f}" for a in existing_sources):
        raise JournalError("source_conflict", "The payment identity already has different bank facts.")
    used = sum((Decimal(a["signed_amount"]) for a in existing_sources), Decimal(0))
    entries, mutations, control_postings, normalized_rows, seen = [], [], [], [], set()
    total = Decimal(0)
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"item_id", "amount"}:
            raise JournalError("invalid_allocation", "Each allocation contains only item_id and amount.")
        item_id, amount = _id(row["item_id"]), _money(row["amount"])
        if item_id in seen:
            raise JournalError("duplicate_allocation", "Combine repeated item allocations into one exact amount.")
        seen.add(item_id)
        item = load_record(store, "items", item_id)
        if not item or item.get("status", "open") in {"settled", "closed", "reversed"}:
            raise JournalError("inactive_item", "The selected item is absent or already extinguished.")
        if item.get("settlement_strategy", "direct") != "direct":
            raise JournalError("settlement_strategy_conflict", "This obligation is assigned to reversal; direct settlement would extinguish it twice.")
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
            raise JournalError("wrong_payment_direction", "Allocation direction conflicts with the signed payment; apply credits separately.")
        total += signed
        control_postings.append((item["account"], -signed))
        normalized_rows.append({"item_id": item_id, "amount": f"{amount:.2f}"})
        changed = {**item, "base_remaining_amount": item.get("base_remaining_amount", item["remaining_amount"]),
                   "remaining_amount": f"{remaining - amount:.2f}", "status": "settled" if remaining == amount else "open"}
        mutations.append(SupportingMutation("items", item_id, changed, record_revision(store, "items", item_id)))
        allocation_id = "settlement:" + operation_id + ":" + item_id
        allocation = {"id": allocation_id, "kind": "settlement", "item_id": item_id, "amount": f"{amount:.2f}",
                      "signed_amount": f"{signed:.2f}", "date": when.isoformat(), "source_id": source_id,
                      "source_mode": source["mode"], "bank_account": bank, "bank_amount": f"{cash:.2f}", "evidence_ids": ids}
        mutations.append(SupportingMutation("allocations", allocation_id, allocation))
    if abs(used + total) > abs(cash):
        raise JournalError("overallocated_payment", "Allocations exceed the available payment amount.")
    if source["mode"] == "new":
        if store.source_exists(source_id) or existing_sources:
            raise JournalError("duplicate_payment", "The payment is already posted; select existing mode to allocate its remaining amount.")
        if total != cash:
            raise JournalError("unallocated_payment", "A new payment must be fully allocated; split/link any other treatment explicitly.")
        staged = next((t for t in _load_pending_categorization(entity) if t.get("id") == source_id), None)
        if staged:
            staged_amount = Decimal(normalize_amount(staged.get("amount")))
            staged_bank = _ledger_account_for_txn(staged, entity.entity_config.get("bank_account_mappings"), entity.entity_config.get("csv_account_mappings"))
            if staged_amount != cash or str(staged.get("date"))[:10] != when.isoformat() or staged_bank != bank:
                raise JournalError("staged_payment_mismatch", "Selected bank facts differ from the staged source.")
        entries.append(_entry(when, source_id, "Settle outstanding items", [(bank, cash), *control_postings], settlement_id=operation_id))
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
            entries.append(_entry(when, "settlement:" + operation_id + ":reclass", "Link prior bank posting to outstanding items",
                                  [(other.account, total), *control_postings], origin_source_id=source_id, settlement_id=operation_id))
    request.update(operation_id=operation_id, source={**source, "amount": f"{cash:.2f}"}, allocations=normalized_rows, evidence_ids=ids)
    return request, entries, mutations


def preview_settlement(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    request, entries, mutations = _settlement_effect(entity, payload)
    return _preview_operation(entity, operation_id=request["operation_id"], kind="item_settlement", request=request,
                              entries=entries, mutations=mutations, evidence_ids=request["evidence_ids"], affected_dates=[request["date"]])


def apply_settlement(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    from ..ledger.importer import _entity_write_lock
    from .journals import _complete_linked_source_review
    with _entity_write_lock(entity.path):
        receipt = _apply(entity, plan, "item_settlement", preview_settlement, _settlement_effect)
        _complete_linked_source_review(entity, plan["request"]["source"]["source_id"], receipt, "settlement")
        return receipt


def _adjustment_effect(entity: Entity, payload: Mapping[str, Any]):
    request = _request(payload, {"operation_id", "item_id", "action", "amount", "date", "counter_account", "source_id", "evidence_ids", "reason"})
    operation_id, item_id = _id(request.get("operation_id")), _id(request.get("item_id"))
    when = _day(request.get("date"), "date")
    source_id, reason = _id(request.get("source_id")), request.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise JournalError("missing_reason", "Credit/writeoff requires a disclosed reason.")
    if request.get("action") not in {"credit", "writeoff"}:
        raise JournalError("invalid_adjustment", "Use an explicit credit or writeoff.")
    store, ids = _store(entity), _evidence_ids(request.get("evidence_ids", []))
    if not ids:
        raise JournalError("evidence_required", "Credits/writeoffs require stored evidence.")
    amount = _money(request.get("amount"))
    counter = _account(store, request.get("counter_account"), "counter_account", ("Income:", "Expenses:"))
    request.update(operation_id=operation_id, item_id=item_id, amount=f"{amount:.2f}", evidence_ids=ids, counter_account=counter)
    logical = {k: v for k, v in request.items() if k not in {"operation_id", "evidence_ids"}}
    claims, replay = _source_claims(store, _evidence(store, ids), canonical_hash(logical), operation_id)
    if replay:
        return request, [], [], replay
    item = load_record(store, "items", item_id)
    if not item or item.get("settlement_strategy", "direct") != "direct" or item.get("status", "open") in {"settled", "closed", "reversed"}:
        raise JournalError("inactive_item", "Adjustment needs an open item using direct settlement.")
    if when.isoformat() < item["as_of"]:
        raise JournalError("adjustment_precedes_origin", "Adjustment cannot precede the outstanding balance.")
    remaining = _remaining(item, list_records(store, "allocations"))
    if amount > remaining:
        raise JournalError("overallocated_item", "Credit/writeoff exceeds the supported remaining amount.")
    sign = item_sign(item)
    entry = _entry(when, "adjustment:" + operation_id, reason,
                   [(item["account"], -sign * amount), (counter, sign * amount)], item_id=item_id, origin_source_id=source_id)
    changed = {**item, "base_remaining_amount": item.get("base_remaining_amount", item["remaining_amount"]),
               "remaining_amount": f"{remaining - amount:.2f}", "status": "settled" if amount == remaining else "open"}
    allocation_id = "adjustment:" + source_id
    allocation = {"id": allocation_id, "kind": request["action"], "item_id": item_id, "amount": f"{amount:.2f}",
                  "date": when.isoformat(), "source_id": source_id, "operation_id": operation_id,
                  "counter_account": counter, "evidence_ids": ids, "reason": reason}
    if load_record(store, "allocations", allocation_id):
        raise JournalError("source_conflict", "This adjustment source is already allocated; use its original receipt or a correction.")
    return request, [entry], [SupportingMutation("items", item_id, changed, record_revision(store, "items", item_id)),
                             SupportingMutation("allocations", allocation_id, allocation), *claims], None


def preview_adjustment(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    request, entries, mutations, replay = _adjustment_effect(entity, payload)
    plan = _preview_operation(entity, operation_id=request["operation_id"], kind="item_adjustment", request=request,
                              entries=entries, mutations=mutations, evidence_ids=request["evidence_ids"])
    if replay:
        plan["existing_receipt"] = replay
    return plan


def apply_adjustment(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    return _apply(entity, plan, "item_adjustment", preview_adjustment, _adjustment_effect)


def _reversal_effect(entity: Entity, payload: Mapping[str, Any]):
    """Reverse exactly one supported obligation, including a carried balance."""
    from .journals import _text
    request = _request(payload, {"operation_id", "item_id", "date", "counter_account", "reason", "evidence_ids"})
    operation_id, item_id = _id(request.get("operation_id")), _id(request.get("item_id"))
    when, reason = _day(request.get("date"), "date"), _text(request.get("reason"), "reason")
    store = _store(entity)
    item = load_record(store, "items", item_id)
    if not item or item.get("status", "open") in {"settled", "closed", "reversed"}:
        raise JournalError("inactive_item", "This outstanding balance is absent or already extinguished.")
    if item.get("settlement_strategy", "direct") != "reverse":
        raise JournalError("settlement_strategy_conflict", "Only a reverse-strategy obligation may use a reversal.")
    if when.isoformat() < item["as_of"]:
        raise JournalError("reversal_precedes_origin", "An obligation reversal cannot precede its supported origin.")
    account = _account(store, request.get("counter_account"), "counter_account", ("Expenses:", "Income:"))
    ids = _evidence_ids(request.get("evidence_ids", item.get("evidence_ids", [])))
    if not ids:
        raise JournalError("evidence_required", "A carried reversal requires canonical source evidence.")
    remaining = _remaining(item, list_records(store, "allocations"))
    if remaining <= 0:
        raise JournalError("obligation_extinguished", "There is no remaining obligation to reverse.")
    signed = item_sign(item) * remaining
    entry = _entry(when, "item-reversal:" + operation_id, reason,
        [(item["account"], -signed), (account, signed)], item_id=item_id,
        reverses_obligation=item_id, origin_source_id=item["origin"]["source_id"])
    changed = {**item, "base_remaining_amount": item.get("base_remaining_amount", f"{remaining:.2f}"),
               "remaining_amount": "0.00", "status": "reversed", "reversal_operation_id": operation_id}
    allocation_id = "reversal:" + operation_id + ":" + item_id
    mutations = [SupportingMutation("items", item_id, changed, record_revision(store, "items", item_id)),
        SupportingMutation("allocations", allocation_id, {"id": allocation_id, "kind": "reversal", "item_id": item_id,
            "amount": f"{remaining:.2f}", "date": when.isoformat(), "source_id": entry.source_id,
            "operation_id": operation_id, "evidence_ids": ids})]
    original_id = item.get("created_operation_id")
    if item["origin"]["mode"] == "new" and original_id:
        marker = "extinguished:" + original_id
        if load_record(store, "intents", marker):
            raise JournalError("obligation_extinguished", "The financial origin has already been extinguished.")
        mutations.append(SupportingMutation("intents", marker, {"id": marker, "kind": "item_reversal",
            "original_id": original_id, "operation_id": operation_id}))
        due = load_record(store, "intents", "reversal:" + original_id)
        if due and due.get("status") == "pending":
            mutations.append(SupportingMutation("intents", due["id"], {**due, "status": "applied", "operation_id": operation_id},
                record_revision(store, "intents", due["id"])))
    request.update(operation_id=operation_id, item_id=item_id, date=when.isoformat(), counter_account=account,
                   reason=reason, evidence_ids=ids)
    return request, [entry], mutations


def preview_reversal(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    request, entries, mutations = _reversal_effect(entity, payload)
    return _preview_operation(entity, operation_id=request["operation_id"], kind="item_reversal", request=request,
        entries=entries, mutations=mutations, evidence_ids=request["evidence_ids"])


def apply_reversal(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    return _apply(entity, plan, "item_reversal", preview_reversal, _reversal_effect)


def list_items(entity: Entity) -> list[dict[str, Any]]:
    from .journals import _read_store
    return list_records(_read_store(entity), "items")
