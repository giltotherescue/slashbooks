"""Company fiscal-year configuration and deterministic annual boundaries."""
from __future__ import annotations

from datetime import date
import json
import os
import re
from typing import Any


def validate_start(value: Any) -> str:
    """Regular fiscal years start on the first of a month; week-based years are unsupported."""
    if not isinstance(value, str) or not re.fullmatch(r"(?:0[1-9]|1[0-2])-01", value):
        raise ValueError("Fiscal year start must be MM-01 (01-01 through 12-01). 52/53-week years are not supported.")
    return value


def year_start(day: date, start: str = "01-01") -> date:
    month = int(validate_start(start)[:2])
    return date(day.year - (day.month < month), month, 1)


def preview(entity: Any, start: Any) -> dict[str, Any]:
    from .accrual.enable import _integrity
    from .accrual.intake import canonical_hash
    from .accrual.journals import JournalError
    from .accrual.store import load_record
    from .entity import load_entity
    from .ledger.store import LedgerStore, default_store_path

    entity = load_entity(entity.path)
    start = validate_start(start)
    _integrity(entity, "set the fiscal year")
    store = LedgerStore(default_store_path(entity.path))
    # Imported summaries cannot be split or reinterpreted with a different calendar.
    if store.schema_version() == 2:
        opening = load_record(store, "coverage", "opening")
        if opening and opening.get("fiscal_start", "")[5:] != start:
            raise JournalError("fiscal_year_conflicts_with_opening", "The fiscal year differs from the imported opening summary.",
                               "Review the opening coverage with your accountant before changing the fiscal year.")
    current = entity.entity_config.get("fiscal_year_start", "01-01")
    body = {"schema_version": 1, "kind": "entity_set_fiscal_year", "request": {"fiscal_year_start": start},
            "changes": current != start, "ledger_digest": store.content_digest(),
            "configuration_hash": canonical_hash(entity.entity_config),
            "effects": {"from": current, "to": start, "entries_changed": 0,
                        "published_statements_changed": False,
                        "note": "Sets the calendar used by new tax views and fiscal earnings reports. Existing exports stay unchanged. This does not elect a tax year with a tax authority."}}
    body["plan_hash"] = canonical_hash(body)
    return body


def apply(entity: Any, start: Any, plan_hash: str | None = None) -> dict[str, Any]:
    from .accrual.enable import _seal
    from .accrual.journals import JournalError
    from .entity import load_entity
    from .ledger.importer import _entity_write_lock
    from .ledger.store import LedgerStore, default_store_path

    with _entity_write_lock(entity.path):
        plan = preview(entity, start)
        if plan_hash is not None and plan_hash != plan["plan_hash"]:
            raise JournalError("stale_preview", "The company changed after the preview.", "Run the preview again.")
        if not plan["changes"]:
            return {"status": "already_applied", **plan}
        entity = load_entity(entity.path)
        config = {**entity.entity_config, **plan["request"]}
        temp = entity.path / "entity.json.fiscal.tmp"
        temp.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        store = LedgerStore(default_store_path(entity.path))
        try:
            with store.transaction(immediate=True) as conn:
                store.append_audit_event("fiscal-year-set", {"from": plan["effects"]["from"], "to": start,
                                                           "plan_hash": plan["plan_hash"]}, conn)
                _seal(store, conn, "entity-set-fiscal-year")
                os.replace(temp, entity.path / "entity.json")
        finally:
            temp.unlink(missing_ok=True)
        return {"status": "applied", **plan}
