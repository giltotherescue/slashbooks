"""`books import check`: does what the books hold agree with an imported file.

Everything here is read-only and deterministic. Sums are Decimal and happen in
this module. The check never writes to the ledger or the review queue. The
only file this module writes is the stored result that an import leaves next
to its record (``ingestion/statements/checks/<import_id>.json``); the command
``books import check`` itself writes nothing.

Result
------
``schema_version`` 1. ``kind`` names the import source. Only ``statement`` is
implemented; other kinds (Stripe, bank feed, QuickBooks, migration) can add
their own checks later with the same result shape.

``status`` is ``matches`` (every check matches), ``differs`` (at least one
check differs), ``partial`` (some checks match and some are ``not_checkable``)
or ``not_checkable`` (no check could run).

Checks (statement)
------------------
- ``count``: rows in the file equal rows recorded + queued + duplicate +
  pending skipped. A file row that is in none of these places is unaccounted
  and the check differs (``rows_unaccounted``).
- ``debits`` and ``credits``: the file's money out (charges, withdrawals) and
  money in (payments, deposits) against the same totals in the books. Posted
  rows use their ledger postings, queued rows use their queued amount.
  Duplicate rows, and rows outside the profile boundary, are accounted for by
  another entry or source, so they count at the file amount
  (``duplicates_at_file_amount``). Both totals are positive numbers.
- ``closing``: the books agree with the closing balance. The statement's
  opening balance plus the books movement in its period is compared with the
  closing balance (``period_tieout``). The rows of the file are also added to
  the opening balance (``statement_tieout``), and a mismatch there makes the
  check differ (``file_rows_differ``). If the period cannot be used, only the
  file rows are compared (``file_rows_only``).
- ``opening``: the opening balance equals the closing balance of a recorded
  statement for the same account that ends the day before this one begins
  (``prior_statement_closing``).

Balances use the statement presentation (money owed is positive for a card or
loan), the same as ``tieout.py``. Debit and credit totals are statement words:
money out and money in, for an asset and a liability alike.

``not_checkable`` checks carry a ``detail_code``: ``foreign_currency``,
``no_balances``, ``period_unconfirmed``, ``not_aligned`` (another recorded
statement for the account overlaps this period, so the books movement in the
period cannot be tied to this file alone, the same rule coverage uses for a
month), ``no_prior_statement`` (an earlier statement exists but none ends the day
before), ``no_prior_balance``, or ``first_statement`` (nothing is earlier; this
detail does not lower ``status``).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence

from . import tieout
from .ledger.store import LedgerStore, default_store_path

SCHEMA_VERSION = 1
KINDS = ("statement",)
STATUSES = ("matches", "differs", "partial", "not_checkable")
CHECK_STATUSES = ("matches", "differs", "not_checkable")
CHECK_IDS = ("count", "debits", "credits", "closing", "opening")
CHECKS_DIR = Path("ingestion") / "statements" / "checks"
_ZERO = Decimal("0.00")

JsonObject = dict[str, Any]


class ImportCheckError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def as_dict(self) -> JsonObject:
        return {"code": self.code, "message": str(self)}


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stored_path(entity_path: Path | str, import_id: str) -> Path:
    return Path(entity_path) / CHECKS_DIR / f"{import_id}.json"


def load_stored(entity_path: Path | str, import_id: str) -> JsonObject | None:
    """The result stored when the import ran, or None. Never recomputes."""
    try:
        data = json.loads(stored_path(entity_path, import_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def store_result(entity_path: Path | str, result: Mapping[str, Any]) -> Path:
    import os

    path = stored_path(entity_path, str(result["import_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Books side (read only)
# ---------------------------------------------------------------------------

def books_revision(entity: Any) -> str:
    """A short digest of the books state a check read: ledger size and review files."""
    digest = hashlib.sha256()
    store_path = default_store_path(Path(entity.path))
    if store_path.is_file():
        try:
            with LedgerStore(store_path).connection() as conn:
                for table in ("entries", "postings", "source_transactions"):
                    count, top = conn.execute(f"SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM {table}").fetchone()
                    digest.update(f"{table}:{count}:{top};".encode())
        except sqlite3.DatabaseError:
            digest.update(b"ledger:unreadable;")
    for name in ("pending-categorization.json", "duplicate-candidates.json", "source-id-aliases.json"):
        try:
            digest.update(name.encode() + hashlib.sha256((Path(entity.staging_dir) / name).read_bytes()).digest())
        except OSError:
            digest.update(name.encode() + b":none;")
    return "sha256:" + digest.hexdigest()[:16]


def _ledger_rows(entity: Any, account: str) -> tuple[dict[str, Decimal], set[str]]:
    """Posting sum on ``account`` per source ID, and every source ID the ledger knows."""
    sums: dict[str, Decimal] = {}
    known: set[str] = set()
    store_path = default_store_path(Path(entity.path))
    if not store_path.is_file():
        return sums, known
    try:
        with LedgerStore(store_path).connection() as conn:
            for source_id, raw in conn.execute(
                    "SELECT e.source_id, p.amount FROM entries e JOIN postings p ON p.entry_id = e.id "
                    "WHERE p.account = ? AND e.source_id IS NOT NULL", (account,)):
                amount = tieout._dec(raw)
                if amount is not None:
                    sums[str(source_id)] = sums.get(str(source_id), _ZERO) + amount
            known = {str(row[0]) for row in conn.execute("SELECT id FROM source_transactions")}
    except sqlite3.DatabaseError:
        pass
    return sums, known


def _queued_rows(entity: Any) -> dict[str, Decimal]:
    from .statement_import import _txn_amount

    path = Path(entity.staging_dir) / "pending-categorization.json"
    try:
        staged = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows: dict[str, Decimal] = {}
    for txn in staged if isinstance(staged, list) else []:
        if isinstance(txn, dict) and txn.get("id"):
            try:
                rows[str(txn["id"])] = _txn_amount(txn)
            except (ArithmeticError, ValueError):
                continue
    return rows


def _list(entity: Any, name: str) -> list[JsonObject]:
    try:
        data = json.loads((Path(entity.staging_dir) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Statement check
# ---------------------------------------------------------------------------

def _check(check_id: str, status: str, source: Any, books: Any, detail_code: str) -> JsonObject:
    def text(value: Any) -> str | None:
        return None if value is None else (value if isinstance(value, str) else str(value))

    difference = None
    try:
        if status != "not_checkable" and source is not None and books is not None:
            difference = tieout._fmt(Decimal(str(source)) - Decimal(str(books)))
            if check_id == "count":
                difference = str(int(Decimal(str(source)) - Decimal(str(books))))
    except ArithmeticError:
        difference = None
    return {"id": check_id, "status": status, "source": text(source), "books": text(books),
            "difference": difference, "detail_code": detail_code}


def _unchecked(check_id: str, detail_code: str) -> JsonObject:
    return _check(check_id, "not_checkable", None, None, detail_code)


def _overall(checks: Sequence[Mapping[str, Any]]) -> str:
    # The first statement of an account has nothing before it, so its opening is not a gap.
    states = [c["status"] for c in checks if c["detail_code"] != "first_statement"]
    if "differs" in states:
        return "differs"
    if all(s == "matches" for s in states):
        return "matches"
    return "partial" if "matches" in states else "not_checkable"


def check_statement(entity: Any, record: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], *,
                    outside_boundary: Sequence[Mapping[str, Any]] = (), pending_skipped: int = 0,
                    in_file_duplicates: int = 0, ts: str | None = None) -> JsonObject:
    """Check one statement file against the books. Read-only.

    ``rows`` are the file rows the profile keeps (normalized transactions);
    ``outside_boundary`` are rows the profile boundary leaves to another source.
    """
    from . import coverage

    account = str(record["account"])
    statement_id = str(record["statement_id"])
    ledger_sums, ledger_known = _ledger_rows(entity, account)
    queued = _queued_rows(entity)
    held = {str(c.get("source_id")) for c in _list(entity, "duplicate-candidates.json")
            if c.get("status") == "duplicate-candidate"}
    confirmed_duplicate = {str(a.get("source_id")) for a in _list(entity, "source-id-aliases.json")
                           if a.get("status") == "confirmed-duplicate"}
    confirmed_duplicate |= {str(c.get("source_id")) for c in _list(entity, "duplicate-candidates.json")
                            if c.get("status") == "confirmed-duplicate"}

    counts = {"in_source": len(rows) + len(outside_boundary) + pending_skipped + in_file_duplicates,
              "recorded": 0, "queued": 0,
              "duplicate": len(outside_boundary) + in_file_duplicates,
              "pending_skipped": pending_skipped}
    waiting_rows = 0
    source_debits = source_credits = books_debits = books_credits = _ZERO
    counted_at_file_amount = bool(outside_boundary)

    def add(amount: Decimal, side: str) -> None:
        nonlocal source_debits, source_credits, books_debits, books_credits
        if side == "source":
            source_debits += -amount if amount < 0 else _ZERO
            source_credits += amount if amount > 0 else _ZERO
        else:
            books_debits += -amount if amount < 0 else _ZERO
            books_credits += amount if amount > 0 else _ZERO

    for row in outside_boundary:
        amount = tieout._dec(row.get("amount")) or _ZERO
        add(amount, "source")
        add(amount, "books")
    for row in rows:
        sid = str(row["id"])
        amount = tieout._dec(row.get("amount")) or _ZERO
        add(amount, "source")
        if sid in ledger_sums or sid in ledger_known:
            counts["recorded"] += 1
            add(ledger_sums.get(sid, amount), "books")
        elif sid in queued:
            counts["queued"] += 1
            waiting_rows += 1
            add(queued[sid], "books")
        elif sid in held or sid in confirmed_duplicate:
            counts["duplicate"] += 1
            counted_at_file_amount = True
            waiting_rows += 1 if sid in held else 0
            add(amount, "books")
    accounted = counts["recorded"] + counts["queued"] + counts["duplicate"] + counts["pending_skipped"]
    unaccounted = counts["in_source"] - accounted

    checks: list[JsonObject] = [_check(
        "count", "matches" if unaccounted == 0 else "differs", counts["in_source"], accounted,
        "all_rows_accounted" if unaccounted == 0 else "rows_unaccounted")]

    period_start, period_end = tieout._day(record.get("period_start")), tieout._day(record.get("period_end"))
    confirmed = bool(record.get("period_confirmed")) and period_start is not None and period_end is not None
    opening, closing = tieout._dec(record.get("opening_balance")), tieout._dec(record.get("closing_balance"))
    currency = str(record.get("currency") or "USD").upper()
    foreign = currency != "USD" or any(str(r.get("currency") or "USD").upper() != "USD" for r in rows)

    if foreign:
        checks += [_unchecked(c, "foreign_currency") for c in ("debits", "credits", "closing", "opening")]
    else:
        ok_code = "duplicates_at_file_amount" if counted_at_file_amount else "totals_agree"
        for check_id, source, books in (("debits", source_debits, books_debits),
                                        ("credits", source_credits, books_credits)):
            same = source == books
            checks.append(_check(check_id, "matches" if same else "differs", tieout._fmt(source),
                                 tieout._fmt(books), ok_code if same else "totals_differ"))
        checks.append(_closing_check(entity, record, account, statement_id, period_start, period_end,
                                     confirmed, opening, closing, rows))
        checks.append(_opening_check(entity, coverage, account, statement_id, period_start, confirmed, opening))

    return {
        "schema_version": SCHEMA_VERSION,
        "import_id": statement_id,
        "kind": "statement",
        "status": _overall(checks),
        "account": account,
        "period": {"from": record.get("period_start"), "to": record.get("period_end")},
        "checks": checks,
        "counts": counts,
        "waiting_rows": waiting_rows,
        "computed_at": ts or _now(),
        "books_revision": books_revision(entity),
    }


def _closing_check(entity: Any, record: Mapping[str, Any], account: str, statement_id: str,
                   start: date | None, end: date | None, confirmed: bool,
                   opening: Decimal | None, closing: Decimal | None,
                   rows: Sequence[Mapping[str, Any]]) -> JsonObject:
    from . import coverage

    if opening is None or closing is None:
        return _unchecked("closing", "no_balances")
    sign = tieout.presentation_sign(account)
    total = sum((tieout._dec(r.get("amount")) or _ZERO for r in rows), _ZERO)
    file_tie = tieout.statement_tieout(
        {"opening_balance": opening, "closing_balance": closing, "transactions_total": total,
         "transaction_count": len(rows)}, sign)
    if not confirmed:
        # The period is not known, so the books movement cannot be selected. The file rows still tie.
        return _file_only("closing", file_tie, "period_unconfirmed")
    others = [r for r in coverage.load_statements(entity.path)[0]
              if r.kind == "statement" and r.account == account and r.statement_id != statement_id
              and r.period_start <= end and r.period_end >= start]
    if others:
        return _file_only("closing", file_tie, "not_aligned")
    postings = tieout.load_postings(entity.path, [account])[account]
    waiting = tieout.load_waiting(entity, [account])[account]
    period = tieout.period_tieout(account=account, statement_id=statement_id, start=start, end=end,
                                  opening=opening, closing=closing, postings=postings, waiting=waiting)
    # Rows waiting in review are held in the books, so they count with the posted rows.
    books = Decimal(period["books_balance_through"]) + Decimal(period["waiting_total"])
    if closing != books:
        return _check("closing", "differs", tieout._fmt(closing), tieout._fmt(books), "books_differ")
    if file_tie["status"] == "differs":
        return _check("closing", "differs", tieout._fmt(closing), file_tie["expected_closing"], "file_rows_differ")
    return _check("closing", "matches", tieout._fmt(closing), tieout._fmt(books),
                  "queued_rows_included" if period["waiting_rows"] else "ok")


def _file_only(check_id: str, file_tie: Mapping[str, Any], why: str) -> JsonObject:
    """The books cannot be tied to the period; the rows of the file are still compared."""
    if file_tie["status"] == "differs":
        return _check(check_id, "differs", file_tie["closing"], file_tie["expected_closing"], "file_rows_differ")
    return _unchecked(check_id, why)


def _opening_check(entity: Any, coverage: Any, account: str, statement_id: str, start: date | None,
                   confirmed: bool, opening: Decimal | None) -> JsonObject:
    if opening is None:
        return _unchecked("opening", "no_balances")
    if not confirmed or start is None:
        return _unchecked("opening", "period_unconfirmed")
    prior = [r for r in coverage.load_statements(entity.path)[0]
             if r.kind == "statement" and r.account == account and r.statement_id != statement_id
             and r.period_end == start - timedelta(days=1)]
    if not prior:
        earlier = [r for r in coverage.load_statements(entity.path)[0]
                   if r.kind == "statement" and r.account == account and r.statement_id != statement_id
                   and r.period_end < start]
        return _unchecked("opening", "no_prior_statement" if earlier else "first_statement")
    closings = sorted({r.closing_balance for r in prior if r.closing_balance is not None})
    if not closings:
        return _unchecked("opening", "no_prior_balance")
    books = closings[0]
    return _check("opening", "matches" if opening == books else "differs", tieout._fmt(opening),
                  tieout._fmt(books), "prior_statement_closing")


# ---------------------------------------------------------------------------
# Recompute from the archived file
# ---------------------------------------------------------------------------

def check_import(entity_path: Path | str, import_id: str, *, ts: str | None = None) -> JsonObject:
    """Recompute the check for one recorded statement import. Read-only."""
    from . import statement_import as si
    from .connectors import ofx
    from .entity import load_entity

    entity = load_entity(entity_path)
    records = [r for r in si.load_statement_records(entity.path) if r.get("statement_id") == import_id]
    if not records:
        raise ImportCheckError("import_not_found", f"No recorded import has the ID {import_id}.")
    record = records[0]
    if record.get("writer") != si.WRITER or not record.get("evidence_ref"):
        raise ImportCheckError("unsupported_import", "Only a file that books statement import recorded can be checked.")
    path = Path(entity.path) / str(record["evidence_ref"])
    profiles = si._profiles(entity.entity_config)
    name = str(record.get("profile") or "")
    if name not in profiles:
        raise ImportCheckError("profile_not_found", f"The profile '{name}' is no longer saved.")
    try:
        fmt, text = si._load(path)
    except (OSError, si.StatementFileError) as exc:
        raise ImportCheckError("file_unreadable", f"Cannot read the archived file: {exc}") from exc
    sha = si._file_sha256(path)
    if fmt == "ofx":
        statements = [si._ofx_statement(s, sha, name, profiles[name]) for s in ofx.parse_ofx(text)]
    else:
        statements = [si._csv_statement(path, text, sha, name, profiles[name])]
    statement = next((s for s in statements if s["statement_id"] == import_id), None)
    if statement is None:
        raise ImportCheckError("file_changed", "The archived file no longer matches this import.")
    return check_parsed(entity, record, profiles[name], statement, ts=ts)


def check_parsed(entity: Any, record: Mapping[str, Any], profile: Mapping[str, Any],
                 statement: Mapping[str, Any], *, ts: str | None = None) -> JsonObject:
    """Check one parsed statement (see ``statement_import``) against the books."""
    from . import statement_import as si

    kept, outside = si._apply_boundary(list(statement["transactions"]), dict(profile))
    kept_ids = {t["id"] for t in kept}
    return check_statement(
        entity, record, kept, outside_boundary=[t for t in statement["transactions"] if t["id"] not in kept_ids],
        pending_skipped=int(statement.get("pending_rows") or 0),
        in_file_duplicates=int(statement.get("in_file_duplicates") or 0), ts=ts)


# ---------------------------------------------------------------------------
# CLI: books import check
# ---------------------------------------------------------------------------

def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("import", help="Check an import against the books (read-only)")
    sub = parser.add_subparsers(dest="import_command", required=True)
    check = sub.add_parser(
        "check", help="Check that what the books hold agrees with an imported statement file")
    check.add_argument("import_id", help="The statement ID (see books statement list)")
    check.add_argument("--entity", required=True, type=Path)
    check.add_argument("--stored", action="store_true",
                       help="Show the result stored when the file was imported, without recomputing")
    check.add_argument("--format", choices=["json", "text"], default="json", dest="fmt")


def run(args: Any) -> int:
    try:
        if args.import_command != "check":
            raise ImportCheckError("unknown_command", f"Unknown import command: {args.import_command}")
        if args.stored:
            from .entity import load_entity

            load_entity(args.entity)
            document = load_stored(args.entity, args.import_id)
            if document is None:
                raise ImportCheckError("no_stored_check", "No result is stored for this import. Run without --stored.")
        else:
            document = check_import(args.entity, args.import_id)
    except ImportCheckError as exc:
        print(json.dumps({"status": "error", "error": exc.as_dict()}, indent=2, sort_keys=True))
        return 1
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.fmt == "json":
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        print(f"{document['import_id']}: {document['status']} ({document['account']}, "
              f"{document['period']['from']} to {document['period']['to']})")
        for item in document["checks"]:
            print(f"  {item['id']}: {item['status']} (file {item['source']}, books {item['books']}, "
                  f"difference {item['difference']}, {item['detail_code']})")
    return 0
