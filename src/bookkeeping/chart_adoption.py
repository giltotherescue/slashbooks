"""Preview and atomically adopt an explicitly mapped, additive source chart.

Ledger keys never change. Exact source labels/numbers are evidence metadata, not
an unannounced rename. File/image interpretation belongs to the caller; this
command accepts only its reviewed structured manifest, never source instructions.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any

from .ledger.importer import _entity_write_lock, check_store_integrity
from .ledger.model import Open
from .ledger.store import LedgerStore, SUPPORTED_SCHEMA_VERSIONS, validate_financial_kind

KEY = "chart_adoption_v1"
ROOTS = {"Assets", "Liabilities", "Equity", "Income", "Expenses"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value: Any, field: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{field} must be nonempty text of at most {limit} characters without controls.")
    return value  # Preserve spacing, punctuation, case and Unicode exactly.


def validate(manifest: Any) -> dict[str, Any]:
    fields = {"version", "operation_id", "source", "decision", "scope", "open_date", "accounts"}
    if (not isinstance(manifest, dict) or set(manifest) - {"presentation"} != fields
            or type(manifest["version"]) is not int or manifest["version"] not in (1, 2)):
        raise ValueError("Expected chart manifest version 1 or 2 with exactly the documented fields.")
    typed = manifest["version"] == 2
    optional = {"active", "financial_kind"} if typed else {"active"}
    if manifest.get("presentation", "evidence_only") not in ("activate", "evidence_only"):
        raise ValueError("presentation must be activate or evidence_only.")
    _text(manifest["operation_id"], "operation_id", 100)
    _text(manifest["source"], "source", 100)
    if manifest["decision"] not in ("preserve", "rebuild") or manifest["scope"] not in ("complete", "partial"):
        raise ValueError("Explicit preserve/rebuild decision and complete/partial source scope required.")
    # Neither complete nor rebuild authorizes removal of existing accounts.
    date.fromisoformat(_text(manifest["open_date"], "open_date", 10))
    rows = manifest["accounts"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000 or len(_json(manifest).encode()) > 2_000_000:
        raise ValueError("A chart manifest needs 1-1000 accounts and at most 2 MB.")
    ids, targets = set(), set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - optional != {"source_id", "name", "number", "parent_id", "type", "account"}:
            raise ValueError("Each account needs source_id, name, number, parent_id, type and explicit account mapping.")
        if "active" in row and type(row["active"]) is not bool:
            raise ValueError("Source active state must be boolean.")
        identity = _text(row["source_id"], "source_id", 200)
        _text(row["name"], "name")
        if row["number"] is not None and row["number"] != "":
            _text(row["number"], "number", 100)
        if row["parent_id"] is not None:
            _text(row["parent_id"], "parent_id", 200)
        target = _text(row["account"], "account")
        if not isinstance(row["type"], str) or row["type"] not in ROOTS or target.split(":")[0] != row["type"] or ":" not in target:
            raise ValueError("The mapped ledger root must match the explicit account type.")
        needs_kind = typed and row.get("active", True) and row["type"] in ("Assets", "Liabilities")
        if needs_kind:
            if "financial_kind" not in row:
                raise ValueError("Every active asset or liability needs a reviewed financial kind in version 2.")
            validate_financial_kind(target, row["financial_kind"])
        elif "financial_kind" in row:
            raise ValueError("Only an active asset or liability can carry a financial kind.")
        Open(date=date.fromisoformat(manifest["open_date"]), account=target)
        if identity in ids or target.casefold() in targets:
            raise ValueError("Duplicate source identity or multiple source accounts mapped to one ledger key; resolve explicitly.")
        ids.add(identity)
        targets.add(target.casefold())
    parents = {r["source_id"]: r["parent_id"] for r in rows}
    by_id = {r["source_id"]: r for r in rows}
    for row in rows:
        parent = by_id.get(row["parent_id"])
        if parent and parent["type"] != row["type"]:
            raise ValueError("Parent and child must have the same account type.")
        if parent and manifest["decision"] == "preserve" and not row["account"].startswith(parent["account"] + ":"):
            raise ValueError("Preserve requires mapped ledger hierarchy to retain the source parent; rebuild must be explicit.")
    for identity in ids:
        seen = set()
        current = identity
        while current is not None:
            if current in seen:
                raise ValueError("Source hierarchy contains a cycle.")
            if current not in parents:
                raise ValueError("Source hierarchy has a missing parent; supply its row, even for a partial chart.")
            seen.add(current)
            current = parents[current]
    return manifest


def _stored(conn: Any) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
    return json.loads(row[0]) if row else {"sources": {}}


def adopt(root: Path, manifest: dict[str, Any], *, apply: bool = False, plan_hash: str | None = None) -> dict[str, Any]:
    manifest = validate(manifest)
    root = root.resolve()
    store = LedgerStore(root / "ledger.sqlite")
    if not store.path.is_file() or store.path.is_symlink():
        raise ValueError("Chart adoption requires an initialized ledger, not a symbolic link.")
    with _entity_write_lock(root):
        if check_store_integrity(store).status != "ok":
            raise ValueError("Chart adoption requires valid ledger integrity.")
        with store.transaction(immediate=apply) as conn:
            return apply_reviewed_chart(root, store, conn, manifest, apply=apply, plan_hash=plan_hash)


def apply_reviewed_chart(root: Path, store: LedgerStore, conn: Any, manifest: dict[str, Any], *,
                         apply: bool, plan_hash: str | None = None) -> dict[str, Any]:
    """Typed chart operation for an existing locked, integrity-checked ledger transaction.

    QuickBooks opening uses the same transaction for chart activation and entries.
    Callers must raise/roll back if any subsequent financial operation fails.
    """
    manifest = validate(manifest)
    request_hash = _hash(manifest)
    if store.schema_version(conn) not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError("Unsupported ledger schema.")
    if conn.execute("SELECT 1 FROM entries LIMIT 1").fetchone() and not conn.execute("SELECT 1 FROM audit_events LIMIT 1").fetchone():
        raise ValueError("Populated ledger has no audit history.")
    events = conn.execute("SELECT payload_json FROM audit_events WHERE type='chart-adopted'").fetchall()
    for event in events:
        receipt = json.loads(event[0])
        if receipt["operation_id"] == manifest["operation_id"]:
            if receipt["request_hash"] != request_hash:
                raise ValueError("Operation identity reused with a changed chart.")
            return {"status": "already_applied", "receipt": receipt, "entries_changed": 0}
    previous = _stored(conn)
    existing = {r["name"]: dict(r) for r in conn.execute("SELECT name,currency,open_date FROM accounts")}
    sources = previous["sources"]
    prior = sources.get(manifest["source"], {}).get("accounts", {})
    from .entity import flat_bank_account_mappings, load_entity
    config = load_entity(root).entity_config
    kinds = {row["account"]: row["financial_kind"] for row in manifest["accounts"] if "financial_kind" in row}
    current_kinds = store.load_financial_kinds(conn) if kinds else {}
    mapped = set(flat_bank_account_mappings(config).values()) if kinds else set()
    csv_mappings = config.get("csv_account_mappings")
    if kinds and isinstance(csv_mappings, dict):
        mapped.update(row["ledger_account"] for row in csv_mappings.values()
                      if isinstance(row, dict) and row.get("confirmed") is True and not row.get("proposed")
                      and isinstance(row.get("ledger_account"), str))
    for account, kind in kinds.items():
        if account in current_kinds and current_kinds[account] != kind:
            raise ValueError(f"Account '{account}' already has a different financial kind.")
        if kind == "other" and account in mapped:
            raise ValueError(f"Account '{account}' has a mapped financial source. Review its financial kind.")
    outcomes = []
    source_rows = [r for source in sources.values() for r in source["accounts"].values()]
    # Source evidence retains its original shape across manifest versions.
    identities = {row["source_id"]: {key: value for key, value in row.items() if key != "financial_kind"}
                  for row in manifest["accounts"]}
    for row in manifest["accounts"]:
        old = prior.get(row["source_id"])
        if old is not None and old != identities[row["source_id"]]:
            raise ValueError("Source account identity changed; adoption cannot rename, remap or overwrite previous source evidence.")
        for prior_row in source_rows:
            if prior_row["account"] == row["account"] and (prior_row["name"], prior_row["number"], prior_row["type"]) != (row["name"], row["number"], row["type"]):
                raise ValueError("Mapped ledger account already has conflicting source name, number or type; resolve before adoption.")
        # Avoid visually ambiguous differently-cased ledger identities.
        if row["account"] not in existing and row["account"].casefold() in {a.casefold() for a in existing}:
            raise ValueError("Mapped account differs only by case from an existing account.")
        outcomes.append({**row, "action": "map" if row["account"] in existing else "create" if row.get("active", True) else "reference_only"})
    duplicate_numbers = sorted(k for k, count in Counter(r["number"] for r in manifest["accounts"] if r["number"] is not None).items() if count > 1)
    fingerprint = _hash({"ledger": store.content_digest(conn), "entity_config": config, "request": request_hash})
    preview = {"status": "preview", "plan_hash": fingerprint, "accounts": outcomes,
               "duplicate_numbers": duplicate_numbers, "scope": manifest["scope"], "decision": manifest["decision"],
               "existing_accounts_removed": 0, "entries_changed": 0,
               "active_display_names_changed": manifest.get("presentation") == "activate",
               "active_tax_numbers_changed": manifest.get("presentation") == "activate"}
    if not apply:
        return preview
    if plan_hash != fingerprint:
        raise ValueError("Stale or missing chart preview; preview the current books before applying.")
    currency = conn.execute("SELECT value FROM meta WHERE key='currency'").fetchone()
    currency = currency[0] if currency else "USD"
    created = [r["account"] for r in outcomes if r["action"] == "create"]
    store.insert_opens([Open(date=date.fromisoformat(manifest["open_date"]), account=a, currencies=(currency,)) for a in created], conn)
    store.assign_financial_kinds(kinds, conn)
    source = sources.setdefault(manifest["source"], {"accounts": {}, "decisions": []})
    source["accounts"].update(identities)
    source["decisions"].append({"operation_id": manifest["operation_id"], "decision": manifest["decision"], "scope": manifest["scope"]})
    store.set_meta(KEY, _json(previous), conn)
    if manifest.get("presentation") == "activate":
        from . import chart_presentation
        presentation = chart_presentation.read(conn)
        presentation.update({row["account"]: {"display_name": row["name"], "number": row["number"]}
                             for row in manifest["accounts"] if row.get("active", True)})
        store.set_meta(chart_presentation.KEY, _json(presentation), conn)
    receipt = {"operation_id": manifest["operation_id"], "request_hash": request_hash,
               "source": manifest["source"], "created": created, "entries_changed": 0}
    if manifest["version"] == 2:
        receipt["financial_kinds"] = kinds
    store.append_audit_event("chart-adopted", receipt, conn)
    store.append_audit_event("ledger-store-sealed", {"session_id": "chart-adoption", "entries": 0,
                             "source_ids": [], "store_sha256": store.content_digest(conn)}, conn)
    return {**preview, "status": "applied", "receipt": receipt}

def protected_accounts(conn: Any) -> set[str]:
    """Explicit adopted identities survive starter selection and unused removal."""
    return {row["account"] for source in _stored(conn)["sources"].values()
            for row in source["accounts"].values()}


def inspect(root: Path) -> dict[str, Any]:
    """Return active ledger keys alongside source evidence without presentation substitution."""
    store = LedgerStore(root.resolve() / "ledger.sqlite")
    if not store.path.is_file() or store.path.is_symlink():
        raise ValueError("Chart inspection requires an initialized ledger.")
    with _entity_write_lock(root.resolve()), store.connection() as conn:
        from . import chart_presentation
        presentation = chart_presentation.read(conn)
        return {"accounts": [{**dict(r), "display_name": chart_presentation.label(r["account"], presentation),
                              "account_number": presentation.get(r["account"], {}).get("number")}
                             for r in conn.execute("SELECT name AS account,currency,open_date FROM accounts ORDER BY name")],
                "source_evidence": _stored(conn), "ledger_digest": store.content_digest(conn)}
