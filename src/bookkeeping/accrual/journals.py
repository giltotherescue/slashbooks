"""Reviewable, source-independent journals and atomic supporting operations.

Only validated domain code constructs supporting mutations. Serialized journal
plans carry accounting requests, never arbitrary table writes or authority.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from ..entity import Entity, load_entity
from .scope import accrual_active, require_write_scope
from ..ledger.importer import _atomic_ledger_write, _entity_write_lock
from ..ledger.model import Entry, Posting, Open
from ..ledger.store import LedgerStore
from .intake import (IntakeValidationError, canonical_hash, canonical_json, evidence_hash,
                     facts_hash, normalize_amount, normalize_evidence, source_identity)
from .store import SupportingMutation, list_records, load_record, record_revision


class JournalError(ValueError):
    """An operation is invalid, stale, conflicting or requires missing facts."""

    def __init__(self, code: str, message: str, action: str = "Resolve the item and create a new preview.") -> None:
        self.code, self.action = code, action
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"status": "blocked", "errors": [{"code": self.code, "message": str(self), "action": self.action}]}


def _store(entity: Entity) -> LedgerStore:
    require_write_scope(entity)
    store = LedgerStore(entity.path / "ledger.sqlite")
    if store.schema_version() != 2:
        raise JournalError("upgrade_required", "Accrual writes require an explicit ledger schema upgrade.")
    return store


def _read_store(entity: Entity) -> LedgerStore:
    """Read supported accrual state even when the company has disabled writes."""
    entity = load_entity(entity.path)
    if not accrual_active(entity):
        raise JournalError("accrual_basis_required", "Accrual records require accrual-basis books.")
    store = LedgerStore(entity.path / "ledger.sqlite")
    if store.schema_version() != 2:
        raise JournalError("unsupported_read_schema", "Use an engine that can read this accrual ledger schema.")
    return store


def _complete_linked_source_review(entity: Entity, source_id: str, receipt: Mapping[str, Any], treatment: str) -> None:
    """Replay-safe sidecar cleanup after a canonical accounting receipt exists.

    A failed sidecar write leaves the financial operation committed. Retrying
    its original plan repeats cleanup without posting again or learning a cash
    category from an accrual settlement/link.
    """
    from .. import queue
    from ..ledger.staging import StagingStore
    # A replayed receipt carries status already_applied; the committed receipt carries applied.
    receipt = {**receipt, "status": "applied"}
    owner = load_record(_store(entity), "intents", "operation:" + receipt["id"])
    if not owner or owner.get("receipt") != dict(receipt):
        raise JournalError("missing_committed_receipt", "Linked-source cleanup requires its exact canonical receipt.")
    if not _store(entity).source_exists(source_id):
        # A second transport may be an explicit canonical alias of the same
        # evidence-backed payment. Its immutable intent points to this receipt.
        aliases = [record for record in list_records(_store(entity), "intents")
            if record.get("kind") == "item_settlement" and record.get("receipt") == dict(receipt)
            and record.get("request", {}).get("source", {}).get("source_id") == source_id]
        if not aliases:
            raise JournalError("missing_linked_source", "The accounting source has no committed financial entry or exact canonical payment alias.")
    for item in queue.list_queue_items(entity):
        ids = set(item.get("source_ids") or [item.get("source_id")])
        if source_id not in ids or item.get("status") not in queue.ACTIONABLE_QUEUE_STATUSES:
            continue
        if ids != {source_id}:
            raise JournalError("grouped_review_requires_linkage", "A grouped review proposal spans additional sources.",
                               "Resolve the entire group against committed accounting receipts before closing the period.")
        queue._save_item(entity, {**item, "status": "confirmed", "confirmed_category": treatment,
            "accrual_treatment": treatment, "accrual_operation_id": receipt["id"],
            "updated_at": queue._now_iso()})
    StagingStore(entity.staging_dir).mark_seen(source_id)
    queue._remove_from_pending_categorization(entity, source_id, strict=True)


def _day(value: Any, field: str) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise JournalError("invalid_date", f"{field} must be YYYY-MM-DD.")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise JournalError("invalid_date", f"{field} is not a calendar date.") from exc
    if result.isoformat() != value:
        raise JournalError("invalid_date", f"{field} must be YYYY-MM-DD.")
    return result


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise JournalError("missing_fact", f"{field} requires non-empty text of at most 2000 characters.")
    return value


def _id(value: Any) -> str:
    result = _text(value, "id")
    if len(result) > 160 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._/-" for ch in result):
        raise JournalError("invalid_id", "id must be a bounded plain identifier.")
    return result


def _entry_document(entry: Entry) -> dict[str, Any]:
    return {"date": entry.date.isoformat(), "description": entry.narration,
            "meta": dict(entry.meta), "postings": [{"account": p.account, "amount": str(p.amount),
            "currency": p.currency, "debit": f"{max(p.amount, Decimal(0)):.2f}",
            "credit": f"{max(-p.amount, Decimal(0)):.2f}"} for p in entry.postings],
            "totals": _journal_totals([entry])}


def _journal_totals(entries: Sequence[Entry]) -> dict[str, str]:
    debit = sum((p.amount for entry in entries for p in entry.postings if p.amount > 0), Decimal(0))
    credit = sum((-p.amount for entry in entries for p in entry.postings if p.amount < 0), Decimal(0))
    return {"debits": f"{debit:.2f}", "credits": f"{credit:.2f}",
            "difference": f"{debit - credit:.2f}", "currency": "USD"}


def _posting_amount(row: Mapping[str, Any]) -> str:
    sides = {"debit", "credit"} & set(row)
    if "amount" in row:
        if sides:
            raise JournalError("mixed_posting_amount", "Use either signed amount or debit/credit fields on a posting.")
        return normalize_amount(row["amount"])
    if not sides:
        raise JournalError("missing_amount", "Supply a signed amount, debit, or credit.")
    debit = Decimal(normalize_amount(row.get("debit", "0.00")))
    credit = Decimal(normalize_amount(row.get("credit", "0.00")))
    if debit < 0 or credit < 0 or (debit > 0 and credit > 0):
        raise JournalError("invalid_debit_credit", "Debit and credit must be nonnegative, with only one nonzero side.")
    return f"{debit - credit:.2f}"


def _configuration(entity: Entity) -> str:
    return canonical_hash({"entity": entity.entity_config, "trust_policy": entity.trust_policy})


def _check_dates(entity: Entity, store: LedgerStore, dates: Sequence[str], kind: str) -> None:
    if kind in {"evidence", "evidence_link"}:
        return
    closed = entity.entity_config.get("closed_periods", [])
    if not isinstance(closed, list):
        raise JournalError("invalid_period_state", "closed_periods must be an array.")
    through = entity.entity_config.get("closed_through")
    if through is not None:
        _day(through, "closed_through")
    locks = {row.get("id"): row.get("status") for row in list_records(store, "periods")}
    opening = load_record(store, "coverage", "opening")
    cutover = opening.get("cutover_date") if opening else None
    if cutover:
        _day(cutover, "cutover_date")
    for raw in dates:
        _day(raw, "affected date")
        if kind != "period_reopen" and (raw[:7] in closed or locks.get(raw[:7]) in {"closed", "locked"} or (through and raw <= through)):
            raise JournalError("closed_period", f"The period containing {raw} is closed.",
                               "Use existing reopen authority or choose an allowed correction date.")
        if cutover and raw < cutover and kind not in {"migration", "opening"}:
            raise JournalError("outside_coverage", f"{raw} precedes the supported cutover {cutover}.")


def _evidence(store: LedgerStore, ids: Iterable[str], pending: Sequence[SupportingMutation] = ()) -> list[dict[str, Any]]:
    if not isinstance(ids, (list, tuple)) or len(ids) > 1000 or any(not isinstance(i, str) for i in ids):
        raise JournalError("invalid_evidence", "`evidence_ids` must be a bounded array of stored supporting document IDs.")
    if len(set(ids)) != len(ids):
        raise JournalError("invalid_evidence", "Supporting document IDs must be unique.")
    result = []
    for record_id in sorted(ids):
        record = load_record(store, "evidence", record_id)
        if record is None:
            record = next((m.payload for m in pending if m.table == "evidence" and m.record_id == record_id), None)
        if not record or record.get("id") != record_id:
            raise JournalError("missing_evidence", f"Stored supporting document {record_id} is missing.")
        envelope = normalize_evidence(record["envelope"])
        if (canonical_hash(envelope) != record_id or source_identity(envelope) != record.get("source_identity")
                or evidence_hash(envelope) != record.get("evidence_hash") or facts_hash(envelope) != record.get("facts_hash")):
            raise JournalError("evidence_changed", f"Stored supporting document {record_id} no longer matches its content hash.")
        result.append(record)
    return result


def _authority(entity: Entity, dates: Sequence[str], records: Sequence[dict[str, Any]], entries: Sequence[Entry]) -> dict[str, Any]:
    from ..preparation_runtime import authorize_accrual_operation
    return authorize_accrual_operation(entity, dates, records, entries)


def _check_entries(store: LedgerStore, entries: Sequence[Entry], opens: Sequence[Open]) -> None:
    catalog = {o.account: o for o in [*store.load_opens(), *opens]}
    source_ids = set()
    for entry in entries:
        if not entry.source_id or entry.source_id in source_ids or store.source_exists(entry.source_id):
            raise JournalError("identity_conflict", "Every new financial entry requires a fresh unique source identity.")
        source_ids.add(entry.source_id)
        total = 0
        for posting in entry.postings:
            account = catalog.get(posting.account)
            if account is None:
                raise JournalError("unmapped_account", f"Posting account {posting.account} is not in the chart.")
            if entry.date < account.date:
                raise JournalError("account_not_open", f"Account {posting.account} is not open on {entry.date}.")
            if posting.currency != "USD" or (account.currencies and "USD" not in account.currencies):
                raise JournalError("unsupported_currency", "Every posting and account must support USD.")
            total += int(Decimal(normalize_amount(str(posting.amount))) * 100)
        if total != 0:
            raise JournalError("unbalanced", "Financial entries must balance exactly to the cent.")


def _preview_operation(entity: Entity, *, operation_id: str, kind: str, request: Mapping[str, Any],
                       entries: Sequence[Entry], mutations: Sequence[SupportingMutation],
                       evidence_ids: Sequence[str] = (), affected_dates: Sequence[str] = (),
                       warnings: Sequence[dict[str, Any]] = (), opens: Sequence[Open] = (),
                       details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Internal domain adapter: validate first, then bind typed effects to state.

    Domain apply must regenerate this plan from its validated request, compare
    plan_hash, and supply regenerated entries/mutations to _apply_operation.
    No CLI accepts arbitrary supporting mutations.
    """
    entity = load_entity(entity.path)
    store = _store(entity)
    operation_id = _id(operation_id)
    _check_entries(store, entries, opens)
    dates = sorted(set(affected_dates) | {entry.date.isoformat() for entry in entries})
    _check_dates(entity, store, dates, kind)
    records = _evidence(store, evidence_ids, mutations)
    authority = _authority(entity, dates, records, entries)
    body = {"schema_version": 1, "kind": kind, "id": operation_id, "request": dict(request),
            "ledger_digest": store.content_digest(), "configuration_hash": _configuration(entity),
            "authority": authority, "affected_dates": dates,
            "evidence": [{"id": r["id"], "source_identity": r["source_identity"],
                          "facts_hash": r["facts_hash"], "evidence_hash": r["evidence_hash"]} for r in records],
            "entries": [_entry_document(entry) for entry in entries],
            "opens": [{"account": o.account, "date": o.date.isoformat(), "currencies": list(o.currencies)} for o in opens],
            "supporting_hash": canonical_hash([asdict(m) for m in mutations]),
            "warnings": list(warnings), "details": {**dict(details or {}), "journal_totals": _journal_totals(entries)}}
    body["request_hash"] = canonical_hash({"kind": kind, "request": dict(request)})
    body["plan_hash"] = canonical_hash(body)
    return body


def _existing_receipt(entity: Entity, operation_id: str, request_hash: str) -> dict[str, Any] | None:
    record = load_record(_store(entity), "intents", "operation:" + operation_id)
    if record is None:
        return None
    if record.get("request_hash") != request_hash:
        raise JournalError("identity_conflict", f"Operation ID {operation_id} already has different committed facts.",
                           f"Inspect it with `books accrual receipt --id {operation_id}`; "
                           "use the committed facts, or make an explicit correction with a new operation_id.")
    return record["receipt"]


def _replay_operation(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return an exact committed receipt before rebuilding changed domain state."""
    if not isinstance(plan, Mapping) or not isinstance(plan.get("request"), Mapping):
        raise JournalError("invalid_plan", "Expected a versioned accounting preview.")
    request_hash = canonical_hash({"kind": plan["kind"], "request": plan["request"]})
    if request_hash != plan.get("request_hash"):
        raise JournalError("plan_changed", "The preview request has been changed.")
    bound_fields = {"schema_version", "kind", "id", "request", "ledger_digest", "configuration_hash",
                    "authority", "affected_dates", "evidence", "entries", "opens", "supporting_hash", "warnings", "details", "request_hash"}
    if not bound_fields <= set(plan) or canonical_hash({key: plan[key] for key in bound_fields}) != plan.get("plan_hash"):
        raise JournalError("plan_changed", "The accounting preview has been changed.")
    receipt = _existing_receipt(entity, _id(plan["id"]), request_hash)
    if receipt is not None:
        # The request hash already matches the committed operation. A fresh preview of the same request
        # has a newer ledger digest and plan_hash; it still replays, because a replay writes nothing.
        records = _evidence(_store(entity), [r["id"] for r in plan["evidence"]])
        _authority(entity, plan["affected_dates"], records, [])
        # An exact repeat is a no-op: return the original receipt, marked as a replay.
        return {**receipt, "status": "already_applied"}
    return receipt


def _apply_operation(entity: Entity, plan: Mapping[str, Any], *, entries: Sequence[Entry],
                     mutations: Sequence[SupportingMutation], opens: Sequence[Open] = (),
                     replay_receipt: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Publish regenerated domain effects and an immutable receipt atomically."""
    entity = load_entity(entity.path)
    store = _store(entity)
    operation_id = _id(plan["id"])
    if list(plan["entries"]) != [_entry_document(e) for e in entries] or plan["supporting_hash"] != canonical_hash([asdict(m) for m in mutations]):
        raise JournalError("plan_changed", "The regenerated accounting effects differ from the preview.")
    if plan.get("opens", []) != [{"account": o.account, "date": o.date.isoformat(), "currencies": list(o.currencies)} for o in opens]:
        raise JournalError("plan_changed", "Account opens differ from the preview.")
    with _entity_write_lock(entity.path):
        receipt = _existing_receipt(entity, operation_id, plan["request_hash"])
        if receipt is not None:
            return {**receipt, "status": "already_applied"}
        current = load_entity(entity.path)
        if _configuration(current) != plan["configuration_hash"]:
            raise JournalError("stale_preview", "Company configuration changed after preview.")
        _check_dates(current, store, plan["affected_dates"], plan["kind"])
        records = _evidence(store, [row["id"] for row in plan["evidence"]], mutations)
        if _authority(current, plan["affected_dates"], records, entries) != plan["authority"]:
            raise JournalError("authority_changed", "Current trusted authority differs from the preview.")
        receipt = {"status": "applied", "id": operation_id, "kind": plan["kind"],
                   "request_hash": plan["request_hash"], "plan_hash": plan["plan_hash"],
                   "entry_source_ids": [e.source_id for e in entries], "authority": plan["authority"],
                   "evidence_ids": [row["id"] for row in plan["evidence"]], "warnings": plan["warnings"]}
        true_up = (plan.get("details") or {}).get("true_up")
        if true_up:
            receipt["details"] = {"true_up": dict(true_up)}
        if replay_receipt is not None:
            if entries or mutations or opens:
                raise JournalError("invalid_replay", "An alias receipt cannot add financial or supporting effects.")
            receipt = dict(replay_receipt)
        intent = {"id": "operation:" + operation_id, "kind": plan["kind"], "status": "applied",
                  "request_hash": plan["request_hash"], "plan_hash": plan["plan_hash"],
                  "request": plan["request"], "plan": dict(plan), "receipt": receipt}
        from ..ledger.importer import _atomic_ledger_write_unlocked
        _atomic_ledger_write_unlocked(current, list(opens), list(entries), operation_id, None,
            f"Accrual {plan['kind']}: {operation_id}",
            supporting_mutations=[*mutations, SupportingMutation("intents", intent["id"], intent)],
            expected_digest=plan["ledger_digest"])
        from ..preparation_runtime import advance_accrual_batch
        advance_accrual_batch(current, plan["ledger_digest"])
        return receipt


def _check_intake_conflict(store: LedgerStore, incoming: Mapping[str, Any]) -> None:
    """Refuse a new fact set for a source identity and revision that is already stored."""
    from .store import IntakeConflict
    identity, facts = source_identity(incoming), facts_hash(incoming)
    for record in list_records(store, "evidence"):
        if record.get("source_identity") == identity and record.get("facts_hash") != facts:
            stored = record["envelope"]
            if stored["source"] == incoming["source"]:
                view = lambda e: {k: e.get(k) for k in ("source", "dates", "amounts", "service_period", "observed_facts", "settlement_links") if k in e}
                raise IntakeConflict(view(stored), view(incoming), record["id"])


def intake_evidence(entity: Entity, envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Persist immutable supporting documents, retaining every distinct transport provenance."""
    normalized = normalize_evidence(envelope)
    record_id = canonical_hash(normalized)
    store = _store(entity)
    existing = load_record(store, "evidence", record_id)
    if existing:
        return {key: existing[key] for key in ("id", "source_identity", "facts_hash", "evidence_hash")}
    _check_intake_conflict(store, normalized)
    record = {"id": record_id, "envelope": normalized, "source_identity": source_identity(normalized),
              "facts_hash": facts_hash(normalized), "evidence_hash": evidence_hash(normalized)}
    mutation = SupportingMutation("evidence", record_id, record)
    dates = list(normalized.get("dates", {}).values())
    # Evidence may be undated. It has no financial effect, and is not approval.
    plan = _preview_operation(entity, operation_id="evidence:" + record_id, kind="evidence", request=record,
                              entries=[], mutations=[mutation], evidence_ids=[record_id], affected_dates=dates)
    _apply_operation(entity, plan, entries=[], mutations=[mutation])
    return {key: record[key] for key in ("id", "source_identity", "facts_hash", "evidence_hash")}


def _link_effect(entity: Entity, request: Mapping[str, Any]) -> tuple[dict[str, Any], list[SupportingMutation], list[str]]:
    if not isinstance(request, Mapping) or set(request) - {"operation_id", "target_operation_id", "evidence_ids", "reason"}:
        raise JournalError("invalid_request", "Supporting document links require explicit identities, a stored supporting document and a reason.")
    operation_id, target_id = _id(request.get("operation_id")), _id(request.get("target_operation_id"))
    reason = _text(request.get("reason"), "reason")
    store = _store(entity)
    target = load_record(store, "intents", "operation:" + target_id)
    if not target or not target.get("receipt", {}).get("entry_source_ids"):
        raise JournalError("missing_financial_origin", "The target must be a committed financial operation.")
    records = _evidence(store, request.get("evidence_ids", []))
    if not records:
        raise JournalError("evidence_required", "Supply a stored supporting document for the explicit duplicate link.")
    normalized = {"operation_id": operation_id, "target_operation_id": target_id,
                  "evidence_ids": [r["id"] for r in records], "reason": reason}
    mutations, seen = [], {}
    target_owner = target.get("receipt", {}).get("id", target_id)
    for record in records:
        identity = record["source_identity"]
        if identity in seen:
            if seen[identity] != record["facts_hash"]:
                raise JournalError("source_conflict", "Alias supporting documents contain conflicting versions of one source.")
            continue
        seen[identity] = record["facts_hash"]
        claim_id = "source:" + identity
        prior = load_record(store, "intents", claim_id)
        if prior:
            if prior.get("operation_id") not in {target_id, target_owner} or prior.get("facts_hash") != record["facts_hash"]:
                raise JournalError("source_already_owned", "This source is already owned by another financial operation or revision.",
                                   "Inspect and correct its current owner; a supporting document link cannot reassign it.")
            continue
        mutations.append(SupportingMutation("intents", claim_id, {"id": claim_id, "kind": "source_claim",
            "source_identity": identity, "facts_hash": record["facts_hash"],
            "financial_hash": canonical_hash({"explicit_alias_target": target_id}),
            "operation_id": target_id, "alias_operation_id": operation_id, "reason": reason}))
    dates = target.get("plan", {}).get("affected_dates", [])
    if not dates:
        owned = set(target["receipt"]["entry_source_ids"])
        dates = sorted({e.date.isoformat() for e in store.load_entries() if e.source_id in owned})
    return normalized, mutations, dates


def preview_evidence_link(entity: Entity, request: Mapping[str, Any]) -> dict[str, Any]:
    """Explicitly identify a supporting document for an existing effect, without a matcher."""
    normalized, mutations, dates = _link_effect(entity, request)
    prior = load_record(_store(entity), "intents", "operation:" + normalized["operation_id"])
    if prior:
        _existing_receipt(entity, normalized["operation_id"], canonical_hash({"kind": "evidence_link", "request": normalized}))
        if isinstance(prior.get("plan"), dict):
            return prior["plan"]
    return _preview_operation(entity, operation_id=normalized["operation_id"], kind="evidence_link",
        request=normalized, entries=[], mutations=mutations, evidence_ids=normalized["evidence_ids"], affected_dates=dates)


def apply_evidence_link(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or plan.get("kind") != "evidence_link":
        raise JournalError("invalid_plan", "Expected an explicit `evidence-link` preview.")
    receipt = _replay_operation(entity, plan)
    if receipt is not None:
        return receipt
    current = preview_evidence_link(entity, plan["request"])
    if current["plan_hash"] != plan.get("plan_hash"):
        raise JournalError("stale_preview", "The `evidence-link` preview changed.")
    _, mutations, _ = _link_effect(entity, current["request"])
    return _apply_operation(entity, current, entries=[], mutations=mutations)


def _journal(entity: Entity, request: Mapping[str, Any]) -> tuple[dict[str, Any], Entry]:
    if not isinstance(request, Mapping):
        raise JournalError("invalid_request", "Journal request must be an object.")
    allowed = {"id", "date", "description", "postings", "evidence_ids", "estimate_basis", "reversal_date", "origin"}
    if set(request) - allowed:
        raise JournalError("unknown_field", "Journal request contains unsupported fields or authority claims: "
                           + ", ".join(sorted(map(str, set(request) - allowed))) + ".",
                           "Remove the listed fields; accepted journal fields are " + ", ".join(sorted(allowed)) + ". See `books accrual schema`.")
    normalized = dict(request)
    normalized["id"] = _id(request.get("id"))
    when = _day(request.get("date"), "date")
    description = _text(request.get("description"), "description")
    records = _evidence(_store(entity), request.get("evidence_ids", []))
    normalized["evidence_ids"] = [r["id"] for r in records]
    estimate = request.get("estimate_basis")
    if not records and not estimate:
        raise JournalError("evidence_required", "A journal requires a stored supporting document or a disclosed estimate basis.")
    if estimate is not None:
        normalized["estimate_basis"] = _text(estimate, "estimate_basis")
    raw = request.get("postings")
    if not isinstance(raw, list) or not 2 <= len(raw) <= 1000:
        raise JournalError("invalid_postings", "A journal needs between 2 and 1000 explicit postings.")
    accounts = {o.account: o for o in _store(entity).load_opens()}
    postings, serialized = [], []
    for row in raw:
        if not isinstance(row, Mapping) or set(row) - {"account", "amount", "debit", "credit", "currency"}:
            raise JournalError("invalid_posting", "A posting accepts account, signed amount or debit/credit, and USD currency.")
        account = _text(row.get("account"), "posting account")
        if account not in accounts:
            raise JournalError("unmapped_account", f"Posting account {account} is not in the chart.", "Map or add the account explicitly, then preview again.")
        if when < accounts[account].date:
            raise JournalError("account_not_open", f"Account {account} is not open on {when}.")
        if accounts[account].currencies and "USD" not in accounts[account].currencies:
            raise JournalError("unsupported_currency", f"Account {account} is not open for USD.")
        if row.get("currency", "USD") != "USD":
            raise JournalError("unsupported_currency", "Journal postings must use USD.")
        amount = _posting_amount(row)
        if Decimal(amount) == 0:
            raise JournalError("zero_posting", "Zero-value postings are not supported.")
        postings.append(Posting(account, Decimal(amount), "USD"))
        serialized.append({"account": account, "amount": amount, "currency": "USD"})
    if sum((p.amount for p in postings), Decimal(0)) != 0:
        raise JournalError("unbalanced", "Journal postings must balance exactly to zero.")
    normalized["postings"] = serialized
    if "reversal_date" in request:
        reversal_date = _day(request["reversal_date"], "reversal_date")
        if reversal_date <= when:
            raise JournalError("invalid_reversal_date", "A scheduled reversal must be later than its journal.")
    if "origin" in request:
        origin = request["origin"]
        if not isinstance(origin, Mapping) or set(origin) != {"mode", "source_id"} or origin["mode"] != "existing":
            raise JournalError("invalid_origin", "origin must explicitly link an existing posted source_id.")
        original = next((e for e in _store(entity).load_entries() if e.source_id == origin["source_id"]), None)
        if original is None:
            raise JournalError("missing_origin", "The linked payment/journal is missing.")
        # A reclassification of existing activity cannot introduce cash again.
        cash_accounts = set(entity.entity_config.get("bank_account_mappings", {}).values())
        cash_accounts |= {o.account for o in _store(entity).load_opens() if o.account.startswith(("Assets:Bank:", "Assets:Cash"))}
        if any(p.account in cash_accounts for p in postings):
            raise JournalError("duplicate_cash_effect", "An existing-origin reclassification must not repost cash.")
        normalized["origin"] = dict(origin)
    meta = [("source-id", "accrual:" + normalized["id"]), ("accrual-intent", normalized["id"]),
            ("evidence-ids", canonical_json(normalized["evidence_ids"]))]
    if estimate:
        meta.append(("estimate-basis", estimate))
    if "origin" in normalized:
        meta.append(("origin-source-id", normalized["origin"]["source_id"]))
    return normalized, Entry(when, description, postings=tuple(postings), meta=tuple(meta))


def _warnings(entity: Entity, entry: Entry, auto_reversing: bool = False) -> list[dict[str, Any]]:
    """Warn only about an account that registered items, schedules or a support snapshot back.

    A ledger-only account never blocks, and an auto-reversing estimate is its
    own supporting detail, so neither one gets a warning.
    """
    if auto_reversing:
        return []
    store = _store(entity)
    backed = {record.get("account") for table in ("items", "schedules") for record in list_records(store, table)}
    backed |= {record.get("account") for record in list_records(store, "coverage")
               if record.get("kind") in {"external_support", "external_support_snapshot"}}
    return [{"code": "control_support_required", "account": p.account,
             "message": f"{p.account} has registered items, schedules or a supporting-detail snapshot. This posting changes its balance "
                        "without one, so the account is unreconciled until the amount is registered as an item or schedule, or a "
                        "supporting-detail snapshot explains it (see `books accrual report --kind reconciliation`)."}
            for p in entry.postings if p.account in backed]


def _source_claims(store: LedgerStore, records: Sequence[dict[str, Any]], financial_hash: str,
                   operation_id: str) -> tuple[list[SupportingMutation], dict[str, Any] | None]:
    mutations = []
    previous_ids = set()
    by_source: dict[str, dict[str, Any]] = {}
    for record in records:
        earlier = by_source.get(record["source_identity"])
        if earlier:
            if earlier["facts_hash"] != record["facts_hash"]:
                raise JournalError("source_conflict", "One source object has conflicting observed facts in this request.")
            continue
        by_source[record["source_identity"]] = record
        claim_id = "source:" + record["source_identity"]
        prior = load_record(store, "intents", claim_id)
        if prior:
            if prior.get("facts_hash") != record["facts_hash"] or prior.get("financial_hash") != financial_hash:
                raise JournalError("source_conflict", "The source object already has a different financial treatment.",
                                   "Use an explicit linked correction; do not reimport it as new income or expense.")
            previous_ids.add(prior["operation_id"])
        else:
            mutations.append(SupportingMutation("intents", claim_id, {"id": claim_id, "kind": "source_claim",
                "source_identity": record["source_identity"], "facts_hash": record["facts_hash"],
                "financial_hash": financial_hash, "operation_id": operation_id}))
    if previous_ids:
        if len(previous_ids) != 1 or mutations:
            raise JournalError("ambiguous_source_overlap", "Only part of the supporting document has an existing financial effect.")
        prior = load_record(store, "intents", "operation:" + next(iter(previous_ids)))
        return [], prior["receipt"]
    return mutations, None


def _journal_effect(entity: Entity, request: Mapping[str, Any]) -> tuple[dict[str, Any], Entry, list[SupportingMutation], dict[str, Any] | None]:
    entity = load_entity(entity.path)
    normalized, entry = _journal(entity, request)
    store = _store(entity)
    records = _evidence(store, normalized["evidence_ids"])
    logical = {key: value for key, value in normalized.items() if key not in {"id", "evidence_ids"}}
    logical["evidence_facts"] = sorted({r["facts_hash"] for r in records})
    financial_hash = canonical_hash(logical)
    mutations, replay = _source_claims(store, records, financial_hash, normalized["id"])
    if "reversal_date" in normalized:
        reverse_id = "reversal:" + normalized["id"]
        mutations.append(SupportingMutation("intents", reverse_id, {"id": reverse_id, "kind": "scheduled_reversal",
            "status": "pending", "date": normalized["reversal_date"], "original_id": normalized["id"],
            "strategy": "reverse"}))
    return normalized, entry, mutations, replay


def preview_journal(entity: Entity, request: Mapping[str, Any]) -> dict[str, Any]:
    normalized, candidate = _journal(entity, request)
    request_hash = canonical_hash({"kind": "journal", "request": normalized})
    prior = load_record(_store(entity), "intents", "operation:" + normalized["id"])
    if prior:
        _existing_receipt(entity, normalized["id"], request_hash)
        if isinstance(prior.get("plan"), dict):
            _authority(entity, prior["plan"]["affected_dates"], _evidence(_store(entity), normalized["evidence_ids"]), [])
            return prior["plan"]
    normalized, entry, mutations, replay = _journal_effect(entity, request)
    plan = _preview_operation(entity, operation_id=normalized["id"], kind="journal", request=normalized,
        entries=[] if replay else [entry], mutations=[] if replay else mutations, evidence_ids=normalized["evidence_ids"],
        affected_dates=[entry.date.isoformat(), *([normalized["reversal_date"]] if "reversal_date" in normalized else [])],
        warnings=_warnings(entity, entry, "reversal_date" in normalized))
    if replay:
        plan["existing_receipt"] = replay
    return plan


def apply_journal(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or plan.get("kind") != "journal" or not isinstance(plan.get("request"), Mapping):
        raise JournalError("invalid_plan", "Expected a journal preview.")
    existing = _replay_operation(entity, plan)
    if existing is not None:
        return existing
    normalized, entry, mutations, replay = _journal_effect(entity, plan["request"])
    request_hash = canonical_hash({"kind": "journal", "request": normalized})
    existing = _existing_receipt(entity, normalized["id"], request_hash)
    if existing is not None:
        _authority(entity, [entry.date.isoformat()], _evidence(_store(entity), normalized["evidence_ids"]), [entry])
        return existing
    current = preview_journal(entity, normalized)
    if plan.get("plan_hash") != current["plan_hash"]:
        raise JournalError("stale_preview", "The journal preview changed; create and review a current preview.")
    if replay is not None:
        return _apply_operation(entity, current, entries=[], mutations=[], replay_receipt=replay)
    return _apply_operation(entity, current, entries=[entry], mutations=mutations)


def _original(entity: Entity, original_id: str) -> tuple[dict[str, Any], Entry]:
    store = _store(entity)
    intent = load_record(store, "intents", "operation:" + original_id)
    if intent and intent.get("receipt", {}).get("id") != original_id:
        raise JournalError("alias_not_financial_origin", "This is a replay alias; reverse or correct its original receipt ID.")
    source_ids = intent.get("receipt", {}).get("entry_source_ids", []) if intent else []
    entries = [e for e in store.load_entries() if e.source_id in source_ids]
    entry = entries[0] if len(entries) == 1 else None
    if not intent or entry is None:
        raise JournalError("missing_original", "Original accrual journal is missing.")
    if load_record(store, "intents", "extinguished:" + original_id):
        raise JournalError("original_not_live", "Original journal has already been reversed or corrected.")
    return intent, entry


def _correction_effect(entity: Entity, request: Mapping[str, Any], kind: str) -> tuple[dict[str, Any], list[Entry], list[SupportingMutation]]:
    allowed = {"id", "original_id", "date", "description", "replacement"}
    if not isinstance(request, Mapping) or set(request) - allowed:
        raise JournalError("invalid_request", "Expected an explicit original, dated reversal, and optional replacement.")
    operation_id, original_id = _id(request.get("id")), _id(request.get("original_id"))
    intent, original = _original(entity, original_id)
    linked_schedules = [row for row in list_records(_store(entity), "schedules")
        if row.get("origin", {}).get("source_id") == original.source_id or row.get("created_operation_id") == original_id
        or row.get("id") == dict(original.meta).get("schedule-id")]
    linked_items = [row for row in list_records(_store(entity), "items")
        if row.get("origin", {}).get("source_id") == original.source_id or row.get("created_operation_id") == original_id
        or row.get("id") == dict(original.meta).get("item-id")]
    if linked_schedules or intent.get("kind", "").startswith("schedule_"):
        raise JournalError("managed_schedule_requires_change", "This financial origin funds a recognition schedule.",
                           "Use a reviewed schedule change/refund so its remaining balance and allocations change atomically.")
    if linked_items or (intent.get("kind", "").startswith("item_") and intent["kind"] != "item_create"):
        raise JournalError("managed_item_requires_change", "This financial origin carries outstanding-item support.",
                           "Use `books accrual item adjust-preview` (credit, writeoff or true_up) or `item offset-preview` so financial and supporting balances stay consistent.")
    when = _day(request.get("date"), "date")
    if when < original.date:
        raise JournalError("reversal_precedes_origin", "A correction or reversal cannot precede its original financial entry.")
    description = _text(request.get("description"), "description")
    normalized = {"id": operation_id, "original_id": original_id, "date": when.isoformat(), "description": description}
    meta = (("source-id", "accrual:" + operation_id), ("reverses", original.source_id),
            ("evidence-ids", dict(original.meta).get("evidence-ids", "[]")))
    reversal = Entry(when, description, postings=tuple(Posting(p.account, -p.amount, p.currency) for p in original.postings), meta=meta)
    entries = [reversal]
    store = _store(entity)
    mutations = [SupportingMutation("intents", "extinguished:" + original_id,
        {"id": "extinguished:" + original_id, "kind": kind, "operation_id": operation_id, "original_id": original_id})]
    if kind == "correction":
        replacement_request = request.get("replacement")
        replacement, entry = _journal(entity, replacement_request)
        if replacement["id"] in {operation_id, original_id} or store.source_exists(entry.source_id):
            raise JournalError("identity_conflict", "Replacement requires a fresh identity.")
        if "reversal_date" in replacement:
            raise JournalError("unsupported_correction", "Schedule a new reversal after reviewing the correction.")
        entry = Entry(entry.date, entry.narration, postings=entry.postings,
                      meta=entry.meta + (("correction-of", original.source_id),))
        entries.append(entry)
        normalized["replacement"] = replacement
        child_id = "operation:" + replacement["id"]
        if load_record(store, "intents", child_id):
            raise JournalError("identity_conflict", "Replacement operation identity is already committed.")
        child_plan = _preview_operation(entity, operation_id=replacement["id"], kind="journal", request=replacement,
            entries=[entry], mutations=[], evidence_ids=replacement["evidence_ids"],
            warnings=_warnings(entity, entry))
        child_receipt = {"status": "applied", "id": replacement["id"], "kind": "journal",
            "request_hash": canonical_hash({"kind": "journal", "request": replacement}),
            "plan_hash": child_plan["plan_hash"],
            "entry_source_ids": [entry.source_id], "evidence_ids": replacement["evidence_ids"],
            "authority": {"mode": "linked_correction", "operation_id": operation_id},
            "warnings": _warnings(entity, entry)}
        mutations.append(SupportingMutation("intents", child_id, {"id": child_id, "kind": "journal",
            "status": "applied", "parent_operation_id": operation_id, "request": replacement,
            "request_hash": child_receipt["request_hash"], "plan": child_plan, "receipt": child_receipt}))
        # Preserve source ownership; new versions may replace the original only
        # through this correction, never through a second ordinary journal.
        replacement_evidence = _evidence(store, replacement["evidence_ids"])
        by_source = {}
        for record in replacement_evidence:
            earlier = by_source.get(record["source_identity"])
            if earlier:
                if earlier["facts_hash"] != record["facts_hash"]:
                    raise JournalError("source_conflict", "Replacement contains conflicting observed facts for one source object.")
                continue
            by_source[record["source_identity"]] = record
            claim_id = "source:" + record["source_identity"]
            prior = load_record(store, "intents", claim_id)
            if prior and prior.get("operation_id") != original_id:
                raise JournalError("source_conflict", "Replacement supporting document belongs to another financial effect.")
            logical = {k: v for k, v in replacement.items() if k not in {"id", "evidence_ids"}}
            logical["evidence_facts"] = sorted({r["facts_hash"] for r in replacement_evidence})
            mutations.append(SupportingMutation("intents", claim_id, {"id": claim_id, "kind": "source_claim",
                "source_identity": record["source_identity"], "facts_hash": record["facts_hash"],
                "financial_hash": canonical_hash(logical), "operation_id": replacement["id"]}, record_revision(store, "intents", claim_id)))
    elif "replacement" in request:
        raise JournalError("invalid_request", "A reversal cannot contain a replacement.")
    due = load_record(store, "intents", "reversal:" + original_id)
    if due and due.get("status") == "pending":
        mutations.append(SupportingMutation("intents", due["id"], {**due, "status": "applied" if kind == "reversal" else "superseded",
            "operation_id": operation_id}, record_revision(store, "intents", due["id"])))
    return normalized, entries, mutations


def preview_correction(entity: Entity, request: Mapping[str, Any], *, reverse_only: bool = False) -> dict[str, Any]:
    kind = "reversal" if reverse_only else "correction"
    normalized, entries, mutations = _correction_effect(entity, request, kind)
    original = load_record(_store(entity), "intents", "operation:" + normalized["original_id"])
    ids = set(original["request"].get("evidence_ids", []))
    if "replacement" in normalized:
        ids.update(normalized["replacement"]["evidence_ids"])
    return _preview_operation(entity, operation_id=normalized["id"], kind=kind, request=normalized,
        entries=entries, mutations=mutations, evidence_ids=sorted(ids),
        affected_dates=[], warnings=[warning for entry in entries[1:]
            for warning in _warnings(entity, entry)])


def apply_correction(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or plan.get("kind") not in {"correction", "reversal"}:
        raise JournalError("invalid_plan", "Expected a correction or reversal preview.")
    existing = _replay_operation(entity, plan)
    if existing is not None:
        return existing
    request_hash = canonical_hash({"kind": plan["kind"], "request": plan["request"]})
    existing = _existing_receipt(entity, plan["id"], request_hash)
    if existing is not None:
        return {**existing, "status": "already_applied"}
    current = preview_correction(entity, plan["request"], reverse_only=plan["kind"] == "reversal")
    if plan.get("plan_hash") != current["plan_hash"]:
        raise JournalError("stale_preview", "The correction preview changed.")
    _, entries, mutations = _correction_effect(entity, current["request"], plan["kind"])
    return _apply_operation(entity, current, entries=entries, mutations=mutations)


def _reversal_narration(original: Entry) -> str:
    """Human narration for a scheduled reversal: the original description, else its date. Never an ID."""
    text = " ".join(str(original.narration or "").split())[:1900]
    return "Reversal of: " + (text or "entry dated " + original.date.isoformat())


def due_reversals(entity: Entity, as_of: str, *, apply: bool = False) -> dict[str, Any]:
    """Preview or explicitly apply pending reversals due through an evidenced date."""
    with _entity_write_lock(entity.path):
        return _due_reversals_unlocked(entity, as_of, apply=apply)


def _due_reversals_unlocked(entity: Entity, as_of: str, *, apply: bool = False) -> dict[str, Any]:
    _day(as_of, "as_of")
    results, blockers = [], []
    for record in list_records(_store(entity), "intents"):
        if record.get("kind") != "scheduled_reversal" or record.get("status") != "pending" or record["date"] > as_of:
            continue
        try:
            request = {"id": "due:" + record["original_id"], "original_id": record["original_id"], "date": record["date"],
                       "description": _reversal_narration(_original(entity, record["original_id"])[1])}
            plan = preview_correction(entity, request, reverse_only=True)
            results.append(apply_correction(entity, plan) if apply else plan)
        except (JournalError, IntakeValidationError, PermissionError) as exc:
            blockers.append({"id": record["id"], "code": getattr(exc, "code", "authority_required"), "message": str(exc)})
    return {"status": "blocked" if blockers else "ok", "as_of": as_of, "results": results, "blockers": blockers}
