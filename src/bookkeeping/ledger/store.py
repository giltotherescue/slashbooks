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
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .model import Balance, Entry, Open, Posting

SCHEMA_VERSION = 1
# Cash stores retain their v1 digest until an explicit, backed-up upgrade.
ACCRUAL_SCHEMA_VERSION = 2
BUSINESS_SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION, ACCRUAL_SCHEMA_VERSION, BUSINESS_SCHEMA_VERSION})
FINANCIAL_KINDS = ("bank", "credit_card", "cash", "other")
_FINANCIAL_KIND_SCHEMA_SUFFIX = "+financial-kind"
FINANCIAL_KIND_COLUMN = (
    "financial_kind TEXT CHECK(financial_kind IN ('bank', 'credit_card', 'cash', 'other'))"
)


def parse_catalog_schema(value: str) -> tuple[int, bool]:
    """Parse the disk format and return the ledger capability version and catalog layout.

    The disk grammar is exactly 1, 2, 3, or one of those with +financial-kind.
    Older engines reject the tagged format before they can change the catalog.
    The base number still controls the cash, accrual and business tables.
    """
    version, tag, extension = value.partition("+")
    if version not in {str(v) for v in SUPPORTED_SCHEMA_VERSIONS} or (tag and extension != "financial-kind"):
        raise ValueError("Unsupported ledger schema; use a compatible engine and do not downgrade.")
    return int(version), bool(tag)


def validate_financial_kind(account: str, kind: str) -> None:
    """Check the explicit kind against the accounting root, independent of its label."""
    root = account.partition(":")[0]
    if kind not in FINANCIAL_KINDS:
        raise ValueError("Financial kind must be bank, credit_card, cash or other.")
    allowed_roots = {
        "bank": ("Assets",),
        "cash": ("Assets",),
        "credit_card": ("Liabilities",),
        "other": ("Assets", "Liabilities"),
    }
    if root not in allowed_roots[kind]:
        raise ValueError("Financial kind does not match the account's accounting type.")

STORE_FILENAME = "ledger.sqlite"

# The meta key of the open-date provenance record (``bookkeeping.books_from``). Meta is part of the content
# digest, so the audit seal covers the record, and an earlier engine reads and keeps it as an unknown key.
OPEN_DATE_PROVENANCE_KEY = "open_date_provenance"
OPEN_DATE_SOURCES = frozenset({"default", "scope", "cutover", "user", "import", "opening", "legacy"})

# The meta key of an unfinished account rename (``bookkeeping.account_rename``). The rename writes it in the same
# transaction as the ledger change and removes it, with a sealed event, after every company file names the new
# account. While it exists, no writer may name an old account: company files can still name it, and a write would
# create the old account again and split its balance.
RENAME_PENDING_KEY = "account_rename_pending"


def refuse_rename_pending(entity_path: Path | str, accounts: Any) -> None:
    """``check_rename_pending`` for a writer of company files (a reconciliation, a mapping, a statement record)."""
    path = default_store_path(entity_path)
    if not path.is_file():
        return
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        try:
            check_rename_pending(conn, list(accounts))
        except sqlite3.OperationalError:
            return  # No meta table: no rename.


def check_rename_pending(conn: sqlite3.Connection, accounts: Any) -> None:
    """Refuse a write that names an account of an unfinished rename. ``accounts`` is an iterable of names."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (RENAME_PENDING_KEY,)).fetchone()
    if row is None:
        return
    try:
        record = json.loads(str(row[0]))
        old, new = str(record["from"]), str(record["to"])
    except (ValueError, KeyError, TypeError):
        raise ValueError("The record of an unfinished account rename is not valid; nothing was written.") from None
    for account in accounts:
        if isinstance(account, str) and (account == old or account.startswith(old + ":")):
            raise ValueError(
                f"The rename of {old} to {new} is not finished, so nothing can be written to {account}. Finish it "
                f"with: books entity account-rename PATH --account {old} --to {new} --apply. Then use {new}.")


def _iso_day(value: Any) -> bool:
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _check_provenance(record: Any) -> None:
    """Refuse a provenance record of the wrong shape."""
    valid = (isinstance(record, dict)
             and set(record) == {"version", "books_from", "books_from_source", "accounts", "catalog_sha256"}
             and record["version"] == 1 and isinstance(record["accounts"], dict)
             and isinstance(record["catalog_sha256"], str)
             and (record["books_from"] is None or _iso_day(record["books_from"]))
             and record["books_from_source"] in ((None,) if record["books_from"] is None else ("scope", "cutover")))
    if valid:
        for name, marker in record["accounts"].items():
            if (not isinstance(name, str) or not isinstance(marker, dict) or set(marker) != {"source", "open_date"}
                    or marker["source"] not in OPEN_DATE_SOURCES or not _iso_day(marker["open_date"])):
                valid = False
                break
    if not valid:
        raise ValueError("The open-date provenance record of the ledger is not valid.")


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

CREATE UNIQUE INDEX IF NOT EXISTS entries_source_id_unique
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

CREATE INDEX IF NOT EXISTS audit_events_type_idx
    ON audit_events(type, id);
"""

_GENESIS_PREV = "0" * 64


def _key_guards() -> dict[str, str]:
    """Triggers that keep the row id of each posted table above every earlier id.

    ``INSERT OR REPLACE`` and ``REPLACE INTO`` delete the row that they replace without firing a DELETE trigger
    (``recursive_triggers`` is off by default and is a setting of each connection). So a BEFORE INSERT trigger
    refuses an insert whose id, or whose ``source_id`` for an entry, is already in the table. An AFTER INSERT
    trigger refuses an id below 1 and an id that is not above every other id. SQLite gives an unset id the value
    -1 in a BEFORE trigger, so the first rule cannot tell an unset id from a real one: the second rule covers it.
    With both rules, a row that is added always has a higher id than every row that exists, and the incremental
    checks, which read the ids above a saved position, cannot skip it.
    """
    guards = {}
    for table in ("entries", "postings", "balance_assertions", "audit_events"):
        duplicate = f"EXISTS (SELECT 1 FROM {table} WHERE id = NEW.id)"
        if table == "entries":
            duplicate += (" OR (NEW.source_id IS NOT NULL AND NEW.source_id != '' "
                          "AND EXISTS (SELECT 1 FROM entries WHERE source_id = NEW.source_id))")
        guards[f"{table}_append_only_no_overwrite"] = (
            f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_no_overwrite BEFORE INSERT ON {table} "
            f"WHEN {duplicate} BEGIN SELECT RAISE(ABORT, '{table} rows are append-only: no replace'); END"
        )
        guards[f"{table}_append_only_ascending"] = (
            f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_ascending AFTER INSERT ON {table} "
            f"WHEN NEW.id < 1 OR EXISTS (SELECT 1 FROM {table} WHERE id > NEW.id) "
            f"BEGIN SELECT RAISE(ABORT, '{table} rows are append-only: ids must rise'); END"
        )
    return guards


def _append_only_guard_ddl() -> dict[str, str]:
    """Return the engine's append-only triggers, keyed by trigger name.

    The database refuses a rewrite of posted rows, so a host does not need to compare the whole ledger before and
    after a write to find one. The legitimate paths stay open:

    * ``books entity account-rename`` may change ``postings.account`` and ``balance_assertions.account`` while its
      pending-rename record is in ``meta``. It changes no other column.
    * ``_discard_last_audit_event`` may delete the newest ``feed-intake`` event.
    * A correction writes a reversing entry and a replacement entry. It never edits the original entry.

    A new audit event must name the current newest record hash as ``prev_hash``, so a link can only be appended
    to the head of the chain.
    """
    pending = f"EXISTS (SELECT 1 FROM meta WHERE key = '{RENAME_PENDING_KEY}')"
    refuse = "SELECT RAISE(ABORT, '{}');"
    return {
        "entries_append_only_update": (
            "CREATE TRIGGER IF NOT EXISTS entries_append_only_update BEFORE UPDATE ON entries "
            "BEGIN " + refuse.format("posted entries are append-only") + " END"
        ),
        "entries_append_only_delete": (
            "CREATE TRIGGER IF NOT EXISTS entries_append_only_delete BEFORE DELETE ON entries "
            "BEGIN " + refuse.format("posted entries are append-only") + " END"
        ),
        "postings_append_only_update": (
            "CREATE TRIGGER IF NOT EXISTS postings_append_only_update BEFORE UPDATE ON postings "
            "WHEN OLD.id IS NOT NEW.id OR OLD.entry_id IS NOT NEW.entry_id OR OLD.amount IS NOT NEW.amount "
            "OR OLD.currency IS NOT NEW.currency OR OLD.metadata_json IS NOT NEW.metadata_json "
            f"OR (OLD.account IS NOT NEW.account AND NOT {pending}) "
            "BEGIN " + refuse.format("posted postings are append-only") + " END"
        ),
        "postings_append_only_delete": (
            "CREATE TRIGGER IF NOT EXISTS postings_append_only_delete BEFORE DELETE ON postings "
            "BEGIN " + refuse.format("posted postings are append-only") + " END"
        ),
        "balance_assertions_append_only_update": (
            "CREATE TRIGGER IF NOT EXISTS balance_assertions_append_only_update BEFORE UPDATE ON balance_assertions "
            "WHEN OLD.id IS NOT NEW.id OR OLD.date IS NOT NEW.date OR OLD.amount IS NOT NEW.amount "
            "OR OLD.currency IS NOT NEW.currency "
            f"OR (OLD.account IS NOT NEW.account AND NOT {pending}) "
            "BEGIN " + refuse.format("balance assertions are append-only") + " END"
        ),
        "balance_assertions_append_only_delete": (
            "CREATE TRIGGER IF NOT EXISTS balance_assertions_append_only_delete BEFORE DELETE ON balance_assertions "
            "BEGIN " + refuse.format("balance assertions are append-only") + " END"
        ),
        "audit_events_append_only_update": (
            "CREATE TRIGGER IF NOT EXISTS audit_events_append_only_update BEFORE UPDATE ON audit_events "
            "BEGIN " + refuse.format("audit events are append-only") + " END"
        ),
        "audit_events_append_only_delete": (
            "CREATE TRIGGER IF NOT EXISTS audit_events_append_only_delete BEFORE DELETE ON audit_events "
            f"WHEN NOT (OLD.type = '{_DISCARDABLE_AUDIT_TYPE}' AND OLD.id = (SELECT MAX(id) FROM audit_events)) "
            "BEGIN " + refuse.format("audit events are append-only") + " END"
        ),
        **_key_guards(),
        "audit_events_chain_head": (
            "CREATE TRIGGER IF NOT EXISTS audit_events_chain_head BEFORE INSERT ON audit_events "
            "WHEN NEW.prev_hash IS NOT COALESCE("
            f"(SELECT record_hash FROM audit_events ORDER BY id DESC LIMIT 1), '{_GENESIS_PREV}') "
            "BEGIN " + refuse.format("an audit event must extend the newest record") + " END"
        ),
    }


def install_append_only_guard(conn: sqlite3.Connection) -> None:
    """Install the append-only triggers. Safe to repeat; it changes no row, no content digest and no seal."""
    for sql in _append_only_guard_ddl().values():
        conn.execute(sql)


def append_only_guard_problems(conn: sqlite3.Connection) -> list[str]:
    """Return what is missing or changed in the append-only triggers. Cost: a fixed number of catalog rows."""
    found = {
        str(row[0]): str(row[1])
        for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
    }
    problems = []
    for name, sql in _append_only_guard_ddl().items():
        expected = sql.replace(" IF NOT EXISTS", "", 1)
        if name not in found:
            problems.append(f"Append-only trigger {name} is missing.")
        elif " ".join(found[name].split()) != " ".join(expected.split()):
            problems.append(f"Append-only trigger {name} was changed.")
    return problems


@dataclass(frozen=True)
class StoreCounts:
    accounts: int = 0
    entries: int = 0
    postings: int = 0
    balances: int = 0
    audit_events: int = 0


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    metadata: tuple[tuple[str, str], ...] = ()


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


#: The one audit event type that ``_discard_last_audit_event`` may remove (``source_ids.FEED_INTAKE_EVENT``).
_DISCARDABLE_AUDIT_TYPE = "feed-intake"



#: Audit event types that are in the hash chain but are not ledger writes: no ledger table changes, and no seal
#: follows. ``feed-intake`` records which provider sent which rows (``source_ids.FEED_INTAKE_EVENT``).
#: ``store-migrated`` marks the point where books came from another store (``source_ids.MIGRATED_EVENT``).
NON_LEDGER_EVENT_TYPES = frozenset({"feed-intake", "store-migrated"})


def has_ledger_audit_history(conn: sqlite3.Connection) -> bool:
    """Whether the store has an audit event of a ledger write or change.

    A ``feed-intake`` or ``store-migrated`` event is not ledger history, so it does not make a ledger with entries
    look as if its entries have a history.
    """
    marks = ",".join("?" for _ in NON_LEDGER_EVENT_TYPES)
    return conn.execute(f"SELECT 1 FROM audit_events WHERE type NOT IN ({marks}) LIMIT 1",
                        tuple(sorted(NON_LEDGER_EVENT_TYPES))).fetchone() is not None


@dataclass(frozen=True)
class AuditHead:
    """A position in the audit chain: the event id and its record hash."""

    position: int
    record_hash: str


@dataclass(frozen=True)
class AuditChainCheck:
    """Result of an incremental chain check. ``head`` is the new verified head, or ``None`` after an error."""

    errors: list[str]
    head: AuditHead | None


@dataclass(frozen=True)
class PostingCheck:
    """Result of ``check_new_postings``. ``watermark`` is the next checkpoint, or ``None`` after an error."""

    errors: list[str]
    watermark: int | None


def _verify_links(rows: Any, prev: str) -> list[str]:
    errors: list[str] = []
    for row in rows:
        if row["prev_hash"] != prev:
            errors.append(f"Audit event {row['id']}: expected prev={prev}, got {row['prev_hash']}")
        expected = _hash_record(row["type"], row["ts"], row["payload_json"], row["prev_hash"])
        if row["record_hash"] != expected:
            errors.append(f"Audit event {row['id']}: record hash mismatch")
        prev = row["record_hash"]
    return errors


class LedgerStore:
    """SQLite-backed canonical ledger store."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            self.schema_version(conn)
        except Exception:
            conn.close()
            raise
        return conn

    def schema_version(self, conn: sqlite3.Connection | None = None) -> int | None:
        """Admit known formats before any read or write can reinterpret data."""
        if conn is None:
            with self.connection() as tx:
                return self.schema_version(tx)
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta' AND type='table'").fetchone():
            return None
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version, typed = parse_catalog_schema(str(row[0]) if row else "")
        columns = {column[1] for column in conn.execute("PRAGMA table_info(accounts)")}
        if typed != ("financial_kind" in columns):
            raise ValueError("Account catalog layout does not match its ledger schema.")
        if version in {ACCRUAL_SCHEMA_VERSION, BUSINESS_SCHEMA_VERSION}:
            from ..accrual.store import RECORD_VERSION, TABLES
            for table in TABLES:
                if conn.execute(f"SELECT 1 FROM accrual_{table} WHERE version<>? LIMIT 1", (RECORD_VERSION,)).fetchone():
                    raise ValueError("Unsupported accrual record version; use a compatible engine.")
        if version == BUSINESS_SCHEMA_VERSION:
            from ..business import BUSINESS_TABLES
            for table in BUSINESS_TABLES:
                if conn.execute(f"SELECT 1 FROM business_{table} WHERE version<>1 LIMIT 1").fetchone():
                    raise ValueError("Unsupported business record version; use a compatible engine.")
        return version

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            if self.schema_version(conn) is not None:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
                if not {"session", "late_arrival"}.issubset(columns):
                    self._repair_legacy_columns()
                # An index changes no content and no seal. An older store gets it the first time it is opened for
                # writing; a store that cannot be written (read only) keeps working without it.
                try:
                    conn.execute("CREATE INDEX IF NOT EXISTS audit_events_type_idx ON audit_events(type, id)")
                    install_append_only_guard(conn)
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
                return
            conn.executescript(_DDL)
            install_append_only_guard(conn)
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

    def _backup(self, suffix: str) -> Path:
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{self.path.name}.{suffix}.{uuid.uuid4().hex}.sqlite"
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        # Another source connection reads the committed state while the caller
        # holds a writer reservation. Both connections must close explicitly.
        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        return backup

    def _repair_legacy_columns(self) -> None:
        """Keep old cash layouts readable without silently discarding a seal."""
        from .importer import _entity_write_lock, check_store_integrity
        with _entity_write_lock(self.path.parent), self.transaction(immediate=True) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
            missing = {"session", "late_arrival"} - columns
            if not missing:
                return
            integrity = check_store_integrity(self)
            if integrity.status != "ok":
                raise ValueError(f"Cannot repair legacy ledger: {integrity.message}")
            if conn.execute("SELECT 1 FROM entries LIMIT 1").fetchone() and not has_ledger_audit_history(conn):
                raise ValueError("Cannot repair populated legacy ledger without sealed audit history.")
            old_digest = self.content_digest(conn)
            backup = self._backup("legacy-v1")
            if "session" in missing:
                conn.execute("ALTER TABLE entries ADD COLUMN session TEXT")
            if "late_arrival" in missing:
                conn.execute("ALTER TABLE entries ADD COLUMN late_arrival INTEGER NOT NULL DEFAULT 0")
            self.append_audit_event("schema-repaired", {
                "schema_version": SCHEMA_VERSION, "columns": sorted(missing),
                "prior_store_sha256": old_digest, "backup": backup.name,
            }, conn)
            self.append_audit_event("ledger-store-sealed", {
                "session_id": "schema-repair", "entries": 0, "store_sha256": self.content_digest(conn),
            }, conn)

    def upgrade(self) -> Path | None:
        """Explicitly upgrade v1 to v2, preserving the old sealed backup.

        The caller must keep using a compatible reader/writer after upgrade.
        All schema, audit and seal changes commit together; a failed upgrade
        leaves the old database intact and the backup available for inspection.
        """
        from ..accrual.store import TABLES, table_ddl
        from .importer import _entity_write_lock, check_store_integrity

        with _entity_write_lock(self.path.parent):
            self.initialize()
            with self.transaction(immediate=True) as conn:
                if self.schema_version(conn) in {ACCRUAL_SCHEMA_VERSION, BUSINESS_SCHEMA_VERSION}:
                    return None
                integrity = check_store_integrity(self)
                if integrity.status != "ok":
                    raise ValueError(f"Cannot upgrade ledger: {integrity.message}")
                if conn.execute("SELECT 1 FROM entries LIMIT 1").fetchone() and not has_ledger_audit_history(conn):
                    raise ValueError("Cannot upgrade populated ledger without sealed audit history.")
                old_digest = self.content_digest(conn)
                backup = self._backup("schema-v1")
                for table in TABLES:
                    conn.execute(table_ddl(table))
                self.set_meta("schema_version", str(ACCRUAL_SCHEMA_VERSION), conn)
                self.append_audit_event("schema-upgraded", {
                    "from_version": SCHEMA_VERSION, "to_version": ACCRUAL_SCHEMA_VERSION,
                    "prior_store_sha256": old_digest, "backup": backup.name,
                }, conn)
                self.append_audit_event("ledger-store-sealed", {
                    "session_id": "schema-upgrade", "entries": 0,
                    "store_sha256": self.content_digest(conn),
                }, conn)
            verified = check_store_integrity(self)
            if verified.status != "ok":
                raise ValueError(f"Upgraded ledger failed verification: {verified.message}")
            return backup

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
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
        if key == "schema_version" and self.has_financial_kinds(conn):
            value = value.removesuffix(_FINANCIAL_KIND_SCHEMA_SUFFIX) + _FINANCIAL_KIND_SCHEMA_SUFFIX
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row else None

    def catalog_fingerprint(self, conn: sqlite3.Connection) -> str:
        """A hash of each catalog row's id, name and open date.

        The id is an AUTOINCREMENT key, so an account that is removed and added again gets a new id and a new
        fingerprint, also on the same date.
        """
        rows = [list(row) for row in conn.execute("SELECT id, name, open_date FROM accounts ORDER BY id")]
        return hashlib.sha256(_json(rows).encode("utf-8")).hexdigest()

    def load_open_date_provenance(self, conn: sqlite3.Connection) -> dict[str, Any] | None:
        """The open-date provenance record (``bookkeeping.books_from``), or ``None`` for a ledger made before it.

        A record that is not valid refuses, so a damaged record is never read as evidence. Every writer of this
        engine saves the record with the catalog fingerprint in the same transaction as its catalog change. A
        record whose fingerprint differs from the catalog shows a catalog change by a writer that does not keep
        the record (an earlier engine, or a direct edit). Then no marker is evidence: the loaded record has
        ``stale: true`` and every marker has the source ``legacy``. The next save keeps that downgrade.
        """
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (OPEN_DATE_PROVENANCE_KEY,)).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(str(row[0]))
        except ValueError:
            record = None
        _check_provenance(record)
        if record["catalog_sha256"] != self.catalog_fingerprint(conn):
            record["accounts"] = {name: {"source": "legacy", "open_date": marker["open_date"]}
                                  for name, marker in record["accounts"].items()}
            record["stale"] = True
        return record

    def save_open_date_provenance(self, record: dict[str, Any], conn: sqlite3.Connection) -> None:
        """Save ``record`` with the fingerprint of the catalog as this transaction sees it now."""
        record.pop("stale", None)
        record["catalog_sha256"] = self.catalog_fingerprint(conn)
        _check_provenance(record)
        self.set_meta(OPEN_DATE_PROVENANCE_KEY, _json(record), conn)

    def delete_accounts(self, accounts: Sequence[str], conn: sqlite3.Connection) -> None:
        """Remove accounts from the catalog and their provenance markers, in the caller's transaction."""
        record = self.load_open_date_provenance(conn)
        for name in accounts:
            conn.execute("DELETE FROM accounts WHERE name = ?", (name,))
            if record is not None:
                record["accounts"].pop(name, None)
        if record is not None:
            self.save_open_date_provenance(record, conn)

    def insert_opens(self, opens: Sequence[Open], conn: sqlite3.Connection,
                     provenance: str | Mapping[str, str] | None = "import") -> None:
        """Insert or update catalog accounts. An existing account keeps the earlier of its two open dates.

        ``provenance`` is the source of the stated open dates: one source for every account, or a source for
        each account name. An account whose open date this call sets (a new account, an earlier date, or the
        same date) gets that marker. ``None`` changes no marker; use it only for a call that does not state an
        open date. A ledger made before the provenance record gets one at its first call with a source: the
        accounts already in the catalog get ``legacy``. So from this engine version on, every stated date has
        a marker.
        """
        check_rename_pending(conn, (item.account for item in opens))
        record = self.load_open_date_provenance(conn)
        if record is None and provenance is not None and opens:
            record = {"version": 1, "books_from": None, "books_from_source": None, "catalog_sha256": "",
                      "accounts": {str(name): {"source": "legacy", "open_date": str(opened)[:10]}
                                   for name, opened in conn.execute("SELECT name, open_date FROM accounts")}}
        for item in opens:
            before = conn.execute("SELECT open_date FROM accounts WHERE name = ?", (item.account,)).fetchone() \
                if record is not None else None
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
                    item.currencies[0] if item.currencies else self.book_currency(conn),
                    item.date.isoformat(),
                ),
            )
            if record is not None and provenance is not None:
                day = item.date.isoformat()
                if before is None or day <= str(before[0])[:10]:
                    source = provenance.get(item.account, "import") if isinstance(provenance, Mapping) else provenance
                    record["accounts"][item.account] = {"source": str(source), "open_date": day}
        if record is not None:
            self.save_open_date_provenance(record, conn)

    def has_financial_kinds(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return row is not None and parse_catalog_schema(str(row[0]))[1]

    def load_financial_kinds(self, conn: sqlite3.Connection | None = None) -> dict[str, str]:
        """Read only explicit classifications. Missing keys are unclassified."""
        if conn is None:
            with self.connection() as tx:
                return self.load_financial_kinds(tx)
        if not self.has_financial_kinds(conn):
            return {}
        kinds = dict(conn.execute("SELECT name, financial_kind FROM accounts WHERE financial_kind IS NOT NULL"))
        for account, kind in kinds.items():
            validate_financial_kind(account, kind)
        return kinds

    def assign_financial_kinds(self, kinds: dict[str, str], conn: sqlite3.Connection) -> None:
        """Assign previously unclassified accounts in a caller's audited transaction."""
        if not kinds:
            return
        check_rename_pending(conn, kinds)
        current = self.load_financial_kinds(conn)
        for account, kind in kinds.items():
            validate_financial_kind(account, kind)
            if account in current and current[account] != kind:
                raise ValueError(f"Account '{account}' already has a different financial kind.")
        if not self.has_financial_kinds(conn):
            version = self.schema_version(conn)
            if version is None:
                raise ValueError("Financial kinds require an initialized account catalog.")
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {FINANCIAL_KIND_COLUMN}")
            self.set_meta("schema_version", str(version) + _FINANCIAL_KIND_SCHEMA_SUFFIX, conn)
        for account, kind in kinds.items():
            if current.get(account) == kind:
                continue
            result = conn.execute("UPDATE accounts SET financial_kind=? WHERE name=?", (kind, account))
            if result.rowcount != 1:
                raise ValueError(f"Account '{account}' is not in the account catalog.")

    def book_currency(self, conn: sqlite3.Connection) -> str:
        """The company currency recorded in meta (absent means USD, as before)."""
        row = conn.execute("SELECT value FROM meta WHERE key='currency'").fetchone()
        return row[0] if row else "USD"

    def load_account_names(self, conn: sqlite3.Connection | None = None) -> set[str]:
        """Return account names defined in the canonical account catalog."""
        if conn is None:
            with self.connection() as tx:
                return self.load_account_names(tx)
        rows = conn.execute("SELECT name FROM accounts ORDER BY name").fetchall()
        return {str(row["name"]) for row in rows}

    def insert_balances(self, balances: Sequence[Balance], conn: sqlite3.Connection) -> None:
        check_rename_pending(conn, (item.account for item in balances))
        for item in balances:
            conn.execute(
                """INSERT INTO balance_assertions (date, account, amount, currency)
                   VALUES (?, ?, ?, ?)""",
                (item.date.isoformat(), item.account, str(item.amount), item.currency),
            )

    def insert_entries(self, entries: Sequence[Entry], conn: sqlite3.Connection) -> None:
        check_rename_pending(conn, (posting.account for entry in entries for posting in entry.postings))
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
                """SELECT 1 FROM entries WHERE source_id = ?
                   UNION ALL
                   SELECT 1 FROM source_transactions WHERE id = ?
                   LIMIT 1""",
                (source_id, source_id),
            ).fetchone()
            return row is not None

    def load_source_refs(self) -> list[SourceRef]:
        """Return source IDs and entry metadata without hydrating postings."""
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT source_id, metadata_json
                   FROM entries
                   WHERE source_id IS NOT NULL AND source_id != ''
                   UNION ALL
                   SELECT id AS source_id, '[]' AS metadata_json
                   FROM source_transactions
                   WHERE id NOT IN (
                     SELECT source_id FROM entries
                     WHERE source_id IS NOT NULL AND source_id != ''
                   )
                   ORDER BY source_id"""
            ).fetchall()
        return [
            SourceRef(str(row["source_id"]), _loads_pairs(row["metadata_json"]))
            for row in rows
        ]

    def content_digest(self, conn: sqlite3.Connection | None = None) -> str:
        """Return a deterministic digest of canonical ledger tables."""
        if conn is None:
            with self.connection() as tx:
                return self.content_digest(tx)
        payload: dict[str, list[dict[str, Any]]] = {}
        queries = {
            "meta": "SELECT key, value FROM meta ORDER BY key",
            "accounts": "SELECT name, currency, open_date FROM accounts ORDER BY name",
            "entries": (
                "SELECT id, date, flag, payee, narration, tags_json, links_json, "
                "metadata_json, source_id, session, late_arrival FROM entries ORDER BY id"
            ),
            "postings": (
                "SELECT entry_id, account, amount, currency, metadata_json "
                "FROM postings ORDER BY entry_id, id"
            ),
            "balance_assertions": (
                "SELECT date, account, amount, currency FROM balance_assertions ORDER BY id"
            ),
            "source_transactions": (
                "SELECT id, payload_json, status, imported_at FROM source_transactions ORDER BY id"
            ),
        }
        # The first v1 writer did not include these additive columns. Verify
        # that exact historical shape before the backed-up repair transition.
        entry_columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
        optional_columns = ", ".join(c for c in ("session", "late_arrival") if c in entry_columns)
        queries["entries"] = (
            "SELECT id, date, flag, payee, narration, tags_json, links_json, metadata_json, source_id"
            + (", " + optional_columns if optional_columns else "") + " FROM entries ORDER BY id"
        )
        if self.has_financial_kinds(conn):
            queries["accounts"] = "SELECT name, currency, open_date, financial_kind FROM accounts ORDER BY name"
        for table, sql in queries.items():
            payload[table] = [dict(row) for row in conn.execute(sql).fetchall()]
        if self.schema_version(conn) in {ACCRUAL_SCHEMA_VERSION, BUSINESS_SCHEMA_VERSION}:
            from ..accrual.store import TABLES
            for table in TABLES:
                payload[f"accrual_{table}"] = [dict(row) for row in conn.execute(
                    f"SELECT id, version, revision, payload_json FROM accrual_{table} ORDER BY id"
                ).fetchall()]
        if self.schema_version(conn) == BUSINESS_SCHEMA_VERSION:
            from ..business import BUSINESS_TABLES
            for table in BUSINESS_TABLES:
                payload[f"business_{table}"] = [dict(row) for row in conn.execute(
                    f"SELECT id, version, revision, payload_json FROM business_{table} ORDER BY id").fetchall()]
        return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()

    def append_audit_event(
        self,
        record_type: str,
        payload: dict[str, Any],
        conn: sqlite3.Connection,
        *,
        ts: str | None = None,
    ) -> str:
        from .. import provenance

        event_ts = ts or _utc_now()
        # A caller kind other than the default ``person`` is sealed inside the hash chain. A person event
        # stays byte-identical to older events, which have no kind and also read as a person.
        kind = provenance.caller_kind()
        if kind != provenance.DEFAULT_KIND and isinstance(payload, dict) and "caller_kind" not in payload:
            payload = {**payload, "caller_kind": kind}
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

    def _discard_last_audit_event(self, record_type: str, record_hash: str) -> bool:
        """Remove the newest audit event when it has *record_type* and *record_hash*; never any other event.

        Only ``feed-intake`` may be removed: a caller undoes its own announcement after the work that it announced
        failed, so the chain stays valid. If a later event exists, the work wrote something and the event stays.
        """
        if record_type != _DISCARDABLE_AUDIT_TYPE:
            raise ValueError(f"An audit event of type {record_type!r} cannot be removed.")
        with self.transaction(immediate=True) as conn:
            cursor = conn.execute(
                "DELETE FROM audit_events WHERE type = ? AND record_hash = ? "
                "AND id = (SELECT MAX(id) FROM audit_events)",
                (record_type, record_hash),
            )
            return cursor.rowcount == 1

    def audit_head(self, conn: sqlite3.Connection | None = None) -> AuditHead | None:
        """Return the newest audit event as a position and hash, or ``None`` for an empty chain. Cost: one index read."""
        if conn is None:
            with self.connection() as tx:
                return self.audit_head(tx)
        row = conn.execute("SELECT id, record_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        return AuditHead(int(row[0]), str(row[1])) if row else None

    def verify_audit_chain(self) -> list[str]:
        """Verify every link from the first event. Cost grows with the whole chain: use it for scheduled checks."""
        with self.connection() as conn:
            return _verify_links(conn.execute(
                "SELECT id, type, ts, payload_json, prev_hash, record_hash FROM audit_events ORDER BY id"
            ), _GENESIS_PREV)

    def verify_audit_chain_since(self, verified: AuditHead | None) -> AuditChainCheck:
        """Verify only the links after a head that an earlier check already verified.

        The caller passes the head from its last good check (``AuditChainCheck.head``). The check proves that
        this head is still in the chain with the same hash. Then it checks each later event: its ``prev_hash``
        and its ``record_hash``. Cost: one index read, plus the number of new events. With ``verified=None`` it
        verifies the whole chain.

        The new head is the last event that this check read, in the same read transaction. An event that a
        concurrent writer adds after the read is not in the head, so the next check verifies it. After any
        error the result has no head, and the caller must run the full check.

        The check does not read events at or before ``verified``. The append-only triggers stop a change to
        them (``append_only_guard_problems`` confirms the triggers), and the scheduled ``verify_audit_chain``
        covers the rest.
        """
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                position, prev = 0, _GENESIS_PREV
                if verified is not None:
                    row = conn.execute("SELECT record_hash FROM audit_events WHERE id = ?",
                                       (verified.position,)).fetchone()
                    if row is None or str(row[0]) != verified.record_hash:
                        return AuditChainCheck([f"Audit event {verified.position}: the verified head is not in the "
                                                "chain; run the full check"], None)
                    position, prev = verified.position, verified.record_hash
                errors: list[str] = []
                head = AuditHead(position, prev) if verified is not None else None
                for row in conn.execute(
                    "SELECT id, type, ts, payload_json, prev_hash, record_hash FROM audit_events "
                    "WHERE id > ? ORDER BY id", (position,)
                ):
                    errors.extend(_verify_links([row], prev))
                    prev = row["record_hash"]
                    head = AuditHead(int(row["id"]), str(row["record_hash"]))
                return AuditChainCheck(errors, None if errors else head)
            finally:
                conn.rollback()

    def posting_watermark(self, conn: sqlite3.Connection | None = None) -> int:
        """Return the newest posting id (0 if none). Cost: one index read.

        Prefer ``check_new_postings``, which returns the next watermark from the rows that it read.
        """
        if conn is None:
            with self.connection() as tx:
                return self.posting_watermark(tx)
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM postings").fetchone()[0])

    def check_new_postings(self, after_posting_id: int) -> PostingCheck:
        """Check the postings added after ``after_posting_id``; return the errors and the next watermark.

        The only foreign key of the ledger tables is ``postings.entry_id``. The append-only triggers refuse a
        delete of an entry, so an existing posting cannot become an orphan; only a new posting can be one. The
        triggers also keep each new posting id above every earlier id, so a saved watermark cannot skip a new
        posting. The watermark is the highest posting id in the same read transaction as the check, so a posting
        that a concurrent writer adds later is checked next time. After an error the watermark is ``None``.
        Cost: the number of new postings, each with one primary key read. The full
        ``PRAGMA foreign_key_check`` stays the check for a scheduled run.
        """
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                rows = conn.execute(
                    "SELECT p.id, p.entry_id, e.id IS NULL FROM postings p LEFT JOIN entries e ON e.id = p.entry_id "
                    "WHERE p.id > ? ORDER BY p.id", (after_posting_id,)
                ).fetchall()
            finally:
                conn.rollback()
        errors = [f"Posting {row[0]}: entry {row[1]} does not exist" for row in rows if row[2]]
        mark = int(rows[-1][0]) if rows else after_posting_id
        return PostingCheck(errors, None if errors else mark)

    def new_posting_foreign_key_errors(self, after_posting_id: int) -> list[str]:
        """Return only the errors of ``check_new_postings``."""
        return self.check_new_postings(after_posting_id).errors

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
