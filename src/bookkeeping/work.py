"""Work events: typed, countable records of what a books command changed.

A work event says what happened to the books in counts, with references to
the rows behind each count. The engine computes work events from the actual
change to company state: it takes a read-only snapshot before a command, a
second snapshot after it, and compares them. The request text and any model
narrative never contribute to a count.

The engine produces only books facts. Who did the work (``actor``), through
which surface (``via``), in which run (``run``), and when the host committed it
(``occurred_at``) are host facts. A host such as Slashbooks Cloud adds them;
the engine never sets them. See ``docs/work-events.md`` and
``docs/work-event.schema.json``.

Phase 1 events contain counts only. They never contain money amounts.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Iterator, Mapping, Sequence

SCHEMA_VERSION = 1

# Every kind in schema version 1, in display order.
KINDS: tuple[str, ...] = (
    "imported",
    "categorized",
    "recorded",
    "corrected",
    "matched_receipt",
    "reconciled",
    "flagged",
    "asked_question",
    "applied_answer",
    "published",
    "taught",
    "reopened",
)

# Kinds that this engine version produces. The other kinds are reserved for
# hosts (questions, answers, publication) or for later engine commands.
ENGINE_KINDS: tuple[str, ...] = (
    "imported",
    "categorized",
    "recorded",
    "corrected",
    "reconciled",
    "flagged",
)

# Required and optional count names per kind.
COUNT_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "imported": (("transactions", "duplicates_skipped"), ()),
    "categorized": (("suggestions",), ()),
    "recorded": (("entries", "from_suggestions", "direct"), ()),
    "corrected": (("entries", "reversals", "withdrawn"), ()),
    "matched_receipt": (("documents",), ()),
    "reconciled": (("accounts", "clean", "open"), ()),
    "flagged": ((), ("duplicates", "transfer_exceptions", "held_emails")),
    "asked_question": (("questions",), ()),
    "applied_answer": (("answers", "entries"), ()),
    "published": (("months",), ()),
    "taught": (("instructions", "waiting_fixed"), ()),
    "reopened": (("months",), ()),
}

SUBJECT_TYPES = (
    "source", "entry", "suggestion", "evidence", "question",
    "reconciliation", "publication", "email",
)
ACTOR_TYPES = ("person", "agent", "automation", "rule", "import", "email", "system")
VIA_VALUES = (
    "web", "connected_agent", "developer_key", "hosted_automation",
    "routine", "stripe", "bank_sync", "company_email",
)
RUN_KINDS = ("automation_run", "agent_run", "agent_session", "routine_job", "stripe_sync", "web_day")
VISIBILITY_VALUES = ("everyone", "staff")
HOST_FIELDS = ("actor", "via", "run", "command_id", "occurred_at", "visibility", "subjects_key")

MAX_ACCOUNTS = 20
MAX_BREAKDOWN = 5

_OPEN_STATUSES = frozenset({"open", "reopened"})
_CLEAN_RECONCILIATION = frozenset({"clean", "resolved"})
_RECONCILIATION_FIELDS = (
    "account", "as_of", "status", "ledger_balance", "source_balance", "discrepancy",
    "source_integrity_residual", "source_opening_balance", "source_transaction_total",
    "source_snapshot_at", "source_through", "source_evidence_refs", "source_evidence_hashes",
)


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryRow:
    """One ledger entry, reduced to the fields that work events use."""

    id: int
    date: str
    source_id: str
    paired_source_id: str
    meta: Mapping[str, str]
    accounts: tuple[str, ...]


@dataclass(frozen=True)
class Snapshot:
    """Read-only view of the company state that work events compare.

    ``entries`` holds only the entry rows with an ID above ``entries_after``.
    The other parts hold the complete current state. To add a new state source
    (for example statement coverage records), add a field here, read it in
    :func:`take_snapshot`, and add one builder in :func:`work_events`.
    """

    max_entry_id: int = 0
    entry_source_ids: frozenset[str] = frozenset()
    entries: tuple[EntryRow, ...] = ()
    pending: Mapping[str, dict] = field(default_factory=dict)
    queue: Mapping[str, dict] = field(default_factory=dict)
    reconciliations: Mapping[str, dict] = field(default_factory=dict)
    duplicates: Mapping[str, dict] = field(default_factory=dict)
    account_mappings: Mapping[str, Any] = field(default_factory=dict)
    csv_mappings: Mapping[str, Any] = field(default_factory=dict)


EMPTY_SNAPSHOT = Snapshot()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None


def _connect_read_only(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_ledger(path: Path, entries_after: int | None) -> tuple[int, frozenset[str], tuple[EntryRow, ...]]:
    conn = _connect_read_only(path)
    if conn is None:
        return 0, frozenset(), ()
    try:
        try:
            max_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM entries").fetchone()[0])
            source_ids = frozenset(
                str(row[0]) for row in conn.execute(
                    "SELECT source_id FROM entries WHERE source_id IS NOT NULL AND source_id != ''"
                )
            )
        except sqlite3.DatabaseError:
            return 0, frozenset(), ()
        if entries_after is None:
            return max_id, source_ids, ()
        rows = conn.execute(
            "SELECT id, date, source_id, metadata_json FROM entries WHERE id > ? ORDER BY id",
            (entries_after,),
        ).fetchall()
        posting_accounts: dict[int, list[str]] = {}
        for posting in conn.execute(
            "SELECT entry_id, account FROM postings WHERE entry_id > ? ORDER BY entry_id, id",
            (entries_after,),
        ):
            posting_accounts.setdefault(int(posting[0]), []).append(str(posting[1]))
        entries: list[EntryRow] = []
        for row in rows:
            try:
                meta = {str(k): str(v) for k, v in json.loads(row["metadata_json"] or "[]")}
            except (ValueError, TypeError):
                meta = {}
            accounts = tuple(posting_accounts.get(int(row["id"]), ()))
            entries.append(EntryRow(
                id=int(row["id"]),
                date=str(row["date"] or ""),
                source_id=str(row["source_id"] or ""),
                paired_source_id=meta.get("paired-source-id", ""),
                meta=meta,
                accounts=accounts,
            ))
        return max_id, source_ids, tuple(entries)
    finally:
        conn.close()


def _queue_item(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or not raw.get("source_id"):
        return None
    raw_sources = raw.get("source_ids")
    if isinstance(raw_sources, list) and raw_sources:
        sources = tuple(str(value) for value in raw_sources if value)
    else:
        sources = (str(raw["source_id"]),)
    return {
        "id": str(raw["source_id"]),
        "status": str(raw.get("status") or ""),
        "proposal_type": str(raw.get("proposal_type") or "category"),
        "proposed_category": str(raw.get("proposed_category") or ""),
        "confirmed_category": str(raw.get("confirmed_category") or ""),
        "date": str(raw.get("date") or "")[:10],
        "sources": sources,
        "signature": json.dumps(
            [raw.get("proposal_type"), raw.get("proposed_category"), raw.get("split_postings"),
             list(sources), raw.get("related_entity")],
            sort_keys=True, default=str,
        ),
    }


def _load_queue(directory: Path) -> dict[str, dict]:
    items: dict[str, dict] = {}
    if not directory.is_dir():
        return items
    for path in sorted(directory.glob("*.json")):
        item = _queue_item(_read_json(path))
        if item is not None:
            items[item["id"]] = item
    return items


def _load_list(path: Path) -> list[dict]:
    data = _read_json(path)
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _in_window(value: str, window: tuple[str, str] | None) -> bool:
    if window is None:
        return True
    day = str(value or "")[:10]
    return bool(day) and window[0] <= day <= window[1]


def take_snapshot(
    entity_path: Path | str,
    *,
    entries_after: int | None = None,
    window: tuple[str, str] | None = None,
) -> Snapshot:
    """Read the company state without writing or creating any file.

    ``entries_after`` selects the entry rows to load (``None`` loads none).
    ``window`` limits every part to rows dated inside ``(from, to)``; it exists
    for the derived summary of ``books work``.
    """
    root = Path(entity_path)
    if not root.is_dir():
        return EMPTY_SNAPSHOT
    config = _read_json(root / "entity.json")
    config = config if isinstance(config, dict) else {}
    max_id, source_ids, entries = _load_ledger(root / "ledger.sqlite", entries_after)
    pending = {
        str(txn.get("id")): txn
        for txn in _load_list(root / "staging" / "pending-categorization.json")
        if txn.get("id") and _in_window(str(txn.get("date") or ""), window)
    }
    queue = {
        key: item for key, item in _load_queue(root / "review-queue").items()
        if _in_window(item["date"], window)
    }
    reconciliations = {}
    for record in _load_list(root / "reports" / "reconciliation.json"):
        record_id = str(record.get("record_id") or f"{record.get('account')}@{record.get('as_of')}")
        if _in_window(str(record.get("as_of") or ""), window):
            reconciliations[record_id] = {name: record.get(name) for name in _RECONCILIATION_FIELDS}
    duplicates = {
        str(item.get("source_id")): {
            "status": str(item.get("status") or ""),
            "date": str(item.get("date") or "")[:10],
            "account": str(item.get("account") or ""),
        }
        for item in _load_list(root / "staging" / "duplicate-candidates.json")
        if item.get("source_id") and _in_window(str(item.get("date") or ""), window)
    }
    if window is not None:
        entries = tuple(entry for entry in entries if _in_window(entry.date, window))
    mappings = config.get("bank_account_mappings")
    csv_mappings = config.get("csv_account_mappings")
    return Snapshot(
        max_entry_id=max_id,
        entry_source_ids=source_ids,
        entries=entries,
        pending=pending,
        queue=queue,
        reconciliations=reconciliations,
        duplicates=duplicates,
        account_mappings=mappings if isinstance(mappings, dict) else {},
        csv_mappings=csv_mappings if isinstance(csv_mappings, dict) else {},
    )


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------


def _fingerprint(event: Mapping[str, Any]) -> str:
    body = {key: value for key, value in event.items() if key != "fingerprint"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _period(dates: Iterable[str]) -> dict[str, str] | None:
    days = sorted(day[:10] for day in dates if _valid_day(day[:10] if day else ""))
    return {"from": days[0], "to": days[-1]} if days else None


def _valid_day(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return len(value) == 10


def _accounts(values: Iterable[str]) -> list[str]:
    return sorted({value for value in values if value})[:MAX_ACCOUNTS]


def _breakdown(labels: Iterable[str]) -> list[dict[str, Any]]:
    totals: dict[str, int] = {}
    for label in labels:
        if label:
            totals[label] = totals.get(label, 0) + 1
    ordered = sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))
    return [{"label": label, "count": count} for label, count in ordered[:MAX_BREAKDOWN]]


def _subjects(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    unique = {json.dumps(value, sort_keys=True): value for value in values}
    return sorted(unique.values(), key=lambda value: (value["type"], value["id"], value.get("source_id", "")))


def _event(kind: str, counts: dict[str, int], *, dates: Iterable[str], accounts: Iterable[str],
           labels: Iterable[str], subjects: Iterable[dict[str, str]], derived: bool) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "period": _period(dates),
        "accounts": _accounts(accounts),
        "counts": counts,
        "breakdown": _breakdown(labels),
        "subjects": _subjects(subjects),
    }
    if derived:
        event["derived"] = True
    event["fingerprint"] = _fingerprint(event)
    return event


def _entry_category(entry: EntryRow) -> str:
    workflow = entry.meta.get("review-workflow")
    if workflow == "transfer-pair":
        return "Internal transfer"
    if workflow == "split":
        return "Split transaction"
    others = sorted(set(entry.accounts[1:]))
    if len(others) == 1:
        return others[0]
    return "Split transaction" if others else ""


def _entry_subject(entry: EntryRow) -> dict[str, str]:
    subject = {"type": "entry", "id": str(entry.id)}
    if entry.source_id:
        subject["source_id"] = entry.source_id
    return subject


def _source_account(snapshot: Snapshot, txn: Mapping[str, Any]) -> str:
    from .ledger.importer import _ledger_account_for_txn

    try:
        return _ledger_account_for_txn(dict(txn), dict(snapshot.account_mappings), dict(snapshot.csv_mappings))
    except (ValueError, TypeError):
        return ""


def work_events(
    before: Snapshot,
    after: Snapshot,
    *,
    derived: bool = False,
    observations: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Return the work events for the change from *before* to *after*.

    The result is deterministic: the same two snapshots always give the same
    events, in the same order, with the same fingerprints. An unchanged state
    gives an empty list.
    """
    observations = observations or {}

    # Review queue transitions.
    newly: dict[str, list[dict]] = {"confirmed": [], "corrected": [], "withdrawn": []}
    suggested: list[dict] = []
    for item_id, item in sorted(after.queue.items()):
        prior = before.queue.get(item_id)
        status = item["status"]
        if status in newly and (prior is None or prior["status"] != status):
            newly[status].append(item)
        if status in _OPEN_STATUSES and (
            prior is None or prior["status"] not in _OPEN_STATUSES or prior["signature"] != item["signature"]
        ):
            suggested.append(item)
    confirmed_sources = {source for item in newly["confirmed"] for source in item["sources"]}
    corrected_sources = {source for item in newly["corrected"] for source in item["sources"]}

    # New ledger entries, each classified exactly once.
    recorded_suggested: list[EntryRow] = []
    recorded_direct: list[EntryRow] = []
    corrections: list[EntryRow] = []
    reversals: list[EntryRow] = []
    for entry in after.entries:
        if entry.id <= before.max_entry_id:
            continue
        entry_sources = {entry.source_id, entry.paired_source_id} - {""}
        if "reverses" in entry.meta:
            reversals.append(entry)
        elif "correction-of" in entry.meta or entry_sources & corrected_sources:
            corrections.append(entry)
        elif entry_sources & confirmed_sources:
            recorded_suggested.append(entry)
        else:
            recorded_direct.append(entry)

    events: list[dict[str, Any]] = []

    # imported: sources that arrived in the books state during this change.
    known = set(before.entry_source_ids) | set(before.pending)
    arrived: dict[str, tuple[str, str]] = {}
    for source_id, txn in after.pending.items():
        if source_id not in known:
            arrived[source_id] = (str(txn.get("date") or "")[:10], _source_account(after, txn))
    # Queue corrections post a staged source; ledger corrections (correction-of)
    # post a replacement of an entry that already existed, which is no import.
    arrivals = recorded_direct + recorded_suggested + [
        entry for entry in corrections if "correction-of" not in entry.meta
    ]
    for entry in sorted(arrivals, key=lambda row: row.id):
        for source_id in (entry.source_id, entry.paired_source_id):
            if source_id and source_id not in known:
                arrived[source_id] = (entry.date, entry.accounts[0] if entry.accounts else "")
    if arrived:
        events.append(_event(
            "imported",
            {"transactions": len(arrived), "duplicates_skipped": int(observations.get("duplicates_skipped", 0))},
            dates=(value[0] for value in arrived.values()),
            accounts=(value[1] for value in arrived.values()),
            labels=(value[1] for value in arrived.values()),
            subjects=({"type": "source", "id": source_id} for source_id in arrived),
            derived=derived,
        ))

    # categorized: suggestions that are new or changed and wait for review.
    if suggested:
        events.append(_event(
            "categorized",
            {"suggestions": len(suggested)},
            dates=(item["date"] for item in suggested),
            accounts=(),
            labels=(item["proposed_category"] or item["proposal_type"] for item in suggested),
            subjects=({"type": "suggestion", "id": item["id"]} for item in suggested),
            derived=derived,
        ))

    # recorded: new entries that are not corrections.
    recorded = recorded_suggested + recorded_direct
    if recorded:
        recorded.sort(key=lambda entry: entry.id)
        events.append(_event(
            "recorded",
            {"entries": len(recorded), "from_suggestions": len(recorded_suggested),
             "direct": len(recorded_direct)},
            dates=(entry.date for entry in recorded),
            accounts=(entry.accounts[0] for entry in recorded if entry.accounts),
            labels=(_entry_category(entry) for entry in recorded),
            subjects=(_entry_subject(entry) for entry in recorded),
            derived=derived,
        ))

    # corrected: replacement entries, reversals, and withdrawn suggestions.
    if corrections or reversals or newly["withdrawn"]:
        changed = corrections + reversals
        events.append(_event(
            "corrected",
            {"entries": len(corrections), "reversals": len(reversals), "withdrawn": len(newly["withdrawn"])},
            dates=[entry.date for entry in changed] + [item["date"] for item in newly["withdrawn"]],
            accounts=(entry.accounts[0] for entry in changed if entry.accounts),
            labels=(_entry_category(entry) for entry in corrections),
            subjects=[_entry_subject(entry) for entry in changed]
            + [{"type": "suggestion", "id": item["id"]} for item in newly["withdrawn"]],
            derived=derived,
        ))

    # reconciled: reconciliation records that are new or changed in substance.
    reconciled = [
        (record_id, record) for record_id, record in sorted(after.reconciliations.items())
        if before.reconciliations.get(record_id) != record
    ]
    if reconciled:
        events.append(_event(
            "reconciled",
            {
                "accounts": len({str(record.get("account") or "") for _, record in reconciled}),
                "clean": sum(1 for _, record in reconciled if record.get("status") in _CLEAN_RECONCILIATION),
                "open": sum(1 for _, record in reconciled if record.get("status") not in _CLEAN_RECONCILIATION),
            },
            dates=(str(record.get("as_of") or "") for _, record in reconciled),
            accounts=(str(record.get("account") or "") for _, record in reconciled),
            labels=(str(record.get("account") or "") for _, record in reconciled),
            subjects=({"type": "reconciliation", "id": record_id} for record_id, _ in reconciled),
            derived=derived,
        ))

    # flagged: new possible duplicates that wait for a decision.
    flagged = [
        (source_id, item) for source_id, item in sorted(after.duplicates.items())
        if item["status"] == "duplicate-candidate"
        and (before.duplicates.get(source_id) or {}).get("status") != "duplicate-candidate"
    ]
    if flagged:
        events.append(_event(
            "flagged",
            {"duplicates": len(flagged)},
            dates=(item["date"] for _, item in flagged),
            accounts=(item["account"] for _, item in flagged),
            labels=(item["account"] for _, item in flagged),
            subjects=({"type": "source", "id": source_id} for source_id, _ in flagged),
            derived=derived,
        ))
    return events


# ---------------------------------------------------------------------------
# Capture around one command
# ---------------------------------------------------------------------------


_observations: ContextVar[dict[str, int] | None] = ContextVar("work_observations", default=None)


def observe_import(duplicates_skipped: int) -> None:
    """Record engine import facts that leave no state change (no-op outside a capture)."""
    current = _observations.get()
    if current is not None:
        current["duplicates_skipped"] = current.get("duplicates_skipped", 0) + int(duplicates_skipped)


@dataclass
class WorkCapture:
    """Result holder for :func:`capture`; ``events`` is set when the block exits."""

    entity_path: Path | None
    events: list[dict[str, Any]] = field(default_factory=list)


@contextmanager
def capture(entity_path: Path | str | None) -> Iterator[WorkCapture]:
    """Compute the work events for the command that runs inside the block.

    A host wraps one engine command::

        with work.capture(entity_dir) as receipt:
            code = cli.main(argv)
        result["work"] = receipt.events

    The events are computed also when the command fails, because a failed
    command can still change state (for example a partial import). The host
    decides whether it commits them. With ``entity_path=None`` the result is
    always an empty list.
    """
    holder = WorkCapture(Path(entity_path) if entity_path is not None else None)
    if holder.entity_path is None:
        yield holder
        return
    before = take_snapshot(holder.entity_path)
    observations: dict[str, int] = {}
    token = _observations.set(observations)
    try:
        yield holder
    finally:
        _observations.reset(token)
        after = take_snapshot(holder.entity_path, entries_after=before.max_entry_id)
        holder.events = work_events(before, after, observations=observations)


def entity_path_from_args(args: argparse.Namespace) -> Path | None:
    """Return the company directory that a parsed command writes, if any."""
    for name in ("entity", "entity_path"):
        value = getattr(args, name, None)
        if value:
            return Path(value)
    if getattr(args, "command", None) in {"entity", "demo"} and getattr(args, "path", None):
        return Path(args.path)
    return None


def write_document(path: Path, events: Sequence[Mapping[str, Any]]) -> None:
    """Write the machine-readable work document for ``--work-out``."""
    document = {"schema_version": SCHEMA_VERSION, "work": list(events)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_text(value: Any, maximum: int = 500) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum


def validate_event(event: Any, *, engine: bool = False) -> list[str]:
    """Return schema errors for one work event; an empty list means valid.

    This dependency-free validator mirrors ``docs/work-event.schema.json``.
    With ``engine=True`` it also rejects host fields, because the engine must
    never set them.
    """
    errors: list[str] = []
    if not isinstance(event, dict):
        return ["event must be an object"]
    allowed = {"schema_version", "kind", "period", "accounts", "counts", "breakdown",
               "subjects", "fingerprint", "derived", *HOST_FIELDS}
    for key in sorted(set(event) - allowed):
        errors.append(f"unknown field: {key}")
    for key in ("schema_version", "kind", "period", "accounts", "counts", "breakdown", "subjects"):
        if key not in event:
            errors.append(f"missing field: {key}")
    if event.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version must be 1")
    kind = event.get("kind")
    if kind not in KINDS:
        errors.append(f"unknown kind: {kind!r}")
    period = event.get("period")
    if period is not None:
        if (not isinstance(period, dict) or set(period) != {"from", "to"}
                or not all(_valid_day(str(period.get(k) or "")) for k in ("from", "to"))
                or period["from"] > period["to"]):
            errors.append("period must be null or {from, to} ISO dates with from <= to")
    accounts = event.get("accounts")
    if (not isinstance(accounts, list) or len(accounts) > MAX_ACCOUNTS
            or not all(_is_text(value) for value in accounts) or len(set(accounts or [])) != len(accounts or [])):
        errors.append(f"accounts must be at most {MAX_ACCOUNTS} unique account names")
    counts = event.get("counts")
    if not isinstance(counts, dict):
        errors.append("counts must be an object")
    elif kind in COUNT_KEYS:
        required, optional = COUNT_KEYS[kind]
        for key in required:
            if key not in counts:
                errors.append(f"counts.{key} is required for {kind}")
        for key, value in counts.items():
            if key not in required and key not in optional:
                errors.append(f"counts.{key} is not a {kind} count")
            elif not _is_count(value):
                errors.append(f"counts.{key} must be a non-negative integer")
        if not required and not counts:
            errors.append(f"counts must name at least one {kind} count")
    breakdown = event.get("breakdown")
    if (not isinstance(breakdown, list) or len(breakdown) > MAX_BREAKDOWN or not all(
            isinstance(row, dict) and set(row) == {"label", "count"}
            and _is_text(row.get("label")) and _is_count(row.get("count")) for row in breakdown)):
        errors.append(f"breakdown must be at most {MAX_BREAKDOWN} {{label, count}} rows")
    subjects = event.get("subjects")
    if not isinstance(subjects, list) or not all(
            isinstance(row, dict) and row.get("type") in SUBJECT_TYPES and _is_text(row.get("id"), 1000)
            and set(row) <= {"type", "id", "source_id"}
            and ("source_id" not in row or _is_text(row["source_id"], 1000))
            for row in subjects):
        errors.append("subjects must be {type, id, source_id?} references")
    fingerprint = event.get("fingerprint")
    if fingerprint is not None and not (isinstance(fingerprint, str) and len(fingerprint) == 64
                                        and all(c in "0123456789abcdef" for c in fingerprint)):
        errors.append("fingerprint must be a SHA-256 hex digest")
    if "derived" in event and event["derived"] is not True:
        errors.append("derived must be true when present")
    if engine:
        for key in HOST_FIELDS:
            if key in event:
                errors.append(f"{key} is a host field; the engine must not set it")
        if fingerprint is None:
            errors.append("engine events must carry a fingerprint")
        elif fingerprint != _fingerprint(event):
            errors.append("fingerprint does not match the event content")
    errors.extend(_validate_host_fields(event))
    return errors


def _validate_host_fields(event: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    actor = event.get("actor")
    if "actor" in event and not (isinstance(actor, dict) and set(actor) == {"type", "name"}
                                 and actor.get("type") in ACTOR_TYPES and _is_text(actor.get("name"), 200)):
        errors.append("actor must be {type, name}")
    if "via" in event and event["via"] not in VIA_VALUES:
        errors.append("via is not a known surface")
    run = event.get("run")
    if "run" in event and run is not None and not (
            isinstance(run, dict) and set(run) == {"kind", "id"} and run.get("kind") in RUN_KINDS
            and _is_text(run.get("id"), 200)):
        errors.append("run must be null or {kind, id}")
    if "command_id" in event and event["command_id"] is not None and not _is_text(event["command_id"], 200):
        errors.append("command_id must be null or a string")
    if "occurred_at" in event and not (_is_text(event["occurred_at"], 40) and event["occurred_at"].endswith("Z")):
        errors.append("occurred_at must be an ISO UTC timestamp")
    if "visibility" in event and event["visibility"] not in VISIBILITY_VALUES:
        errors.append("visibility must be everyone or staff")
    if "subjects_key" in event and not _is_text(event["subjects_key"], 500):
        errors.append("subjects_key must be a string")
    return errors


# ---------------------------------------------------------------------------
# Summary and the ``books work`` command
# ---------------------------------------------------------------------------


def summarize(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Merge events into one line per kind: counts add, money never appears."""
    merged: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = str(event.get("kind"))
        line = merged.setdefault(kind, {"kind": kind, "counts": {}, "labels": {}, "accounts": set(), "dates": []})
        for key, value in (event.get("counts") or {}).items():
            line["counts"][key] = line["counts"].get(key, 0) + int(value)
        for row in event.get("breakdown") or []:
            line["labels"][row["label"]] = line["labels"].get(row["label"], 0) + int(row["count"])
        line["accounts"].update(event.get("accounts") or [])
        period = event.get("period")
        if period:
            line["dates"].extend([period["from"], period["to"]])
    lines = []
    for kind in [k for k in KINDS if k in merged] + sorted(set(merged) - set(KINDS)):
        line = merged[kind]
        ordered = sorted(line["labels"].items(), key=lambda pair: (-pair[1], pair[0]))[:MAX_BREAKDOWN]
        summary = {
            "kind": kind,
            "counts": dict(sorted(line["counts"].items())),
            "breakdown": [{"label": label, "count": count} for label, count in ordered],
            "accounts": sorted(line["accounts"])[:MAX_ACCOUNTS],
            "period": _period(line["dates"]),
        }
        summary["text"] = describe(summary)
        lines.append(summary)
    return lines


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def describe(line: Mapping[str, Any]) -> str:
    """Return one plain sentence for a summary line (local text output only)."""
    kind = line["kind"]
    counts = line.get("counts") or {}
    period = line.get("period") or {}
    if kind == "imported":
        text = f"Imported {_plural(counts.get('transactions', 0), 'transaction')}."
        if counts.get("duplicates_skipped"):
            text += f" Skipped {_plural(counts['duplicates_skipped'], 'duplicate')}."
        return text
    if kind == "categorized":
        return f"Suggested categories for {_plural(counts.get('suggestions', 0), 'transaction')}."
    if kind == "recorded":
        text = f"Recorded {_plural(counts.get('entries', 0), 'entry', 'entries')}."
        if counts.get("from_suggestions"):
            text += f" {counts['from_suggestions']} came from approved suggestions."
        return text
    if kind == "corrected":
        parts = []
        if counts.get("entries"):
            parts.append(f"corrected {_plural(counts['entries'], 'entry', 'entries')}")
        if counts.get("reversals"):
            parts.append(f"reversed {_plural(counts['reversals'], 'entry', 'entries')}")
        if counts.get("withdrawn"):
            parts.append(f"withdrew {_plural(counts['withdrawn'], 'suggestion')}")
        text = "; ".join(parts) or "made no corrections"
        return text[0].upper() + text[1:] + "."
    if kind == "reconciled":
        text = f"Reconciled {_plural(counts.get('accounts', 0), 'account')}"
        if period.get("to"):
            text += f" through {period['to']}"
        text += "."
        if counts.get("open"):
            text += f" {_plural(counts['open'], 'reconciliation')} still {'needs' if counts['open'] == 1 else 'need'} review."
        return text
    if kind == "flagged":
        parts = [
            _plural(value, label)
            for key, label in (("duplicates", "possible duplicate"), ("transfer_exceptions", "transfer exception"),
                               ("held_emails", "held email"))
            if (value := counts.get(key))
        ]
        return "Flagged " + (", ".join(parts) or "nothing") + " for review."
    return f"{kind.replace('_', ' ').capitalize()}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + "."


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "work",
        help="Summarize the books work in a period (counts only; derived from the current books)",
    )
    parser.add_argument("--entity", required=True, help="Path to entity directory")
    parser.add_argument("--from", dest="from_date", default=None, help="First books date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", default=None, help="Last books date (YYYY-MM-DD)")
    parser.add_argument("--format", choices=["text", "json"], default="text", dest="fmt")


def derived_events(entity_path: Path | str, from_date: str | None = None,
                   to_date: str | None = None) -> list[dict[str, Any]]:
    """Return derived events for the books dated inside the window.

    The engine does not keep a per-command work history. This view compares the
    current books with empty books, limited to rows dated inside the window,
    so its period is the books date, not the time of the work. Each event has
    ``derived: true``.
    """
    window = None
    if from_date or to_date:
        window = (from_date or "0001-01-01", to_date or "9999-12-31")
    after = take_snapshot(entity_path, entries_after=0, window=window)
    return work_events(EMPTY_SNAPSHOT, after, derived=True)


def run(args: argparse.Namespace) -> int:
    for value in (args.from_date, args.to_date):
        if value is not None and not _valid_day(value):
            print("Error: dates must use YYYY-MM-DD.", file=sys.stderr)
            return 1
    if args.from_date and args.to_date and args.from_date > args.to_date:
        print("Error: --from must not be after --to.", file=sys.stderr)
        return 1
    if not (Path(args.entity) / "entity.json").is_file():
        print(f"Error: No entity.json found at '{args.entity}'.", file=sys.stderr)
        return 1
    events = derived_events(args.entity, args.from_date, args.to_date)
    lines = summarize(events)
    if args.fmt == "json":
        print(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "derived": True,
            "period": {"from": args.from_date, "to": args.to_date},
            "summary": lines,
            "work": events,
        }, indent=2, sort_keys=True))
        return 0
    if not lines:
        print("No books work found for this period.")
        return 0
    for line in lines:
        print(line["text"])
    return 0
