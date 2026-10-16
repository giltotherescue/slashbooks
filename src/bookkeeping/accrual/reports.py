"""Coverage-aware supporting reports, external support, and local period locks.

Supporting statements reconcile recorded facts with the financial ledger; they
do not calculate payroll, tax, depreciation, or provider accounting policies.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any, Mapping

from ..entity import Entity, load_entity, resolve_basis
from ..ledger.importer import _entity_write_lock
from ..ledger.store import LedgerStore
from .intake import canonical_hash, normalize_amount
from .store import SupportingMutation, TABLES, list_records, load_record, record_revision


class CoverageError(ValueError):
    """A requested subperiod cannot be reconstructed from summary evidence."""


def _records(conn, table):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", ("accrual_" + table,)).fetchone():
        return []
    return [json.loads(row[0]) for row in conn.execute(f"SELECT payload_json FROM accrual_{table} ORDER BY id")]


def basis(conn):
    from ..entity import resolve_basis
    values = dict(conn.execute("SELECT key,value FROM meta WHERE key IN ('basis','accounting_basis')"))
    return resolve_basis(values)


def opening_coverage(conn):
    return next((r for r in _records(conn, "coverage") if r.get("id") == "opening"), None)


def coverage_metadata(conn, kind, start, end):
    """Validate coverage without pretending a partial imported period is known."""
    result = {"basis": basis(conn), "currency": "USD", "coverage": None,
              "requested_from": start.isoformat() if start else None, "requested_to": end.isoformat(),
              "unresolved_issues": []}
    opening = opening_coverage(conn)
    if result["basis"] != "accrual" or not opening:
        return result
    result["coverage"] = deepcopy(opening)
    source_as_of = date.fromisoformat(opening["source_as_of"])
    cutover = date.fromisoformat(opening["cutover_date"])
    if end < source_as_of:
        raise CoverageError(f"Historical balances before {source_as_of} are unavailable from the imported opening summary.")
    if start is not None and start < cutover:
        summary_from, summary_to = opening.get("summary_from"), opening.get("summary_to")
        if (not summary_from or start.isoformat() != summary_from
                or end.isoformat() < summary_to):
            raise CoverageError("The requested pre-cutover subperiod is unavailable; request the full fiscal summary or a post-cutover period.")
        result["summary_included"] = True
        result["historical_detail_available"] = False
    else:
        result["summary_included"] = False
    if opening.get("verified") is not True or opening.get("support_status") != "reconciled":
        result["unresolved_issues"].append({"code": "opening_unreconciled", "refs": ["opening"]})
    return result


def financial_metadata(conn, kind, start, end):
    result = coverage_metadata(conn, kind, start, end)
    if result["basis"] == "accrual":
        result["unresolved_issues"].extend(unresolved(conn, end))
    return result


def _balances(conn, as_of):
    totals = {}
    for account, amount in conn.execute("SELECT p.account,p.amount FROM postings p JOIN entries e ON e.id=p.entry_id WHERE e.date<=?", (as_of,)):
        totals[account] = totals.get(account, Decimal(0)) + Decimal(amount)
    return totals


def _at_history(row, as_of):
    candidates = sorted(row.get("history", []), key=lambda h: h["effective_date"])
    for history in candidates:
        if history["effective_date"] > as_of:
            return deepcopy(history["record"])
    return deepcopy(row)


def _origin_date(record, opening):
    if record.get("effective_from"):
        return record["effective_from"]
    if record.get("origin", {}).get("mode") == "opening" and opening:
        return opening["source_as_of"]
    return record.get("as_of") or record.get("origin", {}).get("date") or record.get("start_date")


def _signed(record):
    if record.get("kind") == "credit":
        return Decimal(1 if record.get("direction") == "debit" else -1)
    return Decimal(1 if record["account"].startswith("Assets:") else -1)


def _managed_support(conn, as_of):
    opening = opening_coverage(conn)
    allocations = _records(conn, "allocations")
    items, schedules = [], []
    for current in _records(conn, "items"):
        row = _at_history(current, as_of)
        if (_origin_date(row, opening) or "9999-12-31") > as_of:
            continue
        relevant = [a for a in allocations if a.get("item_id") == row["id"] and a.get("kind") in {"settlement", "credit", "writeoff", "reversal"} and a.get("date", "9999-12-31") <= as_of]
        all_allocations = [a for a in allocations if a.get("item_id") == row["id"] and a.get("kind") in {"settlement", "credit", "writeoff", "reversal"}]
        base = Decimal(row.get("base_remaining_amount", row["remaining_amount"]))
        if "base_remaining_amount" not in row and all_allocations:
            base = Decimal(row["original_amount"])
        remaining = base - sum((Decimal(a["amount"]) for a in relevant), Decimal(0))
        if row.get("reversal_operation_id"):
            reversal = next((r for r in _records(conn, "intents") if r.get("id") == "operation:" + row["reversal_operation_id"]), None)
            reversed_on = reversal.get("request", {}).get("date") if reversal else None
            if reversed_on and reversed_on <= as_of:
                remaining = Decimal(0)
        row.update(remaining_amount=f"{remaining:.2f}", remaining_as_of=f"{remaining:.2f}", signed_balance=f"{_signed(row) * remaining:.2f}",
                   status="open" if remaining else "reversed" if row.get("reversal_operation_id") and reversed_on and reversed_on <= as_of else "settled",
                   allocations_as_of=[a["id"] for a in relevant])
        items.append(row)
    # Recognition and reviewed schedule changes have matching journal metadata.
    # Starting from the origin/control balance and following its immutable
    # entries reconstructs the earlier balance even after later recognition.
    for current in _records(conn, "schedules"):
        row = _at_history(current, as_of)
        origin_date = _origin_date(row, opening)
        if (origin_date or "9999-12-31") > as_of:
            continue
        relevant = [a for a in allocations if a.get("schedule_id") == row["id"] and a.get("date", "9999-12-31") <= as_of]
        all_recognition = [a for a in allocations if a.get("schedule_id") == row["id"] and a.get("kind") == "recognition"]
        base_remaining = Decimal(row.get("base_remaining_amount", row["remaining_amount"]))
        if "history" in current and current["history"]:
            first = sorted(current["history"], key=lambda h: h["effective_date"])[0]["record"]
            base_remaining = Decimal(first.get("base_remaining_amount", first["remaining_amount"]))
        elif "base_remaining_amount" not in row and all_recognition:
            base_remaining = Decimal(row["remaining_amount"]) + sum((Decimal(a["amount"]) for a in all_recognition), Decimal(0))
        remaining = base_remaining - sum((Decimal(a["remaining_refund_amount"] if a.get("kind") in {"schedule_refund", "schedule_cancel"} else a["amount"])
                                         for a in relevant if a.get("kind") in {"recognition", "schedule_refund", "schedule_cancel", "schedule_transfer"}), Decimal(0))
        for change in current.get("changes", []):
            if change["action"] == "supersede" and change["date"] <= as_of:
                remaining = Decimal(0)
        recognized = sum((Decimal(a["amount"]) for a in relevant if a.get("kind") == "recognition"), Decimal(0))
        initial = sorted(current.get("history", []), key=lambda h: h["effective_date"])[0]["record"] if current.get("history") else current
        initial_recognized = Decimal(initial.get("base_recognized_amount", initial.get("recognized_amount", "0")))
        row.update(remaining_amount=f"{remaining:.2f}", remaining_as_of=f"{remaining:.2f}", signed_balance=f"{_signed(row) * remaining:.2f}",
                   recognized_amount=f"{initial_recognized + recognized:.2f}",
                   status=row.get("status") if row.get("status") in {"cancelled", "superseded"} and not remaining else "active" if remaining else "completed",
                   recognized_refund_amount=f"{sum((Decimal(a.get('recognized_refund_amount', '0')) for a in relevant if a.get('kind') == 'schedule_refund'), Decimal(0)):.2f}",
                   refunded_amount=f"{sum((Decimal(a.get('remaining_refund_amount', '0')) for a in relevant if a.get('kind') == 'schedule_refund'), Decimal(0)):.2f}",
                   recognized_in_ledger=f"{recognized:.2f}", allocations_as_of=[a["id"] for a in relevant])
        schedules.append(row)
    return items, schedules


def support_snapshot(conn, as_of):
    """Reconstruct managed balances and choose immutable external snapshots."""
    as_of = as_of.isoformat() if isinstance(as_of, date) else as_of
    items, schedules = _managed_support(conn, as_of)
    external = [r for r in _records(conn, "coverage") if r.get("kind") in {"external_support", "external_support_snapshot"}
                and r.get("as_of", "9999-12-31") <= as_of]
    grouped = {}
    for row in external:
        account = row["account"]
        grouped.setdefault(account, []).append(row)
    selected = []
    for account, rows in grouped.items():
        snapshots = [r for r in rows if r["kind"] == "external_support_snapshot"]
        if snapshots:
            selected.append(max(snapshots, key=lambda r: (r["as_of"], r.get("snapshot_revision", 0), r["id"])))
        else:
            selected.extend(rows)
    return {"items": items, "schedules": schedules, "external": sorted(selected, key=lambda r: r["id"])}


def reconciliation(conn, as_of):
    as_of = as_of.isoformat() if isinstance(as_of, date) else as_of
    support = support_snapshot(conn, as_of)
    totals, refs = {}, {}
    for row in [*support["items"], *support["schedules"], *support["external"]]:
        account = row["account"]
        amount = Decimal(row.get("signed_balance", row.get("balance", "0")))
        totals[account] = totals.get(account, Decimal(0)) + amount
        refs.setdefault(account, []).append(row["id"])
    ledger = _balances(conn, as_of)
    rows = []
    for account in sorted(set(ledger) | set(totals)):
        if not account.startswith(("Assets:", "Liabilities:")) or account.startswith(("Assets:Bank:", "Assets:Cash:")):
            continue
        financial, detail = ledger.get(account, Decimal(0)), totals.get(account, Decimal(0))
        if financial or detail:
            rows.append({"account": account, "ledger_balance": f"{financial:.2f}", "support_balance": f"{detail:.2f}",
                         "difference": f"{detail - financial:.2f}", "status": "reconciled" if detail == financial else "unreconciled",
                         "support_ids": sorted(refs.get(account, [])), "currency": "USD"})
    return rows


def unresolved(conn, as_of):
    from .schedules import installments
    as_of = as_of.isoformat() if isinstance(as_of, date) else as_of
    issues = []
    database = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    if database:
        import hashlib
        root = Path(database).resolve().parent
        refs = []
        opening = opening_coverage(conn)
        if opening:
            refs.extend((path, opening.get("source_hashes", {}).get(kind)) for kind, path in opening.get("source_paths", {}).items())
        for evidence in _records(conn, "evidence"):
            envelope = evidence.get("envelope", {})
            refs.extend((r.get("uri"), r.get("content_hash")) for r in envelope.get("references", [])
                        if r.get("uri") and (not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", r["uri"]) or r["uri"].startswith("file:")))
        for reference, digest in refs:
            path = Path(reference)
            safe = (not path.is_absolute() and ".." not in path.parts and (root / path).is_file()
                    and not any(root.joinpath(*path.parts[:i]).is_symlink() for i in range(1, len(path.parts) + 1)))
            if not safe or not digest or hashlib.sha256((root / path).read_bytes()).hexdigest() != digest:
                issues.append({"code": "supporting_evidence_changed", "message": "Retained accounting evidence is missing or differs from its recorded hash.", "refs": [reference]})
    for row in reconciliation(conn, as_of):
        if row["status"] != "reconciled":
            issues.append({"code": "control_balance_unreconciled", "message": f"{row['account']} support differs by {row['difference']} USD.",
                           "refs": [row["account"], *row["support_ids"]]})
    allocated = {a["id"] for a in _records(conn, "allocations") if a.get("date", "9999-12-31") <= as_of}
    for current in _records(conn, "schedules"):
        row = _at_history(current, as_of)
        if row.get("status") != "active" or (_origin_date(row, opening_coverage(conn)) or "9999-12-31") > as_of:
            continue
        try:
            for part in installments(row):
                identity = f"recognition:{row['id']}:{row.get('generation', 0)}:{part['date']}"
                if part["date"] <= as_of and identity not in allocated:
                    issues.append({"code": "recognition_due", "message": f"Schedule {row['id']} has unposted recognition on {part['date']} ({part['amount']} USD).", "refs": [row["id"], identity]})
        except ValueError as exc:
            issues.append({"code": "schedule_incomplete", "message": str(exc), "refs": [row["id"]]})
    intents = _records(conn, "intents")
    for intent in _records(conn, "intents"):
        if intent.get("kind") != "scheduled_reversal" or intent.get("strategy") != "reverse" or intent.get("date", "9999-12-31") > as_of:
            continue
        original_id = intent["original_id"]
        extinguished = next((r for r in intents if r.get("id") == "extinguished:" + original_id), None)
        operation = next((r for r in intents if extinguished and r.get("id") == "operation:" + extinguished["operation_id"]), None)
        source_ids = operation.get("receipt", {}).get("entry_source_ids", []) if operation else []
        posted = any(conn.execute("SELECT 1 FROM entries WHERE source_id=? AND date<=?", (source_id, as_of)).fetchone() for source_id in source_ids)
        if not posted:
            issues.append({"code": "reversal_due", "message": f"Journal {original_id} has an unposted reversal due {intent['date']}.", "refs": [original_id]})
    for row in _records(conn, "coverage"):
        if row.get("kind") == "unresolved_settlement" and row.get("status") != "resolved" and row.get("as_of", row.get("date", "9999-12-31")) <= as_of:
            issues.append({"code": "settlement_unresolved", "message": "An imported cash movement needs explicit settlement treatment.", "refs": [row["id"]]})
    return sorted(issues, key=lambda i: (i["code"], i["refs"]))


def _adjustment_rows(store, conn, start, end):
    associations = {}
    for operation in _records(conn, "intents"):
        receipt = operation.get("receipt", {})
        identity = receipt.get("id")
        # Transport aliases replay another operation's receipt and create no
        # financial effects of their own.
        if (operation.get("status") != "applied" or not identity
                or operation.get("id") != "operation:" + identity):
            continue
        affected = operation.get("plan", {}).get("affected_dates", [])
        if affected and min(affected) > end.isoformat():
            continue
        trace = {"id": identity, "kind": receipt["kind"], "plan_hash": receipt["plan_hash"],
                 "evidence_ids": receipt.get("evidence_ids", [])}
        for source_id in receipt.get("entry_source_ids", []):
            associations.setdefault(source_id, {})[identity] = {**trace, "role": "posted_effect"}
        # Existing bank/control origins may need only supporting allocations,
        # or a reclassification. Retain their original dated financial entry
        # separately from any appended correction, without duplicating cash.
        request = operation.get("request", {})
        for name in ("origin", "source"):
            source = request.get(name, {})
            if source.get("mode") == "existing" and source.get("source_id"):
                associations.setdefault(source["source_id"], {}).setdefault(
                    identity, {**trace, "role": "linked_existing_source"})
    opening = opening_coverage(conn)
    opening_source = opening.get("opening_source_id") if opening else None
    rows = []
    for entry in store.load_entries(from_date=start, to_date=end):
        linked = associations.get(entry.source_id, {})
        if not linked and not (opening_source and entry.source_id == opening_source):
            continue
        rows.append({"date": entry.date.isoformat(), "source_id": entry.source_id,
                     "description": entry.narration, "metadata": dict(entry.meta),
                     "operations": [linked[key] for key in sorted(linked)],
                     "postings": [{"account": p.account, "amount": str(p.amount), "currency": p.currency}
                                  for p in entry.postings]})
    return rows


AGING_BUCKETS = {
    "current": "Current", "1_30": "1–30 days overdue", "31_60": "31–60 days overdue",
    "61_90": "61–90 days overdue", "over_90": "Over 90 days overdue",
    "unknown_due_date": "Due date not provided", "unapplied_credit": "Unapplied credit",
}


def aging(items, as_of):
    """Age supported open balances by explicit due date, never the cutover date.

    Credits remain separately identified and are not automatically allocated or
    netted against another item. Group totals are separated by kind and account.
    """
    from .items import validate_due_date
    rows, groups = [], {}
    for item in items:
        if item.get("kind") not in {"receivable", "payable", "credit"}:
            continue
        if item.get("currency") != "USD":
            raise ValueError("Aging supports USD balances only; foreign currencies cannot be combined.")
        remaining = Decimal(normalize_amount(item["remaining_amount"]))
        if remaining < 0:
            raise ValueError("Aging requires nonnegative remaining item balances.")
        if remaining == 0:
            continue
        due = validate_due_date(item)
        overdue = max(0, (as_of - date.fromisoformat(due)).days) if due else None
        if item["kind"] == "credit":
            bucket, overdue = "unapplied_credit", None
        elif overdue is None:
            bucket = "unknown_due_date"
        elif overdue == 0:
            bucket = "current"
        else:
            bucket = "1_30" if overdue <= 30 else "31_60" if overdue <= 60 else "61_90" if overdue <= 90 else "over_90"
        # Receivables/payables display their positive normal-side balance. A
        # credit displays its signed effect relative to that control's normal side.
        amount = remaining
        if item["kind"] == "credit":
            amount = Decimal(item["signed_balance"]) * (1 if item["account"].startswith("Assets:") else -1)
        counterparty = item.get("counterparty") or None
        if counterparty is not None and not isinstance(counterparty, str):
            raise ValueError("Aging counterparty must be text when provided.")
        row = {**item, "due_date": due, "days_past_due": overdue, "bucket": bucket,
               "bucket_label": AGING_BUCKETS[bucket], "aging_amount": f"{amount:.2f}"}
        rows.append(row)
        key = (item["kind"], item["account"], counterparty)
        group = groups.setdefault(key, {"kind": item["kind"], "account": item["account"],
            "counterparty": counterparty, "currency": "USD", "item_count": 0,
            "buckets": {name: Decimal(0) for name in AGING_BUCKETS}, "total": Decimal(0)})
        group["buckets"][bucket] += amount
        group["total"] += amount
        group["item_count"] += 1
    summary = []
    for key in sorted(groups, key=lambda k: (k[0], k[1], k[2] or "")):
        group = groups[key]
        summary.append({**group, "buckets": {name: f"{amount:.2f}" for name, amount in group["buckets"].items()},
                        "total": f"{group['total']:.2f}"})
    return sorted(rows, key=lambda r: (r["kind"], r["account"], r.get("counterparty") or "", r["id"])), summary


def report(entity, kind, *, as_of=None, from_date=None, to_date=None):
    entity = load_entity(entity.path) if isinstance(entity, Entity) else load_entity(entity)
    if resolve_basis(entity.entity_config) != "accrual":
        raise ValueError("Accrual supporting reports require an accrual company.")
    if entity.entity_config.get("accrual_capability_version") not in {0, 1}:
        raise ValueError("Unsupported accrual capability; use a compatible reader.")
    end = date.fromisoformat(str(as_of or to_date))
    start = date.fromisoformat(str(from_date)) if from_date else None
    with LedgerStore(entity.path / "ledger.sqlite").connection() as conn:
        meta = coverage_metadata(conn, kind, start, end)
        snapshot = support_snapshot(conn, end)
        extra = {}
        if kind == "aging":
            rows, summary = aging(snapshot["items"], end)
            extra = {"summary": summary, "bucket_labels": dict(AGING_BUCKETS),
                     "aging_basis": "due_date", "credits_netted": False}
        elif kind in {"schedules", "outstanding", "support"}:
            rows = snapshot[{"schedules": "schedules", "outstanding": "items", "support": "external"}[kind]]
        elif kind == "reconciliation":
            rows = reconciliation(conn, end)
        elif kind == "adjustments":
            rows = _adjustment_rows(LedgerStore(entity.path / "ledger.sqlite"), conn, start, end)
        else:
            raise ValueError("Supported accrual report kinds: schedules, outstanding, aging, adjustments, support, reconciliation.")
        result = {"schema_version": 1, "kind": kind, "as_of": end.isoformat(), "from_date": start.isoformat() if start else None,
                  **meta, **extra, "rows": rows, "issues": unresolved(conn, end), "canonical_digest": LedgerStore(entity.path / "ledger.sqlite").content_digest(conn)}
    result["fingerprint"] = canonical_hash(result)
    return result


def _support_effect(entity, payload):
    from .journals import _store, _id, _day, _evidence
    if not isinstance(payload, Mapping) or set(payload) - {"operation_id", "account", "as_of", "currency", "details", "evidence_ids", "supersedes", "mode", "managed_support_ids"}:
        raise ValueError("External support requires an explicit bounded account, date, detail rows, and evidence IDs.")
    request = deepcopy(dict(payload))
    operation_id, when = _id(request.get("operation_id")), _day(request.get("as_of"), "as_of")
    store = _store(entity)
    account = request.get("account")
    if account not in store.load_account_names() or not account.startswith(("Assets:", "Liabilities:")) or account.startswith(("Assets:Bank:", "Assets:Cash:")):
        raise ValueError("External support requires a mapped non-bank asset or liability control account.")
    if request.get("currency") != "USD":
        raise ValueError("External support currency must be USD.")
    ids = request.get("evidence_ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError("External support requires canonical source evidence.")
    _evidence(store, ids)
    details = request.get("details")
    if not isinstance(details, list) or not details or len(details) > 1000:
        raise ValueError("External support requires 1 to 1000 detail rows.")
    total, seen = Decimal(0), set()
    for detail in details:
        if not isinstance(detail, dict) or set(detail) - {"id", "balance", "description"}:
            raise ValueError("External supporting details contain id, signed balance, and optional description.")
        identity = _id(detail.get("id"))
        if identity in seen:
            raise ValueError("External supporting detail IDs must be unique.")
        seen.add(identity)
        detail["balance"] = normalize_amount(detail.get("balance"))
        total += Decimal(detail["balance"])
    with store.connection() as conn:
        coverage_metadata(conn, "support", None, when)
        snapshot = support_snapshot(conn, when)
        managed_rows = [r for r in [*snapshot["items"], *snapshot["schedules"]] if r["account"] == account and Decimal(r["signed_balance"])]
        managed = sum((Decimal(r["signed_balance"]) for r in managed_rows), Decimal(0))
        financial = _balances(conn, when.isoformat()).get(account, Decimal(0))
        previous = [r for r in snapshot["external"] if r["account"] == account]
        all_snapshots = [r for r in _records(conn, "coverage") if r.get("kind") == "external_support_snapshot" and r.get("account") == account]
    latest = max(all_snapshots, key=lambda r: (r["as_of"], r.get("snapshot_revision", 0), r["id"])) if all_snapshots else None
    if latest and (latest["as_of"] > when.isoformat() or latest["as_of"] == when.isoformat() and request.get("supersedes") != latest["id"]):
        raise ValueError("External support must advance its as-of date or explicitly supersede the latest same-day snapshot.")
    mode = request.get("mode", "full")
    managed_ids = sorted(r["id"] for r in managed_rows)
    if mode not in {"full", "residual"} or managed_ids and (mode != "residual" or sorted(request.get("managed_support_ids", [])) != managed_ids):
        raise ValueError("Mixed managed/external control support requires explicit residual mode and exact managed_support_ids.")
    if not managed_ids and request.get("managed_support_ids"):
        raise ValueError("External support names absent or settled managed balances.")
    if total + managed != financial:
        raise ValueError(f"External support does not reconcile: ledger {financial:.2f}, managed {managed:.2f}, external {total:.2f}, difference {total + managed - financial:.2f} USD.")
    previous_ids = sorted(r["id"] for r in previous)
    if request.get("supersedes") and request["supersedes"] not in previous_ids:
        raise ValueError("External support supersedes a stale or unrelated snapshot.")
    request.update(operation_id=operation_id, as_of=when.isoformat(), evidence_ids=sorted(ids), mode=mode, managed_support_ids=managed_ids)
    identity = "external-support:" + operation_id
    record = {"id": identity, "kind": "external_support_snapshot", "account": account, "as_of": when.isoformat(),
              "balance": f"{total:.2f}", "currency": "USD", "details": details, "evidence_ids": sorted(ids),
              "supersedes": previous_ids, "operation_id": operation_id}
    record.update(snapshot_revision=(latest.get("snapshot_revision", 0) + 1) if latest else 1,
                  mode=mode, managed_balance=f"{managed:.2f}", managed_support_ids=managed_ids)
    return request, SupportingMutation("coverage", identity, record)


def preview_support(entity, payload):
    from .journals import _preview_operation
    request, mutation = _support_effect(entity, payload)
    return _preview_operation(entity, operation_id=request["operation_id"], kind="external_support", request=request,
        entries=[], mutations=[mutation], evidence_ids=request["evidence_ids"], affected_dates=[request["as_of"]])


def apply_support(entity, plan):
    from .journals import _replay_operation, _apply_operation
    if not isinstance(plan, Mapping) or plan.get("kind") != "external_support":
        raise ValueError("Expected an external-support preview.")
    with _entity_write_lock(entity.path):
        replay = _replay_operation(entity, plan)
        if replay:
            return replay
        fresh = preview_support(entity, plan["request"])
        if fresh["plan_hash"] != plan.get("plan_hash"):
            raise ValueError("Stale external-support preview; review a fresh snapshot.")
        _, mutation = _support_effect(entity, fresh["request"])
        return _apply_operation(entity, fresh, entries=[], mutations=[mutation])


def _local_period_authority():
    from ..preparation_runtime import is_preparation, PreparationDenied
    if is_preparation():
        raise PreparationDenied(0, "period_authority_required")


def _period_effect(entity, payload):
    from ..readiness import assess_review_readiness, local_dependency_versions, _period_dates
    from .journals import _store, _id, _day, _text
    _local_period_authority()
    entity = load_entity(entity.path)
    if not isinstance(payload, Mapping) or set(payload) - {"operation_id", "period", "action", "as_of", "reason", "readiness_package_id"}:
        raise ValueError("A local period operation requires operation_id, period, action, as_of, and reason.")
    request = deepcopy(dict(payload))
    operation_id = _id(request.get("operation_id"))
    start, end = _period_dates(request.get("period"))
    when = _day(request.get("as_of"), "as_of")
    reason = _text(request.get("reason"), "reason")
    action = request.get("action")
    store = _store(entity)
    existing = load_record(store, "periods", request["period"])
    if action == "close":
        assessment = assess_review_readiness(entity.path, request["period"], local_dependency_versions(entity.path), as_of=when)
        if assessment.state != "ready_for_review":
            raise ValueError("Period cannot close while readiness has unresolved blockers.")
        if request.get("readiness_package_id") != assessment.package_id:
            raise ValueError("Period readiness changed; review the current exact readiness package before closing.")
        if existing and existing.get("status") == "closed":
            raise ValueError("Period is already closed; replay its original close receipt.")
        status = "closed"
    elif action == "reopen":
        if not existing or existing.get("status") != "closed":
            raise ValueError("Only a locally closed period can be reopened.")
        config = entity.entity_config
        if request["period"] in config.get("closed_periods", []) or config.get("closed_through", "") >= start.isoformat():
            raise ValueError("Host-owned period locks require the host's existing reopen authority.")
        status = "open"
    else:
        raise ValueError("Period action must be close or reopen.")
    request.update(operation_id=operation_id, as_of=when.isoformat(), reason=reason)
    record = {"id": request["period"], "status": status, "operation_id": operation_id, "as_of": when.isoformat(),
              "reason": reason, "readiness_package_id": request.get("readiness_package_id"),
              "history": [*(existing or {}).get("history", []), *([{k: v for k, v in existing.items() if k != "history"}] if existing else [])]}
    return request, SupportingMutation("periods", request["period"], record, record_revision(store, "periods", request["period"]))


def preview_period(entity, payload):
    from .journals import _preview_operation
    from ..readiness import _period_dates
    request, mutation = _period_effect(entity, payload)
    _, end = _period_dates(request["period"])
    return _preview_operation(entity, operation_id=request["operation_id"], kind="period_" + request["action"],
        request=request, entries=[], mutations=[mutation], affected_dates=[end.isoformat()])


def apply_period(entity, plan):
    from .journals import _replay_operation, _apply_operation
    _local_period_authority()
    if not isinstance(plan, Mapping) or plan.get("kind") not in {"period_close", "period_reopen"}:
        raise ValueError("Expected a local period close/reopen preview.")
    with _entity_write_lock(entity.path):
        replay = _replay_operation(entity, plan)
        if replay:
            return replay
        fresh = preview_period(entity, plan["request"])
        if fresh["plan_hash"] != plan.get("plan_hash"):
            raise ValueError("Stale period preview; review current readiness and period authority.")
        _, mutation = _period_effect(entity, fresh["request"])
        return _apply_operation(entity, fresh, entries=[], mutations=[mutation])
