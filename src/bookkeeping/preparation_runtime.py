"""Trusted execution boundary for supported ordinary bookkeeping commands.

No CLI flag, environment variable, or workspace file can establish this context.
An embedding host must authenticate and fence the operation independently.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any, Mapping
import uuid

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
    ("queue", "propose-intercompany"): "categorize",
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
    if path[0] == "accrual":
        if path[1:] in {("schema",), ("capabilities",), ("schedule", "list"), ("item", "list"), ("report",),
                        ("accounts",), ("receipt",), ("opening", "draft")}:
            return PreparationCommandSpec(path, "read", None)
        if path[1:] in {("intake",), ("journal", "preview"), ("journal", "apply"),
                        ("journal", "correct-preview"), ("journal", "correct-apply"),
                        ("journal", "reverse-preview"), ("journal", "reverse-apply"),
                        ("journal", "due-reversals"), ("schedule", "preview"), ("schedule", "apply"),
                        ("schedule", "due-preview"), ("schedule", "due-apply"),
                        ("schedule", "change-preview"), ("schedule", "change-apply"),
                        ("item", "preview"), ("item", "apply"), ("item", "settle-preview"),
                        ("item", "settle-apply"), ("item", "adjust-preview"), ("item", "adjust-apply"),
                        ("item", "offset-preview"), ("item", "offset-apply"),
                        ("opening", "preview"), ("opening", "apply"),
                        ("migration", "preview"), ("migration", "apply"), ("support", "preview"), ("support", "apply"),
                        ("evidence-link", "preview"), ("evidence-link", "apply")}:
            return PreparationCommandSpec(path, "ledger", "adjust")
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
    proposal_sources_json: str | None = None
    reconciliation_intent: PreparationIntent | None = None
    accrual_intent: PreparationIntent | None = None
    accrual_batch: bool = False


_active: ContextVar[_Boundary | None] = ContextVar("books_preparation", default=None)


def is_preparation() -> bool:
    return _active.get() is not None


def active_preparation_identity() -> tuple[str, str, dict[str, str]] | None:
    """Return trusted recovery identity for the active in-process boundary."""
    bound = _active.get()
    if bound is None:
        return None
    return bound.context.entity_key, bound.context.execution_id, dict(bound.source_hashes)


def authorize_accrual_operation(entity: Entity, dates: Any, records: Any, entries: Any) -> dict[str, Any]:
    """Derive adjustment authority from validated dates and canonical evidence.

    Called only by accounting domain code. Workspace policies and JSON authority
    claims never establish the in-process host boundary.
    """
    bound = _active.get()
    if bound is None:
        return {"mode": "local_operator"}
    if bound.action != "adjust" or entity.path.resolve() != bound.entity_path:
        raise PreparationDenied(0, "adjustment_context_required")
    expected = dict(bound.source_hashes)
    actual = {row["id"]: row["evidence_hash"] for row in records}
    if actual != expected and not (bound.accrual_batch and all(expected.get(key) == value for key, value in actual.items())):
        raise PreparationDenied(0, "source_evidence_changed")
    periods = tuple(sorted({date.fromisoformat(day).isoformat()[:7] for day in dates}))
    if not periods:
        raise PreparationDenied(0, "source_period_invalid")
    source_ids = {value for row in records for value in (row["id"], "evidence:" + row["source_identity"])}
    referenced_sources = {value for entry in entries for key, value in entry.meta
                          if key in {"reverses", "correction-of", "origin-source-id"}}
    source_ids.update(referenced_sources)
    source_ids.update(entry.source_id for entry in entries if entry.source_id)
    entry_ids = set()
    if referenced_sources:
        with LedgerStore(entity.path / "ledger.sqlite").connection() as conn:
            entry_ids = {f"entry:{row['id']}" for row in conn.execute("SELECT id,source_id FROM entries")
                         if row["source_id"] in referenced_sources}
    ids = tuple(sorted(source_ids))
    hashes = tuple(sorted({row["evidence_hash"] for row in records} | {evidence_hash([asdict(e) for e in entries])}))
    intent = PreparationIntent("adjust", periods, ids, tuple(sorted(entry_ids)), hashes)
    receipt, = authorize_batch(bound.context, [intent])
    _active.set(replace(bound, accrual_intent=intent))
    return {"mode": "standing_policy", "entity_key": bound.context.entity_key,
            "policy_hash": receipt.policy_hash, "policy_version": receipt.policy_version,
            "revision": bound.context.current_revision}


def advance_accrual_batch(entity: Entity, previous_digest: str) -> None:
    """Advance a fenced due-reversal batch only after its own committed write.

    Domain code holds the company write lock across the complete batch. Single
    operations keep their original fence and exact verified-evidence set.
    """
    bound = _active.get()
    if bound is None or not bound.accrual_batch:
        return
    if entity.path.resolve() != bound.entity_path or bound.ledger_digest != previous_digest:
        raise PreparationDenied(0, "ledger_changed")
    _active.set(replace(bound, ledger_digest=LedgerStore(entity.path / "ledger.sqlite").content_digest()))


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
    accrual = path[0] == "accrual"
    resolved = entity_path.resolve()
    if Path(args.entity).resolve() != resolved:
        raise PreparationDenied(0, "workspace_mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_ledger_digest):
        raise PolicyError("Expected the trusted ledger fingerprint.")
    if not isinstance(source_hashes, Mapping) or not (0 if correction or accrual else 1) <= len(source_hashes) <= 1000 or any(
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
    if accrual:
        bound = _Boundary(context, resolved, expected_ledger_digest, tuple(sorted(source_hashes.items())), "", "", "adjust",
                          accrual_batch=path == ("accrual", "journal", "due-reversals"))
    elif correction:
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
        if proposal not in {"category", "split", "transfer-pair", "intercompany"}:
            raise PreparationDenied(0, "specialized_policy_required")
        action = {"split": "split", "transfer-pair": "match_transfer"}.get(proposal, "categorize")
        raw_ids = item.get("source_ids") if action == "match_transfer" else [item.get("source_id")]
        if (not isinstance(raw_ids, list) or not raw_ids
                or any(not isinstance(source_id, str) or not source_id for source_id in raw_ids)
                or len(set(raw_ids)) != len(raw_ids)
                or set(raw_ids) != set(source_hashes)):
            raise PreparationDenied(0, "source_evidence_changed")
        ids = raw_ids
        if item.get("status") not in {"open", "reopened"} and any(
            store.source_exists(source_id) for source_id in ids
        ):
            raise PreparationDenied(0, "posted_source_requires_reconciliation")
        bound = _Boundary(context, resolved, expected_ledger_digest, tuple(sorted(source_hashes.items())),
                          args.item_id, evidence_hash(item), action)
    elif path in _PROPOSAL_ACTIONS:
        ids = list(args.source_ids) if hasattr(args, "source_ids") else [args.source_id]
        intent, sources = _source_intent(entity, _PROPOSAL_ACTIONS[path], ids, source_hashes)
        authorize_batch(context, [intent])
        bound = _Boundary(
            context, resolved, expected_ledger_digest, tuple(sorted(source_hashes.items())),
            "", "", _PROPOSAL_ACTIONS[path],
            proposal_sources_json=json.dumps(
                sources, sort_keys=True, separators=(",", ":"), allow_nan=False
            ),
        )
    elif path == ("reconcile",):
        intent = _reconciliation_intent(args, source_hashes)
        authorize_batch(context, [intent])
        bound = _Boundary(
            context, resolved, expected_ledger_digest, tuple(sorted(source_hashes.items())),
            "", "", "reconcile", reconciliation_intent=intent,
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
        if accrual:
            from .accrual.cli import run
            return run(args)
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
    if bound.action == "adjust":
        if bound.accrual_intent is None:
            raise PreparationDenied(0, "adjustment_intent_required")
        # The financial writer receives the exact validated postings authorized
        # by the domain preflight; no caller-supplied affected period is trusted.
        actual_periods = {entry.date.isoformat()[:7] for entry in entries}
        if not actual_periods <= set(bound.accrual_intent.periods):
            raise PreparationDenied(0, "source_period_invalid")
        if evidence_hash([asdict(e) for e in entries]) not in bound.accrual_intent.evidence_hashes:
            raise PreparationDenied(0, "entry_evidence_changed")
        intent = bound.accrual_intent
    elif bound.action == "correct":
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


_RECONCILIATION_TRANSACTION_DIR = ".reconciliation-transactions"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_fsynced(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _reconciliation_transaction_root(entity_path: Path) -> Path:
    return Path(entity_path) / "reports" / _RECONCILIATION_TRANSACTION_DIR


def _remove_reconciliation_transaction(path: Path) -> None:
    parent = path.parent
    shutil.rmtree(path)
    _fsync_directory(parent)


def _load_reconciliation_transaction(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        report = (path / "report.json").read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Reconciliation transaction '{path.name}' is unreadable.") from exc
    required = {
        "schema_version", "transaction_id", "execution_id", "intent_hash",
        "report_sha256", "destination", "ledger_digest", "receipt",
    }
    receipt = manifest.get("receipt") if isinstance(manifest, dict) else None
    receipt_keys = {
        "rule", "policy_id", "policy_version", "policy_hash", "execution_id",
        "expected_revision", "intent_hash",
    }
    if (not isinstance(manifest, dict) or set(manifest) != required
            or manifest.get("schema_version") != 1
            or manifest.get("transaction_id") != path.name
            or manifest.get("destination") != "reconciliation.json"
            or any(not isinstance(manifest.get(key), str) or not manifest[key]
                   for key in required - {"schema_version", "receipt"})
            or not re.fullmatch(r"[0-9a-f]{32}", manifest["transaction_id"])
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["intent_hash"])
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["report_sha256"])
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["ledger_digest"])
            or not isinstance(receipt, dict) or set(receipt) != receipt_keys
            or receipt.get("rule") != "standing_preparation_v1"
            or receipt.get("execution_id") != manifest["execution_id"]
            or receipt.get("intent_hash") != manifest["intent_hash"]
            or any(not isinstance(receipt.get(key), str) or not receipt[key]
                   for key in ("policy_id", "execution_id"))
            or type(receipt.get("policy_version")) is not int
            or type(receipt.get("expected_revision")) is not int
            or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("policy_hash", "")))
            or hashlib.sha256(report).hexdigest() != manifest["report_sha256"]):
        raise ValueError(f"Reconciliation transaction '{path.name}' is invalid.")
    return manifest, report


def _reconciliation_audit(store: LedgerStore, manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    matches = []
    for event in store.load_audit_events():
        if event.get("type") != "automation-preparation":
            continue
        payload = event.get("payload")
        receipt = payload.get("receipt") if isinstance(payload, dict) else None
        commit = payload.get("reconciliation_commit") if isinstance(payload, dict) else None
        intent = payload.get("intent") if isinstance(payload, dict) else None
        if (isinstance(receipt, dict) and isinstance(commit, dict)
                and isinstance(intent, dict)
                and evidence_hash(intent) == receipt.get("intent_hash")
                and receipt == manifest.get("receipt")
                and commit.get("transaction_id") == manifest.get("transaction_id")
                and commit.get("report_sha256") == manifest.get("report_sha256")
                and commit.get("ledger_digest") == manifest.get("ledger_digest")):
            matches.append(event)
    if len(matches) > 1:
        raise ValueError("Duplicate reconciliation preparation audits require review.")
    return matches[0] if matches else None


def _published_reconciliation_audit(store: LedgerStore, receipt_identity: Mapping[str, Any],
                                    ledger_digest: str) -> dict[str, Any] | None:
    matches = []
    for event in store.load_audit_events():
        if event.get("type") != "automation-preparation":
            continue
        payload = event.get("payload")
        receipt = payload.get("receipt") if isinstance(payload, dict) else None
        commit = payload.get("reconciliation_commit") if isinstance(payload, dict) else None
        intent = payload.get("intent") if isinstance(payload, dict) else None
        if (isinstance(receipt, dict) and isinstance(commit, dict)
                and isinstance(intent, dict)
                and evidence_hash(intent) == receipt.get("intent_hash")
                and receipt.get("execution_id") == receipt_identity.get("execution_id")
                and receipt.get("intent_hash") == receipt_identity.get("intent_hash")):
            matches.append(event)
    if len(matches) > 1:
        raise ValueError("Duplicate reconciliation preparation audits require review.")
    if not matches:
        return None
    event = matches[0]
    payload = event["payload"]
    if (payload["receipt"] != dict(receipt_identity)
            or payload["reconciliation_commit"].get("ledger_digest") != ledger_digest):
        raise PreparationDenied(0, "reconciliation_replay_identity_changed")
    return event


def _publish_reconciliation_report(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    _write_fsynced(temporary, payload)
    os.replace(temporary, destination)
    _fsync_directory(destination.parent)


def recover_preparation_reconciliations(entity_path: Path) -> None:
    """Finish audited reconciliation reports and discard unaudited stages."""
    root = _reconciliation_transaction_root(Path(entity_path))
    if not root.exists():
        return
    transactions = sorted(path for path in root.iterdir() if path.is_dir())
    if len(transactions) > 1:
        raise ValueError("Multiple unfinished reconciliation transactions require review.")
    store = LedgerStore(Path(entity_path) / "ledger.sqlite")
    for transaction in transactions:
        if not (transaction / "manifest.json").exists():
            _remove_reconciliation_transaction(transaction)
            continue
        manifest, report = _load_reconciliation_transaction(transaction)
        if _reconciliation_audit(store, manifest) is not None:
            if store.content_digest() != manifest["ledger_digest"]:
                raise PreparationDenied(0, "ledger_changed")
            _publish_reconciliation_report(
                Path(entity_path) / "reports" / manifest["destination"], report
            )
        _remove_reconciliation_transaction(transaction)


def _stage_reconciliation_report(entity_path: Path, manifest: dict[str, Any],
                                 report: bytes) -> Path:
    root = _reconciliation_transaction_root(entity_path)
    root.mkdir(parents=True, exist_ok=True)
    _fsync_directory(root.parent)
    transaction = root / manifest["transaction_id"]
    transaction.mkdir()
    _write_fsynced(transaction / "report.json", report)
    _write_fsynced(
        transaction / "manifest.json",
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    _fsync_directory(transaction)
    _fsync_directory(root)
    return transaction


def _append_reconciliation_audit(bound: _Boundary, intent: PreparationIntent,
                                 manifest: Mapping[str, Any], ts: str | None) -> None:
    store = LedgerStore(bound.entity_path / "ledger.sqlite")
    with store.transaction(immediate=True) as conn:
        if store.content_digest(conn) != bound.ledger_digest:
            raise PreparationDenied(0, "ledger_changed")
        receipt, = authorize_batch(bound.context, [intent])
        if receipt.intent_hash != manifest["intent_hash"]:
            raise PreparationDenied(0, "reconciliation_scope_changed")
        if asdict(receipt) != manifest["receipt"] or bound.ledger_digest != manifest["ledger_digest"]:
            raise PreparationDenied(0, "reconciliation_replay_identity_changed")
        store.append_audit_event(
            "automation-preparation",
            {
                "receipt": asdict(receipt),
                "intent": asdict(intent),
                "reconciliation_commit": {
                    "schema_version": 1,
                    "transaction_id": manifest["transaction_id"],
                    "report_sha256": manifest["report_sha256"],
                    "ledger_digest": manifest["ledger_digest"],
                },
            },
            conn,
            ts=ts,
        )


def commit_preparation_reconciliation(entity_path: Path, account: str, as_of: str,
                                      records: list[dict[str, Any]],
                                      ts: str | None = None) -> bool:
    """Commit one prepared report only after its final authority audit."""
    bound = _active.get()
    if bound is None:
        return False
    primary = f"reconciliation:{account}:{as_of}"
    intent = bound.reconciliation_intent
    if (bound.action != "reconcile" or intent is None
            or Path(entity_path).resolve() != bound.entity_path
            or primary not in intent.source_ids):
        raise PreparationDenied(0, "reconciliation_scope_changed")
    report = (json.dumps(records, indent=2, default=str) + "\n").encode()
    intent_hash = evidence_hash(asdict(intent))
    receipt_identity = {
        "rule": "standing_preparation_v1",
        "policy_id": bound.context.policy.policy_id,
        "policy_version": bound.context.policy.version,
        "policy_hash": bound.context.policy.fingerprint,
        "execution_id": bound.context.execution_id,
        "expected_revision": bound.context.expected_revision,
        "intent_hash": intent_hash,
    }
    store = LedgerStore(bound.entity_path / "ledger.sqlite")
    previous = _published_reconciliation_audit(
        store, receipt_identity, bound.ledger_digest
    )
    destination = bound.entity_path / "reports" / "reconciliation.json"
    if previous is not None:
        commit = previous["payload"]["reconciliation_commit"]
        if (destination.is_file()
                and hashlib.sha256(destination.read_bytes()).hexdigest()
                == commit["report_sha256"]):
            return True
        raise PreparationDenied(0, "reconciliation_recovery_missing")
    manifest = {
        "schema_version": 1,
        "transaction_id": uuid.uuid4().hex,
        "execution_id": bound.context.execution_id,
        "intent_hash": intent_hash,
        "report_sha256": hashlib.sha256(report).hexdigest(),
        "destination": "reconciliation.json",
        "ledger_digest": bound.ledger_digest,
        "receipt": receipt_identity,
    }
    transaction = _stage_reconciliation_report(bound.entity_path, manifest, report)
    _append_reconciliation_audit(bound, intent, manifest, ts)
    _publish_reconciliation_report(destination, report)
    _remove_reconciliation_transaction(transaction)
    return True


def _source_intent(entity: Entity, action: str, ids: list[str],
                   source_hashes: Mapping[str, str]) -> tuple[PreparationIntent, list[dict[str, Any]]]:
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
    return (PreparationIntent(action, periods, tuple(sorted(ids)), (),
                              tuple(sorted(evidence_hash(source) for source in sources))), sources)


def prepared_proposal_sources(entity: Entity, action: str,
                              source_ids: list[str]) -> list[dict[str, Any]] | None:
    """Revalidate and return the authorized proposal snapshot at persistence.

    Queue proposal writers call this while holding the entity write lock. The
    same lock also fences canonical ledger writers, so neither the staged rows
    nor the ledger revision can change between this check and proposal saves.
    """
    bound = _active.get()
    if bound is None or bound.proposal_sources_json is None:
        return None
    if entity.path.resolve() != bound.entity_path:
        raise PreparationDenied(0, "workspace_mismatch")
    if action != bound.action or tuple(sorted(source_ids)) != tuple(sorted(dict(bound.source_hashes))):
        raise PreparationDenied(0, "source_evidence_changed")
    store = LedgerStore(entity.path / "ledger.sqlite")
    if store.content_digest() != bound.ledger_digest:
        raise PreparationDenied(0, "ledger_changed")
    snapshots = json.loads(bound.proposal_sources_json)
    if not isinstance(snapshots, list):
        raise PreparationDenied(0, "source_evidence_changed")
    expected = dict(bound.source_hashes)
    from . import queue
    for source_id in source_ids:
        staged = queue._get_pending_txn(entity, source_id)
        if (staged is None or expected.get(source_id) != evidence_hash(staged)):
            raise PreparationDenied(0, "source_evidence_changed")
    snapshot_by_id = {str(item.get("id") or ""): item for item in snapshots if isinstance(item, dict)}
    if (set(snapshot_by_id) != set(source_ids)
            or any(evidence_hash(snapshot_by_id[source_id]) != expected[source_id]
                   for source_id in source_ids)):
        raise PreparationDenied(0, "source_evidence_changed")
    intent, _ = _source_intent(entity, action, source_ids, expected)
    authorize_batch(bound.context, [intent])
    return [dict(snapshot_by_id[source_id]) for source_id in source_ids]


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
