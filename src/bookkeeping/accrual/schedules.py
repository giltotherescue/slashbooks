"""Deterministic prepaid/deferred schedules with reviewed, atomic recognition."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Mapping

from ..entity import Entity
from ..source_ids import source_id_variants
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


_ROOT_NAMES = {"Assets:": "an asset", "Liabilities:": "a liability", "Expenses:": "an expense", "Income:": "an income"}
KINDS = ("prepaid", "deferred", "depreciation")
# How a cash-method filer treats a prepaid expense for tax. Stored only when a person sets it.
TAX_TREATMENTS = ("expense_when_paid", "capitalize")
METHODS = ("actual_day", "equal_month", "custom")


def _account(store, value: Any, field: str, roots: tuple[str, ...] = (), purpose: str = "") -> str:
    account = _text(value, field)
    catalog = {o.account: o for o in store.load_opens()}
    if account not in catalog:
        raise JournalError("unmapped_account", f"{field} {account} is not an open account.",
                           f"Open it first with `books entity account-add <entity> --account {account} --open-date YYYY-MM-DD`, or name an existing account in {field}.")
    if roots and not account.startswith(roots):
        wanted = " or ".join(_ROOT_NAMES.get(r, r) for r in roots)
        raise JournalError("wrong_account_direction", f"{field} {account} must be {wanted} account ({'/'.join(roots)}...)"
                           + (f" because {purpose}." if purpose else "."),
                           f"Set {field} to an existing {'/'.join(roots)} account.")
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
    if not isinstance(value, Mapping):
        raise JournalError("invalid_request", "Request must be a JSON object.")
    if set(value) - allowed:
        raise JournalError("invalid_request", "Request contains unknown fields or authority claims: "
                           + ", ".join(sorted(map(str, set(value) - allowed))) + ".",
                           "Remove the listed fields; see `books accrual schema` for the accepted fields.")
    return deepcopy(dict(value))


def _normal_sign(record: Mapping[str, Any]) -> Decimal:
    return Decimal(-1 if record["kind"] == "deferred" else 1)


def _credit_account(record: Mapping[str, Any]) -> str:
    """Account credited (debited for deferred) when recognizing: the contra account for depreciation."""
    return record["contra_account"] if record["kind"] == "depreciation" else record["account"]


def _label(record: Mapping[str, Any]) -> str:
    return {"prepaid": "prepaid expense", "deferred": "deferred revenue", "depreciation": "depreciation"}[record["kind"]]


def _reject_legacy_dates(value: Mapping[str, Any], where: str = "") -> None:
    old = [name for name in ("start_date", "end_date") if isinstance(value, Mapping) and name in value]
    if old:
        raise JournalError("renamed_service_fields", f"{where}{' and '.join(old)} no longer exist: end_date was exclusive and is replaced by inclusive dates.",
                           f"Use {where}service_start (first day of service) and {where}service_end (last day of service, inclusive). "
                           "For an old exclusive end_date, service_end is the day before it.")


def _service(record: Mapping[str, Any]) -> tuple[date, date]:
    """First and last day of service, both inclusive. Opening records stored before the rename carry an exclusive end_date."""
    if "service_start" in record:
        return _day(record["service_start"], "service_start"), _day(record["service_end"], "service_end")
    return _day(record["start_date"], "start_date"), _day(record["end_date"], "end_date") - timedelta(days=1)


def _depreciable(record: Mapping[str, Any]) -> Decimal:
    return _money(record["amount"]) - _money(record.get("salvage_value", "0.00"), "salvage_value", positive=False)


def _check_service(method: Any, start: date, end: date, recognition_start: date, installments_in: Any,
                   total: Decimal, remaining: Decimal, field: str = "") -> list[dict[str, str]] | None:
    """Validate service dates against a method; return normalized custom installments (else None)."""
    if method not in METHODS:
        raise JournalError("invalid_method", f"{field}method must be one of actual_day, equal_month, custom.",
                           "Use actual_day (exact daily), equal_month (whole calendar months), or custom with an installments list.")
    if end < start or (end - start).days > 36600:
        raise JournalError("invalid_service_period", f"{field}service_end must not precede {field}service_start, and service may span at most 100 years. "
                           "Both dates are inclusive: a one-year contract starting 2026-01-15 ends 2027-01-14.")
    if not start <= recognition_start <= end:
        raise JournalError("invalid_recognition_start", "Recognition start must fall inside the service interval.")
    if method == "equal_month":
        problems = []
        if start.day != 1:
            problems.append(f"{field}service_start {start} is not the first day of a month")
        if (end + timedelta(days=1)).day != 1:
            problems.append(f"{field}service_end {end} is not the last day of a month")
        if recognition_start.day != 1:
            problems.append(f"recognition_start {recognition_start} is not the first day of a month")
        if problems:
            raise JournalError("incomplete_calendar_month", "equal_month needs whole calendar months, but " + "; ".join(problems)
                               + ", so a month would be partial.",
                               "Change method to actual_day (daily, exact over the real number of days), or to custom with an installments list of dated amounts.")
    if method != "custom":
        if installments_in is not None:
            raise JournalError("installments_not_allowed", "installments is only allowed with method custom.",
                               "Remove installments, or set method to custom.")
        return None
    if not isinstance(installments_in, list) or not installments_in or len(installments_in) > 1200:
        raise JournalError("invalid_installments", "method custom needs installments: a list of 1 to 1200 {\"date\", \"amount\"} objects.",
                           'Supply installments such as [{"date": "2026-03-31", "amount": "100.00"}, ...].')
    result, previous, total_seen, remaining_seen = [], None, Decimal(0), Decimal(0)
    for index, row in enumerate(installments_in):
        if not isinstance(row, Mapping) or set(row) != {"date", "amount"}:
            raise JournalError("invalid_installments", f"installments[{index}] must have exactly date and amount.")
        when = _day(row.get("date"), f"installments[{index}].date")
        if previous is not None and when <= previous:
            raise JournalError("installments_not_ascending", f"installments[{index}].date {when} is not after the previous date {previous}.",
                               "List installments in strictly ascending date order, one per date.")
        if not start <= when <= end:
            raise JournalError("installment_outside_service", f"installments[{index}].date {when} is outside service {start} to {end}.",
                               "Move the date inside the service period or widen service_start/service_end.")
        amount = _money(row.get("amount"), f"installments[{index}].amount")
        previous, total_seen = when, total_seen + amount
        if when >= recognition_start:
            remaining_seen += amount
        result.append({"date": when.isoformat(), "amount": f"{amount:.2f}"})
    if total_seen != total:
        raise JournalError("installments_sum_mismatch", f"installments add up to {total_seen:.2f} but the schedule amount to recognize is {total:.2f} (difference {total_seen - total:+.2f}).",
                           "Adjust the installment amounts so they add up exactly to the amount to recognize (amount minus salvage_value).")
    if remaining_seen != remaining:
        raise JournalError("installments_sum_mismatch", f"installments from recognition_start add up to {remaining_seen:.2f} but the remaining amount is {remaining:.2f} (difference {remaining_seen - remaining:+.2f}).",
                           "Installments dated before recognition_start must equal recognized_amount.")
    return result


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _human(text: Any, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


def _month_label(day: str | date) -> str:
    when = date.fromisoformat(day) if isinstance(day, str) else day
    return f"{_MONTHS[when.month - 1]} {when.year}"


def _item_ref(record: Mapping[str, Any]) -> str:
    """Human reference of a schedule or item: reference, then counterparty, then its start date. Never an ID."""
    for field in ("reference", "counterparty"):
        label = _human(record.get(field))
        if label:
            return label
    start = record.get("service_start") or record.get("as_of") or record.get("start_date")
    return f"dated {start}" if start else "no reference"


def _party_ref(record: Mapping[str, Any]) -> str:
    """Counterparty and reference as one label. A reference that repeats the counterparty is not written twice."""
    who, ref = _human(record.get("counterparty")), _human(record.get("reference"))
    if not ref or ref.casefold() == who.casefold():
        return who
    return f"{who} {ref}" if who else ref


def _labelled(label: str, text: str) -> str:
    return f"{label}: {text}"[:2000] if text else label


_FUNDING = {"prepaid": "Prepaid expense", "deferred": "Deferred revenue received", "depreciation": "Equipment purchase"}
_RECLASS = {"prepaid": "Reclassified as prepaid expense", "deferred": "Reclassified as deferred revenue",
            "depreciation": "Reclassified as equipment purchase"}


def _funding_narration(record: Mapping[str, Any]) -> str:
    return _labelled(_FUNDING[record["kind"]], _human(record.get("reference"), 1900) or _human(record.get("counterparty"), 1900))


def _reclass_narration(record: Mapping[str, Any]) -> str:
    return _labelled(_RECLASS[record["kind"]], _human(record.get("reference"), 1900) or _human(record.get("counterparty"), 1900))


def _recognition_narration(record: Mapping[str, Any], when: str) -> str:
    verb = {"prepaid": "Prepaid expense used", "deferred": "Deferred revenue earned", "depreciation": "Depreciation"}[record["kind"]]
    return f"{verb}: {_item_ref(record)} ({_month_label(when)})"


def _origin_effect(entity: Entity, record: dict[str, Any], operation_id: str) -> list[Entry]:
    """Plan the origin journal. Also records `origin_date`, the day the control balance starts to exist."""
    store, origin = _store(entity), record["origin"]
    mode, source_id = origin["mode"], origin["source_id"]
    amount = _money(record["amount"])
    remaining = _money(record["remaining_amount"], positive=False)
    sign, account = _normal_sign(record), record["account"]
    label = _label(record)
    when = _day(origin["date"], "origin.date") if origin.get("date") else None
    if mode == "new":
        when = when or _service(record)[0]
        record["origin_date"] = when.isoformat()
        if any(store.source_exists(s) for s in source_id_variants(source_id)):
            raise JournalError("duplicate_origin", "Funding source is already posted; link it as an existing origin.")
        bank = _account(store, origin.get("funding_account"), "origin.funding_account")
        card = bank == "Liabilities:CreditCard" or bank.startswith("Liabilities:CreditCard:")
        if not bank.startswith("Assets:") and not (sign > 0 and card):
            raise JournalError("invalid_funding_account", "Prepaid and depreciation funding requires an asset or credit-card account; deferred revenue requires an incoming asset account.")
        if bank == account:
            raise JournalError("invalid_origin", "Funding and control accounts must differ.")
        from ..ledger.importer import _load_pending_categorization, _ledger_account_for_txn
        staged = next((row for row in _load_pending_categorization(entity) if row.get("id") in source_id_variants(source_id)), None)
        if staged:
            staged_bank = _ledger_account_for_txn(staged, entity.entity_config.get("bank_account_mappings"), entity.entity_config.get("csv_account_mappings"))
            if (Decimal(normalize_amount(staged.get("amount"))) != -sign * amount
                    or str(staged.get("date"))[:10] != when.isoformat() or staged_bank != bank):
                raise JournalError("staged_payment_mismatch", "Schedule funding differs from the exact staged bank source.")
            if str(staged.get("id")) != source_id:
                raise JournalError("staged_source_id_differs",
                                   f"The staged row is {staged.get('id')}; use that source_id for the funding source.",
                                   f"Use source_id {staged.get('id')} and create a new preview.")
        return [_entry(when, source_id, _funding_narration(record),
                       [(account, sign * amount), (bank, -sign * amount)], schedule_id=record["id"])]
    original = _find_live_entry(store, source_id)
    # The posted payment date is when the control balance starts, whatever the service dates are.
    when = when or original.date
    record["origin_date"] = original.date.isoformat()
    if original.date > when and mode == "existing":
        raise JournalError("invalid_origin_date", "The reclassification cannot precede its posted payment.")
    if mode == "opening":
        available = sum((p.amount for p in original.postings if p.account == account), Decimal(0)) * sign
        reserved = sum((_money(s.get("origin_reserved_amount", s.get("base_remaining_amount", s["remaining_amount"])), positive=False)
                        for s in list_records(store, "schedules") if s.get("origin", {}).get("source_id") == source_id
                        and s.get("account") == account and not s.get("supersedes")), Decimal(0))
        if reserved + (amount if record["kind"] == "depreciation" else remaining) > available:
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
                           ("Expenses:",) if sign > 0 else ("Income:",), "the payment is being reclassified out of the original expense or income")
    matched = [p for p in original.postings if p.account == old_account and p.amount == sign * amount]
    if len(matched) != 1:
        raise JournalError("origin_amount_mismatch", "The existing payment's category amount does not match this schedule.")
    return [_entry(when, "schedule:" + operation_id + ":reclass", _reclass_narration(record),
                   [(account, sign * amount), (old_account, -sign * amount)],
                   schedule_id=record["id"], origin_source_id=source_id)]


def _supporting_period_check(records: list[dict[str, Any]], start: date, end: date) -> None:
    for record in records:
        period = record.get("envelope", {}).get("service_period")
        if not period:
            continue
        first = date.fromisoformat(period["start"])
        last = date.fromisoformat(period["end"]) - (timedelta(days=0) if period.get("end_inclusive", True) else timedelta(days=1))
        if start < first or end > last:
            raise JournalError("service_outside_supporting_document",
                f"Schedule service {start} to {end} is not inside the supporting document's service period {first} to {last} (both inclusive).",
                f"Set service_start and service_end inside {first} to {last}, or link the supporting document that covers the schedule (supporting document {record['id']}).")


def _schedule_effect(entity: Entity, payload: Mapping[str, Any]):
    _reject_legacy_dates(payload)
    request = _request(payload, {"operation_id", "id", "kind", "account", "recognition_account", "contra_account", "currency",
        "amount", "salvage_value", "service_start", "service_end", "method", "installments", "origin", "evidence_ids",
        "recognition_start", "recognized_amount", "reference", "tax_treatment"})
    operation_id, schedule_id = _id(request.get("operation_id")), _id(request.get("id"))
    if "tax_treatment" in request:
        from ..preparation_runtime import is_preparation
        if is_preparation():
            raise JournalError("tax_treatment_person_only", "Recording a tax treatment requires the owner or tax preparer.")
        request["tax_treatment"] = _tax_treatment(request["tax_treatment"], request.get("kind"))
    if "reference" in request:
        request["reference"] = _text(request["reference"], "reference")
    store = _store(entity)
    kind = request.get("kind")
    if kind not in KINDS or request.get("currency", "USD") != "USD":
        raise JournalError("invalid_schedule", "Schedules support kind prepaid, deferred or depreciation in USD.")
    start, end = _day(request.get("service_start"), "service_start"), _day(request.get("service_end"), "service_end")
    method = request.get("method")
    recognition_start = _day(request.get("recognition_start", start.isoformat()), "recognition_start") if method in METHODS else start
    if kind == "deferred":
        control_roots, recognition_roots = ("Liabilities:",), ("Income:",)
        why, why_recognition = "deferred revenue is money received before it is earned (a liability)", "deferred revenue is released into income"
    else:
        control_roots, recognition_roots = ("Assets:",), ("Expenses:",)
        why = "a " + kind + " schedule spreads an asset" + (" (the cost of the asset)" if kind == "depreciation" else " paid in advance")
        why_recognition = "a " + kind + " schedule releases the asset into expense"
    account = _account(store, request.get("account"), "account", control_roots, why)
    recognition_account = _account(store, request.get("recognition_account"), "recognition_account", recognition_roots, why_recognition)
    if kind == "depreciation":
        contra = _account(store, request.get("contra_account"), "contra_account", ("Assets:",),
                          "accumulated depreciation is a contra-asset account such as Assets:Fixed:Accumulated Depreciation")
        if contra == account:
            raise JournalError("invalid_contra_account", "contra_account must differ from the asset account.",
                               "Use a separate accumulated depreciation account such as Assets:Fixed:Accumulated Depreciation.")
        request["contra_account"] = contra
    elif "contra_account" in request or "salvage_value" in request:
        raise JournalError("depreciation_only_field", "contra_account and salvage_value apply only to kind depreciation.",
                           "Remove them, or set kind to depreciation.")
    amount = _money(request.get("amount"))
    salvage = _money(request.get("salvage_value", "0.00"), "salvage_value", positive=False)
    if salvage >= amount:
        raise JournalError("invalid_salvage_value", f"salvage_value {salvage:.2f} must be less than amount {amount:.2f}.")
    if "salvage_value" in request:
        request["salvage_value"] = f"{salvage:.2f}"
    base = amount - salvage
    recognized = _money(request.get("recognized_amount", "0.00"), "recognized_amount", positive=False)
    if recognized >= base:
        raise JournalError("invalid_recognized_amount", "Initial recognition must leave a positive supported balance.")
    installments_in = _check_service(method, start, end, recognition_start, request.get("installments"), base, base - recognized)
    if installments_in is not None:
        request["installments"] = installments_in
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
        raise JournalError("evidence_required", "New schedule funding requires a stored supporting document.")
    _supporting_period_check(_evidence(store, ids), start, end)
    request.update(operation_id=operation_id, id=schedule_id, kind=kind, account=account,
                   recognition_account=recognition_account, currency="USD", amount=f"{amount:.2f}",
                   recognized_amount=f"{recognized:.2f}", recognition_start=recognition_start.isoformat(), evidence_ids=ids)
    record = {key: deepcopy(value) for key, value in request.items() if key != "operation_id"}
    record.update(remaining_amount=f"{base - recognized:.2f}", base_remaining_amount=f"{base - recognized:.2f}",
                  base_recognized_amount=f"{recognized:.2f}", status="active", generation=0,
                  origin_reserved_amount=f"{base - recognized:.2f}" if origin["mode"] == "opening" and kind != "depreciation" else f"{amount:.2f}",
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
                              affected_dates=[(record or {}).get("origin_date") or request["origin"].get("date", request["recognition_start"])], details=details)
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
    """Allocate cents exactly. Service dates are inclusive.

    equal_month gives each whole month floor(remaining / months) cents and actual_day gives each month
    floor(remaining * days in month / total days) cents; the final installment receives the residual cents.
    custom returns the supplied installments dated on or after recognition_start.
    """
    first, last = _service(record)
    start = _day(record.get("recognition_start", first.isoformat()), "recognition_start")
    end = last + timedelta(days=1)
    remaining = _money(record.get("base_remaining_amount", record["remaining_amount"]), positive=False)
    if record["method"] == "custom":
        return [{"date": row["date"], "amount": row["amount"], "service_start": row["date"], "service_end": row["date"]}
                for row in record.get("installments", []) if row["date"] >= start.isoformat()]
    segments = []
    cursor = start
    while cursor < end:
        following = end if cursor.year == 9999 and cursor.month == 12 else date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        stop = min(following, end)
        segments.append((cursor, stop))
        cursor = stop
    if record["method"] == "equal_month" and (start.day != 1 or end.day != 1):
        raise JournalError("incomplete_calendar_month", "equal_month needs whole calendar months; the remaining service does not start on the 1st and end on a month end.",
                           "Supersede with method actual_day (daily, exact) or custom installments.")
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
                            "service_start": begin.isoformat(), "service_end": (stop - timedelta(days=1)).isoformat()})
    return results


def _schedule_details(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"schedule_id": record["id"], "installments": installments(record) if record.get("status") == "active" else [],
            "service_dates": "service_start and service_end are both inclusive",
            "rounding": "Floor exact minor units (cents) per installment; the final installment receives the residual cents. custom uses the supplied amounts.",
            "recognized_amount": record["recognized_amount"], "remaining_amount": record["remaining_amount"],
            "recognized_refund_amount": record.get("recognized_refund_amount", "0.00")}


def _recognition_entry(record: Mapping[str, Any], allocation_id: str, installment: Mapping[str, str], generation: int):
    amount, sign = _money(installment["amount"]), _normal_sign(record)
    source_id = "schedule:" + allocation_id
    entry = _entry(_day(installment["date"], "recognition date"), source_id, _recognition_narration(record, installment["date"]),
                   [(record["recognition_account"], sign * amount), (_credit_account(record), -sign * amount)], schedule_id=record["id"])
    allocation = {"id": allocation_id, "kind": "recognition", "schedule_id": record["id"], **installment,
                  "source_id": source_id, "generation": generation, "evidence_ids": record.get("evidence_ids", [])}
    return entry, allocation


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
        if record["kind"] == "depreciation":
            _account(store, record["contra_account"], "contra_account")
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
            entry, allocation = _recognition_entry(record, allocation_id, installment, generation)
            due_total += _money(installment["amount"])
            entries.append(entry)
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


def _terminate(store, record: dict[str, Any], changed: dict[str, Any], request: dict[str, Any], when: date,
               operation_id: str, ids: list[str], remaining: Decimal, entries: list[Entry], mutations: list[SupportingMutation]) -> None:
    """Recognize service through effective_date (inclusive), then release the unearned balance to offset_account."""
    effective = _day(request.get("effective_date"), "effective_date")
    first, last = _service(record)
    current_start = _day(record.get("recognition_start", first.isoformat()), "recognition_start")
    generation = record.get("generation", 0)
    if effective >= last:
        raise JournalError("terminate_after_service_end", f"effective_date {effective} is not before the last service day {last}; nothing is left to terminate.",
                           "Run `books accrual schedule due-preview` to recognize the rest, or choose an earlier effective_date.")
    if effective < current_start:
        raise JournalError("terminate_before_service", f"effective_date {effective} is before recognition starts on {current_start}; no service was earned.",
                           "Use action cancel to release the whole remaining balance, or choose a later effective_date.")
    if when < effective:
        raise JournalError("change_precedes_effective", f"date {when} is before effective_date {effective}.",
                           "Set date (the posting date of the refund or write-off) on or after effective_date.")
    sign, label = _normal_sign(record), _label(record)
    earned, parts = Decimal(0), []
    for part in installments(record):
        allocation_id = f"recognition:{record['id']}:{generation}:{part['date']}"
        if part["service_end"] <= effective.isoformat():
            if not load_record(store, "allocations", allocation_id):
                parts.append((allocation_id, part))
        elif record["method"] != "custom" and part["service_start"] <= effective.isoformat():
            days = (effective - date.fromisoformat(part["service_start"])).days + 1
            total_days = (date.fromisoformat(part["service_end"]) - date.fromisoformat(part["service_start"])).days + 1
            cents = int(_money(part["amount"]) * 100) * days // total_days
            if cents:
                parts.append((f"recognition:{record['id']}:{generation}:{effective.isoformat()}",
                              {"date": effective.isoformat(), "amount": f"{Decimal(cents) / 100:.2f}",
                               "service_start": part["service_start"], "service_end": effective.isoformat()}))
    for allocation_id, part in parts:
        entry, allocation = _recognition_entry(record, allocation_id, part, generation)
        entries.append(entry)
        mutations.append(SupportingMutation("allocations", allocation_id, allocation))
        earned += _money(part["amount"])
    unearned = remaining - earned
    if unearned < 0:
        raise JournalError("schedule_balance_conflict", "Earned recognition exceeds the supported remaining balance.")
    offset = None
    if record["kind"] == "depreciation":
        if request.get("offset_account"):
            raise JournalError("offset_not_allowed", "Terminating depreciation posts no refund or write-off, so offset_account is not allowed.",
                               "Remove offset_account. The asset cost and accumulated depreciation stay; depreciation simply stops.")
    elif unearned:
        offset = _account(store, request.get("offset_account"), "offset_account")
        if offset == record["account"]:
            raise JournalError("invalid_offset", "Offset and control accounts must differ.")
        entries.append(_entry(when, "schedule:" + operation_id + ":terminate", "Ended early: " + _item_ref(record),
                              [(record["account"], -sign * unearned), (offset, sign * unearned)], schedule_id=record["id"]))
    elif request.get("offset_account"):
        raise JournalError("offset_not_allowed", "Nothing is left unearned at effective_date, so offset_account is not allowed.", "Remove offset_account.")
    allocation_id = "schedule-change:" + operation_id
    mutations.append(SupportingMutation("allocations", allocation_id, {
        "id": allocation_id, "kind": "schedule_terminate", "schedule_id": record["id"], "date": when.isoformat(),
        "effective_date": effective.isoformat(), "amount": f"{unearned:.2f}", "remaining_refund_amount": f"{unearned:.2f}",
        "recognized_refund_amount": "0.00", "recognized_refund_account": None, "offset_account": offset,
        "recognized_through_termination": f"{earned:.2f}", "evidence_ids": ids, "operation_id": operation_id}))
    changed.update(status="terminated", remaining_amount="0.00", terminated_amount=f"{unearned:.2f}", terminated_on=effective.isoformat(),
                   recognized_amount=f"{_money(record['recognized_amount'], positive=False) + earned:.2f}")


def _change_effect(entity: Entity, payload: Mapping[str, Any]):
    _reject_legacy_dates(payload.get("replacement") if isinstance(payload, Mapping) else None, "replacement.")
    request = _request(payload, {"operation_id", "id", "action", "date", "effective_date", "reason", "evidence_ids", "amount", "offset_account", "replacement",
                                 "remaining_refund_amount", "recognized_refund_amount", "recognized_refund_account"})
    operation_id, schedule_id = _id(request.get("operation_id")), _id(request.get("id"))
    when, reason = _day(request.get("date"), "date"), _text(request.get("reason"), "reason")
    ids = _evidence_ids(request.get("evidence_ids", []))
    if not ids:
        raise JournalError("evidence_required", "Schedule changes require a stored supporting document.")
    store = _store(entity)
    request.update(operation_id=operation_id, id=schedule_id, date=when.isoformat(), reason=reason, evidence_ids=ids)
    for field in ("amount", "remaining_refund_amount", "recognized_refund_amount"):
        if field in request:
            request[field] = f"{_money(request[field], field, positive=field == 'amount'):.2f}"
    if "effective_date" in request:
        request["effective_date"] = _day(request["effective_date"], "effective_date").isoformat()
    logical = {key: value for key, value in request.items() if key not in {"operation_id", "evidence_ids"}}
    claims, replay = _source_claims(store, _evidence(store, ids), canonical_hash({"kind": "schedule_change", **logical}), operation_id)
    if replay:
        return request, [], [], replay
    record = load_record(store, "schedules", schedule_id)
    if not record or (record.get("status") != "active" and not (request.get("action") == "refund" and record.get("status") == "completed")):
        raise JournalError("inactive_schedule", "Only an active schedule can be changed.")
    remaining = _money(record["remaining_amount"], positive=False)
    action = request.get("action")
    if action not in {"cancel", "supersede", "refund", "terminate"}:
        raise JournalError("invalid_change", "Use cancel, supersede, refund, or terminate with explicit effects.")
    if action == "terminate" and "effective_date" not in request:
        raise JournalError("effective_date_required", "terminate needs effective_date, the last day of service (inclusive).",
                           "Add effective_date, for example the last day the customer or vendor service ran.")
    if action != "terminate" and "effective_date" in request:
        raise JournalError("invalid_request", "effective_date applies only to action terminate.", "Remove effective_date, or use action terminate.")
    if record["kind"] == "depreciation" and action != "terminate":
        raise JournalError("depreciation_change_unsupported", f"Action {action} does not apply to a depreciation schedule.",
                           "Use action terminate with effective_date to stop depreciation (for example at disposal); the asset cost stays in place.")
    original = _find_live_entry(store, record["origin"]["source_id"])
    earliest = max(original.date, _day(record.get("effective_from", original.date.isoformat()), "effective_from"))
    if when < earliest:
        raise JournalError("change_precedes_origin", "A schedule change cannot precede its financial origin or effective support date.")
    posted_dates = [a["date"] for a in list_records(store, "allocations") if a.get("schedule_id") == schedule_id and a.get("kind") == "recognition"]
    guard = request["effective_date"] if action == "terminate" else when.isoformat()
    if posted_dates and guard < max(posted_dates):
        last_posted = max(posted_dates)
        raise JournalError("change_precedes_recognition",
            f"Recognition is already posted through {last_posted}, which is after the change date {guard}. A posted recognition cannot be rewritten by a schedule change.",
            f"Set {'effective_date' if action == 'terminate' else 'date'} on or after {last_posted}. If service really stopped earlier, "
            "run `books accrual schedule change-preview` with action refund, recognized_refund_amount (the over-recognized amount), "
            "recognized_refund_account and offset_account, dated on or after " + last_posted + ".")
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
    roots = ("Expenses:",) if record["kind"] == "prepaid" else ("Income:",)
    if action == "terminate":
        _terminate(store, record, changed, request, when, operation_id, ids, remaining, entries, mutations)
    elif action == "supersede":
        replacement = request.get("replacement")
        if not isinstance(replacement, Mapping) or set(replacement) - {"id", "service_start", "service_end", "method", "installments", "recognition_account"}:
            raise JournalError("invalid_replacement", "Replacement defines a fresh ID, service_start, service_end, method (with installments for custom) and recognition account.")
        replacement_id = _id(replacement.get("id"))
        if load_record(store, "schedules", replacement_id) or replacement_id == schedule_id:
            raise JournalError("identity_conflict", "Replacement schedule requires a new identity.")
        start, end = _day(replacement.get("service_start"), "replacement.service_start"), _day(replacement.get("service_end"), "replacement.service_end")
        if start < when:
            raise JournalError("invalid_service_period", "Replacement service must begin on/after the change date.")
        method = replacement.get("method")
        custom = _check_service(method, start, end, start, replacement.get("installments"), remaining, remaining, "replacement.")
        account = _account(store, replacement.get("recognition_account", record["recognition_account"]), "recognition_account", roots,
                           "the recognition account follows the schedule kind")
        successor = {**{k: v for k, v in record.items() if k not in {"installments", "service_start", "service_end", "start_date", "end_date"}},
            "id": replacement_id, "amount": f"{remaining:.2f}", "recognized_amount": "0.00",
            "remaining_amount": f"{remaining:.2f}", "base_recognized_amount": "0.00", "base_remaining_amount": f"{remaining:.2f}",
            "service_start": start.isoformat(), "recognition_start": start.isoformat(), "service_end": end.isoformat(),
            "method": method, "recognition_account": account, "status": "active", "generation": 0,
            "supersedes": schedule_id, "evidence_ids": ids, "changes": [], "created_operation_id": operation_id}
        if custom is not None:
            successor["installments"] = custom
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
            recognized_account = _account(store, request.get("recognized_refund_account"), "recognized_refund_account", roots,
                                          "the recognized portion of a refund reverses expense or income")
            postings.append((recognized_account, -sign * recognized_refund))
        if amount:
            entries.append(_entry(when, "schedule:" + operation_id + ":" + action,
                {"cancel": "Cancelled", "refund": "Refund"}.get(action, "Balance released") + ": " + _item_ref(record), postings, schedule_id=schedule_id))
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
            if record["method"] == "custom":
                raise JournalError("custom_partial_refund", "A partial refund cannot be spread over the remaining custom installments automatically.",
                                   "Use action supersede with a new custom installments list for the reduced balance, or refund the whole remaining balance.")
            first, last = _service(record)
            new_start = max(_day(record.get("recognition_start", first.isoformat()), "recognition_start"), when + timedelta(days=1))
            if new_start > last:
                raise JournalError("no_future_service", "No future service remains to allocate the revised balance.")
            if record["method"] == "equal_month" and new_start.day != 1:
                raise JournalError("incomplete_calendar_month", "A refund dated mid-month cannot keep equal_month, which needs whole calendar months.",
                                   "To end service mid-month with daily proration, use action terminate with effective_date set to the last service day; "
                                   "date is the posting date of the refund and may differ. Example: {\"operation_id\": \"end-service\", \"id\": \"" + record["id"]
                                   + "\", \"action\": \"terminate\", \"effective_date\": \"2026-03-14\", \"date\": \"2026-03-15\", "
                                   "\"offset_account\": \"Assets:Bank:Checking\", \"reason\": \"Customer refund\", \"evidence_ids\": [\"<supporting-document-id>\"]}. "
                                   "Or use action supersede with new terms (for example method actual_day) for the remaining service.")
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


# ---------------------------------------------------------------------------
# Tax treatment of prepaid expenses (cash-method filers)
# ---------------------------------------------------------------------------

def _tax_treatment(value: Any, kind: Any) -> str:
    if value not in TAX_TREATMENTS:
        raise JournalError("invalid_tax_treatment", "tax_treatment must be expense_when_paid or capitalize.",
                           "Use expense_when_paid to deduct the payment when paid, or capitalize to deduct it as it is used.")
    if kind != "prepaid":
        raise JournalError("tax_treatment_prepaid_only", "tax_treatment applies only to prepaid schedules.",
                           "Remove tax_treatment, or use it on a prepaid schedule.")
    return value


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    from calendar import monthrange
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def default_tax_treatment(record: Mapping[str, Any], fiscal_year_start: str = "01-01") -> str | None:
    """The default tax treatment of a prepaid schedule, or None for other kinds.

    expense_when_paid only when the benefit lasts 12 months or less and ends by the end of the tax year
    after the payment's tax year (the 12-month rule). Otherwise capitalize. The company fiscal year determines the next year end.
    This default needs review by a tax professional.
    """
    if record.get("kind") != "prepaid":
        return None
    start, end = _service(record)
    paid_raw = record.get("origin_date") or (record.get("origin") or {}).get("date") or start.isoformat()
    paid = _day(paid_raw, "origin date")
    within_12_months = end < _add_months(start, 12)
    from ..fiscal import year_start
    anchor = year_start(paid, fiscal_year_start)
    by_next_year_end = end < date(anchor.year + 2, anchor.month, anchor.day)
    return "expense_when_paid" if within_12_months and by_next_year_end else "capitalize"


def effective_tax_treatment(record: Mapping[str, Any], fiscal_year_start: str = "01-01") -> tuple[str | None, str]:
    """Return (treatment, source): source is "recorded" when a person set it, else "default"."""
    if "tax_treatment" in record:
        return _tax_treatment(record["tax_treatment"], record.get("kind")), "recorded"
    return default_tax_treatment(record, fiscal_year_start), "default"


TAX_TREATMENT_KIND = "schedule_tax_treatment_set"


def preview_tax_treatment(entity: Entity, schedule_id: Any, treatment: Any) -> dict[str, Any]:
    """Preview recording the tax treatment of one prepaid schedule. Writes nothing."""
    from .journals import _read_store
    from ..entity import load_entity
    from .enable import _integrity
    entity = load_entity(entity.path)
    _integrity(entity, "record the schedule tax treatment")
    store = _read_store(entity)
    schedule_id = _id(schedule_id)
    record = load_record(store, "schedules", schedule_id)
    if record is None:
        raise JournalError("schedule_not_found", f"No schedule has id {schedule_id}.", "Run `books accrual schedule list --entity PATH`.")
    treatment = _tax_treatment(treatment, record.get("kind"))
    current, source = effective_tax_treatment(record, entity.entity_config.get("fiscal_year_start", "01-01"))
    body = {"schema_version": 1, "kind": TAX_TREATMENT_KIND, "request": {"id": schedule_id, "tax_treatment": treatment},
            "changes": record.get("tax_treatment") != treatment, "ledger_digest": store.content_digest(),
            "configuration_hash": canonical_hash(entity.entity_config),
            "record_revision": record_revision(store, "schedules", schedule_id),
            "effects": {"from": record.get("tax_treatment"), "current_effective": current, "current_source": source, "to": treatment,
                        "entries_changed": 0, "published_statements_changed": False,
                        "note": ("Only the tax outputs of a cash-method filer change (`books tax map` and `books tax export` with the "
                                 "tax method view on). The books, the reports and the schedule's recognition entries stay as they are.")}}
    body["request_hash"] = canonical_hash({"kind": TAX_TREATMENT_KIND, "request": body["request"]})
    body["plan_hash"] = canonical_hash(body)
    return body


def apply_tax_treatment(entity: Entity, schedule_id: Any, treatment: Any, plan_hash: str | None = None) -> dict[str, Any]:
    """Record the tax treatment. Repeating the same value returns already_applied."""
    from ..ledger.importer import _entity_write_lock
    from ..entity import load_entity
    from .enable import _seal
    from .store import apply_mutations
    with _entity_write_lock(entity.path):
        entity = load_entity(entity.path)
        fresh = preview_tax_treatment(entity, schedule_id, treatment)
        if plan_hash is not None and plan_hash != fresh["plan_hash"]:
            raise JournalError("stale_preview", "The company changed after the preview.", "Run the preview again.")
        request = fresh["request"]
        if not fresh["changes"]:
            return {"status": "already_applied", "kind": TAX_TREATMENT_KIND, **request, "request_hash": fresh["request_hash"]}
        store = _store(entity)
        record = load_record(store, "schedules", request["id"])
        updated = {**record, "tax_treatment": request["tax_treatment"]}
        with store.transaction(immediate=True) as conn:
            apply_mutations(store, [SupportingMutation("schedules", request["id"], updated, fresh["record_revision"])], conn)
            store.append_audit_event("schedule-tax-treatment-set", {"schedule_id": request["id"], "from": fresh["effects"]["from"],
                                                                    "to": request["tax_treatment"], "plan_hash": fresh["plan_hash"]}, conn)
            _seal(store, conn, "schedule-tax-treatment")
        return {"status": "applied", "kind": TAX_TREATMENT_KIND, **request, "from": fresh["effects"]["from"],
                "request_hash": fresh["request_hash"], "plan_hash": fresh["plan_hash"], "entries_changed": 0}


# ---------------------------------------------------------------------------
# Deposit kind of a deferred schedule or a customer deposit item (cash-method tax view)
# ---------------------------------------------------------------------------

# Stored only when a person sets it. Unset means advance_payment, and the tax outputs say so.
DEPOSIT_KINDS = ("advance_payment", "refundable")
DEPOSIT_KIND_KIND = "deposit_kind_set"
_DEPOSIT_TABLES = {"schedules": "schedule", "items": "item"}


def is_deposit(record: Mapping[str, Any], table: str) -> bool:
    """A deferred schedule, or a credit item that the company holds for a customer (a liability credit)."""
    if table == "schedules":
        return record.get("kind") == "deferred"
    return (record.get("kind") == "credit" and str(record.get("account", "")).startswith("Liabilities:")
            and record.get("direction") == "credit")


def deposit_kind(value: Any, record: Mapping[str, Any], table: str) -> str:
    if value not in DEPOSIT_KINDS:
        raise JournalError("invalid_deposit_kind", "deposit_kind must be advance_payment or refundable.",
                           "Use advance_payment for money that is the customer's payment, or refundable for money the company must give back.")
    if not is_deposit(record, table):
        raise JournalError("deposit_kind_deposits_only",
                           "deposit_kind applies only to deferred schedules and customer deposit items (credit items on a liability account).",
                           "Choose a deferred schedule or a customer deposit item.")
    return value


def effective_deposit_kind(record: Mapping[str, Any], table: str) -> tuple[str, str]:
    """Return (kind, source): source is "recorded" when a person set it, else "assumed" (advance_payment)."""
    if "deposit_kind" in record:
        return deposit_kind(record["deposit_kind"], record, table), "recorded"
    return "advance_payment", "assumed"


def preview_deposit_kind(entity: Entity, table: str, record_id: Any, kind: Any) -> dict[str, Any]:
    """Preview recording the deposit kind of one deferred schedule or customer deposit item. Writes nothing."""
    from .journals import _read_store
    from ..entity import load_entity
    from .enable import _integrity
    if table not in _DEPOSIT_TABLES:
        raise JournalError("unsupported_operation", "deposit_kind is recorded on schedules or items.")
    entity = load_entity(entity.path)
    _integrity(entity, "record the deposit kind")
    store = _read_store(entity)
    record_id = _id(record_id)
    record = load_record(store, table, record_id)
    if record is None:
        raise JournalError(f"{_DEPOSIT_TABLES[table]}_not_found", f"No {_DEPOSIT_TABLES[table]} has id {record_id}.",
                           f"Run `books accrual {_DEPOSIT_TABLES[table]} list --entity PATH`.")
    kind = deposit_kind(kind, record, table)
    current, source = effective_deposit_kind(record, table)
    body = {"schema_version": 1, "kind": DEPOSIT_KIND_KIND, "request": {"table": table, "id": record_id, "deposit_kind": kind},
            "changes": record.get("deposit_kind") != kind, "ledger_digest": store.content_digest(),
            "configuration_hash": canonical_hash(entity.entity_config),
            "record_revision": record_revision(store, table, record_id),
            "effects": {"from": record.get("deposit_kind"), "current_effective": current, "current_source": source, "to": kind,
                        "entries_changed": 0, "published_statements_changed": False,
                        "note": ("Only the cash-method tax outputs change (`books tax map` and `books tax export` with the tax method view on). "
                                 "advance_payment: income when received. refundable: a liability until it is applied to an invoice or "
                                 "forfeited. The books, the reports and the recorded entries stay as they are.")}}
    body["request_hash"] = canonical_hash({"kind": DEPOSIT_KIND_KIND, "request": body["request"]})
    body["plan_hash"] = canonical_hash(body)
    return body


def apply_deposit_kind(entity: Entity, table: str, record_id: Any, kind: Any, plan_hash: str | None = None) -> dict[str, Any]:
    """Record the deposit kind (person-only). Repeating the same value returns already_applied."""
    from ..ledger.importer import _entity_write_lock
    from ..entity import load_entity
    from ..preparation_runtime import is_preparation
    from .enable import _seal
    from .store import apply_mutations
    if is_preparation():
        raise JournalError("deposit_kind_person_only", "Recording a deposit kind requires the owner or tax preparer.")
    with _entity_write_lock(entity.path):
        entity = load_entity(entity.path)
        fresh = preview_deposit_kind(entity, table, record_id, kind)
        if plan_hash is not None and plan_hash != fresh["plan_hash"]:
            raise JournalError("stale_preview", "The company changed after the preview.", "Run the preview again.")
        request = fresh["request"]
        if not fresh["changes"]:
            return {"status": "already_applied", "kind": DEPOSIT_KIND_KIND, **request, "request_hash": fresh["request_hash"]}
        store = _store(entity)
        record = load_record(store, table, request["id"])
        updated = {**record, "deposit_kind": request["deposit_kind"]}
        with store.transaction(immediate=True) as conn:
            apply_mutations(store, [SupportingMutation(table, request["id"], updated, fresh["record_revision"])], conn)
            store.append_audit_event("deposit-kind-set", {"table": table, "id": request["id"], "from": fresh["effects"]["from"],
                                                         "to": request["deposit_kind"], "plan_hash": fresh["plan_hash"]}, conn)
            _seal(store, conn, "deposit-kind")
        return {"status": "applied", "kind": DEPOSIT_KIND_KIND, **request, "from": fresh["effects"]["from"],
                "request_hash": fresh["request_hash"], "plan_hash": fresh["plan_hash"], "entries_changed": 0}
