"""Choose the current statement without importing or deleting financial activity."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .entity import _locked_entity_operation, load_entity


def selection_path(entity_path: Path | str) -> Path:
    return Path(entity_path) / "ingestion/statements/history/selection.json"


def load_selection(entity_path: Path | str) -> dict[str, Any]:
    path = selection_path(entity_path)
    if not path.exists():
        return {"replaced": {}, "history": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or not isinstance(state.get("replaced"), dict)
                or not isinstance(state.get("history"), list)
                or any(not isinstance(a, str) or not isinstance(b, str) or a == b
                       for a, b in state["replaced"].items())):
            raise ValueError("Invalid statement selection history.")
        return state
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Statement selection history could not be read.") from exc


def selection_revision(entity_path: Path | str) -> str:
    return hashlib.sha256(json.dumps(load_selection(entity_path), sort_keys=True).encode()).hexdigest()


@_locked_entity_operation
def select_statement(entity_path: Path | str, statement_id: str, *, expected: list[str], expected_revision: str) -> dict[str, Any]:
    """Select one saved statement; fence the exact set reviewed by the caller.

    One atomic history file preserves originals, supports retry and switching
    back, and never changes transaction intake, ledger entries or source files.
    New uploads are not silently covered by an earlier selection.
    """
    from .coverage import load_statements, _months

    entity = load_entity(entity_path)
    records, findings = load_statements(entity.path, include_replaced=True)
    if findings:
        raise ValueError("Check the saved statements before replacing one.")
    selected = next((r for r in records if r.statement_id == statement_id and r.kind == "statement"), None)
    if selected is None:
        raise ValueError("Import and check the replacement statement first.")
    peers = sorted(r.statement_id for r in records if r.kind == "statement" and
                   (r.account, r.period_start, r.period_end) ==
                   (selected.account, selected.period_start, selected.period_end))
    if sorted(set(expected)) != peers:
        raise ValueError("The saved statements changed. Review them again.")
    if selected.opening_balance is None or selected.closing_balance is None:
        raise ValueError("The replacement needs both starting and ending balances.")
    state = load_selection(entity.path)
    replaced = {a: b for a, b in state["replaced"].items() if a not in peers}
    replaced.update({old: statement_id for old in peers if old != statement_id})
    if replaced == state["replaced"]:
        return {"status": "selected", "statement_id": statement_id, "replaced": peers, "changed": False}
    if expected_revision != selection_revision(entity.path):
        raise ValueError("The current statement changed. Review it again.")
    from .ledger.store import LedgerStore, default_store_path
    from .accrual.journals import _check_dates
    _check_dates(entity, LedgerStore(default_store_path(entity.path)),
                 [day.isoformat() for _, first, last in _months(selected.period_start, selected.period_end)
                  for day in (first, last)], "statement_selection")
    state["replaced"] = replaced
    state["history"].append({"selected": statement_id, "replaced": [p for p in peers if p != statement_id],
                             "at": datetime.now(timezone.utc).isoformat()})
    path = selection_path(entity.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return {"status": "selected", "statement_id": statement_id, "replaced": peers, "changed": True}


def replaced_evidence(entity_path: Path | str) -> set[str]:
    from .coverage import load_statements
    state = load_selection(entity_path)
    if not state["replaced"]:
        return set()
    records, findings = load_statements(entity_path, include_replaced=True)
    if findings:
        raise ValueError("Check the saved statement history first.")
    active_hashes = {r.evidence_sha256 for r in records if r.statement_id not in state["replaced"]}
    return {r.evidence_sha256 for r in records if r.statement_id in state["replaced"]
            and r.evidence_sha256 and r.evidence_sha256 not in active_hashes}


def stale_reconciliations(entity_path: Path | str) -> list[dict[str, Any]]:
    from .reconcile import list_discrepancies
    hashes = replaced_evidence(entity_path)
    return [r for r in list_discrepancies(entity_path)
            if hashes.intersection((r.get("source_evidence_hashes") or {}).values())]
