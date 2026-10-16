"""Deterministic prepaid/deferred schedules with reviewed, atomic recognition."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Mapping

from ..entity import Entity
from ..ledger.importer import _find_live_entry
from ..ledger.model import Entry, Posting
from .intake import canonical_hash, normalize_amount
from .journals import (JournalError, _apply_operation, _day, _evidence, _id,
                       _preview_operation, _replay_operation, _source_claims, _store, _text)
from .store import SupportingMutation, list_records, load_record, record_revision


def _money(value: Any, field: str = "amount", *, positive: bool = True) -> Decimal:
    result = Decimal(normalize_amount(value, field))
    if result < 0 or (positive and result == 0):
        raise JournalError("invalid_amount", f"{field} must be {'positive' if positive else 'nonnegative'}.")
    return result


def _account(store, value: Any, field: str, roots: tuple[str, ...] = ()) -> str:
    account = _text(value, field)
    catalog = {o.account: o for o in store.load_opens()}
    if account not in catalog or (roots and not account.startswith(roots)):
        raise JournalError("unmapped_account", f"{field} must name a permitted existing account: {account}.")
    if catalog[account].currencies and catalog[account].currencies != ("USD",):
        raise JournalError("unsupported_currency", f"{account} is not a USD account.")
    return account


def _entry(when: date, source_id: str, description: str, postings: list[tuple[str, Decimal]], **meta: str) -> Entry:
    return Entry(when, description, postings=tuple(Posting(a, n, "USD") for a, n in postings if n),
                 meta=(("source-id", source_id), *((k.replace("_", "-"), v) for k, v in meta.items())))


def _evidence_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(i, str) for i in value) or len(set(value)) != len(value):
        raise JournalError("invalid_evidence", "evidence_ids must be unique stored IDs.")
    return sorted(value)


def _request(value: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise JournalError("invalid_request", "Request contains unknown fields or authority claims.")
    return deepcopy(dict(value))


def _normal_sign(record: Mapping[str, Any]) -> Decimal:
    return Decimal(1 if record["kind"] == "prepaid" else -1)


def _narration(action: str, record: Mapping[str, Any]) -> str:
    """Use bounded human labels for display; canonical IDs remain in metadata."""
    for field in ("reference", "counterparty"):
        label = record.get(field)
        if isinstance(label, str) and label.strip():
            return action + ": " + " ".join(label.split())[:2000]
    return action


def _origin_effect(entity: Entity, record: dict[str, Any], operation_id: str) -> list[Entry]:
    store, origin = _store(entity), record["origin"]
    mode, source_id = origin["mode"], origin["source_id"]
    amount = _money(record["amount"])
    remaining = _money(record["remaining_amount"], positive=False)
    sign, account = _normal_sign(record), record["account"]
    label = "prepaid expense" if record["kind"] == "prepaid" else "deferred revenue"
    when = _day(origin.get("date", record["start_date"]), "origin.date")
    if mode == "new":
        if store.source_exists(source_id):
            raise JournalError("duplicate_origin", "Funding source is already posted; link it as an existing origin.")
        bank = _account(store, origin.get("funding_account"), "origin.funding_account")
        card = bank == "Liabilities:CreditCard" or bank.startswith("Liabilities:CreditCard:")
        if not bank.startswith("Assets:") and not (record["kind"] == "prepaid" and card):
            raise JournalError("invalid_funding_account", "Prepaid funding requires an asset or credit-card account; deferred revenue requires an incoming asset account.")
        if bank == account:
            raise JournalError("invalid_origin", "Funding and control accounts must differ.")
        from ..ledger.importer import _load_pending_categorization, _ledger_account_for_txn
        staged = next((row for row in _load_pending_categorization(entity) if row.get("id") == source_id), None)
        if staged:
            staged_bank = _ledger_account_for_txn(staged, entity.entity_config.get("bank_account_mappings"), entity.entity_config.get("csv_account_mappings"))
            if (Decimal(normalize_amount(staged.get("amount"))) != -sign * amount
                    or str(staged.get("date"))[:10] != when.isoformat() or staged_bank != bank):
                raise JournalError("staged_payment_mismatch", "Schedule funding differs from the exact staged bank source.")
        return [_entry(when, source_id, _narration("Fund " + label, record),
                       [(account, sign * amount), (bank, -sign * amount)], schedule_id=record["id"])]
    original = _find_live_entry(store, source_id)
    if original.date > when and mode == "existing":
        raise JournalError("invalid_origin_date", "The reclassification cannot precede its posted payment.")
    if mode == "opening":
        available = sum((p.amount for p in original.postings if p.account == account), Decimal(0)) * sign
        reserved = sum((_money(s.get("origin_reserved_amount", s.get("base_remaining_amount", s["remaining_amount"])), positive=False)
                        for s in list_records(store, "schedules") if s.get("origin", {}).get("source_id") == source_id
                        and s.get("account") == account and not s.get("supersedes")), Decimal(0))
        if reserved + remaining > available:
            raise JournalError("opening_support_exceeds_balance", "Schedules exceed the linked opening control balance.")
        return []
    prior = [s for s in list_records(store, "schedules") if s.get("origin", {}).get("source_id") == source_id]
    if prior:
        raise JournalError("duplicate_origin", "This payment already funds a schedule; use a reviewed change.")
    if len(original.postings) != 2 or any(p.currency != "USD" for p in original.postings):
        raise JournalError("ambiguous_origin", "Existing funding needs an exact two-posting USD payment linkage.")
    control = [p for p in original.postings if p.account == account and p.amount == sign * amount]
    if control:
        return []
    old_account = _account(store, origin.get("reclass_account"), "origin.reclass_account",
                           ("Expenses:",) if sign > 0 else ("Income:",))
    matched = [p for p in original.postings if p.account == old_account and p.amount == sign * amount]
    if len(matched) != 1:
        raise JournalError("origin_amount_mismatch", "The existing payment's category amount does not match this schedule.")
    return [_entry(when, "schedule:" + operation_id + ":reclass", _narration("Reclassify funding for " + label, record),
                   [(account, sign * amount), (old_account, -sign * amount)],
                   schedule_id=record["id"], origin_source_id=source_id)]


def _schedule_effect(entity: Entity, payload: Mapping[str, Any]):
    request = _request(payload, {"operation_id", "id", "kind", "account", "recognition_account", "currency",
        "amount", "start_date", "end_date", "method", "origin", "evidence_ids", "recognition_start", "recognized_amount", "reference"})
    operation_id, schedule_id = _id(request.get("operation_id")), _id(request.get("id"))
    if "reference" in request:
        request["reference"] = _text(request["reference"], "reference")
    store = _store(entity)
    kind = request.get("kind")
    if kind not in {"prepaid", "deferred"} or request.get("currency", "USD") != "USD":
        raise JournalError("invalid_schedule", "Schedules support prepaid/deferred in USD.")
    start, end = _day(request.get("start_date"), "start_date"), _day(request.get("end_date"), "end_date")
    if end <= start or (end - start).days > 36600:
        raise JournalError("invalid_service_period", "Service dates must cover a positive interval of at most 100 years.")
    method = request.get("method")
    if method not in {"actual_day", "equal_month"}:
        raise JournalError("invalid_method", "Use actual_day or equal_month.")
    recognition_start = _day(request.get("recognition_start", start.isoformat()), "recognition_start")
    if not start <= recognition_start < end:
        raise JournalError("invalid_recognition_start", "Recognition start must fall inside the service interval.")
    if method == "equal_month" and (start.day != 1 or end.day != 1 or recognition_start.day != 1):
        raise JournalError("incomplete_calendar_month", "equal_month requires whole calendar months; use actual_day for partial months.")
    account = _account(store, request.get("account"), "account", ("Assets:",) if kind == "prepaid" else ("Liabilities:",))
    recognition_account = _account(store, request.get("recognition_account"), "recognition_account",
                                   ("Expenses:",) if kind == "prepaid" else ("Income:",))
    amount = _money(request.get("amount"))
    recognized = _money(request.get("recognized_amount", "0.00"), "recognized_amount", positive=False)
    if recognized >= amount:
        raise JournalError("invalid_recognized_amount", "Initial recognition must leave a positive supported balance.")
    origin = request.get("origin")
    if not isinstance(origin, Mapping) or set(origin) - {"mode", "source_id", "date", "funding_account", "reclass_account"}:
        raise JournalError("invalid_origin", "Supply a bounded new, existing, or opening origin.")
    if origin.get("mode") not in {"new", "existing", "opening"}:
        raise JournalError("invalid_origin", "Origin mode must be new, existing, or opening.")
    _id(origin.get("source_id"))
    if origin["mode"] != "opening" and (recognized or recognition_start != start):
        raise JournalError("invalid_initial_recognition", "Prior recognition is only carried from an evidenced opening balance.")
    ids = _evidence_ids(request.get("evidence_ids", []))
    if not ids and origin["mode"] == "new":
        raise JournalError("evidence_required", "New schedule funding requires stored evidence.")
    request.update(operation_id=operation_id, id=schedule_id, kind=kind, account=account,
                   recognition_account=recognition_account, currency="USD", amount=f"{amount:.2f}",
                   recognized_amount=f"{recognized:.2f}", recognition_start=recognition_start.isoformat(), evidence_ids=ids)
    record = {key: deepcopy(value) for key, value in request.items() if key != "operation_id"}
    record.update(remaining_amount=f"{amount - recognized:.2f}", base_remaining_amount=f"{amount - recognized:.2f}",
                  base_recognized_amount=f"{recognized:.2f}", status="active", generation=0,
                  origin_reserved_amount=f"{amount - recognized:.2f}" if origin["mode"] == "opening" else f"{amount:.2f}",
                  created_operation_id=operation_id)
    mutations = []
    replay = None
    if origin["mode"] == "new":
        logical = {k: v for k, v in request.items() if k not in {"operation_id", "id", "evidence_ids"}}
        claims, replay = _source_claims(store, _evidence(store, ids), canonical_hash(logical), operation_id)
        mutations.extend(claims)
    if replay:
        return request, [], [], replay
    if load_record(store, "schedules", schedule_id):
        raise JournalError("identity_conflict", "Schedule already exists; use an explicit change.")
    entries = _origin_effect(entity, record, operation_id)
    mutations.insert(0, SupportingMutation("schedules", schedule_id, record))
    return request, entries, mutations, replay


def preview_schedule(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    request, entries, mutations, replay = _schedule_effect(entity, payload)
    record = next((m.payload for m in mutations if m.table == "schedules"), None)
    details = _schedule_details(record) if record else {"existing_receipt": replay}
    plan = _preview_operation(entity, operation_id=request["operation_id"], kind="schedule_create", request=request,
                              entries=entries, mutations=mutations, evidence_ids=request["evidence_ids"],
                              affected_dates=[request["origin"].get("date", request["recognition_start"])], details=details)
    if replay:
        plan["existing_receipt"] = replay
    return plan


def _apply(entity: Entity, plan: Mapping[str, Any], kind: str, preview, effect) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or plan.get("kind") != kind or not isinstance(plan.get("request"), Mapping):
        raise JournalError("invalid_plan", "Expected a current " + kind + " preview.")
    existing = _replay_operation(entity, plan)
    if existing:
        return existing
    current = preview(entity, plan["request"])
    if current["plan_hash"] != plan.get("plan_hash"):
        raise JournalError("stale_preview", "The schedule/item preview changed; review a fresh preview.")
    if current.get("existing_receipt"):
        return _apply_operation(entity, current, entries=[], mutations=[], replay_receipt=current["existing_receipt"])
    _, entries, mutations, *_ = effect(entity, current["request"])
    return _apply_operation(entity, current, entries=entries, mutations=mutations)


def apply_schedule(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    from ..ledger.importer import _entity_write_lock
    from .journals import _complete_linked_source_review
    with _entity_write_lock(entity.path):
        receipt = _apply(entity, plan, "schedule_create", preview_schedule, _schedule_effect)
        if plan["request"]["origin"]["mode"] != "opening":
            _complete_linked_source_review(entity, plan["request"]["origin"]["source_id"], receipt, "schedule_funding")
        return receipt


def installments(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """Allocate cents exactly; end_date is exclusive and the final period gets residual cents."""
    start = _day(record.get("recognition_start", record["start_date"]), "recognition_start")
    end = _day(record["end_date"], "end_date")
    remaining = _money(record.get("base_remaining_amount", record["remaining_amount"]), positive=False)
    segments = []
    cursor = start
    while cursor < end:
        following = end if cursor.year == 9999 and cursor.month == 12 else date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        stop = min(following, end)
        segments.append((cursor, stop))
        cursor = stop
    if record["method"] == "equal_month" and (start.day != 1 or end.day != 1):
        raise JournalError("incomplete_calendar_month", "equal_month requires complete calendar months.")
    if not segments:
        return []
    minor = int(remaining * 100)
    denominator = len(segments) if record["method"] == "equal_month" else (end - start).days
    used, results = 0, []
    for index, (begin, stop) in enumerate(segments):
        weight = 1 if record["method"] == "equal_month" else (stop - begin).days
        cents = minor - used if index == len(segments) - 1 else minor * weight // denominator
        used += cents
        if cents:
            results.append({"date": (stop - timedelta(days=1)).isoformat(), "amount": f"{Decimal(cents) / 100:.2f}",
                            "service_start": begin.isoformat(), "service_end": stop.isoformat()})
    return results


def _schedule_details(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"schedule_id": record["id"], "installments": installments(record) if record.get("status") == "active" else [],
            "end_date_convention": "exclusive", "rounding": "Floor exact minor units; final installment receives residual cents.",
            "recognized_amount": record["recognized_amount"], "remaining_amount": record["remaining_amount"],
            "recognized_refund_amount": record.get("recognized_refund_amount", "0.00")}


def _due_effect(entity: Entity, payload: Mapping[str, Any]):
    request = _request(payload, {"operation_id", "through"})
    request["operation_id"] = _id(request.get("operation_id"))
    through = _day(request.get("through"), "through")
    store, entries, mutations = _store(entity), [], []
    for record in list_records(store, "schedules"):
        if record.get("status") != "active":
            continue
        record = deepcopy(record)
        _account(store, record["account"], "account")
        _account(store, record["recognition_account"], "recognition_account")
        generation = record.get("generation", 0)
        record.setdefault("base_recognized_amount", record["recognized_amount"])
        record.setdefault("base_remaining_amount", record["remaining_amount"])
        record.setdefault("origin_reserved_amount", record["base_remaining_amount"])
        recognized, remaining = _money(record["recognized_amount"], positive=False), _money(record["remaining_amount"], positive=False)
        due_total = Decimal(0)
        for installment in installments(record):
            allocation_id = f"recognition:{record['id']}:{generation}:{installment['date']}"
            if installment["date"] > through.isoformat() or load_record(store, "allocations", allocation_id):
                continue
            amount, sign = _money(installment["amount"]), _normal_sign(record)
            due_total += amount
            source_id = "schedule:" + allocation_id
            entries.append(_entry(_day(installment["date"], "recognition date"), source_id,
                _narration("Recognize " + ("prepaid expense" if record["kind"] == "prepaid" else "deferred revenue"), record),
                [(record["recognition_account"], sign * amount), (record["account"], -sign * amount)], schedule_id=record["id"]))
            allocation = {"id": allocation_id, "kind": "recognition", "schedule_id": record["id"],
                          **installment, "source_id": source_id, "generation": generation,
                          "evidence_ids": record.get("evidence_ids", [])}
            mutations.append(SupportingMutation("allocations", allocation_id, allocation))
        if due_total:
            if due_total > remaining:
                raise JournalError("schedule_balance_conflict", "Due recognition exceeds the supported remaining balance.")
            record.update(recognized_amount=f"{recognized + due_total:.2f}", remaining_amount=f"{remaining - due_total:.2f}")
            if remaining == due_total:
                record["status"] = "completed"
            mutations.append(SupportingMutation("schedules", record["id"], record, record_revision(store, "schedules", record["id"])))
    if len(entries) > 1200:
        raise JournalError("batch_too_large", "Recognize a smaller date range or schedule cohort.")
    return request, entries, mutations


def preview_due(entity: Entity, through: str, *, operation_id: str) -> dict[str, Any]:
    return _preview_due_request(entity, {"operation_id": operation_id, "through": through})


def _preview_due_request(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    request, entries, mutations = _due_effect(entity, payload)
    ids = sorted({i for m in mutations if m.table == "schedules" for i in m.payload.get("evidence_ids", [])})
    return _preview_operation(entity, operation_id=request["operation_id"], kind="schedule_due", request=request,
                              entries=entries, mutations=mutations, evidence_ids=ids)


def apply_due(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    return _apply(entity, plan, "schedule_due", _preview_due_request, _due_effect)


def _change_effect(entity: Entity, payload: Mapping[str, Any]):
    request = _request(payload, {"operation_id", "id", "action", "date", "reason", "evidence_ids", "amount", "offset_account", "replacement",
                                 "remaining_refund_amount", "recognized_refund_amount", "recognized_refund_account"})
    operation_id, schedule_id = _id(request.get("operation_id")), _id(request.get("id"))
    when, reason = _day(request.get("date"), "date"), _text(request.get("reason"), "reason")
    ids = _evidence_ids(request.get("evidence_ids", []))
    if not ids:
        raise JournalError("evidence_required", "Schedule changes require stored supporting evidence.")
    store = _store(entity)
    request.update(operation_id=operation_id, id=schedule_id, date=when.isoformat(), reason=reason, evidence_ids=ids)
    for field in ("amount", "remaining_refund_amount", "recognized_refund_amount"):
        if field in request:
            request[field] = f"{_money(request[field], field, positive=field == 'amount'):.2f}"
    logical = {key: value for key, value in request.items() if key not in {"operation_id", "evidence_ids"}}
    claims, replay = _source_claims(store, _evidence(store, ids), canonical_hash({"kind": "schedule_change", **logical}), operation_id)
    if replay:
        return request, [], [], replay
    record = load_record(store, "schedules", schedule_id)
    if not record or (record.get("status") != "active" and not (request.get("action") == "refund" and record.get("status") == "completed")):
        raise JournalError("inactive_schedule", "Only an active schedule can be changed.")
    remaining = _money(record["remaining_amount"], positive=False)
    action = request.get("action")
    if action not in {"cancel", "supersede", "refund"}:
        raise JournalError("invalid_change", "Use cancel, supersede, or refund with explicit effects.")
    original = _find_live_entry(store, record["origin"]["source_id"])
    earliest = max(original.date, _day(record.get("effective_from", original.date.isoformat()), "effective_from"))
    if when < earliest:
        raise JournalError("change_precedes_origin", "A schedule change cannot precede its financial origin or effective support date.")
    posted_dates = [a["date"] for a in list_records(store, "allocations") if a.get("schedule_id") == schedule_id and a.get("kind") == "recognition"]
    if posted_dates and when.isoformat() < max(posted_dates):
        raise JournalError("change_precedes_recognition", "A change cannot rewrite already recognized service history.")
    request.update(operation_id=operation_id, id=schedule_id, evidence_ids=ids)
    entries, mutations = [], list(claims)
    changed = deepcopy(record)
    changed.setdefault("origin_reserved_amount", record.get("base_remaining_amount", record["remaining_amount"]))
    changed["history"] = [*changed.get("history", []), {
        "effective_date": when.isoformat(), "operation_id": operation_id,
        "record": {k: deepcopy(v) for k, v in record.items() if k != "history"},
    }]
    changed["changes"] = [*changed.get("changes", []), {"operation_id": operation_id, "action": action,
        "date": when.isoformat(), "reason": reason, "evidence_ids": ids}]
    if action == "supersede":
        replacement = request.get("replacement")
        if not isinstance(replacement, Mapping) or set(replacement) - {"id", "start_date", "end_date", "method", "recognition_account"}:
            raise JournalError("invalid_replacement", "Replacement defines a fresh ID, service dates, method and recognition account.")
        replacement_id = _id(replacement.get("id"))
        if load_record(store, "schedules", replacement_id) or replacement_id == schedule_id:
            raise JournalError("identity_conflict", "Replacement schedule requires a new identity.")
        start, end = _day(replacement.get("start_date"), "replacement.start_date"), _day(replacement.get("end_date"), "replacement.end_date")
        if start < when or end <= start:
            raise JournalError("invalid_service_period", "Replacement service must begin on/after the change and end later.")
        method = replacement.get("method")
        if method not in {"actual_day", "equal_month"} or (method == "equal_month" and (start.day != 1 or end.day != 1)):
            raise JournalError("invalid_method", "Replacement method and service dates are inconsistent.")
        account = _account(store, replacement.get("recognition_account", record["recognition_account"]), "recognition_account",
                           ("Expenses:",) if record["kind"] == "prepaid" else ("Income:",))
        successor = {**record, "id": replacement_id, "amount": f"{remaining:.2f}", "recognized_amount": "0.00",
            "remaining_amount": f"{remaining:.2f}", "base_recognized_amount": "0.00", "base_remaining_amount": f"{remaining:.2f}",
            "start_date": start.isoformat(), "recognition_start": start.isoformat(), "end_date": end.isoformat(),
            "method": method, "recognition_account": account, "status": "active", "generation": 0,
            "supersedes": schedule_id, "evidence_ids": ids, "changes": [], "created_operation_id": operation_id}
        successor["history"] = []
        successor["effective_from"] = when.isoformat()
        changed.update(status="superseded", remaining_amount="0.00", transferred_amount=f"{remaining:.2f}", superseded_by=replacement_id)
        mutations.append(SupportingMutation("schedules", replacement_id, successor))
    else:
        amount = remaining if action == "cancel" else _money(request.get("amount"))
        recognized_refund = Decimal(0)
        remaining_refund = amount
        if action == "refund" and ("recognized_refund_amount" in request or "remaining_refund_amount" in request):
            if "recognized_refund_amount" not in request or "remaining_refund_amount" not in request:
                raise JournalError("explicit_refund_split_required", "Supply both remaining_refund_amount and recognized_refund_amount.")
            recognized_refund = _money(request["recognized_refund_amount"], "recognized_refund_amount", positive=False)
            remaining_refund = _money(request["remaining_refund_amount"], "remaining_refund_amount", positive=False)
            if recognized_refund + remaining_refund != amount:
                raise JournalError("refund_split_mismatch", "Explicit remaining and recognized portions must equal the total refund exactly.")
        if remaining_refund > remaining:
            raise JournalError("explicit_refund_split_required", "Refund exceeds unrecognized service; explicitly split remaining and recognized portions and select the recognized refund account.")
        previously_recognized_refund = _money(record.get("recognized_refund_amount", "0.00"), positive=False)
        if recognized_refund + previously_recognized_refund > _money(record["recognized_amount"], positive=False):
            raise JournalError("recognized_refund_exceeds_balance", "Recognized refund exceeds the gross recognized service still available to correct.")
        offset = _account(store, request.get("offset_account"), "offset_account")
        if offset == record["account"]:
            raise JournalError("invalid_offset", "Offset and control accounts must differ.")
        sign = _normal_sign(record)
        postings = [(record["account"], -sign * remaining_refund), (offset, sign * amount)]
        recognized_account = None
        if recognized_refund:
            recognized_account = _account(store, request.get("recognized_refund_account"), "recognized_refund_account",
                                           ("Expenses:",) if record["kind"] == "prepaid" else ("Income:",))
            postings.append((recognized_account, -sign * recognized_refund))
        if amount:
            entries.append(_entry(when, "schedule:" + operation_id + ":" + action,
                reason, postings, schedule_id=schedule_id))
        changed["remaining_amount"] = f"{remaining - remaining_refund:.2f}"
        changed["refunded_amount" if action == "refund" else "terminated_amount"] = f"{_money(changed.get('refunded_amount' if action == 'refund' else 'terminated_amount', '0.00'), positive=False) + remaining_refund:.2f}"
        changed["recognized_refund_amount"] = f"{previously_recognized_refund + recognized_refund:.2f}"
        allocation_id = "schedule-change:" + operation_id
        mutations.append(SupportingMutation("allocations", allocation_id, {
            "id": allocation_id, "kind": "schedule_" + action, "schedule_id": schedule_id, "date": when.isoformat(),
            "amount": f"{amount:.2f}", "remaining_refund_amount": f"{remaining_refund:.2f}",
            "recognized_refund_amount": f"{recognized_refund:.2f}", "recognized_refund_account": recognized_account,
            "offset_account": offset, "evidence_ids": ids, "operation_id": operation_id,
        }))
        if action == "cancel" or remaining_refund == remaining:
            changed["status"] = "cancelled" if action == "cancel" else "completed"
        else:
            new_start = max(_day(record.get("recognition_start", record["start_date"]), "recognition_start"), when + timedelta(days=1))
            if new_start >= _day(record["end_date"], "end_date"):
                raise JournalError("no_future_service", "No future service remains to allocate the revised balance.")
            if record["method"] == "equal_month" and new_start.day != 1:
                raise JournalError("incomplete_calendar_month", "A partial-month refund needs an explicitly superseded actual_day schedule.")
            changed.update(recognition_start=new_start.isoformat(), base_remaining_amount=f"{remaining - remaining_refund:.2f}",
                           base_recognized_amount=record["recognized_amount"], generation=record.get("generation", 0) + 1)
    mutations.append(SupportingMutation("schedules", schedule_id, changed, record_revision(store, "schedules", schedule_id)))
    return request, entries, mutations, None


def preview_change(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    request, entries, mutations, replay = _change_effect(entity, payload)
    details = {"schedules": [_schedule_details(m.payload) for m in mutations if m.table == "schedules"],
               "refund_split": next((m.payload for m in mutations if m.table == "allocations" and m.payload.get("kind") == "schedule_refund"), None)}
    plan = _preview_operation(entity, operation_id=request["operation_id"], kind="schedule_change", request=request,
                              entries=entries, mutations=mutations, evidence_ids=request["evidence_ids"], affected_dates=[request["date"]], details=details)
    if replay:
        plan["existing_receipt"] = replay
    return plan


def apply_change(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    return _apply(entity, plan, "schedule_change", preview_change, _change_effect)


def list_schedules(entity: Entity) -> list[dict[str, Any]]:
    from .journals import _read_store
    return list_records(_read_store(entity), "schedules")
