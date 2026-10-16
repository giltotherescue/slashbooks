"""Versioned supporting records committed with the canonical financial ledger.

Domain modules validate accounting facts before constructing these bounded
mutations. This layer controls table names, JSON shape, record versions and
optimistic revisions; it never accepts SQL or caller-provided callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Sequence

from ..ledger.store import ACCRUAL_SCHEMA_VERSION, LedgerStore

RECORD_VERSION = 1
TABLES = ("evidence", "intents", "schedules", "items", "allocations", "coverage", "periods")
MAX_MUTATIONS = 10000
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


def _table(table: str) -> str:
    if table not in TABLES:
        raise ValueError(f"Unsupported accrual record table: {table}")
    return f"accrual_{table}"


def table_ddl(table: str) -> str:
    return f"""CREATE TABLE {_table(table)} (
        id TEXT PRIMARY KEY,
        version INTEGER NOT NULL CHECK(version = 1),
        revision INTEGER NOT NULL CHECK(revision > 0),
        payload_json TEXT NOT NULL
    )"""


@dataclass(frozen=True)
class SupportingMutation:
    """Insert at revision zero, or replace the exact expected prior revision.

    The envelope owns identity/version/revision. Payload stays domain-shaped
    and is returned unchanged by load_record/list_records. No delete operation
    exists: cancellations and corrections remain inspectable records.
    """
    table: str
    record_id: str
    payload: dict[str, Any]
    expected_revision: int = 0
    version: int = RECORD_VERSION


def _require_schema(store: LedgerStore, conn: sqlite3.Connection) -> None:
    if store.schema_version(conn) != ACCRUAL_SCHEMA_VERSION:
        raise ValueError("Accrual records require an explicit ledger schema upgrade.")


def _record(row: sqlite3.Row) -> dict[str, Any]:
    if row["version"] != RECORD_VERSION:
        raise ValueError("Unsupported accrual record version; use a compatible engine.")
    value = json.loads(row["payload_json"])
    if not isinstance(value, dict):
        raise ValueError("Invalid accrual record payload.")
    return value


def load_record(store: LedgerStore, table: str, record_id: str,
                conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    name = _table(table)
    if conn is None:
        with store.connection() as tx:
            return load_record(store, table, record_id, tx)
    if store.schema_version(conn) != ACCRUAL_SCHEMA_VERSION:
        return None
    row = conn.execute(f"SELECT * FROM {name} WHERE id=?", (record_id,)).fetchone()
    return _record(row) if row else None


def record_revision(store: LedgerStore, table: str, record_id: str,
                    conn: sqlite3.Connection | None = None) -> int:
    name = _table(table)
    if conn is None:
        with store.connection() as tx:
            return record_revision(store, table, record_id, tx)
    if store.schema_version(conn) != ACCRUAL_SCHEMA_VERSION:
        return 0
    row = conn.execute(f"SELECT version, revision FROM {name} WHERE id=?", (record_id,)).fetchone()
    if row and row["version"] != RECORD_VERSION:
        raise ValueError("Unsupported accrual record version; use a compatible engine.")
    return int(row["revision"]) if row else 0


def list_records(store: LedgerStore, table: str,
                 conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    name = _table(table)
    if conn is None:
        with store.connection() as tx:
            return list_records(store, table, tx)
    if store.schema_version(conn) != ACCRUAL_SCHEMA_VERSION:
        return []
    return [_record(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY id").fetchall()]


def apply_mutations(store: LedgerStore, mutations: Sequence[SupportingMutation],
                    conn: sqlite3.Connection) -> None:
    """Apply validated envelopes in the caller's ledger transaction.

    Any conflict raises and rolls back the entire writer transaction, including
    entries and audit events. Replays are resolved by domain receipts before
    writing; replacing an existing identity with revision zero is a conflict.
    """
    _require_schema(store, conn)
    if not conn.in_transaction:
        raise ValueError("Supporting mutations require an active ledger transaction.")
    if len(mutations) > MAX_MUTATIONS:
        raise ValueError("Too many supporting mutations in one operation.")
    seen: set[tuple[str, str]] = set()
    prepared: list[tuple[SupportingMutation, str]] = []
    for mutation in mutations:
        if not isinstance(mutation, SupportingMutation):
            raise ValueError("Expected a typed supporting mutation.")
        _table(mutation.table)
        if not isinstance(mutation.record_id, str) or not mutation.record_id or len(mutation.record_id) > 512:
            raise ValueError("Supporting record identity is required and bounded to 512 characters.")
        if mutation.version != RECORD_VERSION or isinstance(mutation.version, bool):
            raise ValueError("Unsupported accrual record version.")
        if type(mutation.expected_revision) is not int or mutation.expected_revision < 0:
            raise ValueError("Supporting record revision must be a nonnegative integer.")
        identity = (mutation.table, mutation.record_id)
        if identity in seen:
            raise ValueError("Duplicate supporting mutation identity in one operation.")
        seen.add(identity)
        if not isinstance(mutation.payload, dict):
            raise ValueError("Supporting record payload must be a JSON object.")
        payload = json.dumps(mutation.payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("Supporting record payload is too large.")
        if mutation.table == "evidence" and mutation.expected_revision:
            raise ValueError("Evidence records are immutable; add a new identity.")
        if record_revision(store, mutation.table, mutation.record_id, conn) != mutation.expected_revision:
            raise ValueError(f"Stale supporting record revision: {mutation.table}/{mutation.record_id}")
        prepared.append((mutation, payload))
    for mutation, payload in prepared:
        name = _table(mutation.table)
        if mutation.expected_revision == 0:
            conn.execute(f"INSERT INTO {name} (id,version,revision,payload_json) VALUES (?,?,?,?)",
                         (mutation.record_id, mutation.version, 1, payload))
        else:
            conn.execute(f"UPDATE {name} SET payload_json=?,revision=revision+1 WHERE id=? AND revision=?",
                         (payload, mutation.record_id, mutation.expected_revision))


def operation_receipt(store: LedgerStore, operation_id: str) -> dict[str, Any] | None:
    """Return the committed intent record for an operation ID, or None. Read-only."""
    return load_record(store, "intents", "operation:" + operation_id)


class IntakeConflict(ValueError):
    """Same source revision stored earlier with different facts."""

    def __init__(self, existing: dict[str, Any], incoming: dict[str, Any], existing_id: str) -> None:
        self.existing_id = existing_id
        self.existing, self.incoming = existing, incoming
        source = incoming["source"]
        super().__init__(
            f"Source {source['namespace']}/{source['object_id']} version "
            f"{source.get('version', source.get('content_hash', ''))} is already stored (id {existing_id}) with different facts.")

    def as_dict(self) -> dict[str, Any]:
        return {"status": "blocked", "errors": [{
            "code": "intake_conflict", "message": str(self),
            "action": ("If the source document changed, intake it again with a new source.version (or source.content_hash). "
                       "If the earlier intake was wrong, stored supporting documents cannot be edited: intake the corrected facts under a new source.object_id, "
                       "then use `books accrual evidence-link preview` to tie it to the original operation. "
                       "Never reuse the same source revision with different facts."),
            "stored_id": self.existing_id, "stored_version": self.existing, "submitted_version": self.incoming}]}
