"""Regenerable SQLite cache for Beancount snapshot inputs.

The canonical report path reads ``ledger.sqlite`` directly through
``open_cache`` when the store is present.  This module still supports deriving a
temporary/report cache from a Beancount snapshot for tests and import utilities.

Schema
------
Amounts are stored as TEXT decimal strings to preserve ``Decimal`` exactness
(e.g. ``"-1234.56"``).  This is explicitly documented here so every future
reader knows not to cast them to float.

Tables
~~~~~~
meta          (key TEXT PRIMARY KEY, value TEXT)
accounts      (name TEXT PRIMARY KEY, currency TEXT, open_date TEXT)
entries       (id INTEGER PRIMARY KEY, date TEXT, narration TEXT,
               payee TEXT, session TEXT, source_id TEXT, late_arrival INTEGER)
postings      (id INTEGER PRIMARY KEY, entry_id INTEGER REFERENCES entries(id),
               account TEXT, amount TEXT, currency TEXT)
balance_assertions (id INTEGER PRIMARY KEY, date TEXT, account TEXT,
                    amount TEXT, currency TEXT)

``meta`` rows:
  ledger_sha256  — SHA-256 hex of the ledger file bytes at regeneration time
  generated_at   — ISO-8601 UTC timestamp

Public API
----------
regenerate(entity_path, ledger_text=None) -> CacheResult
    Run the validator FIRST; on any error halt and return a failed CacheResult
    (previous cache.sqlite untouched).  On pass, build cache_new.sqlite then
    os.replace into cache.sqlite.

open_cache(entity_path) -> sqlite3.Connection (read-only)
    Opens the canonical ledger store when present, otherwise opens or
    regenerates cache.sqlite from a Beancount snapshot.

get_account_balance(conn, account, as_of_date) -> Decimal
    Sum of all postings to *account* with entry date <= *as_of_date*.

iter_postings(conn, account=None, from_date=None, to_date=None)
    Yield (date, narration, payee, account, amount, currency) rows.

is_stale(entity_path) -> bool
    True when cache.sqlite is absent or its ledger_sha256 doesn't match the
    current Beancount snapshot.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Generator, Iterator, Optional

from ..ledger.store import default_store_path
from ..ledger.validator import parse_ledger, validate


# ---------------------------------------------------------------------------
# CacheResult
# ---------------------------------------------------------------------------


@dataclass
class CacheResult:
    """Result of a ``regenerate()`` call."""

    success: bool
    ledger_sha256: str = ""
    validation_errors: list = field(default_factory=list)
    error_message: str = ""
    cache_path: str = ""

    def __bool__(self) -> bool:  # noqa: D105
        return self.success


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    name      TEXT PRIMARY KEY,
    currency  TEXT NOT NULL DEFAULT 'USD',
    open_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,
    narration    TEXT NOT NULL DEFAULT '',
    payee        TEXT,
    session      TEXT,
    source_id    TEXT,
    late_arrival INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS postings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id  INTEGER NOT NULL REFERENCES entries(id),
    account   TEXT NOT NULL,
    amount    TEXT NOT NULL,
    currency  TEXT NOT NULL DEFAULT 'USD'
);

CREATE TABLE IF NOT EXISTS balance_assertions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT NOT NULL,
    account  TEXT NOT NULL,
    amount   TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD'
);

CREATE INDEX IF NOT EXISTS postings_account_date
    ON postings(account, entry_id);
CREATE INDEX IF NOT EXISTS entries_date
    ON entries(date);
"""

# 30-day threshold for late-arrival flag
_LATE_ARRIVAL_DAYS = 30


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_path(entity_path: Path) -> Path:
    return entity_path / "reports" / "cache.sqlite"


def _ledger_path(entity_path: Path) -> Path:
    return entity_path / "books.beancount"


def _fallback_cache_dir(entity_path: Path) -> Path:
    """Return a deterministic writable cache directory outside the entity folder."""
    root = Path(os.environ.get("BOOKS_CACHE_DIR", Path(tempfile.gettempdir()) / "books-cache"))
    try:
        resolved = entity_path.resolve()
    except OSError:
        resolved = entity_path.absolute()
    key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    return root / key


def _fallback_cache_path(entity_path: Path) -> Path:
    return _fallback_cache_dir(entity_path) / "cache.sqlite"


def _cache_candidates(entity_path: Path) -> list[Path]:
    return [_cache_path(entity_path), _fallback_cache_path(entity_path)]


def _new_cache_path_for(cache_path: Path) -> Path:
    return cache_path.with_name("cache_new.sqlite")


def _cache_ledger_sha(cache: Path) -> str | None:
    if not cache.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(cache))
        row = conn.execute("SELECT value FROM meta WHERE key='ledger_sha256'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()
    return row[0] if row else None


def _store_meta(store_path: Path, key: str) -> str | None:
    if not store_path.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(store_path))
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()
    return str(row[0]) if row else None


def _store_is_current(entity_path: Path) -> bool:
    store_path = default_store_path(entity_path)
    if not store_path.exists():
        return False
    if _store_meta(store_path, "canonical") == "true":
        return True
    ledger = _ledger_path(entity_path)
    if not ledger.exists():
        return False
    return _store_meta(store_path, "source_ledger_sha256") == _sha256_of_file(ledger)


def _read_cache_path(entity_path: Path) -> Path:
    """Return the best cache path to read, preferring one current for the ledger."""
    ledger = _ledger_path(entity_path)
    current_sha = _sha256_of_file(ledger) if ledger.exists() else None
    fallback = _fallback_cache_path(entity_path)
    default = _cache_path(entity_path)

    if current_sha:
        for candidate in (default, fallback):
            if _cache_ledger_sha(candidate) == current_sha:
                return candidate

    if default.exists():
        return default
    if fallback.exists():
        return fallback
    return default


# ---------------------------------------------------------------------------
# Populate helper
# ---------------------------------------------------------------------------


def _populate(conn: sqlite3.Connection, parsed: dict, ledger_sha256: str) -> None:
    """Populate an already-DDL-initialised *conn* from *parsed* ledger data.

    *parsed* is the dict returned by ``parse_ledger``.
    Raises on any unexpected error; the caller handles cleanup.
    """
    opens = parsed["opens"]
    entries = parsed["entries"]
    balances = parsed["balances"]

    with conn:
        # --- meta -----------------------------------------------------------
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("ledger_sha256", ledger_sha256),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("generated_at", _utc_now_iso()),
        )

        # --- accounts -------------------------------------------------------
        for o in opens:
            conn.execute(
                "INSERT OR IGNORE INTO accounts (name, currency, open_date) VALUES (?, ?, ?)",
                (
                    o.account,
                    o.currencies[0] if o.currencies else "USD",
                    o.date.isoformat(),
                ),
            )

        # --- entries + postings ---------------------------------------------
        for entry in entries:
            # Extract metadata fields
            source_id: Optional[str] = None
            session: Optional[str] = None
            late_arrival = 0
            for key, val in entry.meta:
                if key == "source-id":
                    source_id = val
                elif key == "import-session":
                    session = val
                elif key == "late-arrival" and val.upper() == "TRUE":
                    late_arrival = 1

            cur = conn.execute(
                """INSERT INTO entries (date, narration, payee, session, source_id, late_arrival)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    entry.date.isoformat(),
                    entry.narration,
                    entry.payee,
                    session,
                    source_id,
                    late_arrival,
                ),
            )
            entry_id = cur.lastrowid

            for posting in entry.postings:
                conn.execute(
                    "INSERT INTO postings (entry_id, account, amount, currency) VALUES (?, ?, ?, ?)",
                    (entry_id, posting.account, str(posting.amount), posting.currency),
                )

        # --- balance assertions ---------------------------------------------
        for bal in balances:
            conn.execute(
                "INSERT INTO balance_assertions (date, account, amount, currency) VALUES (?, ?, ?, ?)",
                (bal.date.isoformat(), bal.account, str(bal.amount), bal.currency),
            )


def _build_cache_at(cache_path: Path, parsed: dict, ledger_sha256: str) -> None:
    """Build a fresh SQLite cache at *cache_path* and atomically replace it."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    new_path = _new_cache_path_for(cache_path)

    if new_path.exists():
        new_path.unlink()

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(new_path))
        conn.executescript(_DDL)
        _populate(conn, parsed, ledger_sha256)
        conn.close()
    except Exception:
        if conn is not None:
            conn.close()
        if new_path.exists():
            new_path.unlink()
        raise

    os.replace(str(new_path), str(cache_path))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def regenerate(entity_path: Path | str, ledger_text: str | None = None) -> CacheResult:
    """Regenerate the SQLite cache for *entity_path*.

    Steps:
    1. Read ledger text (from file unless *ledger_text* supplied).
    2. Run the validator; on any error return a failed CacheResult (previous
       cache.sqlite is untouched).
    3. Build ``cache_new.sqlite`` with full population.
    4. ``os.replace`` to ``cache.sqlite``.

    Returns a :class:`CacheResult`.
    """
    entity_path = Path(entity_path)
    reports_dir = entity_path / "reports"
    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    ledger_file = _ledger_path(entity_path)

    if ledger_text is None:
        if not ledger_file.exists():
            return CacheResult(
                success=False,
                error_message=f"Ledger file not found: {ledger_file}",
            )
        ledger_text = ledger_file.read_text(encoding="utf-8")

    # --- Gate 1: validation -------------------------------------------------
    errors = validate(ledger_text)
    if errors:
        return CacheResult(
            success=False,
            validation_errors=errors,
            error_message=(
                f"Ledger validation failed with {len(errors)} error(s); "
                "cache not regenerated."
            ),
        )

    sha256 = _sha256_of_text(ledger_text)

    # --- Gate 2: parse ------------------------------------------------------
    try:
        parsed = parse_ledger(ledger_text)
    except ValueError as exc:
        return CacheResult(
            success=False,
            error_message=f"Ledger parse error: {exc}",
        )

    # --- Build cache_new.sqlite --------------------------------------------
    errors: list[str] = []
    for dest in _cache_candidates(entity_path):
        try:
            _build_cache_at(dest, parsed, sha256)
            return CacheResult(success=True, ledger_sha256=sha256, cache_path=str(dest))
        except Exception as exc:
            errors.append(f"{dest}: {exc}")

    return CacheResult(
        success=False,
        error_message="Cache population failed: " + " | ".join(errors),
    )


def is_stale(entity_path: Path | str) -> bool:
    """Return True when cache.sqlite is absent or its sha256 doesn't match the ledger."""
    entity_path = Path(entity_path)
    ledger = _ledger_path(entity_path)

    if not any(candidate.exists() for candidate in _cache_candidates(entity_path)):
        return True
    if not ledger.exists():
        return False  # no ledger → nothing to compare

    current_sha = _sha256_of_file(ledger)
    return all(_cache_ledger_sha(candidate) != current_sha for candidate in _cache_candidates(entity_path))


def open_cache(entity_path: Path | str, *, auto_regenerate: bool = True) -> sqlite3.Connection:
    """Open (or regenerate and open) the cache for *entity_path*.

    When *auto_regenerate* is True (default) and the cache is absent or stale,
    regeneration is attempted.  Raises ``RuntimeError`` when regeneration fails.
    Returns a read-only connection (``PRAGMA query_only=ON``).
    """
    entity_path = Path(entity_path)

    store_path = default_store_path(entity_path)
    if _store_is_current(entity_path):
        conn = sqlite3.connect(str(store_path))
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        return conn

    if auto_regenerate and is_stale(entity_path):
        result = regenerate(entity_path)
        if not result:
            raise RuntimeError(
                f"Cache regeneration failed: {result.error_message}\n"
                + "\n".join(str(e) for e in result.validation_errors)
            )

    cache = _read_cache_path(entity_path)
    conn = sqlite3.connect(str(cache))
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Query helpers (read-only)
# ---------------------------------------------------------------------------


def get_account_balance(
    conn: sqlite3.Connection,
    account: str,
    as_of: date | str | None = None,
) -> Decimal:
    """Return the sum of all postings to *account* with entry date <= *as_of*.

    If *as_of* is None, sums all postings regardless of date.
    """
    if as_of is None:
        rows = conn.execute(
            "SELECT amount FROM postings p "
            "JOIN entries e ON e.id = p.entry_id "
            "WHERE p.account = ?",
            (account,),
        ).fetchall()
    else:
        as_of_str = as_of.isoformat() if isinstance(as_of, date) else as_of
        rows = conn.execute(
            "SELECT amount FROM postings p "
            "JOIN entries e ON e.id = p.entry_id "
            "WHERE p.account = ? AND e.date <= ?",
            (account, as_of_str),
        ).fetchall()

    return sum((Decimal(r[0]) for r in rows), Decimal("0.00"))


def iter_postings(
    conn: sqlite3.Connection,
    account: str | None = None,
    from_date: date | str | None = None,
    to_date: date | str | None = None,
) -> Iterator[tuple]:
    """Yield (date, narration, payee, account, amount, currency) tuples.

    All date boundaries are inclusive.
    """
    clauses: list[str] = []
    params: list = []

    if account is not None:
        clauses.append("p.account = ?")
        params.append(account)

    if from_date is not None:
        fd = from_date.isoformat() if isinstance(from_date, date) else from_date
        clauses.append("e.date >= ?")
        params.append(fd)

    if to_date is not None:
        td = to_date.isoformat() if isinstance(to_date, date) else to_date
        clauses.append("e.date <= ?")
        params.append(td)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT e.date, e.narration, e.payee, p.account, p.amount, p.currency
        FROM postings p
        JOIN entries e ON e.id = p.entry_id
        {where}
        ORDER BY e.date, e.id, p.id
    """
    for row in conn.execute(sql, params):
        yield (row[0], row[1], row[2], row[3], Decimal(row[4]), row[5])


def list_accounts(conn: sqlite3.Connection) -> list[dict]:
    """Return a list of account dicts from the cache."""
    rows = conn.execute(
        "SELECT name, currency, open_date FROM accounts ORDER BY name"
    ).fetchall()
    return [{"name": r[0], "currency": r[1], "open_date": r[2]} for r in rows]
