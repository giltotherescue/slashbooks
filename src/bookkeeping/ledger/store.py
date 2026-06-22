from __future__ import annotations

"""Canonical SQLite ledger store.

This module is intentionally small and model-shaped: callers insert and read
the existing ``Open``, ``Entry``, ``Posting``, and ``Balance`` objects.  That
keeps the first scalable-store slice compatible with the current Beancount
parser/writer while giving future import paths an indexed, transactional write
surface.
"""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Sequence

from .model import Balance, Entry, Open, Posting

SCHEMA_VERSION = 1
STORE_FILENAME = "ledger.sqlite"


_DDL = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    currency TEXT NOT NULL DEFAULT 'USD',
    open_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    flag TEXT NOT NULL DEFAULT '*',
    payee TEXT,
    narration TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    links_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '[]',
    source_id TEXT,
    session TEXT,
    late_arrival INTEGER NOT NULL DEFAULT 0
);

DROP INDEX IF EXISTS entries_source_id_unique;

CREATE INDEX IF NOT EXISTS entries_source_id_idx
    ON entries(source_id)
    WHERE source_id IS NOT NULL AND source_id != '';

CREATE INDEX IF NOT EXISTS entries_date_idx
    ON entries(date, id);

CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    account TEXT NOT NULL,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    metadata_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS postings_entry_idx
    ON postings(entry_id, id);

CREATE INDEX IF NOT EXISTS postings_account_idx
    ON postings(account);

CREATE TABLE IF NOT EXISTS balance_assertions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    account TEXT NOT NULL,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD'
);

CREATE TABLE IF NOT EXISTS source_transactions (
    id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'posted',
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    ts TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL
);
"""

_GENESIS_PREV = "0" * 64


@dataclass(frozen=True)
class StoreCounts:
    accounts: int = 0
    entries: int = 0
    postings: int = 0
    balances: int = 0
    audit_events: int = 0


def default_store_path(entity_path: Path | str) -> Path:
    return Path(entity_path) / STORE_FILENAME


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _loads_pairs(text: str) -> tuple[tuple[str, str], ...]:
    raw = json.loads(text or "[]")
    return tuple((str(k), str(v)) for k, v in raw)


def _loads_strings(text: str) -> tuple[str, ...]:
    raw = json.loads(text or "[]")
    return tuple(str(v) for v in raw)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_record(record_type: str, ts: str, payload_json: str, prev_hash: str) -> str:
    line = _json({
        "payload": json.loads(payload_json),
        "prev": prev_hash,
        "ts": ts,
        "type": record_type,
    })
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


class LedgerStore:
    """SQLite-backed canonical ledger store."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(_DDL)
            for sql in (
                "ALTER TABLE entries ADD COLUMN session TEXT",
                "ALTER TABLE entries ADD COLUMN late_arrival INTEGER NOT NULL DEFAULT 0",
            ):
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def counts(self) -> StoreCounts:
        with self.connection() as conn:
            return StoreCounts(
                accounts=conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
                entries=conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
                postings=conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0],
                balances=conn.execute("SELECT COUNT(*) FROM balance_assertions").fetchone()[0],
                audit_events=conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
            )

    def set_meta(self, key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            with self.transaction() as tx:
                self.set_meta(key, value, tx)
            return
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row else None

    def insert_opens(self, opens: Sequence[Open], conn: sqlite3.Connection) -> None:
        for item in opens:
            conn.execute(
                """INSERT INTO accounts (name, currency, open_date)
                   VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     currency = excluded.currency,
                     open_date = CASE
                       WHEN excluded.open_date < accounts.open_date THEN excluded.open_date
                       ELSE accounts.open_date
                     END""",
                (
                    item.account,
                    item.currencies[0] if item.currencies else "USD",
                    item.date.isoformat(),
                ),
            )

    def insert_balances(self, balances: Sequence[Balance], conn: sqlite3.Connection) -> None:
        for item in balances:
            conn.execute(
                """INSERT INTO balance_assertions (date, account, amount, currency)
                   VALUES (?, ?, ?, ?)""",
                (item.date.isoformat(), item.account, str(item.amount), item.currency),
            )

    def insert_entries(self, entries: Sequence[Entry], conn: sqlite3.Connection) -> None:
        for entry in entries:
            metadata = dict(entry.meta)
            cur = conn.execute(
                """INSERT INTO entries
                   (date, flag, payee, narration, tags_json, links_json, metadata_json, source_id,
                    session, late_arrival)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.date.isoformat(),
                    entry.flag,
                    entry.payee,
                    entry.narration,
                    _json(list(entry.tags)),
                    _json(list(entry.links)),
                    _json(list(entry.meta)),
                    entry.source_id,
                    metadata.get("import-session"),
                    1 if metadata.get("late-arrival") == "true" else 0,
                ),
            )
            entry_id = int(cur.lastrowid)
            for posting in entry.postings:
                conn.execute(
                    """INSERT INTO postings
                       (entry_id, account, amount, currency, metadata_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        entry_id,
                        posting.account,
                        str(posting.amount),
                        posting.currency,
                        _json(list(posting.meta)),
                    ),
                )

    def insert_source_transactions(
        self,
        txns: Sequence[dict[str, Any]],
        conn: sqlite3.Connection,
        *,
        status: str = "posted",
        imported_at: str | None = None,
    ) -> None:
        event_ts = imported_at or _utc_now()
        for txn in txns:
            source_id = str(txn.get("id") or "")
            if not source_id:
                continue
            conn.execute(
                """INSERT INTO source_transactions (id, payload_json, status, imported_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     payload_json = excluded.payload_json,
                     status = excluded.status,
                     imported_at = excluded.imported_at""",
                (source_id, _json(txn), status, event_ts),
            )

    def source_exists(self, source_id: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM entries WHERE source_id = ? LIMIT 1",
                (source_id,),
            ).fetchone()
            return row is not None

    def append_audit_event(
        self,
        record_type: str,
        payload: dict[str, Any],
        conn: sqlite3.Connection,
        *,
        ts: str | None = None,
    ) -> str:
        event_ts = ts or _utc_now()
        payload_json = _json(payload)
        row = conn.execute(
            "SELECT record_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev_hash = str(row[0]) if row else _GENESIS_PREV
        record_hash = _hash_record(record_type, event_ts, payload_json, prev_hash)
        conn.execute(
            """INSERT INTO audit_events (type, ts, payload_json, prev_hash, record_hash)
               VALUES (?, ?, ?, ?, ?)""",
            (record_type, event_ts, payload_json, prev_hash, record_hash),
        )
        return record_hash

    def verify_audit_chain(self) -> list[str]:
        errors: list[str] = []
        prev = _GENESIS_PREV
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id, type, ts, payload_json, prev_hash, record_hash FROM audit_events ORDER BY id"
            ).fetchall()
        for row in rows:
            if row["prev_hash"] != prev:
                errors.append(
                    f"Audit event {row['id']}: expected prev={prev}, got {row['prev_hash']}"
                )
            expected = _hash_record(row["type"], row["ts"], row["payload_json"], row["prev_hash"])
            if row["record_hash"] != expected:
                errors.append(f"Audit event {row['id']}: record hash mismatch")
            prev = row["record_hash"]
        return errors

    def load_opens(self, conn: sqlite3.Connection | None = None) -> list[Open]:
        if conn is None:
            with self.connection() as tx:
                return self.load_opens(tx)
        rows = conn.execute(
            "SELECT name, currency, open_date FROM accounts ORDER BY open_date, name"
        ).fetchall()
        return [
            Open(
                date=date.fromisoformat(row["open_date"]),
                account=row["name"],
                currencies=(row["currency"],) if row["currency"] else (),
            )
            for row in rows
        ]

    def load_balances(self, conn: sqlite3.Connection | None = None) -> list[Balance]:
        if conn is None:
            with self.connection() as tx:
                return self.load_balances(tx)
        rows = conn.execute(
            "SELECT date, account, amount, currency FROM balance_assertions ORDER BY date, id"
        ).fetchall()
        return [
            Balance(
                date=date.fromisoformat(row["date"]),
                account=row["account"],
                amount=Decimal(row["amount"]),
                currency=row["currency"],
            )
            for row in rows
        ]

    def load_entries(
        self,
        from_date: date | None = None,
        to_date: date | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[Entry]:
        clauses: list[str] = []
        params: list[str] = []
        if from_date is not None:
            clauses.append("date >= ?")
            params.append(from_date.isoformat())
        if to_date is not None:
            clauses.append("date <= ?")
            params.append(to_date.isoformat())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        if conn is None:
            with self.connection() as tx:
                return self.load_entries(from_date=from_date, to_date=to_date, conn=tx)
        entry_rows = conn.execute(
            f"""SELECT id, date, flag, payee, narration, tags_json, links_json, metadata_json
                FROM entries
                {where}
                ORDER BY date, id""",
            params,
        ).fetchall()
        entries: list[Entry] = []
        for row in entry_rows:
            posting_rows = conn.execute(
                """SELECT account, amount, currency, metadata_json
                   FROM postings
                   WHERE entry_id = ?
                   ORDER BY id""",
                (row["id"],),
            ).fetchall()
            postings = tuple(
                Posting(
                    account=p["account"],
                    amount=Decimal(p["amount"]),
                    currency=p["currency"],
                    meta=_loads_pairs(p["metadata_json"]),
                )
                for p in posting_rows
            )
            entries.append(
                Entry(
                    date=date.fromisoformat(row["date"]),
                    narration=row["narration"],
                    payee=row["payee"],
                    flag=row["flag"],
                    tags=_loads_strings(row["tags_json"]),
                    links=_loads_strings(row["links_json"]),
                    meta=_loads_pairs(row["metadata_json"]),
                    postings=postings,
                )
            )
        return entries

    def load_audit_events(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT id, type, ts, payload_json, prev_hash, record_hash
                   FROM audit_events
                   ORDER BY id"""
            ).fetchall()
        return [
            {
                "id": row["id"],
                "type": row["type"],
                "ts": row["ts"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "prev_hash": row["prev_hash"],
                "record_hash": row["record_hash"],
            }
            for row in rows
        ]


def replace_store_atomically(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(src), str(dest))
