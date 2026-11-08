"""Exact account presentation, separate from immutable financial account keys."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

KEY = "chart_presentation_v1"


def read(conn: Any) -> dict[str, dict[str, str | None]]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
    return json.loads(row[0]) if row else {}


def for_entity(path: Path | str) -> dict[str, dict[str, str | None]]:
    from .reports.cache import open_cache
    conn = open_cache(path)
    try:
        return read(conn)
    finally:
        conn.close()


def label(account: str, records: dict[str, dict[str, str | None]]) -> str:
    return records.get(account, {}).get("display_name") or account.replace(":", " › ")


def numbers(records: dict[str, dict[str, str | None]], previous: dict[str, str] | None = None) -> dict[str, str]:
    result = dict(previous or {})
    for account, row in records.items():
        if row.get("number") is None:
            result[account] = ""  # Explicitly unnumbered: exports must not invent a number.
        else:
            result[account] = row["number"]
    return result


def update(root: Path, request: dict[str, Any], *, apply: bool = False, plan_hash: str | None = None) -> dict[str, Any]:
    """Preview/apply an explicit display-label/number decision without touching history."""
    from .chart_adoption import _hash, _json, _text
    from .ledger.importer import _entity_write_lock, check_store_integrity
    from .ledger.store import LedgerStore
    from .entity import load_entity
    if not isinstance(request, dict) or set(request) != {"operation_id", "account", "display_name", "number"}:
        raise ValueError("Presentation request needs operation_id, account, display_name and number.")
    for field, limit in (("operation_id", 100), ("account", 300), ("display_name", 300)):
        _text(request[field], field, limit)
    if request["number"] is not None:
        _text(request["number"], "number", 100)
    store = LedgerStore(root.resolve() / "ledger.sqlite")
    if not store.path.is_file() or store.path.is_symlink():
        raise ValueError("Presentation requires an initialized ledger.")
    request_hash = _hash(request)
    with _entity_write_lock(root.resolve()):
        if check_store_integrity(store).status != "ok":
            raise ValueError("Presentation requires valid ledger integrity.")
        with store.transaction(immediate=apply) as conn:
            for row in conn.execute("SELECT payload_json FROM audit_events WHERE type='chart-presentation-set'"):
                receipt = json.loads(row[0])
                if receipt["operation_id"] == request["operation_id"]:
                    if receipt["request_hash"] != request_hash:
                        raise ValueError("Operation identity reused with a changed presentation.")
                    return {"status": "already_applied", "receipt": receipt}
            if not conn.execute("SELECT 1 FROM accounts WHERE name=?", (request["account"],)).fetchone():
                raise ValueError("Account does not exist; no identity was created or renamed.")
            records = read(conn)
            config = load_entity(root).entity_config
            prior_number = (config.get("tax_handoff") or {}).get("account_numbers", {}).get(request["account"])
            before = records.get(request["account"], {"display_name": label(request["account"], records), "number": prior_number})
            after = {"display_name": request["display_name"], "number": request["number"]}
            fingerprint = _hash({"ledger": store.content_digest(conn), "entity_config": config, "request": request_hash})
            result = {"status": "preview", "plan_hash": fingerprint, "account": request["account"],
                      "before": before, "after": after, "entries_changed": 0, "ledger_key_changed": False}
            if not apply:
                return result
            if plan_hash != fingerprint:
                raise ValueError("Stale or missing presentation preview.")
            records[request["account"]] = after
            store.set_meta(KEY, _json(records), conn)
            receipt = {**request, "request_hash": request_hash, "previous": before}
            store.append_audit_event("chart-presentation-set", receipt, conn)
            store.append_audit_event("ledger-store-sealed", {"session_id": "chart-presentation", "entries": 0,
                                    "source_ids": [], "store_sha256": store.content_digest(conn)}, conn)
            return {**result, "status": "applied", "receipt": receipt}
