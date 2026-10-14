"""Trusted execution boundary for supported ordinary bookkeeping commands.

No CLI flag, environment variable, or workspace file can establish this context.
An embedding host must authenticate and fence the operation independently.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from .automation_policy import ExecutionContext, PolicyError, PreparationDenied, PreparationIntent, authorize_batch
from .entity import Entity, load_entity
from .ledger.model import Entry
from .ledger.store import LedgerStore
from .remote_contract import ContractError, parse_command


_READ_PATHS = frozenset({
    ("readiness",),
    ("report", "pnl"), ("report", "balance-sheet"),
    ("report", "trial-balance"), ("report", "general-ledger"),
    ("queue", "list"), ("queue", "show"), ("queue", "summary"),
    ("queue", "split-template-list"), ("queue", "transfer-candidates"),
    ("queue", "transfer-exceptions"),
    ("sanity-check",),
})
_PROPOSAL_ACTIONS = {
    ("queue", "propose"): "categorize",
    ("queue", "propose-group"): "categorize",
    ("queue", "propose-split"): "split",
    ("queue", "propose-transfer"): "match_transfer",
}
_CONFIRM_PATHS = frozenset({
    ("queue", "confirm"), ("queue", "confirm-split"), ("queue", "confirm-transfer"),
})
_CORRECTION_PATHS = frozenset({
    ("ledger", "correct"), ("ledger", "reclassify-bank"), ("ledger", "split-equity"),
})


@dataclass(frozen=True)
class PreparationCommandSpec:
    """Positive-list classification for a trusted preparation integration."""

    path: tuple[str, ...]
    effect: str
    action: str | None


def supported_preparation_command(argv: list[str]) -> PreparationCommandSpec:
    """Classify only commands intentionally exposed to a preparation service."""
    try:
        _, _, path = parse_command(argv)
    except ContractError as exc:
        raise PreparationDenied(0, "command_not_supported") from exc
    if path in _READ_PATHS:
        return PreparationCommandSpec(path, "read", None)
    if path == ("ingest",):
        return PreparationCommandSpec(path, "ledger", "import")
    if path in _PROPOSAL_ACTIONS:
        return PreparationCommandSpec(path, "proposal", _PROPOSAL_ACTIONS[path])
    if path in _CONFIRM_PATHS:
        return PreparationCommandSpec(path, "ledger", None)
    if path in _CORRECTION_PATHS:
        return PreparationCommandSpec(path, "ledger", "correct")
    if path == ("reconcile",):
        return PreparationCommandSpec(path, "reconciliation", "reconcile")
    raise PreparationDenied(0, "command_not_supported")


def evidence_hash(value: Any) -> str:
    """Canonical fingerprint of validated source/entry data, not its authority."""
    def scalar(item: Any) -> str:
        if isinstance(item, (date, Decimal)):
            return str(item)
        raise TypeError("Unsupported evidence value")
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=scalar)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class _Boundary:
    context: ExecutionContext
    entity_path: Path
    ledger_digest: str
    source_hashes: tuple[tuple[str, str], ...]
    item_id: str
    item_hash: str
    action: str
    original_source_id: str | None = None
    replacement_source_id: str | None = None
    entry_hashes: tuple[tuple[str, str], ...] = ()
    input_path: Path | None = None
    input_transactions_json: str | None = None
    input_is_banksync: bool = False


_active: ContextVar[_Boundary | None] = ContextVar("books_preparation", default=None)


def is_preparation() -> bool:
    return _active.get() is not None


def run_preparation_command(argv: list[str], context: ExecutionContext, *, entity_path: Path,
                            expected_ledger_digest: str, source_hashes: Mapping[str, str],
                            entry_hashes: Mapping[str, str] | None = None) -> int:
    """Run a supported ordinary preparation command using trusted authority.

    ``source_hashes`` must come from previously verified/staged source data, not
    model output or freshly invented replacement uploads. Corrections instead
    require ``entry_hashes`` for the original entry and its correction ancestors,
    keyed by ``entry:<SQLite id>``. Other commands fail closed until their own
    commit boundaries are integrated.
    """
    from . import ingest, queue, reconcile
    from .ledger import corrections

    spec = supported_preparation_command(argv)
    if spec.effect == "read":
        raise PreparationDenied(0, "read_command_requires_ordinary_dispatch")
    args, _, path = parse_command(argv)
    correction = path in _CORRECTION_PATHS
    resolved = entity_path.resolve()
    if Path(args.entity).resolve() != resolved:
        raise PreparationDenied(0, "workspace_mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_ledger_digest):
        raise PolicyError("Expected the trusted ledger fingerprint.")
    if not isinstance(source_hashes, Mapping) or not (0 if correction else 1) <= len(source_hashes) <= 1000 or any(
        not isinstance(key, str) or not key or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        for key, value in source_hashes.items()
    ):
        raise PolicyError("Expected bounded verified source fingerprints.")
    if is_preparation():
        raise PolicyError("Nested preparation execution is not supported.")
    entity = load_entity(resolved)
    store = LedgerStore(resolved / "ledger.sqlite")
    if not store.path.is_file():
        raise PreparationDenied(0, "canonical_ledger_required")
    if store.content_digest() != expected_ledger_digest:
        raise PreparationDenied(0, "ledger_changed")
    if correction:
        if not isinstance(entry_hashes, Mapping) or not 1 <= len(entry_hashes) <= 1000 or any(
            not isinstance(key, str) or not re.fullmatch(r"entry:[1-9][0-9]*", key)
            or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for key, value in entry_hashes.items()
        ):
            raise PolicyError("Expected bounded verified entry fingerprints.")
        bound = _Boundary(context, resolved, expected_ledger_digest, (), "", "", "correct",
                          args.source_id, args.new_source_id, tuple(sorted(entry_hashes.items())))
    elif path in _CONFIRM_PATHS:
        item = queue._load_item(entity, args.item_id)
        proposal = item.get("proposal_type", "category")
        if proposal not in {"category", "split", "transfer-pair"}:
            raise PreparationDenied(0, "specialized_policy_required")
        action = {"split": "split", "transfer-pair": "match_transfer"}.get(proposal, "categorize")
        ids = item.get("source_ids") if action == "match_transfer" else [item["source_id"]]
        if any(store.source_exists(source_id) for source_id in ids):
            raise PreparationDenied(0, "posted_source_requires_reconciliation")
        bound = _Boundary(context, resolved, expected_ledger_digest, tuple(sorted(source_hashes.items())),
                          args.item_id, evidence_hash(item), action)
    elif path in _PROPOSAL_ACTIONS:
        ids = list(args.source_ids) if hasattr(args, "source_ids") else [args.source_id]
        intent = _source_intent(entity, _PROPOSAL_ACTIONS[path], ids, source_hashes)
        authorize_batch(context, [intent])
        return queue.run(args)
    elif path == ("reconcile",):
        intent = _reconciliation_intent(args, source_hashes)
        authorize_batch(context, [intent])
        bound = _Boundary(
            context, resolved, expected_ledger_digest, tuple(sorted(source_hashes.items())),
            "", "", "reconcile",
        )
    else:
        try:
            input_document = json.loads(Path(args.input).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            raise PreparationDenied(0, "source_evidence_changed") from exc
        transactions = ingest._transactions_from_data(input_document)
        ids = [str(item.get("id") or "") for item in transactions]
        if (not transactions or any(not value for value in ids) or len(set(ids)) != len(ids)
                or set(source_hashes) != set(ids)
                or any(source_hashes[source_id] != evidence_hash(item)
                       for source_id, item in zip(ids, transactions))):
            raise PreparationDenied(0, "source_evidence_changed")
        try:
            periods = tuple(sorted({date.fromisoformat(str(item["date"])[:10]).isoformat()[:7]
                                    for item in transactions}))
        except (KeyError, TypeError, ValueError) as exc:
            raise PreparationDenied(0, "source_period_invalid") from exc
        authorize_batch(context, [PreparationIntent(
            "import", periods, tuple(sorted(ids)), (),
            tuple(sorted(evidence_hash(item) for item in transactions)),
        )])
        input_path = Path(args.input).resolve()
        bound = _Boundary(
            context, resolved, expected_ledger_digest, tuple(sorted(source_hashes.items())),
            "", "", "import", input_path=input_path,
            input_transactions_json=json.dumps(transactions, sort_keys=True, separators=(",", ":"), allow_nan=False),
            input_is_banksync=bool(
                args.source == "banksync"
                or (isinstance(input_document, dict) and input_document.get("source") == "banksync"
                    and isinstance(input_document.get("banks"), list))
            ),
        )
    token = _active.set(bound)
    try:
        # Same dispatchers as the normal CLI; no .env loading or remote rerouting.
        if correction:
            return corrections.run(args)
        if path == ("ingest",):
            return ingest.run(args)
        if path == ("reconcile",):
            return reconcile.run(args)
        return queue.run(args)
    finally:
        _active.reset(token)


def record_preparation(entity: Entity, store: LedgerStore, conn: sqlite3.Connection,
                       entries: list[Entry], sources: list[dict[str, Any]], ts: str | None) -> None:
    """Enforce authority and record its receipt inside the ledger transaction."""
    bound = _active.get()
    if bound is None:
        return

    if entity.path.resolve() != bound.entity_path:
        raise PreparationDenied(0, "workspace_mismatch")
    if store.content_digest(conn) != bound.ledger_digest:
        raise PreparationDenied(0, "ledger_changed")
    if bound.action == "correct":
        intent = _correction_intent(bound, store, conn, entries, sources)
    elif bound.action == "import":
        intent = _import_intent(bound, entries, sources)
    else:
        intent = _queue_intent(bound, entity, entries, sources)
    receipt, = authorize_batch(bound.context, [intent])
    store.append_audit_event("automation-preparation", {"receipt": asdict(receipt), "intent": asdict(intent)}, conn, ts=ts)


def prepared_ingest_transactions(path: Path) -> tuple[list[dict[str, Any]], bool] | None:
    """Return the host-verified import snapshot while an ingest command runs."""
    bound = _active.get()
    if bound is None or bound.action != "import":
        return None
    if bound.input_path is None or Path(path).resolve() != bound.input_path or bound.input_transactions_json is None:
        raise PreparationDenied(0, "source_evidence_changed")
    data = json.loads(bound.input_transactions_json)
    return data, bound.input_is_banksync


def prepared_reconciliation_evidence(account: str, as_of: str) -> dict[str, str] | None:
    """Return host-verified evidence attestations for the active reconciliation."""
    bound = _active.get()
    if bound is None or bound.action != "reconcile":
        return None
    primary = f"reconciliation:{account}:{as_of}"
    return {key.removeprefix("evidence:"): value for key, value in bound.source_hashes
            if key != primary and key.startswith("evidence:")}


def _source_intent(entity: Entity, action: str, ids: list[str],
                   source_hashes: Mapping[str, str]) -> PreparationIntent:
    from . import queue

    if not ids or len(set(ids)) != len(ids) or set(ids) != set(source_hashes):
        raise PreparationDenied(0, "source_evidence_changed")
    sources = []
    for source_id in ids:
        source = queue._get_pending_txn(entity, source_id)
        if source is None or evidence_hash(source) != source_hashes[source_id]:
            raise PreparationDenied(0, "source_evidence_changed")
        sources.append(source)
    try:
        periods = tuple(sorted({date.fromisoformat(str(source["date"])[:10]).isoformat()[:7] for source in sources}))
    except (KeyError, TypeError, ValueError) as exc:
        raise PreparationDenied(0, "source_period_invalid") from exc
    return PreparationIntent(action, periods, tuple(sorted(ids)), (),
                             tuple(sorted(evidence_hash(source) for source in sources)))


def _reconciliation_intent(args: Any, source_hashes: Mapping[str, str]) -> PreparationIntent:
    if (args.all_accounts or args.balances_json or not args.account or args.source_balance is None
            or args.source_opening_balance is None or args.source_transaction_total is None
            or not args.source_snapshot_at or not args.source_through or not args.source_evidence_refs):
        raise PreparationDenied(0, "affirmative_reconciliation_evidence_required")
    try:
        period = date.fromisoformat(args.as_of).isoformat()[:7]
        date.fromisoformat(args.source_through)
        amounts = (Decimal(args.source_balance), Decimal(args.source_opening_balance),
                   Decimal(args.source_transaction_total))
        snapshot_at = datetime.fromisoformat(
            args.source_snapshot_at.replace("Z", "+00:00")
        )
        if not all(amount.is_finite() for amount in amounts) or snapshot_at.utcoffset() is None:
            raise ValueError
    except (ValueError, ArithmeticError) as exc:
        raise PreparationDenied(0, "reconciliation_evidence_invalid") from exc
    source_id = f"reconciliation:{args.account}:{args.as_of}"
    evidence = {
        "account": args.account,
        "as_of": args.as_of,
        "source_balance": args.source_balance,
        "source_opening_balance": args.source_opening_balance,
        "source_transaction_total": args.source_transaction_total,
        "source_snapshot_at": args.source_snapshot_at,
        "source_through": args.source_through,
    }
    expected = evidence_hash(evidence)
    evidence_keys = tuple(sorted(f"evidence:{value}" for value in args.source_evidence_refs))
    if (len(evidence_keys) != len(set(evidence_keys))
            or set(source_hashes) != {source_id, *evidence_keys}
            or source_hashes.get(source_id) != expected):
        raise PreparationDenied(0, "source_evidence_changed")
    return PreparationIntent("reconcile", (period,), tuple(sorted((source_id, *evidence_keys))), (),
                             tuple(sorted(source_hashes.values())))


def _import_intent(bound: _Boundary, entries: list[Entry],
                   sources: list[dict[str, Any]]) -> PreparationIntent:
    expected = dict(bound.source_hashes)
    ids = tuple(sorted(str(source.get("id") or "") for source in sources))
    if (not ids or len(set(ids)) != len(ids)
            or any(expected.get(str(source.get("id") or "")) != evidence_hash(source) for source in sources)):
        raise PreparationDenied(0, "source_evidence_changed")
    entry_sources = {entry.source_id for entry in entries if entry.source_id}
    if entry_sources != set(ids):
        raise PreparationDenied(0, "source_lineage_changed")
    periods = tuple(sorted({entry.date.isoformat()[:7] for entry in entries} | {
        date.fromisoformat(str(source["date"])[:10]).isoformat()[:7] for source in sources
    }))
    return PreparationIntent("import", periods, ids, (), tuple(sorted(
        {evidence_hash(source) for source in sources} | {evidence_hash([asdict(entry) for entry in entries])}
    )))


def _queue_intent(bound: _Boundary, entity: Entity, entries: list[Entry],
                  sources: list[dict[str, Any]]) -> PreparationIntent:
    from . import queue

    item = queue._load_item(entity, bound.item_id)
    if evidence_hash(item) != bound.item_hash:
        raise PreparationDenied(0, "proposal_changed")
    expected = dict(bound.source_hashes)
    ids = tuple(sorted(str(source.get("id") or "") for source in sources))
    if not ids or len(set(ids)) != len(ids) or any(expected.get(str(source.get("id") or "")) != evidence_hash(source) for source in sources):
        raise PreparationDenied(0, "source_evidence_changed")
    for source_id in ids:
        staged = queue._get_pending_txn(entity, source_id)
        if staged is None or evidence_hash(staged) != expected[source_id]:
            raise PreparationDenied(0, "source_evidence_changed")
    entry_sources = {value for entry in entries for key, value in entry.meta if key in {"source-id", "paired-source-id"}}
    if entry_sources != set(ids):
        raise PreparationDenied(0, "source_lineage_changed")
    periods = tuple(sorted({entry.date.isoformat()[:7] for entry in entries} | {
        date.fromisoformat(str(source["date"])[:10]).isoformat()[:7] for source in sources
    }))
    return PreparationIntent(bound.action, periods, ids, (),
                               tuple(sorted({evidence_hash(source) for source in sources} | {evidence_hash([asdict(entry) for entry in entries])})),
                               requires_human_review=bool(item.get("requires_human_review", False)))


def _correction_intent(bound: _Boundary, store: LedgerStore, conn: sqlite3.Connection,
                       entries: list[Entry], sources: list[dict[str, Any]]) -> PreparationIntent:
    """Bind reversal/replacement to all original ledger lineage at commit."""
    existing = store.load_entries(conn=conn)
    by_source = {entry.source_id: entry for entry in existing if entry.source_id}
    reversed_ids = {dict(entry.meta).get("reverses") for entry in existing}
    original = by_source.get(bound.original_source_id)
    if original is None or bound.original_source_id in reversed_ids:
        raise PreparationDenied(0, "original_entry_not_live")
    if sources or len(entries) != 2:
        raise PreparationDenied(0, "correction_lineage_changed")
    reversal, replacement = entries
    if (dict(reversal.meta).get("reverses") != original.source_id or reversal.source_id is not None
        or reversal.date != original.date or len(reversal.postings) != len(original.postings)
        or any((a.account, a.amount, a.currency) != (b.account, -b.amount, b.currency)
               for a, b in zip(reversal.postings, original.postings))
        or dict(replacement.meta).get("correction-of") != original.source_id
        or not replacement.source_id or replacement.source_id != bound.replacement_source_id
        or replacement.source_id in by_source
        or conn.execute("SELECT 1 FROM source_transactions WHERE id = ?", (replacement.source_id,)).fetchone()):
        raise PreparationDenied(0, "correction_lineage_changed")

    entry_ids = {row["source_id"]: f"entry:{row['id']}" for row in conn.execute(
        "SELECT id, source_id FROM entries WHERE source_id IS NOT NULL")}
    expected = dict(bound.entry_hashes)
    lineage: list[Entry] = []
    seen: set[str] = set()
    current = original
    while True:
        if current.source_id in seen or len(lineage) >= 1000:
            raise PreparationDenied(0, "correction_lineage_changed")
        seen.add(current.source_id)
        lineage.append(current)
        if expected.get(entry_ids[current.source_id]) != evidence_hash(asdict(current)):
            raise PreparationDenied(0, "entry_evidence_changed")
        ancestor_id = dict(current.meta).get("correction-of")
        if ancestor_id is None:
            break
        if ancestor_id not in by_source or ancestor_id not in reversed_ids:
            raise PreparationDenied(0, "correction_lineage_changed")
        current = by_source[ancestor_id]
    source_ids = {value for entry in lineage + [replacement] for key, value in entry.meta
                  if key in {"source-id", "paired-source-id"}}
    return PreparationIntent("correct",
        tuple(sorted({entry.date.isoformat()[:7] for entry in lineage + entries})),
        tuple(sorted(source_ids)), tuple(sorted(entry_ids[entry.source_id] for entry in lineage)),
        tuple(sorted({evidence_hash(asdict(entry)) for entry in lineage} | {evidence_hash([asdict(entry) for entry in entries])})))
