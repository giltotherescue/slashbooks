"""Three separate facts that the word "basis" used to mix together.

capability
    Accrual features are available: entity.json ``accrual_capability_version``
    is 1 and the ledger has the accrual schema (schema_version 2). A company
    activates it later, not at creation (a later change adds the command).
discipline
    Where the strict accrual recording rules apply: ``"all"`` (every
    transaction), ``"per_account"`` (reserved; nothing returns it yet) or
    ``"off"``. It is derived on read and never written. Today an accrual
    default view means ``"all"`` and a cash default view means ``"off"``.
default_view
    The ``basis`` value in entity.json and the ledger meta table. It chooses the
    default for report labels and report shape. It must never gate a write.

Valid combinations of (default view, capability): (cash, 0), (cash, 1) and
(accrual, 1). A company with (accrual, 0) is the legacy read-only state: its
records stay readable, and ``require_write_scope`` refuses every write.

Each helper accepts an Entity, an entity config dict, or a ledger connection.
A connection only knows the meta table, so it reports capability from the
ledger schema version.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..entity import ACCRUAL_CAPABILITY_VERSION, resolve_basis

DISCIPLINE_ALL = "all"
DISCIPLINE_PER_ACCOUNT = "per_account"
DISCIPLINE_OFF = "off"


def _config(source: Any) -> dict[str, Any]:
    return getattr(source, "entity_config", source)


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    keys = ("basis", "accounting_basis", "schema_version")
    marks = ",".join("?" for _ in keys)
    return {k: v for k, v in conn.execute(f"SELECT key,value FROM meta WHERE key IN ({marks})", keys)}


def default_view(source: Any) -> str:
    """Return the default report view, "cash" or "accrual"."""
    if isinstance(source, sqlite3.Connection):
        values = _meta(source)
        return resolve_basis({k: v for k, v in values.items() if k != "schema_version"})
    return resolve_basis(_config(source))


def capability(source: Any) -> int:
    """Return 1 when accrual features are available, otherwise 0."""
    if isinstance(source, sqlite3.Connection):
        try:
            return 1 if str(_meta(source).get("schema_version")) == "2" else 0
        except sqlite3.Error:
            return 0
    resolve_basis(_config(source))
    return 1 if _config(source).get("accrual_capability_version") == ACCRUAL_CAPABILITY_VERSION else 0


def discipline(source: Any) -> str:
    """Return where strict accrual recording rules apply (derived, never stored)."""
    return DISCIPLINE_ALL if default_view(source) == "accrual" else DISCIPLINE_OFF


def accrual_active(source: Any) -> bool:
    """True when strict accrual rules apply to at least some transactions."""
    return discipline(source) != DISCIPLINE_OFF


def require_write_scope(entity_or_config: Any) -> None:
    """Admit accrual writes only when capability is on and discipline is not off."""
    config = _config(entity_or_config)
    if discipline(config) == DISCIPLINE_OFF or capability(config) != 1:
        raise ValueError("Accrual writes require explicit accrual basis and capability version 1.")
