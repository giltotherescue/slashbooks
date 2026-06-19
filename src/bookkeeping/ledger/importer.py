from __future__ import annotations

"""Idempotent transaction importer: ledger write with atomic-write discipline.

Atomic-write protocol (KTD binding):
  1. Append ``intent`` record to the audit log and fsync.
  2. Write new ledger content to ``books.beancount.tmp`` via os.replace.
  3. Append ``ledger-sealed`` record carrying SHA-256 of the file bytes.

Close-start integrity check (``check_integrity``):
  - Find the most-recent ``ledger-sealed`` record.
  - Compare its sha256 to the current raw bytes of books.beancount.
  - On mismatch: tiered response depending on git-awareness.

See module docstring of auditlog.py and staging.py for the storage layout.

Public API
----------
import_transactions(entity, normalized_txns, session_id, categorizer, ts)
    Main entry point.  Returns ImportResult.

check_integrity(entity) -> IntegrityResult
    Close-start check.

acknowledge_mismatch(entity, diff_text, session_id, ts)
    Record that the owner acknowledged a hash mismatch.

ratify_git_commit(entity, commit_hash, author, diff_sha256, session_id, ts)
    Record a ratification (one-step path).

reverse_and_correct(entity, original_source_id, corrected_entry, session_id, ts)
    Write reversing + corrected entries in one atomic ledger write.

Categorizer callable contract:
    categorizer(txn: dict) -> (ledger_account: str, confidence: str)

    ``confidence`` is an opaque string; the importer treats anything that is
    not a falsy value as "categorized".  When the categorizer returns an empty
    account string or raises, the transaction is routed to the
    pending-categorization list (staging/pending-categorization.json).
"""

import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from ..entity import Entity, load_entity
from .auditlog import AuditLog, verify_chain
from .model import Entry, Open, Posting
from .staging import StagingStore
from .validator import validate
from .writer import render_ledger
from .normalize import normalize_description

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LATE_ARRIVAL_DAYS = 30
_BOOKS_FILE = "books.beancount"
_PENDING_CATEGORIZATION_FILE = "pending-categorization.json"
_LOCK_FILE = ".books.lock"

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    """Summary of an import_transactions call."""

    session_id: str
    new_entries: int = 0
    new_pending: int = 0
    superseded: int = 0
    skipped_duplicate: int = 0
    pending_categorization: int = 0
    late_arrivals: int = 0
    orphaned_tmps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class IntegrityResult:
    """Result of a close-start integrity check."""

    status: str  # "ok" | "incomplete-write" | "ratifiable" | "halt" | "acknowledged"
    message: str = ""
    diff: str = ""
    commit_hash: str | None = None
    commit_author: str | None = None
    diff_sha256: str | None = None  # For acknowledged / ratifiable paths.


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Return SHA-256 hex of the raw bytes of *path*."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@contextmanager
def _entity_write_lock(entity_path: Path):
    """Serialize ledger mutations for one entity directory."""
    lock_path = entity_path / _LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as fh:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_date(raw: str) -> date:
    """Parse YYYY-MM-DD prefix from a date string (handles timestamps)."""
    return date.fromisoformat(str(raw)[:10])


def _is_late_arrival(txn_date: date, session_date: date) -> bool:
    return (session_date - txn_date).days > _LATE_ARRIVAL_DAYS


def _git_is_managed(entity_path: Path) -> bool:
    """Return True when the entity directory is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(entity_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _git_commits_since_seal(
    entity_path: Path,
    sealed_record: dict,
    sealed_sha256: str,
) -> list[dict]:
    """Return commits that changed books.beancount after the sealed content."""
    sealed_ts = str(sealed_record.get("ts") or "")
    cmd = ["git", "log", "--format=%H|%ae|%s"]
    if sealed_ts:
        cmd.append(f"--since={sealed_ts}")
    cmd.extend(["--", _BOOKS_FILE])
    try:
        result = subprocess.run(
            cmd,
            cwd=str(entity_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        commits = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) >= 2:
                commits.append({
                    "hash": parts[0],
                    "author_email": parts[1],
                    "subject": parts[2] if len(parts) > 2 else "",
                })
        for idx, commit in enumerate(commits):
            content = _git_file_bytes_at_commit(entity_path, commit["hash"], _BOOKS_FILE)
            if content is not None and hashlib.sha256(content).hexdigest() == sealed_sha256:
                return commits[:idx]
        return commits
    except (OSError, subprocess.TimeoutExpired):
        return []


def _git_file_bytes_at_commit(entity_path: Path, commit_hash: str, filename: str) -> bytes | None:
    """Return file bytes at a commit, or None when unavailable."""
    try:
        result = subprocess.run(
            ["git", "show", f"{commit_hash}:{filename}"],
            cwd=str(entity_path),
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_books_dirty(entity_path: Path) -> bool:
    """Return True when books.beancount has uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", _BOOKS_FILE],
            cwd=str(entity_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return True
        return bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return True


def _git_patch_for_commits(entity_path: Path, commits: list[dict]) -> str:
    """Return a patch for the supplied commits, scoped to books.beancount."""
    patches: list[str] = []
    for commit in commits:
        commit_hash = str(commit.get("hash") or "")
        if not commit_hash:
            continue
        try:
            result = subprocess.run(
                ["git", "show", "--format=fuller", "--patch", commit_hash, "--", _BOOKS_FILE],
                cwd=str(entity_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                patches.append(result.stdout)
        except (OSError, subprocess.TimeoutExpired):
            continue
    return "\n".join(patches)


def _git_local_email(entity_path: Path) -> str | None:
    """Return the local git user.email, or None."""
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            cwd=str(entity_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_diff_since_hash(entity_path: Path) -> str:
    """Return the diff of books.beancount vs HEAD (or empty string on error)."""
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--", _BOOKS_FILE],
            cwd=str(entity_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
        return ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------------------
# Atomic ledger write
# ---------------------------------------------------------------------------


def _atomic_ledger_write(
    entity: Entity,
    audit_log: AuditLog,
    new_opens: list[Open],
    new_entries: list[Entry],
    session_id: str,
    ts: str | None,
    intent_description: str,
) -> str:
    with _entity_write_lock(entity.path):
        return _atomic_ledger_write_unlocked(
            entity=entity,
            audit_log=audit_log,
            new_opens=new_opens,
            new_entries=new_entries,
            session_id=session_id,
            ts=ts,
            intent_description=intent_description,
        )


def _atomic_ledger_write_unlocked(
    entity: Entity,
    audit_log: AuditLog,
    new_opens: list[Open],
    new_entries: list[Entry],
    session_id: str,
    ts: str | None,
    intent_description: str,
) -> str:
    """Execute the full atomic-write protocol and return the new file SHA-256.

    Protocol:
      1. Append ``intent`` record and fsync (handled inside AuditLog.append).
      2. Merge new opens/entries with existing ledger content.
      3. Write ledger to ``books.beancount.tmp``, then os.replace.
      4. Append ``ledger-sealed`` record.

    Returns the SHA-256 of the written file.
    """
    books_path = entity.books_path
    tmp_path = books_path.with_suffix(".beancount.tmp")

    # Step 1: intent.
    audit_log.append(
        "intent",
        ts=ts,
        session_id=session_id,
        description=intent_description,
    )

    # Step 2: read existing ledger content (if any).
    existing_text = ""
    if books_path.exists():
        existing_text = books_path.read_text(encoding="utf-8")

    # Parse existing opens and entries.
    from .validator import parse_ledger
    if existing_text.strip():
        parsed = parse_ledger(existing_text)
        existing_opens: list[Open] = parsed["opens"]
        existing_entries: list[Entry] = parsed["entries"]
        existing_title: str = parsed.get("title", "Books")
    else:
        existing_opens = []
        existing_entries = []
        existing_title = "Books"

    # Merge: existing + new (dedup opens by (date, account)).
    existing_open_keys = {(o.date, o.account) for o in existing_opens}
    merged_opens = list(existing_opens)
    for o in new_opens:
        if (o.date, o.account) not in existing_open_keys:
            merged_opens.append(o)
            existing_open_keys.add((o.date, o.account))

    merged_entries = list(existing_entries) + list(new_entries)

    # Render.
    ledger_text = render_ledger(
        opens=merged_opens,
        entries=merged_entries,
        balances=[],
        title=existing_title,
    )

    # Validate before committing.
    errors = validate(ledger_text)
    if errors:
        raise ValueError(
            f"Ledger validation failed after import: "
            + "; ".join(str(e) for e in errors[:5])
        )

    # Step 3: write via tmp + rename.
    tmp_path.write_text(ledger_text, encoding="utf-8")
    os.replace(str(tmp_path), str(books_path))

    # Step 4: seal.
    file_sha256 = _sha256_file(books_path)
    audit_log.append(
        "ledger-sealed",
        ts=ts,
        session_id=session_id,
        sha256=file_sha256,
    )

    return file_sha256


# ---------------------------------------------------------------------------
# Pending-categorization list
# ---------------------------------------------------------------------------


def _load_pending_categorization(entity: Entity) -> list[dict]:
    path = entity.staging_dir / _PENDING_CATEGORIZATION_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_pending_categorization(entity: Entity, items: list[dict]) -> None:
    path = entity.staging_dir / _PENDING_CATEGORIZATION_FILE
    tmp = path.with_suffix(".json.tmp")
    text = json.dumps(items, indent=2, sort_keys=True) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _add_pending_categorization(entity: Entity, txn: dict) -> bool:
    """Add *txn* to the pending-categorization worklist.

    Returns True if newly added, or False if an item with the same id was
    already queued, so callers can treat a repeat ingest as a duplicate.
    """
    items = _load_pending_categorization(entity)
    txn_id = txn.get("id")
    if txn_id and any(i.get("id") == txn_id for i in items):
        return False  # Already queued.
    items.append(dict(txn))
    _save_pending_categorization(entity, items)
    return True


# ---------------------------------------------------------------------------
# Main import entry point
# ---------------------------------------------------------------------------


def import_transactions(
    entity: Entity,
    normalized_txns: list[dict],
    session_id: str,
    categorizer: Callable[[dict], tuple[str, str]] | None = None,
    ts: str | None = None,
    session_date: date | None = None,
) -> ImportResult:
    """Import a list of normalized transactions into the entity ledger.

    Parameters
    ----------
    entity
        Loaded Entity object (from entity.load_entity).
    normalized_txns
        List of normalized transaction dicts (banksync/csvsource shape).
    session_id
        Caller-supplied import session identifier (for pushtag and audit log).
    categorizer
        callable(txn) -> (account, confidence).  When None, all posted
        transactions are routed to pending-categorization.
    ts
        ISO-8601 timestamp string for audit log records.  Defaults to now.
        Supply a fixed value in tests for deterministic output.
    session_date
        Date to use as the "import date" for late-arrival detection.
        Defaults to today (UTC).

    Returns
    -------
    ImportResult
    """
    if session_date is None:
        session_date = datetime.now(tz=timezone.utc).date()

    audit_log = AuditLog(entity.path / "audit-log.jsonl")
    staging = StagingStore(entity.staging_dir, audit_log)

    result = ImportResult(session_id=session_id)

    # Startup check: orphaned .tmp files.
    result.orphaned_tmps = staging.check_orphaned_tmps()

    # Self-heal seen-ids from the ledger: a crash between a ledger write and
    # the seen-ids update would otherwise let the next run import the same
    # source id twice. The ledger's own source-id metadata is authoritative.
    if entity.books_path.exists():
        from .validator import parse_ledger as _parse_ledger
        ledger_text = entity.books_path.read_text(encoding="utf-8")
        if ledger_text.strip():
            ledger_sids = [
                e.source_id
                for e in _parse_ledger(ledger_text)["entries"]
                if e.source_id and "reverses" not in {k for k, _ in e.meta}
            ]
            missing = [s for s in ledger_sids if not staging.is_seen(s)]
            if missing:
                staging.bulk_mark_seen(missing)

    # Accumulate entries to write in this session.
    entries_to_write: list[Entry] = []
    # Track accounts that need open directives.
    needed_opens: set[str] = set()
    source_ids_to_mark: list[str] = []

    for txn in normalized_txns:
        source_id = str(txn.get("id") or "")
        is_pending = bool(txn.get("pending", False))

        if is_pending:
            # Route to staging; never the ledger.
            staging.add_pending(txn)
            audit_log.append(
                "pending-staged",
                ts=ts,
                source_id=source_id,
                session_id=session_id,
            )
            result.new_pending += 1
            continue

        # Posted transaction.

        # Dedup: source ID is the sole dedup key for posted entries.
        if staging.is_seen(source_id):
            result.skipped_duplicate += 1
            continue

        # Supersede any matching staged pending.
        superseded = staging.supersede_pending(txn)
        if superseded:
            result.superseded += 1

        # Categorize.
        account = ""
        if categorizer is not None:
            try:
                account, _confidence = categorizer(txn)
            except Exception:
                account = ""

        if not account:
            # Route to pending-categorization list. A repeat of an already
            # queued id is a duplicate, not a new pending item.
            if _add_pending_categorization(entity, txn):
                result.pending_categorization += 1
            else:
                result.skipped_duplicate += 1
            continue

        # Build entry metadata.
        raw_date = str(txn.get("date") or "")
        try:
            txn_date = _parse_date(raw_date)
        except (ValueError, TypeError):
            result.errors.append(f"Cannot parse date for source_id={source_id!r}: {raw_date!r}")
            continue

        meta: list[tuple[str, str]] = [
            ("source-id", source_id),
            ("import-session", session_id),
        ]

        # Late-arrival flag.
        if _is_late_arrival(txn_date, session_date):
            meta.append(("late-arrival", "true"))
            result.late_arrivals += 1

        narration = str(txn.get("description") or "")

        # Determine amount and posting direction.
        # BankSync: ``amount`` is signed (positive = credit to account, negative = debit).
        # We create a two-posting balanced entry:
        #   bank account posting (uses `amount` as the primary sign)
        #   category account posting (negated)
        raw_amount = txn.get("amount")
        if raw_amount is None:
            # Fall back to creditAmount - debitAmount.
            credit = Decimal(str(txn.get("creditAmount") or "0"))
            debit = Decimal(str(txn.get("debitAmount") or "0"))
            raw_amount_dec = (credit - debit).quantize(Decimal("0.01"))
        else:
            raw_amount_dec = Decimal(str(raw_amount)).quantize(Decimal("0.01"))

        # Bank account posting: entity bank_account_mappings take precedence.
        bank_account = _ledger_account_for_txn(
            txn, entity.entity_config.get("bank_account_mappings")
        )
        needed_opens.add(bank_account)
        needed_opens.add(account)

        bank_posting = Posting(
            account=bank_account,
            amount=raw_amount_dec,
            currency="USD",
        )
        category_posting = Posting(
            account=account,
            amount=-raw_amount_dec,
            currency="USD",
        )

        entry = Entry(
            date=txn_date,
            narration=narration,
            flag="*",
            meta=tuple(meta),
            tags=(f"import-{session_id}",),
            postings=(bank_posting, category_posting),
        )

        entries_to_write.append(entry)
        source_ids_to_mark.append(source_id)
        result.new_entries += 1

    # Write to ledger if we have new entries.
    if entries_to_write:
        # Build Open directives for any accounts not already in the ledger.
        existing_opens = _get_existing_opens(entity)
        new_opens = [
            Open(date=date(2000, 1, 1), account=acc)
            for acc in sorted(needed_opens)
            if acc not in existing_opens
        ]

        _atomic_ledger_write(
            entity=entity,
            audit_log=audit_log,
            new_opens=new_opens,
            new_entries=entries_to_write,
            session_id=session_id,
            ts=ts,
            intent_description=f"import {len(entries_to_write)} entries",
        )

        # Audit-log each entry written.
        for entry in entries_to_write:
            sid = entry.source_id or ""
            audit_log.append(
                "entry-written",
                ts=ts,
                source_id=sid,
                session_id=session_id,
            )

        # Mark all source IDs as seen.
        staging.bulk_mark_seen(source_ids_to_mark)

    return result


# ---------------------------------------------------------------------------
# Helpers used by import_transactions
# ---------------------------------------------------------------------------


def _ledger_account_for_txn(txn: dict, mappings: dict[str, str] | None = None) -> str:
    """Derive a beancount-safe account name for the bank side of the posting.

    Entity-config ``bank_account_mappings`` (entity.json) take precedence,
    keyed by BankSync accountId first, then accountName — this is how feed
    accounts line up with the QuickBooks-derived opening-balance accounts.
    Falls back based on account type when available:
    ``Assets:Bank:<name>`` for asset-like accounts,
    ``Liabilities:CreditCard:<name>`` for credit cards, or
    ``Assets:Uncategorized`` when nothing usable is present.
    """
    if mappings:
        account_id = str(txn.get("accountId") or "")
        if account_id and account_id in mappings:
            return mappings[account_id]
        account_name_key = str(txn.get("accountName") or "")
        if account_name_key and account_name_key in mappings:
            return mappings[account_name_key]
    account_name = txn.get("accountName") or ""
    if account_name:
        # Sanitize: remove non-alphanumeric characters, capitalize segments.
        safe = "".join(c if c.isalnum() else "-" for c in account_name).strip("-")
        # Remove runs of hyphens.
        import re
        safe = re.sub(r"-+", "-", safe).strip("-")
        if safe:
            account_type = str(txn.get("accountType") or "").lower().replace("-", "_")
            if account_type in {"credit", "credit_card", "creditcard", "card"}:
                return f"Liabilities:CreditCard:{safe}"
            return f"Assets:Bank:{safe}"
    return "Assets:Uncategorized"


def _get_existing_opens(entity: Entity) -> set[str]:
    """Return the set of account names that already have open directives."""
    books_path = entity.books_path
    if not books_path.exists():
        return set()
    text = books_path.read_text(encoding="utf-8")
    if not text.strip():
        return set()
    from .validator import parse_ledger
    parsed = parse_ledger(text)
    return {o.account for o in parsed["opens"]}


# ---------------------------------------------------------------------------
# Close-start integrity check
# ---------------------------------------------------------------------------


def check_integrity(entity: Entity) -> IntegrityResult:
    """Close-start integrity check.

    Reads the most-recent ``ledger-sealed`` record and compares to the
    current raw bytes of books.beancount.

    Returns an IntegrityResult with one of these statuses:
      ``ok``               SHA-256 matches.
      ``incomplete-write`` Last audit-log record is an intent with no seal.
      ``ratifiable``       Git-authored diff by local identity, ≤200 lines.
      ``halt``             Mismatch requiring explicit acknowledgement.
    """
    audit_log = AuditLog(entity.path / "audit-log.jsonl")
    books_path = entity.books_path

    # A tampered log must never let intent/seal matching report "ok":
    # verify the hash chain before trusting any record lookups.
    audit_path = entity.path / "audit-log.jsonl"
    if audit_path.exists():
        chain_errors = verify_chain(audit_path)
        if chain_errors:
            return IntegrityResult(
                status="halt",
                message=(
                    "Audit log hash chain is broken — the log was edited or "
                    f"corrupted: {chain_errors[0]}"
                ),
            )

    # Check for incomplete write (intent without seal).
    last_intent = audit_log.last_intent()
    if last_intent is not None and not audit_log.has_seal_for_intent(last_intent):
        return IntegrityResult(
            status="incomplete-write",
            message=(
                "Last audit-log intent has no matching ledger-sealed record. "
                "An interrupted write was detected. Re-attempting import is safe."
            ),
        )

    if not books_path.exists():
        return IntegrityResult(status="ok", message="No ledger file yet.")

    current_sha256 = _sha256_file(books_path)
    sealed_record = audit_log.last_sealed()

    if sealed_record is None:
        # No seal on record yet (fresh entity or log pre-dates this code).
        return IntegrityResult(status="ok", message="No prior seal; accepting current state.")

    sealed_sha256 = sealed_record.get("sha256", "")
    if current_sha256 == sealed_sha256:
        return IntegrityResult(status="ok", message="Ledger integrity confirmed.")

    # --- Mismatch ---
    diff_text = ""
    diff_sha256: str | None = None

    # Check for incomplete write first (intent with no seal is already handled above,
    # but we guard here for the mismatch case too).
    is_git = _git_is_managed(entity.path)

    if is_git:
        commits = _git_commits_since_seal(entity.path, sealed_record, sealed_sha256)
        local_email = _git_local_email(entity.path)
        diff_text = _git_patch_for_commits(entity.path, commits)
        diff_lines = len(diff_text.splitlines())
        diff_sha256 = _sha256_text(diff_text)

        all_by_local = (
            bool(commits)
            and bool(local_email)
            and all(c["author_email"] == local_email for c in commits)
        )

        if all_by_local and not _git_books_dirty(entity.path) and 0 < diff_lines <= 200:
            return IntegrityResult(
                status="ratifiable",
                message=(
                    f"Ledger was modified by {len(commits)} git commit(s) "
                    f"authored by {local_email}. Diff is {diff_lines} lines. "
                    "One-step ratification is available."
                ),
                diff=diff_text,
                commit_hash=commits[0]["hash"] if commits else None,
                commit_author=local_email,
                diff_sha256=diff_sha256,
            )

    # Halt path.
    if not diff_text:
        if is_git:
            diff_text = _git_diff_since_hash(entity.path)
        else:
            # No git: show full file as "diff".
            diff_text = books_path.read_text(encoding="utf-8")
    diff_sha256 = _sha256_text(diff_text)

    return IntegrityResult(
        status="halt",
        message=(
            "Ledger SHA-256 does not match the last sealed record and cannot "
            "be automatically ratified. Explicit owner acknowledgement required."
        ),
        diff=diff_text,
        diff_sha256=diff_sha256,
    )


# ---------------------------------------------------------------------------
# Acknowledgement and ratification
# ---------------------------------------------------------------------------


def acknowledge_mismatch(
    entity: Entity,
    diff_text: str,
    session_id: str,
    ts: str | None = None,
) -> None:
    """Record that the owner acknowledged a ledger-hash mismatch.

    The acknowledgement also seals the CURRENT ledger bytes: the owner has
    ratified this exact state, so subsequent integrity checks treat it as
    the new baseline instead of halting forever on the same diff.
    """
    audit_log = AuditLog(entity.path / "audit-log.jsonl")
    diff_sha256 = _sha256_text(diff_text)
    audit_log.append(
        "acknowledged",
        ts=ts,
        session_id=session_id,
        diff_sha256=diff_sha256,
    )
    books_path = entity.books_path
    if books_path.exists():
        audit_log.append(
            "ledger-sealed",
            ts=ts,
            session_id=session_id,
            sha256=_sha256_file(books_path),
            sealed_by="acknowledgement",
        )


def ratify_git_commit(
    entity: Entity,
    commit_hash: str,
    author: str,
    diff_sha256: str,
    session_id: str,
    ts: str | None = None,
) -> None:
    """Record a one-step ratification of a git-authored ledger diff."""
    audit_log = AuditLog(entity.path / "audit-log.jsonl")
    audit_log.append(
        "ratified",
        ts=ts,
        session_id=session_id,
        commit_hash=commit_hash,
        author=author,
        diff_sha256=diff_sha256,
    )
    books_path = entity.books_path
    if books_path.exists():
        audit_log.append(
            "ledger-sealed",
            ts=ts,
            session_id=session_id,
            sha256=_sha256_file(books_path),
            sealed_by="git-ratification",
        )


# ---------------------------------------------------------------------------
# reverse_and_correct
# ---------------------------------------------------------------------------


def reverse_and_correct(
    entity: Entity,
    original_source_id: str,
    corrected_entry: Entry,
    session_id: str,
    ts: str | None = None,
) -> tuple[Entry, Entry]:
    """Write a reversing entry and a corrected entry in one atomic ledger write.

    The reversing entry has:
      - All posting amounts negated.
      - Metadata ``reverses: "<original_source_id>"``.
      - Flag ``*``.

    The corrected entry has:
      - Metadata ``correction-of: "<original_source_id>"``.

    Both entries are written in a single atomic ledger write.

    Returns (reversing_entry, corrected_entry_with_meta).
    """
    audit_log = AuditLog(entity.path / "audit-log.jsonl")
    staging = StagingStore(entity.staging_dir, audit_log)

    # Find the original entry in the ledger.
    books_path = entity.books_path
    if not books_path.exists():
        raise FileNotFoundError(f"Ledger not found at {books_path}")

    from .validator import parse_ledger
    parsed = parse_ledger(books_path.read_text(encoding="utf-8"))

    # Find the live entry for this source id: skip reversal entries (they
    # carry a `reverses` key) and prefer the most recent correction so a
    # second correction targets the corrected entry, never re-reverses a
    # reversal.
    original: Entry | None = None
    for e in parsed["entries"]:
        if e.source_id != original_source_id:
            continue
        meta_keys = {k for k, _ in e.meta}
        if "reverses" in meta_keys:
            continue
        original = e  # keep scanning — last non-reversal match wins

    if original is None:
        raise ValueError(f"No entry with source-id={original_source_id!r} found in ledger")

    # Build reversing entry (negate all posting amounts). The reversal's
    # metadata drops the original `source-id` so source-id lookups never
    # resolve to a reversal.
    rev_postings = tuple(
        Posting(
            account=p.account,
            amount=-p.amount,
            currency=p.currency,
        )
        for p in original.postings
    )
    rev_meta = tuple(
        (k, v) for k, v in original.meta if k not in ("source-id", "import-session")
    ) + (
        ("reverses", original_source_id),
        ("import-session", session_id),
    )
    reversing_entry = Entry(
        date=original.date,
        narration=f"Reversal of: {original.narration}",
        flag="*",
        meta=rev_meta,
        postings=rev_postings,
    )

    # Build corrected entry: add correction-of metadata.
    existing_meta = dict(corrected_entry.meta)
    existing_meta["correction-of"] = original_source_id
    existing_meta["import-session"] = session_id
    corrected_with_meta = Entry(
        date=corrected_entry.date,
        narration=corrected_entry.narration,
        payee=corrected_entry.payee,
        flag=corrected_entry.flag,
        meta=tuple(sorted(existing_meta.items())),
        postings=corrected_entry.postings,
    )

    # Find accounts needed.
    needed_accounts = set()
    for e in (reversing_entry, corrected_with_meta):
        for p in e.postings:
            needed_accounts.add(p.account)

    existing_opens = _get_existing_opens(entity)
    new_opens = [
        Open(date=date(2000, 1, 1), account=acc)
        for acc in sorted(needed_accounts)
        if acc not in existing_opens
    ]

    _atomic_ledger_write(
        entity=entity,
        audit_log=audit_log,
        new_opens=new_opens,
        new_entries=[reversing_entry, corrected_with_meta],
        session_id=session_id,
        ts=ts,
        intent_description=(
            f"reverse_and_correct original_source_id={original_source_id!r}"
        ),
    )

    # Audit-log both entries.
    for e, label in ((reversing_entry, "reversal"), (corrected_with_meta, "correction")):
        audit_log.append(
            "entry-written",
            ts=ts,
            session_id=session_id,
            source_id=original_source_id,
            label=label,
        )

    return reversing_entry, corrected_with_meta
