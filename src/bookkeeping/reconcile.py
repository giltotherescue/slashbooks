"""Account reconciliation with persisted discrepancy records.

Compare ledger end-of-day balance for a mapped account against a posted
source balance; persist discrepancy records to ``reports/reconciliation.json``
(append/update by (account, as_of)).

Acceptance Example AE6
----------------------
Ledger checking balance $42,193.55 vs source $42,318.55 -> $125.00 discrepancy
flagged with suspected causes, persisted with open status.

Public API
----------
reconcile(entity_path, account, source_balance, as_of) -> ReconcileResult
    Compute the discrepancy and persist a record.

resolve(entity_path, account, as_of, note) -> None
    Mark an existing discrepancy record as resolved.

add_parser(subparsers) / run(args) — CLI surface.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .reports.cache import get_account_balance, open_cache


# ---------------------------------------------------------------------------
# ReconcileResult
# ---------------------------------------------------------------------------


@dataclass
class ReconcileResult:
    """Result of a single reconciliation check."""

    account: str
    as_of: str           # ISO date string
    ledger_balance: Decimal
    source_balance: Decimal
    discrepancy: Decimal  # source_balance - ledger_balance (positive = source higher)
    status: str           # "clean" | "discrepancy"
    causes: list[str] = field(default_factory=list)
    record_id: str = ""
    source_integrity_residual: Decimal | None = None
    source_snapshot_at: str | None = None
    source_through: str | None = None

    @property
    def is_clean(self) -> bool:
        return self.status == "clean"

    def to_text(self) -> str:
        """Plain-English reconciliation summary."""
        lines = [
            f"Reconciliation  {self.account.replace(':', ' › ')}  as of {self.as_of}",
            "=" * 60,
            f"  Ledger balance:  {_fmt(self.ledger_balance)}",
            f"  Source balance:  {_fmt(self.source_balance)}",
            f"  Difference:      {_fmt(self.discrepancy)}",
            f"  Status:          {self.status.upper()}",
        ]
        if self.source_integrity_residual is not None:
            lines.append(f"  Source integrity residual: {_fmt(self.source_integrity_residual)}")
        if self.status == "source-inconsistent":
            lines.append("  The source activity does not reconcile to its reported balance, so this is not yet a books discrepancy.")
        if self.causes:
            lines.append("  Suspected causes:")
            for c in self.causes:
                lines.append(f"    - {c}")
        return "\n".join(lines)


def _fmt(amount: Decimal) -> str:
    return f"{amount:,.2f}"


# ---------------------------------------------------------------------------
# Reconciliation JSON store
# ---------------------------------------------------------------------------


def _recon_path(entity_path: Path) -> Path:
    return entity_path / "reports" / "reconciliation.json"


def _load_records(entity_path: Path) -> list[dict]:
    """Load existing reconciliation records (returns [] when file absent)."""
    p = _recon_path(entity_path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_records(entity_path: Path, records: list[dict]) -> None:
    """Atomically write reconciliation records."""
    entity_path = Path(entity_path)
    reports_dir = entity_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    dest = _recon_path(entity_path)

    fd, tmp_path = tempfile.mkstemp(dir=str(reports_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, default=str)
            f.write("\n")
        os.replace(tmp_path, str(dest))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _record_id(account: str, as_of: str) -> str:
    """Stable record identifier for (account, as_of) pair."""
    return f"{account}@{as_of}"


# ---------------------------------------------------------------------------
# Heuristic cause detection
# ---------------------------------------------------------------------------


def _detect_causes(
    entity_path: Path,
    account: str,
    as_of: date,
    discrepancy: Decimal,
    source_balance: Decimal | None = None,
    ledger_balance: Decimal | None = None,
) -> list[str]:
    """Return a list of plain-English suspected cause strings."""
    causes: list[str] = []

    # A source balance that is the exact opposite of the ledger balance is a
    # sign mistake: the difference is twice the balance. The shipped sign
    # convention (liabilities negative) stays; say how to restate the figure.
    if (source_balance is not None and ledger_balance is not None and ledger_balance != 0
            and source_balance == -ledger_balance):
        corrected = f"{ledger_balance:.2f}"
        kind = "A liability" if account.startswith("Liabilities:") else "This account"
        causes.append(
            f"Likely sign mistake: the difference ({_fmt(abs(discrepancy))}) is twice the balance. "
            f"{kind} balance in the ledger is {_fmt(ledger_balance)} "
            f"({'liabilities and amounts you owe are negative' if account.startswith('Liabilities:') else 'the sign is the ledger sign'}). "
            f"Run again with the sign flipped: books reconcile --entity {entity_path} --account {account} "
            f"--source-balance {corrected} --as-of {as_of.isoformat()}"
        )

    # Check for pending transactions in staging/pending.json
    pending_file = entity_path / "staging" / "pending.json"
    if pending_file.exists():
        try:
            pending = json.loads(pending_file.read_text(encoding="utf-8"))
            if isinstance(pending, list) and pending:
                causes.append(
                    f"There are {len(pending)} pending transactions in staging "
                    "that may not yet be reflected in the ledger."
                )
        except (json.JSONDecodeError, OSError):
            pass

    # Check for late-arrival entries near as_of (within 30 days)
    try:
        conn = open_cache(entity_path, auto_regenerate=False)
        try:
            from_check = date(as_of.year, as_of.month, max(1, as_of.day - 30))
            rows = conn.execute(
                """SELECT COUNT(*) FROM entries
                   WHERE late_arrival = 1
                     AND date >= ? AND date <= ?""",
                (from_check.isoformat(), as_of.isoformat()),
            ).fetchone()
            if rows and rows[0] > 0:
                causes.append(
                    f"{rows[0]} late-arrival entries were posted near the reconciliation date "
                    "and may affect the balance."
                )

            # Check for entries in a window that might match the discrepancy
            abs_disc = abs(discrepancy)
            rows2 = conn.execute(
                """SELECT e.date, e.narration, p.amount
                   FROM postings p JOIN entries e ON e.id = p.entry_id
                   WHERE p.account = ?
                     AND e.date > ? AND e.date <= ?
                     AND ABS(CAST(p.amount AS REAL) - ?) < 0.01""",
                (
                    account,
                    as_of.isoformat(),
                    date(as_of.year + (1 if as_of.month == 12 else 0),
                         (as_of.month % 12) + 1, 1).isoformat(),
                    float(abs_disc),
                ),
            ).fetchall()
            if rows2:
                causes.append(
                    f"A transaction of {_fmt(abs_disc)} was found just after the reconciliation "
                    "date — it may have been recorded with a slightly different date."
                )
        finally:
            conn.close()
    except Exception:
        pass

    # Check for no entries in the reconciliation window
    try:
        conn = open_cache(entity_path, auto_regenerate=False)
        try:
            rows3 = conn.execute(
                """SELECT COUNT(*) FROM postings p JOIN entries e ON e.id = p.entry_id
                   WHERE p.account = ?""",
                (account,),
            ).fetchone()
            if rows3 and rows3[0] == 0:
                causes.append(
                    f"No entries have been posted to account '{account.replace(':', ' › ')}' in the ledger."
                )
        finally:
            conn.close()
    except Exception:
        pass

    if not causes:
        causes.append(
            "The cause is unclear — review recent transactions for timing differences, "
            "pending items, or missing entries."
        )

    return causes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reconcile(
    entity_path: Path | str,
    account: str,
    source_balance: Decimal,
    as_of: date,
    source_opening_balance: Decimal | None = None,
    source_transaction_total: Decimal | None = None,
    source_snapshot_at: str | None = None,
    source_through: str | None = None,
    source_evidence_refs: list[str] | None = None,
    source_evidence_hashes: dict[str, str] | None = None,
) -> ReconcileResult:
    """Reconcile ledger balance for *account* against *source_balance* as of *as_of*.

    Computes: discrepancy = source_balance - ledger_balance
    Persists a record to ``reports/reconciliation.json`` (open status when
    discrepancy != 0, clean otherwise).
    Returns a :class:`ReconcileResult`.
    """
    entity_path = Path(entity_path)
    from .ledger.importer import _entity_write_lock
    from .preparation_runtime import recover_preparation_reconciliations

    with _entity_write_lock(entity_path):
        recover_preparation_reconciliations(entity_path)
        return _reconcile_unlocked(
            entity_path, account, source_balance, as_of,
            source_opening_balance=source_opening_balance,
            source_transaction_total=source_transaction_total,
            source_snapshot_at=source_snapshot_at,
            source_through=source_through,
            source_evidence_refs=source_evidence_refs,
            source_evidence_hashes=source_evidence_hashes,
        )


def _reconcile_unlocked(
    entity_path: Path,
    account: str,
    source_balance: Decimal,
    as_of: date,
    source_opening_balance: Decimal | None = None,
    source_transaction_total: Decimal | None = None,
    source_snapshot_at: str | None = None,
    source_through: str | None = None,
    source_evidence_refs: list[str] | None = None,
    source_evidence_hashes: dict[str, str] | None = None,
) -> ReconcileResult:
    """Compute and persist a reconciliation while holding the entity lock."""

    conn = open_cache(entity_path)
    try:
        ledger_bal = get_account_balance(conn, account, as_of)
        from .accrual.reports import coverage_metadata, reconciliation as supporting_reconciliation
        from .accrual.scope import accrual_active
        control_support = None
        if accrual_active(conn):
            coverage_metadata(conn, "reconciliation", None, as_of)
            control_support = next((row for row in supporting_reconciliation(conn, as_of) if row["account"] == account), None)
    finally:
        conn.close()

    discrepancy = source_balance - ledger_bal
    source_residual: Decimal | None = None
    source_inconsistent = False
    if source_opening_balance is not None and source_transaction_total is not None:
        source_residual = source_opening_balance + source_transaction_total - source_balance
        source_inconsistent = abs(source_residual) >= Decimal("0.01")
    if control_support and control_support["status"] == "unreconciled":
        source_inconsistent = True
    is_clean = abs(discrepancy) < Decimal("0.01")

    status = "source-inconsistent" if source_inconsistent else ("clean" if is_clean else "discrepancy")
    causes: list[str] = []
    if control_support and control_support["status"] == "unreconciled":
        causes.append(control_support.get("message") or f"Supporting register differs by {control_support['difference']} USD; journal balance alone is insufficient.")

    if not is_clean and not source_inconsistent:
        causes = _detect_causes(entity_path, account, as_of, discrepancy, source_balance, ledger_bal)

    rec_id = _record_id(account, as_of.isoformat())

    # Persist / update record
    records = _load_records(entity_path)
    # Find existing record for this (account, as_of)
    existing_idx: Optional[int] = None
    for i, r in enumerate(records):
        if r.get("record_id") == rec_id:
            existing_idx = i
            break

    now = datetime.now(timezone.utc).isoformat()
    record: dict[str, Any] = {
        "record_id": rec_id,
        "account": account,
        "as_of": as_of.isoformat(),
        "ledger_balance": str(ledger_bal),
        "source_balance": str(source_balance),
        "discrepancy": str(discrepancy),
        "status": "source-inconsistent" if source_inconsistent else ("open" if not is_clean else "clean"),
        "causes": causes,
        "source_integrity_residual": str(source_residual) if source_residual is not None else None,
        "source_opening_balance": str(source_opening_balance) if source_opening_balance is not None else None,
        "source_transaction_total": str(source_transaction_total) if source_transaction_total is not None else None,
        "source_snapshot_at": source_snapshot_at,
        "source_through": source_through,
        "source_evidence_refs": sorted(source_evidence_refs or []),
        "source_evidence_hashes": dict(sorted((source_evidence_hashes or {}).items())),
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
        "resolution_note": None,
    }
    if control_support:
        record["supporting_reconciliation"] = control_support

    if existing_idx is not None:
        existing = records[existing_idx]
        record["created_at"] = existing.get("created_at") or now
        # A resolution applies only to the exact balances that were reviewed.
        # Changed source or ledger data must reopen the record automatically.
        resolution_fields = (
            "ledger_balance",
            "source_balance",
            "discrepancy",
            "source_integrity_residual",
            "source_opening_balance",
            "source_transaction_total",
            "source_snapshot_at",
            "source_through",
            "source_evidence_refs",
            "source_evidence_hashes",
        )
        same_evidence = all(existing.get(field) == record.get(field) for field in resolution_fields)
        if existing.get("status") == "resolved" and same_evidence and not source_inconsistent:
            record["status"] = "resolved"
            record["resolved_at"] = existing.get("resolved_at")
            record["resolution_note"] = existing.get("resolution_note")
        records[existing_idx] = record
    else:
        records.append(record)

    from .preparation_runtime import commit_preparation_reconciliation
    if not commit_preparation_reconciliation(entity_path, account, as_of.isoformat(), records):
        _save_records(entity_path, records)

    return ReconcileResult(
        account=account,
        as_of=as_of.isoformat(),
        ledger_balance=ledger_bal,
        source_balance=source_balance,
        discrepancy=discrepancy,
        status=status,
        causes=causes,
        record_id=rec_id,
        source_integrity_residual=source_residual,
        source_snapshot_at=source_snapshot_at,
        source_through=source_through,
    )


def resolve(
    entity_path: Path | str,
    account: str,
    as_of: date | str,
    note: str = "",
) -> None:
    """Mark a discrepancy record as resolved.

    Raises :class:`KeyError` when the record is not found.
    """
    entity_path = Path(entity_path)
    from .ledger.importer import _entity_write_lock
    from .preparation_runtime import recover_preparation_reconciliations

    with _entity_write_lock(entity_path):
        recover_preparation_reconciliations(entity_path)
        _resolve_unlocked(entity_path, account, as_of, note)


def _resolve_unlocked(
    entity_path: Path,
    account: str,
    as_of: date | str,
    note: str = "",
) -> None:
    """Resolve one reconciliation while holding the entity write lock."""
    as_of_str = as_of.isoformat() if isinstance(as_of, date) else as_of
    rec_id = _record_id(account, as_of_str)

    records = _load_records(entity_path)
    for r in records:
        if r.get("record_id") == rec_id:
            if r.get("status") == "source-inconsistent":
                raise ValueError(
                    "Source-inconsistent reconciliation records cannot be resolved manually; "
                    "refresh the source period and reconcile again."
                )
            r["status"] = "resolved"
            r["resolved_at"] = datetime.now(timezone.utc).isoformat()
            r["resolution_note"] = note
            _save_records(entity_path, records)
            return

    raise KeyError(f"Reconciliation record not found: {rec_id}")


def list_discrepancies(
    entity_path: Path | str,
    *,
    status: Optional[str] = None,
) -> list[dict]:
    """Return reconciliation records, optionally filtered by status."""
    entity_path = Path(entity_path)
    records = _load_records(entity_path)
    if status:
        return [r for r in records if r.get("status") == status]
    return records


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def add_parser(subparsers: Any) -> None:
    """Register the ``reconcile`` subcommand."""
    p = subparsers.add_parser(
        "reconcile",
        help="Reconcile account balances against source/bank balances",
    )
    p.add_argument("--entity", required=True, help="Path to entity directory")
    p.add_argument("--account", default=None, help="Account name to reconcile")
    p.add_argument("--all", dest="all_accounts", action="store_true", help="Reconcile all accounts")
    p.add_argument(
        "--source-balance",
        dest="source_balance",
        default=None,
        help="Source/bank balance as a decimal amount (e.g. 42318.55)",
    )
    p.add_argument("--source-opening-balance", default=None, help="Source opening balance for the activity window")
    p.add_argument("--source-transaction-total", default=None, help="Signed total of source activity in that same window")
    p.add_argument("--source-snapshot-at", default=None, help="Timestamp when the source ending balance was read")
    p.add_argument("--source-through", default=None, help="Latest effective transaction date included by the source")
    p.add_argument("--source-evidence-ref", action="append", dest="source_evidence_refs", default=[],
                   help="Stable provider record or statement reference supporting the source figures")
    p.add_argument("--source-evidence-hash", action="append", dest="source_evidence_hashes", default=[],
                   metavar="REF=SHA256", help="Verified SHA-256 attestation for a source evidence reference")
    p.add_argument(
        "--balances-json",
        dest="balances_json",
        default=None,
        help="JSON file with {account: balance} mapping",
    )
    p.add_argument(
        "--as-of",
        dest="as_of",
        required=True,
        help="Reconciliation date (YYYY-MM-DD)",
    )

    # resolve subcommand
    resolve_p = subparsers.add_parser("reconcile-resolve", help="Mark a reconciliation discrepancy as resolved")
    resolve_p.add_argument("--entity", required=True)
    resolve_p.add_argument("--account", required=True)
    resolve_p.add_argument("--as-of", dest="as_of", required=True)
    resolve_p.add_argument("--note", default="")


def run(args: Any) -> int:
    """Dispatch reconcile command."""
    import sys
    cmd = getattr(args, "command", None)

    if cmd == "reconcile-resolve":
        entity = Path(args.entity)
        try:
            resolve(entity, args.account, args.as_of, args.note)
            print(f"Marked resolved: {args.account} as of {args.as_of}")
        except (KeyError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0

    if cmd == "reconcile":
        entity = Path(args.entity)
        as_of = date.fromisoformat(args.as_of)

        accounts_and_balances: list[tuple[str, Decimal]] = []

        if args.balances_json:
            data = json.loads(Path(args.balances_json).read_text(encoding="utf-8"))
            for acct, bal in data.items():
                accounts_and_balances.append((acct, Decimal(str(bal))))
        elif args.account and args.source_balance:
            accounts_and_balances.append((args.account, Decimal(args.source_balance)))
        else:
            print("Error: provide --account + --source-balance or --balances-json", file=sys.stderr)
            return 1

        if (args.source_opening_balance is None) != (args.source_transaction_total is None):
            print("Error: source integrity needs both --source-opening-balance and --source-transaction-total", file=sys.stderr)
            return 1
        opening = Decimal(args.source_opening_balance) if args.source_opening_balance is not None else None
        activity = Decimal(args.source_transaction_total) if args.source_transaction_total is not None else None
        from .preparation_runtime import prepared_reconciliation_evidence
        verified_evidence = prepared_reconciliation_evidence(args.account or "", args.as_of)
        evidence_refs = list(args.source_evidence_refs)
        evidence_hashes: dict[str, str] = {}
        if verified_evidence is not None:
            evidence_refs = sorted(verified_evidence)
            evidence_hashes = verified_evidence
        else:
            try:
                for value in args.source_evidence_hashes:
                    ref, separator, digest = value.partition("=")
                    if (not separator or not ref or ref in evidence_hashes
                            or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                        raise ValueError("source evidence hashes must use unique REF=SHA256 values")
                    evidence_hashes[ref] = digest
                if evidence_hashes and set(evidence_hashes) != set(evidence_refs):
                    raise ValueError("source evidence hashes must exactly match the evidence references")
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
        for account, source_bal in accounts_and_balances:
            result = reconcile(
                entity, account, source_bal, as_of,
                source_opening_balance=opening,
                source_transaction_total=activity,
                source_snapshot_at=args.source_snapshot_at,
                source_through=args.source_through,
                source_evidence_refs=evidence_refs,
                source_evidence_hashes=evidence_hashes,
            )
            print(result.to_text())
            print()

        return 0

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2
