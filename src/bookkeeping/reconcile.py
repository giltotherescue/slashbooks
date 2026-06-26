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
) -> list[str]:
    """Return a list of plain-English suspected cause strings."""
    causes: list[str] = []

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
) -> ReconcileResult:
    """Reconcile ledger balance for *account* against *source_balance* as of *as_of*.

    Computes: discrepancy = source_balance - ledger_balance
    Persists a record to ``reports/reconciliation.json`` (open status when
    discrepancy != 0, clean otherwise).
    Returns a :class:`ReconcileResult`.
    """
    entity_path = Path(entity_path)

    conn = open_cache(entity_path)
    try:
        ledger_bal = get_account_balance(conn, account, as_of)
    finally:
        conn.close()

    discrepancy = source_balance - ledger_bal
    is_clean = abs(discrepancy) < Decimal("0.01")

    status = "clean" if is_clean else "discrepancy"
    causes: list[str] = []

    if not is_clean:
        causes = _detect_causes(entity_path, account, as_of, discrepancy)

    rec_id = _record_id(account, as_of.isoformat())

    # Persist / update record
    records = _load_records(entity_path)
    # Find existing record for this (account, as_of)
    existing_idx: Optional[int] = None
    for i, r in enumerate(records):
        if r.get("record_id") == rec_id:
            existing_idx = i
            break

    record: dict[str, Any] = {
        "record_id": rec_id,
        "account": account,
        "as_of": as_of.isoformat(),
        "ledger_balance": str(ledger_bal),
        "source_balance": str(source_balance),
        "discrepancy": str(discrepancy),
        "status": "open" if not is_clean else "clean",
        "causes": causes,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolved_at": None,
        "resolution_note": None,
    }

    if existing_idx is not None:
        # Preserve resolution status if already resolved
        existing = records[existing_idx]
        if existing.get("status") == "resolved":
            record["status"] = "resolved"
            record["resolved_at"] = existing.get("resolved_at")
            record["resolution_note"] = existing.get("resolution_note")
        records[existing_idx] = record
    else:
        records.append(record)

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
    as_of_str = as_of.isoformat() if isinstance(as_of, date) else as_of
    rec_id = _record_id(account, as_of_str)

    records = _load_records(entity_path)
    for r in records:
        if r.get("record_id") == rec_id:
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
        except KeyError as e:
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

        for account, source_bal in accounts_and_balances:
            result = reconcile(entity, account, source_bal, as_of)
            print(result.to_text())
            print()

        return 0

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2
