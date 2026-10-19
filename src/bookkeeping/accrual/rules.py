"""Saved accrual rules: what to PROPOSE for a recurring supplier, customer or kind of cost.

A saved rule is a preference, never a permission. It changes the treatment an agent
proposes. It never records anything, never confirms a queue item, and never changes
the trust ramp. A person still approves every posting, and nothing posts outside the
authority the company already granted. `match` is read-only and returns a DRAFT
request for the ordinary preview commands.

Storage (both inside the company directory, outside the ledger):

* Per supplier or customer: the optional ``accrual`` object on that counterparty's
  record in ``learned-context/counterparties.json``. ``taught`` is never touched.
* Company level: ``learned-context/accrual-rules.json``: ``{"version": 1,
  "rules": [...], "receipts": [...]}``. The receipts make a repeated apply a no-op.

Writes go only through ``set_preview/set_apply`` and ``clear_preview/clear_apply``.
"""
from __future__ import annotations

import calendar
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from ..entity import Entity, load_entity
from .scope import accrual_active, require_write_scope
from .intake import IntakeValidationError, canonical_hash, normalize_amount
from .journals import JournalError, _day
from .schedules import _account, _request

RULES_FILE = "learned-context/accrual-rules.json"
RULES_VERSION = 1
TREATMENTS = ("prepaid", "deferred", "depreciation", "bill", "invoice", "estimate")
SCHEDULE_TREATMENTS = ("prepaid", "deferred", "depreciation")
METHODS = ("actual_day", "equal_month")
SOURCES = ("person", "agent")
MAX_TERM_MONTHS = 600
MAX_RECEIPTS = 1000
_ITEM_KIND = {"bill": "payable", "invoice": "receivable", "estimate": "accrual"}
_LABEL = {"prepaid": "a prepaid expense", "deferred": "deferred revenue", "depreciation": "equipment depreciated",
          "bill": "an unpaid bill (payable)", "invoice": "a customer invoice (receivable)", "estimate": "an accrued estimate"}
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

# field -> (allowed roots, subtypes allowed on an Assets/Liabilities account, reason shown to the person)
_SPEC: dict[str, dict[str, tuple[tuple[str, ...], set[str] | None, str]]] = {
    "prepaid": {
        "account": (("Assets:",), {"prepaid", "other_asset"}, "a prepaid expense is an asset until each month uses it"),
        "recognition_account": (("Expenses:",), None, "a prepaid schedule releases the asset into expense")},
    "deferred": {
        "account": (("Liabilities:",), {"deferred_revenue", "other_liability"}, "deferred revenue is money received before it is earned (a liability)"),
        "recognition_account": (("Income:",), None, "deferred revenue is released into income")},
    "depreciation": {
        "account": (("Assets:",), {"fixed_asset", "other_asset"}, "depreciation spreads the cost of an asset"),
        "recognition_account": (("Expenses:",), None, "depreciation is an expense"),
        "contra_account": (("Assets:",), {"accumulated_depreciation", "other_asset"}, "accumulated depreciation is a contra-asset account")},
    "bill": {
        "account": (("Liabilities:",), {"payable", "other_liability"}, "an unpaid bill is a payable (a liability)"),
        "recognition_account": (("Expenses:", "Assets:"), {"prepaid", "fixed_asset", "other_asset"}, "the bill is an expense, or an asset you bought")},
    "invoice": {
        "account": (("Assets:",), {"receivable", "other_asset"}, "an unpaid customer invoice is a receivable (an asset)"),
        "recognition_account": (("Income:",), None, "an invoice records income")},
    "estimate": {
        "account": (("Liabilities:",), {"accrued_liabilities", "other_liability"}, "an accrued estimate is an accrued liability"),
        "recognition_account": (("Expenses:",), None, "an estimate records an expense")},
}

_SET_FIELDS = {"operation_id", "counterparty", "id", "match", "treatment", "term_months", "method", "accounts",
               "threshold", "note", "set_by", "set_at", "source"}
_CLEAR_FIELDS = {"operation_id", "counterparty", "id", "cleared_by", "cleared_at", "source", "reason"}
_FACT_FIELDS = {"counterparty", "amount", "account", "category", "description", "date", "service_start", "service_end",
                "source_id", "funding_account", "evidence_ids", "reference", "due_date", "estimate_basis"}
_BLANK_ENTRY = {"canonical_category": "", "confirmed_count": 0, "last_confirmed_date": "", "reset": False, "notes": ""}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entity(entity: Entity, *, write: bool) -> Entity:
    entity = load_entity(entity.path)
    if not accrual_active(entity):
        raise JournalError("accrual_basis_required", "Saved accrual rules need accrual-basis books.",
                           "Use this command on an accrual company.")
    if write:
        try:
            require_write_scope(entity)
        except ValueError as exc:
            raise JournalError("accrual_writes_off", str(exc), "Turn on accrual writes for this company first.") from exc
    return entity


def _text(value: Any, field: str, limit: int, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if (not isinstance(value, str) or (required and not value.strip()) or len(value) > limit
            or re.search(r"[\x00-\x1f\x7f]", value)):
        raise JournalError("missing_fact", f"{field} requires {'non-empty ' if required else ''}plain text of at most {limit} characters, with no control characters.")
    return " ".join(value.split())


def _stamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        raise JournalError("invalid_timestamp", f"{field} must be a UTC time such as 2026-10-16T10:00:00Z.")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise JournalError("invalid_timestamp", f"{field} is not a real time.") from exc
    return value


def _money(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise JournalError("invalid_threshold", f"{field} must be an exact decimal string such as \"1000.00\".",
                           "Give the amount as text with at most two decimal places.")
    try:
        result = normalize_amount(value, field)
    except IntakeValidationError as exc:
        raise JournalError("invalid_threshold", f"{field} must be an exact decimal string such as \"1000.00\" (at most two decimal places).",
                           "Give the amount as text with at most two decimal places.") from exc
    if Decimal(result) < 0:
        raise JournalError("invalid_threshold", f"{field} cannot be negative.")
    return result


def _key(name: Any, field: str = "counterparty") -> str:
    from ..queue import _counterparty_key
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise JournalError("missing_fact", f"{field} requires a supplier or customer name (or its key) of at most 200 characters.")
    key = _counterparty_key("", name)
    if not key:
        raise JournalError("missing_fact", f"{field} requires a supplier or customer name (or its key).")
    return key


def _subtypes(entity: Entity) -> dict[str, str]:
    from .accounts import list_accounts
    return {row["name"]: row["subtype"] for row in list_accounts(entity)["accounts"]}


def _store(entity: Entity):
    from ..ledger.store import LedgerStore
    return LedgerStore(entity.path / "ledger.sqlite")


def _slug(text: str, limit: int = 80) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:limit] or "x"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _company_path(entity: Entity) -> Path:
    return entity.path / RULES_FILE


def _load_company(entity: Entity) -> dict[str, Any]:
    path = _company_path(entity)
    if not path.exists():
        return {"version": RULES_VERSION, "rules": [], "receipts": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JournalError("rules_file_unreadable", f"{RULES_FILE} cannot be read.",
                           "Restore the file from a backup. Do not replace it by hand.") from exc
    if (not isinstance(doc, dict) or doc.get("version") != RULES_VERSION or not isinstance(doc.get("rules"), list)
            or not isinstance(doc.get("receipts", []), list)):
        raise JournalError("rules_file_unsupported", f"{RULES_FILE} has an unsupported shape or version.",
                           "Use a Slashbooks engine that supports this file, or restore it from a backup.")
    doc.setdefault("receipts", [])
    return doc


def _save_company(entity: Entity, doc: dict[str, Any]) -> None:
    path = _company_path(entity)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"version": RULES_VERSION, "rules": sorted(doc["rules"], key=lambda r: r["id"]),
           "receipts": doc.get("receipts", [])[-MAX_RECEIPTS:]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def _load_context(entity: Entity) -> dict[str, Any]:
    """The counterparty records. An unreadable file is an error here, never silently replaced."""
    path = entity.path / "learned-context" / "counterparties.json"
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JournalError("learned_context_unreadable", "learned-context/counterparties.json cannot be read.",
                           "Restore the file from a backup before saving a rule.") from exc
    if not isinstance(doc, dict):
        raise JournalError("learned_context_unreadable", "learned-context/counterparties.json is not an object.",
                           "Restore the file from a backup before saving a rule.")
    return doc


def _write_counterparty(entity: Entity, key: str, rule: dict[str, Any] | None) -> None:
    from ..queue import _save_learned_context
    context = _load_context(entity)
    entry = context.get(key)
    if not isinstance(entry, dict):
        entry = dict(_BLANK_ENTRY)
    if rule is None:
        entry.pop("accrual", None)
        if entry == _BLANK_ENTRY:
            context.pop(key, None)
            _save_learned_context(entity, context)
            return
    else:
        entry["accrual"] = rule
    context[key] = entry
    _save_learned_context(entity, context)


def _counterparty_rule(entity: Entity, key: str) -> dict[str, Any] | None:
    entry = _load_context(entity).get(key)
    rule = entry.get("accrual") if isinstance(entry, dict) else None
    return rule if isinstance(rule, dict) else None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _check_account(store: Any, subtypes: Mapping[str, str], treatment: str, field: str, value: Any) -> str:
    roots, allowed, why = _SPEC[treatment][field]
    name = _account(store, value, f"accounts.{field}", roots, why)
    if allowed is not None and name.startswith(("Assets:", "Liabilities:")):
        subtype = subtypes.get(name, "other_asset")
        if subtype not in allowed:
            raise JournalError("account_subtype_mismatch",
                               f"accounts.{field} {name} is a {subtype.replace('_', ' ')} account, which does not fit a {treatment} rule.",
                               f"Use an account with subtype {', '.join(sorted(allowed))}. `books accrual accounts --entity PATH` lists each account's subtype.")
    return name


def _rule_body(entity: Entity, request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the treatment fields shared by supplier and company rules."""
    treatment = request.get("treatment")
    if treatment not in TREATMENTS:
        raise JournalError("invalid_treatment", f"treatment must be one of {', '.join(TREATMENTS)}.",
                           "Use prepaid, deferred or depreciation for a schedule; bill, invoice or estimate for an open balance.")
    body: dict[str, Any] = {"version": 1, "treatment": treatment}
    scheduled = treatment in SCHEDULE_TREATMENTS
    if "term_months" in request:
        term = request["term_months"]
        if not scheduled:
            raise JournalError("term_not_allowed", f"term_months applies only to {', '.join(SCHEDULE_TREATMENTS)}, not {treatment}.",
                               "Remove term_months.")
        if type(term) is not int or not 1 <= term <= MAX_TERM_MONTHS:
            raise JournalError("invalid_term", f"term_months must be a whole number from 1 to {MAX_TERM_MONTHS}.")
        body["term_months"] = term
    elif treatment == "depreciation":
        raise JournalError("term_required", "A depreciation rule needs term_months, the useful life in months.",
                           "Add term_months, for example 36. Slashbooks does not choose a useful life.")
    if "method" in request:
        if not scheduled:
            raise JournalError("method_not_allowed", f"method applies only to {', '.join(SCHEDULE_TREATMENTS)}, not {treatment}.",
                               "Remove method.")
        if request["method"] not in METHODS:
            raise JournalError("invalid_method", f"method must be one of {', '.join(METHODS)}.",
                               "A saved rule uses actual_day or equal_month. Use custom installments in the request itself.")
        body["method"] = request["method"]
    if "accounts" in request:
        accounts = request["accounts"]
        allowed = {"account", "recognition_account"} | ({"contra_account"} if treatment == "depreciation" else set())
        if not isinstance(accounts, Mapping) or set(accounts) - allowed:
            extra = sorted(map(str, set(accounts) - allowed)) if isinstance(accounts, Mapping) else []
            raise JournalError("invalid_accounts", "accounts must be an object with " + ", ".join(sorted(allowed))
                               + (f"; unknown or not allowed for {treatment}: {', '.join(extra)}." if extra else "."))
        missing = [name for name in sorted(allowed) if name not in accounts]
        if missing:
            raise JournalError("invalid_accounts", f"accounts is missing {', '.join(missing)}.",
                               "Name every account the treatment needs, or leave accounts out and let the proposal choose them.")
        store, subtypes = _store(entity), _subtypes(entity)
        resolved = {name: _check_account(store, subtypes, treatment, name, accounts[name]) for name in sorted(allowed)}
        if len(set(resolved.values())) != len(resolved):
            raise JournalError("invalid_accounts", "accounts must name different accounts.")
        body["accounts"] = resolved
    if "threshold" in request:
        body["threshold"] = _money(request["threshold"], "threshold")
    body["note"] = _text(request.get("note"), "note", 500)
    body["set_by"] = _text(request.get("set_by"), "set_by", 200)
    if request.get("source") not in SOURCES:
        raise JournalError("invalid_source", "source must be person or agent.",
                           "Use person when a person decided this treatment. Use agent when an agent proposes it.")
    body["source"] = request["source"]
    return body


def _company_match(entity: Entity, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {"accounts", "keywords"}:
        raise JournalError("invalid_match", "match must be an object with accounts and/or keywords.",
                           "Example: {\"accounts\": [\"Expenses:Software\"]}.")
    match: dict[str, Any] = {}
    if "accounts" in value:
        names = value["accounts"]
        if not isinstance(names, list) or not 1 <= len(names) <= 20 or any(not isinstance(n, str) for n in names):
            raise JournalError("invalid_match", "match.accounts must list 1 to 20 account names.")
        subtypes = _subtypes(entity)
        clean = sorted(set(n.strip() for n in names))
        for name in clean:
            if not name.startswith(("Expenses:", "Income:", "Assets:")) or subtypes.get(name) in {"bank", "cash", "receivable"}:
                raise JournalError("invalid_match", f"match.accounts {name} must be an expense, income or non-cash asset account.",
                                   "Name the account a purchase or sale is categorized to, such as Expenses:Software.")
            if name not in subtypes and not any(other.startswith(name + ":") for other in subtypes):
                raise JournalError("unmapped_account", f"match.accounts {name} is not an account, and no account sits under it.",
                                   "Use an account from `books accrual accounts --entity PATH`, or a parent of several accounts.")
        match["accounts"] = clean
    if "keywords" in value:
        words = value["keywords"]
        if not isinstance(words, list) or not 1 <= len(words) <= 10 or any(not isinstance(w, str) or not w.strip() or len(w) > 60 for w in words):
            raise JournalError("invalid_match", "match.keywords must list 1 to 10 words or phrases of at most 60 characters.")
        match["keywords"] = sorted(set(" ".join(w.lower().split()) for w in words))
    if not match:
        raise JournalError("invalid_match", "match needs at least accounts or keywords.",
                           "A threshold alone would match every purchase. Add match.accounts or match.keywords.")
    return match


def _build_set(entity: Entity, payload: Mapping[str, Any], set_at: str | None) -> dict[str, Any]:
    """Validate a set request and describe its exact effect. Writes nothing."""
    request = _request(payload, _SET_FIELDS)
    operation_id = _operation_id(request)
    scope_counterparty, scope_company = "counterparty" in request, "match" in request
    if scope_counterparty == scope_company:
        raise JournalError("invalid_scope", "Give exactly one of counterparty (a supplier or customer rule) or match (a company rule).",
                           "Use counterparty for one supplier or customer. Use match for a rule about a kind of cost.")
    if scope_counterparty and "id" in request:
        raise JournalError("id_not_allowed", "id names a company rule; a supplier or customer rule is identified by its counterparty.",
                           "Remove id.")
    if "set_at" in request:
        _stamp(request["set_at"], "set_at")
    resolved_at = request.get("set_at") or set_at or _now()
    body = _rule_body(entity, request)
    body["set_at"] = resolved_at
    warnings: list[dict[str, str]] = []
    if scope_counterparty:
        key = _key(request["counterparty"])
        target = {"scope": "counterparty", "counterparty": key}
        before = _counterparty_rule(entity, key)
        after = body
    else:
        match = _company_match(entity, request["match"])
        rule_id = request.get("id")
        if rule_id is None:
            rule_id = "rule-" + canonical_hash({"match": match, "threshold": body.get("threshold")})[:10]
        elif not isinstance(rule_id, str) or not _RULE_ID_RE.fullmatch(rule_id):
            raise JournalError("invalid_id", "id must be 1 to 80 letters, digits, dots, dashes or underscores, and start with a letter or digit.")
        target = {"scope": "company", "id": rule_id}
        company = _load_company(entity)
        before = next((r for r in company["rules"] if r.get("id") == rule_id), None)
        after = {"id": rule_id, "match": match, **body}
        twin = [r["id"] for r in company["rules"] if r.get("id") != rule_id and r.get("match") == match and r.get("threshold") == body.get("threshold")]
        if twin:
            warnings.append({"code": "duplicate_match", "message": "Rule " + ", ".join(sorted(twin))
                             + " has the same match and threshold. The rule with the smaller id wins."})
    if before is not None and before.get("source") == "person" and body["source"] == "agent":
        raise JournalError("person_rule_protected", "A person set this rule. An agent cannot replace it.",
                           "Tell the person what you would change and ask them to confirm it with source person.")
    if before is not None and _same_decision(before, after):
        action, after = "unchanged", before
    else:
        action = "replace" if before is not None else "create"
    if body["source"] == "agent":
        warnings.append({"code": "agent_rule", "message": "An agent proposes this rule. Tell the person, who can confirm it by saving it again with source person."})
    return {"operation_id": operation_id, "request": request, "target": target, "action": action, "before": before,
            "after": after, "resolved": {"set_at": resolved_at}, "warnings": warnings}


def _same_decision(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    ignore = {"set_at"}
    return {k: v for k, v in before.items() if k not in ignore} == {k: v for k, v in after.items() if k not in ignore}


def _operation_id(request: Mapping[str, Any]) -> str:
    value = request.get("operation_id")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9:._/-]{1,160}", value):
        raise JournalError("invalid_id", "operation_id must be a plain identifier of at most 160 characters.")
    return value


def _build_clear(entity: Entity, payload: Mapping[str, Any], cleared_at: str | None = None) -> dict[str, Any]:
    request = _request(payload, _CLEAR_FIELDS)
    operation_id = _operation_id(request)
    if ("counterparty" in request) == ("id" in request):
        raise JournalError("invalid_scope", "Give exactly one of counterparty or id (a company rule).")
    request["cleared_by"] = _text(request.get("cleared_by"), "cleared_by", 200)
    if request.get("source") not in SOURCES:
        raise JournalError("invalid_source", "source must be person or agent.")
    if "reason" in request:
        request["reason"] = _text(request["reason"], "reason", 500)
    if "cleared_at" in request:
        _stamp(request["cleared_at"], "cleared_at")
    if "counterparty" in request:
        key = _key(request["counterparty"])
        target, before = {"scope": "counterparty", "counterparty": key}, _counterparty_rule(entity, key)
        label = f"the rule for {key}"
    else:
        rule_id = request["id"]
        if not isinstance(rule_id, str) or not _RULE_ID_RE.fullmatch(rule_id):
            raise JournalError("invalid_id", "id must name a company rule.")
        target = {"scope": "company", "id": rule_id}
        before = next((r for r in _load_company(entity)["rules"] if r.get("id") == rule_id), None)
        label = f"company rule {rule_id}"
    if before is None:
        raise JournalError("rule_not_found", f"There is no saved rule to clear: {label}.", "List the saved rules with `books accrual rules list`.")
    if before.get("source") == "person" and request["source"] == "agent":
        raise JournalError("person_rule_protected", "A person set this rule. An agent cannot clear it.",
                           "Tell the person and ask them to clear it with source person.")
    return {"operation_id": operation_id, "request": request, "target": target, "before": before, "after": None,
            "resolved": {"cleared_at": request.get("cleared_at") or cleared_at or _now()}, "warnings": []}


# ---------------------------------------------------------------------------
# Preview / apply (receipts, plan hash, replay)
# ---------------------------------------------------------------------------

def _plan(kind: str, built: Mapping[str, Any]) -> dict[str, Any]:
    body = {"schema_version": 1, "kind": kind, "id": built["operation_id"], "request": dict(built["request"]),
            "target": built["target"], "before": built["before"], "after": built["after"], "resolved": built["resolved"],
            "warnings": built["warnings"], "effect": "proposals_only",
            "summary": _summary(kind, built)}
    if "action" in built:
        body["action"] = built["action"]
    body["request_hash"] = canonical_hash({"kind": kind, "request": body["request"]})
    body["state_hash"] = canonical_hash(built["before"])
    body["plan_hash"] = canonical_hash(body)
    return body


def _summary(kind: str, built: Mapping[str, Any]) -> str:
    target = built["target"]
    who = f"supplier or customer {target['counterparty']}" if target["scope"] == "counterparty" else f"company rule {target['id']}"
    if kind == "accrual_rule_clear":
        return f"Remove the saved accrual rule for {who}. Nothing is recorded or changed in the books."
    rule = built["after"]
    return (f"Save an accrual rule for {who}: {describe(rule)}. Future proposals will suggest this treatment; "
            "a person still approves, and nothing is recorded by this change.")


def describe(rule: Mapping[str, Any]) -> str:
    text = _LABEL.get(rule.get("treatment"), str(rule.get("treatment")))
    if rule.get("term_months"):
        text += f" over {rule['term_months']} months"
    if rule.get("threshold"):
        text += f", for amounts of {rule['threshold']} or more"
    return text


def set_preview(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    entity = _entity(entity, write=True)
    return _plan("accrual_rule_set", _build_set(entity, payload, None))


def clear_preview(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    entity = _entity(entity, write=True)
    return _plan("accrual_rule_clear", _build_clear(entity, payload))


def _receipt_for(doc: Mapping[str, Any], operation_id: str) -> dict[str, Any] | None:
    return next((r for r in doc["receipts"] if r.get("id") == operation_id), None)


def _check_plan(plan: Any, kind: str) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or plan.get("kind") != kind or not isinstance(plan.get("request"), Mapping):
        raise JournalError("invalid_plan", "Expected the unchanged preview from the matching preview command.")
    if canonical_hash({"kind": kind, "request": plan["request"]}) != plan.get("request_hash"):
        raise JournalError("plan_changed", "The preview request has been changed.")
    if canonical_hash({k: v for k, v in plan.items() if k != "plan_hash"}) != plan.get("plan_hash"):
        raise JournalError("plan_changed", "The preview has been changed.", "Create a new preview.")
    return dict(plan)


def _apply(entity: Entity, plan: Any, kind: str) -> dict[str, Any]:
    entity = _entity(entity, write=True)
    plan = _check_plan(plan, kind)
    from ..queue import _queue_mutation
    with _queue_mutation(entity):
        doc = _load_company(entity)
        existing = _receipt_for(doc, str(plan.get("id")))
        if existing is not None:
            if existing["request_hash"] != plan["request_hash"]:
                raise JournalError("identity_conflict", f"Operation ID {plan['id']} already has different committed facts.",
                                   f"Inspect it with `books accrual receipt --id {plan['id']}`; use a new operation_id for a new change.")
            return {**existing["receipt"], "status": "already_applied"}
        resolved = plan.get("resolved") if isinstance(plan.get("resolved"), Mapping) else {}
        try:
            if kind == "accrual_rule_set":
                built = _build_set(entity, plan["request"], resolved.get("set_at"))
            else:
                built = _build_clear(entity, plan["request"], resolved.get("cleared_at"))
        except JournalError as exc:
            if exc.code != "rule_not_found" or plan.get("after") is not None or plan.get("before") is None:
                raise
            built = None  # a clear that already took effect before its receipt was written
        if built is None:
            finishing = _current(entity, plan["target"], doc) is None
        else:
            rebuilt = _plan(kind, built)
            finishing = built["after"] == _current(entity, built["target"], doc) and plan.get("after") == built["after"]
            if rebuilt["plan_hash"] != plan.get("plan_hash") and not finishing:
                if rebuilt["state_hash"] != plan.get("state_hash"):
                    raise JournalError("stale_preview", "The saved rules changed after this preview.", "Preview the change again.")
                raise JournalError("plan_changed", "The preview does not match its request.", "Create a new preview.")
        if not finishing:
            _write_target(entity, built["target"], built["after"], doc)
            doc = _load_company(entity)
        receipt = {"status": "applied", "id": plan["id"], "kind": kind, "request_hash": plan["request_hash"],
                   "plan_hash": plan["plan_hash"], "target": plan["target"], "action": plan.get("action", "clear"),
                   "before": plan["before"], "after": plan["after"], "effect": "proposals_only"}
        doc["receipts"].append({"id": plan["id"], "kind": kind, "request_hash": plan["request_hash"],
                                "plan_hash": plan["plan_hash"], "receipt": receipt})
        _save_company(entity, doc)
        return receipt


def _current(entity: Entity, target: Mapping[str, Any], doc: Mapping[str, Any]) -> Any:
    if target["scope"] == "counterparty":
        return _counterparty_rule(entity, target["counterparty"])
    return next((r for r in doc["rules"] if r.get("id") == target["id"]), None)


def _write_target(entity: Entity, target: Mapping[str, Any], after: dict[str, Any] | None, doc: dict[str, Any]) -> None:
    if target["scope"] == "counterparty":
        _write_counterparty(entity, target["counterparty"], after)
        return
    rules = [r for r in doc["rules"] if r.get("id") != target["id"]]
    if after is not None:
        rules.append(after)
    doc["rules"] = rules
    _save_company(entity, doc)


def set_apply(entity: Entity, plan: Any) -> dict[str, Any]:
    return _apply(entity, plan, "accrual_rule_set")


def clear_apply(entity: Entity, plan: Any) -> dict[str, Any]:
    return _apply(entity, plan, "accrual_rule_clear")


def stored_receipt(entity: Entity, operation_id: str) -> dict[str, Any] | None:
    """A saved rule receipt, for `books accrual receipt --id`. Read-only."""
    try:
        record = _receipt_for(_load_company(entity), operation_id)
    except JournalError:
        return None
    return None if record is None else {"status": "applied", "id": operation_id, "kind": record["kind"],
                                        "request_hash": record["request_hash"], "plan_hash": record["plan_hash"],
                                        "receipt": record["receipt"]}


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def list_rules(entity: Entity) -> dict[str, Any]:
    from ..queue import validate_accrual_hint
    entity = _entity(entity, write=False)
    counterparty, skipped = [], []
    for key, entry in sorted(_load_context(entity).items()):
        rule = entry.get("accrual") if isinstance(entry, dict) else None
        if rule is None:
            continue
        problems = validate_accrual_hint(rule)
        if problems:
            skipped.append({"scope": "counterparty", "counterparty": key, "problems": problems})
        else:
            counterparty.append({"scope": "counterparty", "counterparty": key, **rule, "summary": describe(rule)})
    company = [{"scope": "company", **rule, "summary": describe(rule)} for rule in sorted(_load_company(entity)["rules"], key=lambda r: r["id"])]
    return {"schema_version": 1, "kind": "accrual_rules", "effect": "proposals_only",
            "count": len(counterparty) + len(company), "counterparty_rules": counterparty, "company_rules": company,
            "skipped": skipped}


def render_rules(result: Mapping[str, Any]) -> str:
    lines = [f"Saved accrual rules ({result['count']}). A rule only changes what is proposed; a person still approves."]
    for row in result["counterparty_rules"]:
        lines.append(f"  Supplier or customer {row['counterparty']}: {row['summary']} (set by {row['set_by']}, {row['source']}, {row['set_at']}). {row['note']}")
    for row in result["company_rules"]:
        match = row["match"]
        scope = "; ".join(filter(None, [("accounts " + ", ".join(match["accounts"])) if "accounts" in match else "",
                                        ("keywords " + ", ".join(match["keywords"])) if "keywords" in match else ""]))
        lines.append(f"  Company rule {row['id']} ({scope}): {row['summary']} (set by {row['set_by']}, {row['source']}, {row['set_at']}). {row['note']}")
    for row in result["skipped"]:
        lines.append(f"  Skipped {row['counterparty']}: " + "; ".join(row["problems"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Match
# ---------------------------------------------------------------------------

def _facts(payload: Mapping[str, Any]) -> dict[str, Any]:
    request = _request(payload, _FACT_FIELDS)
    facts: dict[str, Any] = {}
    if "amount" not in request:
        raise JournalError("missing_fact", "amount is required.", "Give the size of the bill, invoice or payment as a decimal string.")
    try:
        facts["amount"] = f"{abs(Decimal(normalize_amount(request['amount'], 'amount'))):.2f}"
    except IntakeValidationError as exc:
        raise JournalError("invalid_amount", "amount must be an exact decimal string such as \"1200.00\".") from exc
    for name, limit in (("counterparty", 200), ("description", 500), ("reference", 500), ("estimate_basis", 2000),
                        ("source_id", 160), ("funding_account", 200)):
        if request.get(name) is not None:
            facts[name] = _text(request[name], name, limit)
    account = request.get("account", request.get("category"))
    if "account" in request and "category" in request and request["account"] != request["category"]:
        raise JournalError("invalid_request", "account and category name the same fact and differ.", "Give only account.")
    if account is not None:
        facts["account"] = _text(account, "account", 200)
    for name in ("date", "service_start", "service_end", "due_date"):
        if request.get(name) is not None:
            facts[name] = _day(request[name], name).isoformat()
    if "service_start" in facts and "service_end" in facts and facts["service_end"] < facts["service_start"]:
        raise JournalError("invalid_service_period", "service_end must not precede service_start. Both are inclusive.")
    if "evidence_ids" in request:
        ids = request["evidence_ids"]
        if not isinstance(ids, list) or len(ids) > 50 or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise JournalError("invalid_evidence", "evidence_ids must be up to 50 unique stored supporting document IDs.")
        facts["evidence_ids"] = sorted(ids)
    if not any(k in facts for k in ("counterparty", "description", "account")):
        raise JournalError("missing_fact", "Give at least a counterparty, a description or an account to match.")
    return facts


def _problems(rule: Mapping[str, Any]) -> list[str]:
    from ..queue import validate_accrual_hint
    return validate_accrual_hint({k: v for k, v in rule.items() if k not in ("id", "match", "scope")})


def _company_applies(rule: Mapping[str, Any], facts: Mapping[str, Any]) -> tuple[bool, list[str], tuple]:
    match, reasons, depth, hits = rule["match"], [], 0, 0
    account = facts.get("account")
    if "accounts" in match:
        found = [a for a in match["accounts"] if account and (account == a or account.startswith(a + ":"))]
        if not account:
            return False, ["the facts name no account"], ()
        if not found:
            return False, [f"account {account} is not under {', '.join(match['accounts'])}"], ()
        depth = max(len(a.split(":")) for a in found)
        reasons.append(f"account {account} is under {max(found, key=len)}")
    if "keywords" in match:
        haystack = " ".join(str(facts.get(k, "")) for k in ("description", "counterparty")).lower()
        hit = [w for w in match["keywords"] if w in haystack]
        if not hit:
            return False, [f"no keyword ({', '.join(match['keywords'])}) appears in the description"], ()
        hits = len(hit)
        reasons.append("keyword " + ", ".join(hit) + " appears in the description")
    threshold = rule.get("threshold")
    if threshold:
        if Decimal(facts["amount"]) < Decimal(threshold):
            return False, [f"amount {facts['amount']} is below the threshold {threshold}"], ()
        reasons.append(f"amount {facts['amount']} is at or above the threshold {threshold}")
    kinds = ("accounts" in match) + ("keywords" in match) + bool(threshold)
    return True, reasons, (kinds, depth, hits, Decimal(threshold or "0"))


def match(entity: Entity, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the saved rule that applies to these facts and a DRAFT request. Read-only."""
    entity = _entity(entity, write=False)
    facts = _facts(payload)
    from ..queue import _counterparty_key
    key = _counterparty_key(facts.get("description", ""), facts.get("counterparty", "")) or None
    result: dict[str, Any] = {"schema_version": 1, "kind": "accrual_rule_match", "effect": "proposals_only",
                              "facts": facts, "counterparty_key": key, "status": "no_match", "reason": "no_rule", "rule": None, "why": [],
                              "considered": [], "draft": None, "explanation": ""}
    chosen: dict[str, Any] | None = None
    context_rule = _counterparty_rule(entity, key) if key else None
    if context_rule is not None and not _problems(context_rule):
        rule = {"scope": "counterparty", "counterparty": key, **context_rule}
        threshold = context_rule.get("threshold")
        if threshold and Decimal(facts["amount"]) < Decimal(threshold):
            result["considered"].append({"scope": "counterparty", "counterparty": key, "applies": False,
                                         "reason": f"amount {facts['amount']} is below this supplier's threshold {threshold}"})
            result["reason"] = "below_supplier_threshold"
            result["why"] = [f"A saved rule exists for {key}, but the amount {facts['amount']} is below its threshold {threshold}.",
                             "The supplier rule is the most specific, so company rules are not used. Treat the amount as an ordinary expense or income."]
            result["explanation"] = f"Saved accrual rule for {key} does not apply: amount below its threshold {threshold}."
            return result
        result["considered"].append({"scope": "counterparty", "counterparty": key, "applies": True,
                                     "reason": f"saved rule for {key}" + (f"; amount is at or above the threshold {threshold}" if threshold else "")})
        chosen = rule
        result["why"] = [f"A saved rule for {key} applies. A supplier or customer rule wins over company rules."]
    company = _load_company(entity)["rules"]
    candidates = []
    for rule in sorted(company, key=lambda r: r.get("id", "")):
        if not isinstance(rule, dict) or "match" not in rule or _problems(rule):
            continue
        ok, reasons, score = _company_applies(rule, facts)
        result["considered"].append({"scope": "company", "id": rule["id"], "applies": ok, "reason": "; ".join(reasons)})
        if ok:
            candidates.append((score, rule))
    if chosen is not None and candidates:
        result["why"].append("Company rule " + ", ".join(sorted(r["id"] for _, r in candidates)) + " also matched and was not used.")
    if chosen is None and candidates:
        best_score = max(score for score, _ in candidates)
        tied = sorted((rule for score, rule in candidates if score == best_score), key=lambda r: r["id"])
        chosen = {"scope": "company", **tied[0]}
        others = [r["id"] for _, r in candidates if r["id"] != chosen["id"]]
        result["why"] = [f"Company rule {chosen['id']} applies: " + next(c["reason"] for c in result["considered"] if c.get("id") == chosen["id"]) + "."]
        if others:
            result["why"] += ["Most specific rule wins: more criteria first, then the deeper account, then more keywords, then the higher threshold, then the smaller id.",
                              "Other rules also matched and were not used: " + ", ".join(sorted(others)) + "."]
    if chosen is None:
        result["why"] = ["No saved accrual rule applies to these facts."]
        return result
    result["status"], result["reason"] = "matched", "matched"
    result["rule"] = chosen
    result["draft"] = _draft(entity, chosen, facts)
    result["explanation"] = _explanation(chosen)
    return result


def _explanation(rule: Mapping[str, Any]) -> str:
    who = (f"supplier rule for {rule['counterparty']}" if rule["scope"] == "counterparty" else f"company rule {rule['id']}")
    by = f"set by {rule['set_by']} on {str(rule['set_at'])[:10]}" + ("" if rule["source"] == "person" else ", proposed by an agent, not yet confirmed by a person")
    note = rule["note"].rstrip()
    note += "" if note.endswith((".", "!", "?")) else "."
    return (f"Saved accrual rule ({who}, {by}): {describe(rule)}. {note} "
            "This is a suggestion; a person still approves, and nothing is recorded from the rule alone.")


# ---------------------------------------------------------------------------
# Draft request
# ---------------------------------------------------------------------------

def _add_months(start: date, months: int) -> date:
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _whole_months(start: date, end: date) -> bool:
    return start.day == 1 and (end + timedelta(days=1)).day == 1


def _pick(chart: list[dict[str, Any]], subtype: str) -> tuple[str | None, list[str]]:
    names = sorted(row["name"] for row in chart if row["subtype"] == subtype)
    return (names[0] if len(names) == 1 else None), names


def _draft(entity: Entity, rule: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    from .accounts import list_accounts
    chart = list_accounts(entity)["accounts"]
    treatment = rule["treatment"]
    missing: list[dict[str, Any]] = []
    warnings: list[str] = []
    notes: list[str] = []
    stem = _slug(facts["source_id"]) if facts.get("source_id") else canonical_hash(facts)[:12]
    scheduled = treatment in SCHEDULE_TREATMENTS
    kind_name = treatment if scheduled else _ITEM_KIND[treatment]
    request: dict[str, Any] = {"operation_id": f"{kind_name}-{stem}-create", "id": f"{kind_name}-{stem}"}
    accounts = dict(rule.get("accounts") or {})
    account_fact = facts.get("account")

    def need(field: str, why: str, candidates: list[str] | None = None) -> None:
        row: dict[str, Any] = {"field": field, "why": why}
        if candidates:
            row["candidates"] = candidates
        missing.append(row)

    def derived(field: str, value: str | None, candidates: list[str], why: str) -> None:
        if value:
            request[field] = value
            notes.append(f"{field} {value} was chosen from the chart of accounts because it is the only {why} account.")
        else:
            need(field, f"name the {why} account" + (" (none is open)" if not candidates else ""), candidates)

    def by_subtype(field: str, subtype: str) -> None:
        value, names = _pick(chart, subtype)
        derived(field, value, names, subtype.replace("_", " "))

    if scheduled:
        request.update(kind=treatment, currency="USD", amount=facts["amount"])
        recognition_root = "Income:" if treatment == "deferred" else "Expenses:"
        control_subtype = {"prepaid": "prepaid", "deferred": "deferred_revenue", "depreciation": "fixed_asset"}[treatment]
        if accounts:
            request.update(account=accounts["account"], recognition_account=accounts["recognition_account"])
            if "contra_account" in accounts:
                request["contra_account"] = accounts["contra_account"]
        else:
            if treatment == "depreciation":
                if account_fact and any(r["name"] == account_fact and r["subtype"] in ("fixed_asset", "other_asset") for r in chart):
                    request["account"] = account_fact
                else:
                    by_subtype("account", "fixed_asset")
                by_subtype("contra_account", "accumulated_depreciation")
                open_names = {r["name"] for r in chart}
                derived("recognition_account", "Expenses:Depreciation" if "Expenses:Depreciation" in open_names else None,
                        sorted(n for n in open_names if n.startswith("Expenses:Depreciation")), "depreciation expense")
            else:
                by_subtype("account", control_subtype)
                if account_fact and account_fact.startswith(recognition_root):
                    request["recognition_account"] = account_fact
                else:
                    need("recognition_account", f"name the {recognition_root.rstrip(':').lower()} account the {treatment} amount is released into",
                         sorted(r["name"] for r in chart if r["name"].startswith(recognition_root)))
        start = facts.get("service_start") or (facts.get("date") if treatment == "depreciation" else None)
        if not facts.get("service_start") and start:
            warnings.append("service_start was taken from the purchase date; replace it with the in-service date if that differs.")
        end = facts.get("service_end")
        term = rule.get("term_months")
        if start and end:
            if term:
                notes.append(f"The supporting document's service dates were used instead of the saved term of {term} months.")
        elif start and term:
            end = (_add_months(date.fromisoformat(start), term) - timedelta(days=1)).isoformat()
            notes.append(f"service_end is {term} months after service_start, minus one day, because both dates are inclusive.")
        if start:
            request["service_start"] = start
        else:
            need("service_start", "give the first day of service (inclusive) from the supporting document")
        if end:
            request["service_end"] = end
        else:
            need("service_end", "give the last day of service (inclusive); the saved rule has no term_months")
        method = rule.get("method")
        if start and end:
            aligned = _whole_months(date.fromisoformat(start), date.fromisoformat(end))
            method = method or ("equal_month" if aligned else "actual_day")
            if method == "equal_month" and not aligned:
                warnings.append("The saved method equal_month needs whole calendar months, and these dates are not. The preview will block it; choose actual_day or change the dates.")
        if method:
            request["method"] = method
        _draft_origin(entity, request, facts, missing, treatment)
        if facts.get("evidence_ids"):
            request["evidence_ids"] = facts["evidence_ids"]
        else:
            need("evidence_ids", "attach the stored supporting document: run `books accrual intake` and use its id")
        if facts.get("reference") or facts.get("counterparty"):
            request["reference"] = facts.get("reference") or facts["counterparty"]
        command = "books accrual schedule preview --entity PATH --input FILE"
    else:
        request.update(kind=_ITEM_KIND[treatment], currency="USD", original_amount=facts["amount"])
        subtype = {"bill": "payable", "invoice": "receivable", "estimate": "accrued_liabilities"}[treatment]
        if accounts:
            request["account"], counter = accounts["account"], accounts["recognition_account"]
        else:
            by_subtype("account", subtype)
            counter = account_fact
            if counter is None or counter.startswith(("Assets:Bank:", "Assets:Cash", "Liabilities:")):
                counter = None
        origin: dict[str, Any] = {"mode": "new"}
        if facts.get("source_id"):
            origin["source_id"] = facts["source_id"]
        else:
            need("origin.source_id", "give the supporting document's source id")
        if counter:
            origin["counter_account"] = counter
        else:
            need("origin.counter_account", "name the expense or income account for the " + treatment)
        request["origin"] = origin
        if facts.get("date"):
            request["as_of"] = facts["date"]
        else:
            need("as_of", "give the date of the " + treatment)
        if facts.get("evidence_ids"):
            request["evidence_ids"] = facts["evidence_ids"]
        elif treatment == "estimate" and facts.get("estimate_basis"):
            request["estimate_basis"] = facts["estimate_basis"]
        elif treatment == "estimate":
            need("estimate_basis", "state the basis of the estimate in plain text, or attach a stored supporting document")
        else:
            need("evidence_ids", "attach the stored supporting document: run `books accrual intake` and use its id")
        if facts.get("counterparty"):
            request["counterparty"] = facts["counterparty"]
        if facts.get("reference"):
            request["reference"] = facts["reference"]
        if facts.get("due_date"):
            request["due_date"] = facts["due_date"]
        command = "books accrual item preview --entity PATH --input FILE"
    return {"status": "draft", "ready_for_preview": not missing, "command": command, "request": request,
            "missing": missing, "warnings": warnings, "notes": notes,
            "next": "Fill in anything missing, then run the command. The preview shows the exact entries; apply it only with the authority you already have. This draft records nothing."}


def _draft_origin(entity: Entity, request: dict[str, Any], facts: Mapping[str, Any], missing: list[dict[str, Any]], treatment: str) -> None:
    source_id = facts.get("source_id")
    if not source_id:
        missing.append({"field": "origin.source_id", "why": "give the payment's source id (the bank transaction or document id)"})
        return
    origin: dict[str, Any] = {"source_id": source_id}
    if facts.get("date"):
        origin["date"] = facts["date"]
    if _store(entity).source_exists(source_id):
        origin["mode"] = "existing"
        if facts.get("account"):
            origin["reclass_account"] = facts["account"]
        else:
            missing.append({"field": "origin.reclass_account", "why": "the payment is already recorded; name the account it was recorded to"})
    elif facts.get("funding_account"):
        origin.update(mode="new", funding_account=facts["funding_account"])
    else:
        origin["mode"] = "new"
        missing.append({"field": "origin.funding_account", "why": "name the bank or card account that paid, or confirm the payment is already recorded"})
    request["origin"] = dict(sorted(origin.items()))


# ---------------------------------------------------------------------------
# Queue integration
# ---------------------------------------------------------------------------

def proposal_stamp(entity: Entity, txn: Mapping[str, Any], category: str) -> dict[str, Any] | None:
    """Compact record of the saved rule that matches a proposed queue item, or None.

    Never raises and never blocks a proposal: a proposal is useful without it.
    """
    try:
        if not accrual_active(entity):
            return None
        facts = {"amount": str(txn.get("amount") or "0"), "account": category,
                 "description": str(txn.get("description") or ""), "counterparty": str(txn.get("counterparty") or "")}
        if str(txn.get("date") or "")[:10]:
            facts["date"] = str(txn["date"])[:10]
        facts = {k: v for k, v in facts.items() if v != ""}
        result = match(entity, facts)
        if result["status"] != "matched":
            return None
        rule = result["rule"]
        stamp = {"scope": rule["scope"], "treatment": rule["treatment"], "set_by": rule["set_by"], "set_at": rule["set_at"],
                 "source": rule["source"], "explanation": result["explanation"]}
        stamp["counterparty" if rule["scope"] == "counterparty" else "id"] = rule.get("counterparty") or rule.get("id")
        if "term_months" in rule:
            stamp["term_months"] = rule["term_months"]
        return stamp
    except Exception:
        return None
