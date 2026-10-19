"""Turn accrual on inside an existing company, and choose the default report view.

Two person-only writes, both previewed first and both audited:

``accrual enable``
    A cash company turns accrual on at a tracking start date. The ledger takes the
    explicit schema upgrade (sealed backup), entity.json gains capability 1 and
    ``accrual_tracking_start``, and the ledger meta table records the same date.
    No entry, report or default view changes. A company that was created on
    accrual has no tracking start and cannot use this command.

``entity set-view``
    Changes only the default report view (the ``basis`` value in entity.json and the
    ledger meta table). It never changes an entry, a period lock or a report file.
    Accrual is refused while the capability is off.

Accrual figures begin on the tracking start date. ``require_tracking_coverage`` makes
the accrual view refuse a range that starts before it.
"""
from __future__ import annotations

from datetime import date
import json
import os
import sqlite3
from typing import Any, Mapping

from ..entity import ACCRUAL_CAPABILITY_VERSION, Entity, load_entity, resolve_basis
from ..ledger.store import ACCRUAL_SCHEMA_VERSION, LedgerStore, default_store_path
from .intake import canonical_hash
from .journals import JournalError

TRACKING_KEY = "accrual_tracking_start"
VERSION = 1
ENABLE_KIND = "accrual_enable"
SET_VIEW_KIND = "default_view_set"


def _err(code: str, message: str, action: str) -> JournalError:
    return JournalError(code, message, action)


def _day(value: Any, field: str) -> date:
    try:
        if not isinstance(value, str):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise _err("invalid_date", f"{field} must be a YYYY-MM-DD date.",
                   f"Set {field} to the first day that accrual records are kept, for example the first day of next month.") from None


def tracking_start(source: Any) -> str | None:
    """The date accrual tracking began, from an Entity, an entity config dict or a ledger connection."""
    if isinstance(source, sqlite3.Connection):
        try:
            row = source.execute("SELECT value FROM meta WHERE key=?", (TRACKING_KEY,)).fetchone()
        except sqlite3.Error:
            return None
        return str(row[0]) if row and row[0] else None
    value = getattr(source, "entity_config", source).get(TRACKING_KEY)
    return value if isinstance(value, str) and value else None


def require_tracking_coverage(conn: sqlite3.Connection, view: str, start: date | None, end: date) -> None:
    """Refuse an accrual-view range that begins before accrual tracking started.

    The cash view never refuses: before the tracking start every entry is a cash event and the
    recorded books are the cash books.
    """
    from .reports import CoverageError
    first = tracking_start(conn)
    if view != "accrual" or not first:
        return
    begins = start if start is not None else end
    if begins < date.fromisoformat(first):
        raise CoverageError(
            f"Accrual figures start on {first}, the day accrual tracking was turned on. Before then the books are cash books "
            f"with no invoices, bills or prepaid records. Ask for a period that starts on or after {first}, or use the cash view.",
            first, "from" if start is not None else "to")


def _check_plan(plan: Any, kind: str) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or plan.get("kind") != kind or not isinstance(plan.get("request"), Mapping):
        raise _err("invalid_plan", "Expected the unchanged preview from the matching preview command.",
                   "Run the preview command again and pass its output unchanged.")
    if canonical_hash({"kind": kind, "request": plan["request"]}) != plan.get("request_hash"):
        raise _err("plan_changed", "The preview request has been changed.", "Create a new preview.")
    if canonical_hash({k: v for k, v in plan.items() if k != "plan_hash"}) != plan.get("plan_hash"):
        raise _err("plan_changed", "The preview has been changed.", "Create a new preview.")
    return dict(plan)


def _write_config(entity: Entity, changes: Mapping[str, Any]) -> None:
    config = dict(entity.entity_config)
    config.pop("accounting_basis", None)
    config.update(changes)
    temp = entity.path / "entity.json.accrual.tmp"
    temp.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, entity.path / "entity.json")


def _seal(store: LedgerStore, conn: sqlite3.Connection, session: str) -> None:
    store.append_audit_event("ledger-store-sealed", {"session_id": session, "entries": 0, "store_sha256": store.content_digest(conn)}, conn)


def _integrity(entity: Entity, what: str) -> None:
    from ..ledger.importer import check_integrity
    if check_integrity(entity).status != "ok":
        raise _err("ledger_integrity", f"Cannot {what} while ledger integrity is invalid.",
                   "Run `books ledger verify` or the integrity check and fix the reported problem first.")


# ---------------------------------------------------------------------------
# accrual enable
# ---------------------------------------------------------------------------

def _existing_receipt(store: LedgerStore, operation_id: str) -> dict[str, Any] | None:
    from .store import operation_receipt
    return operation_receipt(store, operation_id)


def preview_enable(entity: Entity, tracking_start_date: Any, operation_id: str | None = None) -> dict[str, Any]:
    """Preview turning accrual on at ``tracking_start_date``. Writes nothing."""
    entity = load_entity(entity.path)
    day = _day(tracking_start_date, "tracking_start")
    operation_id = operation_id or f"accrual-enable-{day.isoformat()}"
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 200:
        raise _err("invalid_operation_id", "operation_id must be a short non-empty string.", "Pass a stable operation ID or omit it.")
    config = entity.entity_config
    view = resolve_basis(config)
    capability = 1 if config.get("accrual_capability_version") == ACCRUAL_CAPABILITY_VERSION else 0
    current = tracking_start(config)
    store = LedgerStore(default_store_path(entity.path))
    replayed = False
    if current is not None:
        if current != day.isoformat():
            raise _err("tracking_start_conflict", f"Accrual was already turned on with tracking start {current}.",
                       f"The tracking start cannot change. Use {current}, or leave the company as it is.")
        replayed = True
    elif capability == 1 or view == "accrual":
        raise _err("accrual_already_primary", "This company already keeps accrual books from its first day. There is nothing to turn on.",
                   "Use `books report` and `books accrual` commands as usual.")
    if not store.path.exists():
        raise _err("ledger_missing", "The company has no ledger yet.", "Run `books entity init` first.")
    if not replayed:
        _integrity(entity, "turn on accrual")
        from .journals import _check_dates
        _check_dates(entity, store, [day.isoformat()], "migration")
    with store.connection() as conn:
        schema = store.schema_version(conn)
        entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        later = conn.execute("SELECT COUNT(*) FROM entries WHERE date>=?", (day.isoformat(),)).fetchone()[0]
        digest = store.content_digest(conn)
    warnings = []
    if later and not replayed:
        warnings.append({"code": "entries_on_or_after_tracking_start",
                         "message": (f"{later} existing entries are dated on or after {day.isoformat()}. They stay as recorded. "
                                     "If one of them already paid an invoice or bill that you list as open at the tracking start, "
                                     "the amount would be counted twice. Choose a later tracking start, or list only what is still open.")})
    body = {"schema_version": VERSION, "kind": ENABLE_KIND, "id": operation_id,
            "request": {"operation_id": operation_id, "tracking_start": day.isoformat()},
            "replayed": replayed, "ledger_digest": digest,
            "configuration_hash": canonical_hash(config),
            "effects": {"capability": 1, "default_view": view, "tracking_start": day.isoformat(),
                        "ledger_schema": {"from": schema, "to": ACCRUAL_SCHEMA_VERSION,
                                          "backup": "A sealed backup of the ledger is kept." if schema != ACCRUAL_SCHEMA_VERSION else None},
                        "entries_changed": 0, "reports_changed": False,
                        "existing_entries": entries, "entries_on_or_after_tracking_start": later},
            "warnings": warnings,
            "next_steps": [
                f"Optional: list what is still open on {day.isoformat()}: `books accrual opening preview --entity PATH --input request.json` (cutover {day.isoformat()}).",
                "Optional: `books entity set-view PATH --view accrual` to make accrual the default view."]}
    body["request_hash"] = canonical_hash({"kind": ENABLE_KIND, "request": body["request"]})
    body["plan_hash"] = canonical_hash(body)
    return body


def apply_enable(entity: Entity, plan: Any) -> dict[str, Any]:
    """Apply the unchanged preview. An exact repeat returns the original receipt as already_applied."""
    from ..ledger.importer import _entity_write_lock
    from .store import SupportingMutation, apply_mutations
    plan = _check_plan(plan, ENABLE_KIND)
    day = _day(plan["request"].get("tracking_start"), "tracking_start")
    operation_id = plan["id"]
    with _entity_write_lock(entity.path):
        entity = load_entity(entity.path)
        store = LedgerStore(default_store_path(entity.path))
        stored = _existing_receipt(store, operation_id)
        if stored is not None:
            if stored.get("request_hash") != plan["request_hash"]:
                raise _err("identity_conflict", f"Operation ID {operation_id} already has different committed facts.",
                           f"Inspect it with `books accrual receipt --id {operation_id}`.")
            if tracking_start(entity) != day.isoformat() or entity.entity_config.get("accrual_capability_version") != ACCRUAL_CAPABILITY_VERSION:
                # A previous run stopped after the ledger commit. Finish the company file.
                _write_config(entity, {"accrual_capability_version": ACCRUAL_CAPABILITY_VERSION, TRACKING_KEY: day.isoformat()})
            return {**stored["receipt"], "status": "already_applied"}
        fresh = preview_enable(entity, day.isoformat(), operation_id)
        if fresh["replayed"]:
            return {"status": "already_applied", "id": operation_id, "kind": ENABLE_KIND, "tracking_start": day.isoformat(),
                    "request_hash": plan["request_hash"], "plan_hash": plan["plan_hash"]}
        if fresh["configuration_hash"] != plan["configuration_hash"]:
            raise _err("stale_preview", "Company configuration changed after the preview.", "Run the preview again.")
        if fresh["ledger_digest"] != plan["ledger_digest"]:
            raise _err("stale_preview", "The ledger changed after the preview.", "Run the preview again.")
        backup = store.upgrade()
        receipt = {"status": "applied", "id": operation_id, "kind": ENABLE_KIND, "request_hash": plan["request_hash"],
                   "plan_hash": plan["plan_hash"], "tracking_start": day.isoformat(), "capability": 1,
                   "default_view": fresh["effects"]["default_view"], "backup": backup.name if backup else None,
                   "entries_changed": 0, "warnings": plan["warnings"]}
        intent = {"id": "operation:" + operation_id, "kind": ENABLE_KIND, "status": "applied",
                  "request_hash": plan["request_hash"], "plan_hash": plan["plan_hash"],
                  "request": plan["request"], "plan": dict(plan), "receipt": receipt}
        with store.transaction(immediate=True) as conn:
            apply_mutations(store, [SupportingMutation("intents", intent["id"], intent)], conn)
            store.set_meta(TRACKING_KEY, day.isoformat(), conn)
            store.append_audit_event("accrual-enabled", {
                "operation_id": operation_id, "tracking_start": day.isoformat(),
                "default_view": receipt["default_view"], "plan_hash": plan["plan_hash"]}, conn)
            _seal(store, conn, "accrual-enable")
        _write_config(entity, {"accrual_capability_version": ACCRUAL_CAPABILITY_VERSION, TRACKING_KEY: day.isoformat()})
        return receipt


# ---------------------------------------------------------------------------
# entity set-view
# ---------------------------------------------------------------------------

def preview_set_view(entity: Entity, view: Any) -> dict[str, Any]:
    """Preview changing the default report view. Writes nothing."""
    entity = load_entity(entity.path)
    if view not in {"cash", "accrual"}:
        raise _err("invalid_view", "view must be cash or accrual.", "Use --view cash or --view accrual.")
    config = entity.entity_config
    current = resolve_basis(config)
    store = LedgerStore(default_store_path(entity.path))
    capability = 1 if (config.get("accrual_capability_version") == ACCRUAL_CAPABILITY_VERSION
                       and store.schema_version() == ACCRUAL_SCHEMA_VERSION) else 0
    if view == "accrual" and not capability:
        raise _err("accrual_not_enabled", "Accrual is not turned on for this company, so accrual cannot be the default view.",
                   "Run `books accrual enable preview --entity PATH --tracking-start YYYY-MM-DD` first.")
    _integrity(entity, "change the default view")
    body = {"schema_version": VERSION, "kind": SET_VIEW_KIND, "request": {"view": view},
            "changes": current != view, "ledger_digest": store.content_digest(),
            "configuration_hash": canonical_hash(config),
            "effects": {"from": current, "to": view, "tracking_start": tracking_start(config),
                        "entries_changed": 0, "period_locks_changed": 0, "published_statements_changed": False,
                        "note": "Only the default for `books report`, `books ask` and exports changes. Pass --basis to choose the other view for one command."}}
    body["request_hash"] = canonical_hash({"kind": SET_VIEW_KIND, "request": body["request"]})
    body["plan_hash"] = canonical_hash(body)
    return body


def apply_set_view(entity: Entity, view: Any, plan_hash: str | None = None) -> dict[str, Any]:
    """Change the default view. Repeating the same view returns already_applied."""
    from ..ledger.importer import _entity_write_lock
    with _entity_write_lock(entity.path):
        fresh = preview_set_view(entity, view)
        if plan_hash is not None and plan_hash != fresh["plan_hash"]:
            raise _err("stale_preview", "The company changed after the preview.", "Run the preview again.")
        if not fresh["changes"]:
            return {"status": "already_applied", "kind": SET_VIEW_KIND, "view": view, "request_hash": fresh["request_hash"]}
        entity = load_entity(entity.path)
        store = LedgerStore(default_store_path(entity.path))
        previous = fresh["effects"]["from"]
        with store.transaction(immediate=True) as conn:
            store.set_meta("basis", view, conn)
            store.append_audit_event("default-view-set", {"from": previous, "to": view, "plan_hash": fresh["plan_hash"]}, conn)
            _seal(store, conn, "entity-set-view")
        _write_config(entity, {"basis": view})
        return {"status": "applied", "kind": SET_VIEW_KIND, "from": previous, "view": view, "request_hash": fresh["request_hash"],
                "plan_hash": fresh["plan_hash"], "entries_changed": 0, "published_statements_changed": False}
