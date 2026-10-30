"""Three separate facts that the word "basis" used to mix together.

capability
    Accrual features are available: entity.json ``accrual_capability_version``
    is 1 and the ledger has the accrual schema (schema_version 2). A company
    activates it later, not at creation (a later change adds the command).
discipline
    Where the strict accrual recording rules apply: ``"all"`` (every
    transaction), ``"per_account"`` (only bank activity that touches an open
    accrual item or schedule, from the tracking start date on; see
    ``treatment.needs_treatment``) or ``"off"``. It is derived on read and
    never written:

    - capability 0: ``"off"`` for a cash view; the legacy (accrual, 0) state
      keeps ``"all"`` so its read-only gates stay as they were.
    - capability 1 and ``accrual_tracking_start`` present: ``"per_account"``.
      Only the enable command writes that key.
    - capability 1 without it (existing and new ``--basis accrual`` companies):
      ``"all"``.
default_view
    The ``basis`` value in entity.json and the ledger meta table. It chooses the
    default for report labels and report shape. It must never gate a write.

Valid combinations of (default view, capability): (cash, 0), (cash, 1) and
(accrual, 1). Accrual record writes need only capability 1; the view never gates them. A company with (accrual, 0) is the legacy read-only state: its
records stay readable, and ``require_write_scope`` refuses every write.

Each helper accepts an Entity, an entity config dict, or a ledger connection.
A connection only knows the meta table, so it reports capability from the
ledger schema version.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from ..entity import ACCRUAL_CAPABILITY_VERSION, resolve_basis

DISCIPLINE_ALL = "all"
DISCIPLINE_PER_ACCOUNT = "per_account"
DISCIPLINE_OFF = "off"


def _config(source: Any) -> dict[str, Any]:
    return getattr(source, "entity_config", source)


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    keys = ("basis", "accounting_basis", "schema_version", "accrual_tracking_start")
    marks = ",".join("?" for _ in keys)
    return {k: v for k, v in conn.execute(f"SELECT key,value FROM meta WHERE key IN ({marks})", keys)}


def default_view(source: Any) -> str:
    """Return the default report view, "cash" or "accrual"."""
    if isinstance(source, sqlite3.Connection):
        values = _meta(source)
        return resolve_basis({k: v for k, v in values.items() if k in ("basis", "accounting_basis")})
    return resolve_basis(_config(source))


def capability(source: Any) -> int:
    """Return 1 when accrual features are available, otherwise 0."""
    if isinstance(source, sqlite3.Connection):
        try:
            return 1 if str(_meta(source).get("schema_version")) in {"2", "3"} else 0
        except sqlite3.Error:
            return 0
    resolve_basis(_config(source))
    return 1 if _config(source).get("accrual_capability_version") == ACCRUAL_CAPABILITY_VERSION else 0


def tracking_start(source: Any) -> str | None:
    """Return the ISO date from which per-account rules apply, or None."""
    if isinstance(source, sqlite3.Connection):
        try:
            value = _meta(source).get("accrual_tracking_start")
        except sqlite3.Error:
            return None
    else:
        value = _config(source).get("accrual_tracking_start")
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("accrual_tracking_start must be an ISO date (YYYY-MM-DD).")
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise ValueError("accrual_tracking_start must be an ISO date (YYYY-MM-DD).") from None
    return value


def discipline(source: Any) -> str:
    """Return where strict accrual recording rules apply (derived, never stored)."""
    if capability(source) != 1:
        return DISCIPLINE_ALL if default_view(source) == "accrual" else DISCIPLINE_OFF
    return DISCIPLINE_PER_ACCOUNT if tracking_start(source) else DISCIPLINE_ALL


def accrual_active(source: Any) -> bool:
    """True when strict accrual rules apply to at least some transactions."""
    return discipline(source) != DISCIPLINE_OFF


def require_write_scope(entity_or_config: Any) -> None:
    """Admit accrual record writes when capability is on, whatever the default view."""
    config = _config(entity_or_config)
    if capability(config) != 1:
        raise ValueError("Accrual writes require explicit accrual basis and capability version 1.")
