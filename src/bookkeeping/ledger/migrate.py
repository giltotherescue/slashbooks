from __future__ import annotations

"""Migration helpers for loading Beancount ledgers into the SQLite store."""

import argparse
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..entity import load_entity
from .store import LedgerStore, StoreCounts, default_store_path, replace_store_atomically
from .validator import parse_ledger, validate


@dataclass
class MigrationResult:
    success: bool
    store_path: Path
    dry_run: bool = False
    ledger_sha256: str = ""
    counts: StoreCounts = field(default_factory=StoreCounts)
    validation_errors: list[Any] = field(default_factory=list)
    error_message: str = ""
    skipped: bool = False

    def __bool__(self) -> bool:
        return self.success


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def migrate_beancount_to_store(
    entity_path: Path | str,
    *,
    store_path: Path | str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> MigrationResult:
    """Load ``books.beancount`` into a canonical SQLite store.

    The source ledger is never modified.  Non-dry runs build a temporary store
    first, then atomically replace the destination.
    """
    entity_path = Path(entity_path)
    dest = Path(store_path) if store_path is not None else default_store_path(entity_path)
    books_path = entity_path / "books.beancount"
    if not books_path.exists():
        return MigrationResult(
            success=False,
            store_path=dest,
            dry_run=dry_run,
            error_message=f"Ledger file not found: {books_path}",
        )

    ledger_text = books_path.read_text(encoding="utf-8")
    errors = validate(ledger_text) if ledger_text.strip() else []
    if errors:
        return MigrationResult(
            success=False,
            store_path=dest,
            dry_run=dry_run,
            validation_errors=errors,
            error_message=f"Ledger validation failed with {len(errors)} error(s).",
        )

    ledger_sha = _sha256_text(ledger_text)
    if dest.exists() and not force:
        existing = LedgerStore(dest)
        try:
            if existing.get_meta("source_ledger_sha256") == ledger_sha:
                return MigrationResult(
                    success=True,
                    store_path=dest,
                    dry_run=dry_run,
                    ledger_sha256=ledger_sha,
                    counts=existing.counts(),
                    skipped=True,
                )
        except Exception:
            pass

    parsed = parse_ledger(ledger_text) if ledger_text.strip() else {
        "opens": [],
        "entries": [],
        "balances": [],
        "title": "Books",
    }
    counts = StoreCounts(
        accounts=len(parsed["opens"]),
        entries=len(parsed["entries"]),
        postings=sum(len(entry.postings) for entry in parsed["entries"]),
        balances=len(parsed["balances"]),
        audit_events=1,
    )
    if dry_run:
        return MigrationResult(
            success=True,
            store_path=dest,
            dry_run=True,
            ledger_sha256=ledger_sha,
            counts=counts,
        )

    tmp = dest.with_suffix(dest.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    store = LedgerStore(tmp)
    store.initialize()
    try:
        with store.transaction() as conn:
            store.insert_opens(parsed["opens"], conn)
            store.insert_entries(parsed["entries"], conn)
            store.insert_balances(parsed["balances"], conn)
            store.set_meta("source_ledger_sha256", ledger_sha, conn)
            store.set_meta("title", str(parsed.get("title") or "Books"), conn)
            store.append_audit_event(
                "migration",
                {
                    "source": "books.beancount",
                    "ledger_sha256": ledger_sha,
                    "entries": len(parsed["entries"]),
                    "postings": counts.postings,
                },
                conn,
            )
        replace_store_atomically(tmp, dest)
    except Exception as exc:
        if tmp.exists():
            tmp.unlink()
        return MigrationResult(
            success=False,
            store_path=dest,
            dry_run=dry_run,
            ledger_sha256=ledger_sha,
            error_message=str(exc),
        )

    final_store = LedgerStore(dest)
    return MigrationResult(
        success=True,
        store_path=dest,
        dry_run=False,
        ledger_sha256=ledger_sha,
        counts=final_store.counts(),
    )


def add_parser(subparsers: Any) -> None:
    ledger_parser = subparsers.add_parser("ledger", help="Manage the canonical ledger store")
    ledger_sub = ledger_parser.add_subparsers(dest="ledger_command", required=True)

    migrate = ledger_sub.add_parser("migrate", help="Load books.beancount into ledger.sqlite")
    migrate.add_argument("--entity", required=True, type=Path)
    migrate.add_argument("--store", type=Path, default=None)
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--force", action="store_true")

    snapshot = ledger_sub.add_parser("snapshot", help="Render a Beancount snapshot from ledger.sqlite")
    snapshot.add_argument("--entity", required=True, type=Path)
    snapshot.add_argument("--store", type=Path, default=None)
    snapshot.add_argument("--output", required=True, type=Path)


def run(args: argparse.Namespace) -> int:
    if args.ledger_command == "migrate":
        entity = load_entity(args.entity)
        result = migrate_beancount_to_store(
            entity.path,
            store_path=args.store,
            dry_run=args.dry_run,
            force=args.force,
        )
        if not result:
            print(result.error_message)
            for error in result.validation_errors[:10]:
                print(f"  {error}")
            return 1
        action = "Checked" if result.dry_run else "Migrated"
        if result.skipped:
            action = "Already current"
        print(f"{action}: {result.store_path}")
        print(f"  accounts: {result.counts.accounts}")
        print(f"  entries:  {result.counts.entries}")
        print(f"  postings: {result.counts.postings}")
        print(f"  balances: {result.counts.balances}")
        return 0

    if args.ledger_command == "snapshot":
        from .projections import render_store_ledger

        entity = load_entity(args.entity)
        store_path = args.store or default_store_path(entity.path)
        text = render_store_ledger(store_path)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"Snapshot written: {args.output}")
        return 0

    print(f"Unknown ledger command: {args.ledger_command}")
    return 2
