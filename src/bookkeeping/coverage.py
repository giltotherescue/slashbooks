"""Source-coverage completeness per account and calendar month.

The check answers one question for a period and a set of expected source
accounts: does each month of each account have source evidence for every day?
It is read-only and deterministic. It never infers activity that is not in the
evidence, and it never changes company state except through the explicit
``record_statement`` writer.

Evidence, strongest first
-------------------------
1. ``statement`` records: a bank or card statement with a start date, an end
   date and, when known, the opening and closing balances.
2. ``feed_sync`` records: a date window that a bank feed or provider download
   returned in full.
3. ``declared`` ranges: owner-confirmed coverage recorded with
   ``books entity source-coverage``.
4. ``transactions``: dates of ledger entries with a source ID that post to the
   account. Each run of dates without a quiet gap longer than
   ``max_quiet_days`` counts as covered from its first to its last date. This
   evidence is weak: the report marks such months as not verified.

Statement record contract (schema_version 1)
--------------------------------------------
Importers, connectors and hosts write one JSON object per file (or a JSON list
of objects) under ``<company>/ingestion/statements/``. ``record_statement``
writes the same shape. Fields:

- ``schema_version``: ``1``.
- ``statement_id``: stable, unique text; also used for the file name.
- ``account``: ledger account under ``Assets:`` or ``Liabilities:``.
- ``kind``: ``statement`` or ``feed_sync``. Default ``statement``.
- ``period_start`` and ``period_end``: inclusive ``YYYY-MM-DD`` dates.
- ``opening_balance`` and ``closing_balance``: optional decimal strings in the
  statement's own presentation (money held for an asset account, money owed
  for a card or loan). Only ``statement`` records carry balances.
- ``source``: optional human label, such as ``Operating Checking``.
- ``evidence_ref`` and ``evidence_sha256``: optional pointer to the source file
  and its SHA-256 digest.

Readers ignore unknown fields. A record that breaks the contract is reported
as a ``statement_record_invalid`` finding and contributes no coverage.
Only ``*.json`` files directly in ``ingestion/statements/`` are read;
subdirectories (``files/`` for archived source files and ``unconfirmed/`` for
imports whose period a person has not confirmed) are not evidence.

Writers
-------
- ``books statement import`` writes one ``statement`` record per imported
  statement, plus importer details as extra fields.
- ``books ingest`` writes ``feed_sync`` records for BankSync download windows
  and for any ``coverage_windows`` in a normalized input file
  (``record_feed_window``).
- ``books coverage statement-add`` and ``record_statement`` write records
  that a person or a host supplies.
"""

from __future__ import annotations

import calendar
import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable, Mapping, Sequence

from .entity import load_entity
from .ledger.store import LedgerStore, default_store_path


SCHEMA_VERSION = 1
STATEMENTS_DIR = Path("ingestion") / "statements"
DEFAULT_MAX_QUIET_DAYS = 31
EVIDENCE_KINDS = ("statement", "feed_sync")
BASIS_ORDER = ("statement", "feed_sync", "declared", "transactions")
_STRONG_BASES = {"statement", "feed_sync", "declared"}
_STATEMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class StatementRecord:
    statement_id: str
    account: str
    kind: str
    period_start: date
    period_end: date
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    source: str
    evidence_ref: str
    evidence_sha256: str
    file: str

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "statement_id": self.statement_id,
            "account": self.account,
            "kind": self.kind,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "opening_balance": None if self.opening_balance is None else str(self.opening_balance),
            "closing_balance": None if self.closing_balance is None else str(self.closing_balance),
            "source": self.source or None,
            "evidence_ref": self.evidence_ref or None,
            "evidence_sha256": self.evidence_sha256 or None,
        }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]) if len(value) == 10 else None
    except ValueError:
        return None


def _parse_amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Balances must be decimal strings.")
    try:
        amount = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("Balances must be decimal strings.") from exc
    if not amount.is_finite() or amount.quantize(Decimal("0.01")) != amount:
        raise ValueError("Balances must be finite with at most two decimal places.")
    return amount.quantize(Decimal("0.01"))


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _months(start: date, end: date) -> list[tuple[str, date, date]]:
    result = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        first, last = _month_bounds(year, month)
        result.append((f"{year:04d}-{month:02d}", max(first, start), min(last, end)))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return result


def _fmt_day(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}"


def format_range(start: date, end: date) -> str:
    """Render ``Mar 3–31``, ``Mar 3`` or ``Mar 30–Apr 2``; add years only across a year end."""
    if start == end:
        text = _fmt_day(start)
    elif (start.year, start.month) == (end.year, end.month):
        text = f"{_fmt_day(start)}–{end.day}"
    else:
        text = f"{_fmt_day(start)}–{_fmt_day(end)}"
    if start.year != end.year:
        return f"{_fmt_day(start)}, {start.year}–{_fmt_day(end)}, {end.year}"
    return text


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _account_label(account: str) -> str:
    return account.replace(":", " › ")


def _label(config: Mapping[str, Any], account: str, records: Sequence["StatementRecord"]) -> str:
    """A human name: statement label, owner-named source, else the account leaf."""
    for record in sorted(records, key=lambda r: r.statement_id):
        if record.source:
            return record.source
    names = []
    coverage = config.get("source_coverage")
    if isinstance(coverage, Mapping):
        names += [str(name) for name, item in coverage.items()
                  if isinstance(item, Mapping) and item.get("account") == account]
    for item in config.get("declared_sources") or []:
        if isinstance(item, Mapping) and item.get("account") == account:
            names.append(str(item.get("name") or item.get("source") or ""))
    names = sorted(name for name in names if name.strip())
    if names:
        return names[0]
    return re.sub(r"[-_]+", " ", account.split(":")[-1]).strip() or account


def _merge(intervals: Iterable[tuple[date, date]]) -> list[tuple[date, date]]:
    merged: list[tuple[date, date]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _clip(intervals: Iterable[tuple[date, date]], start: date, end: date) -> list[tuple[date, date]]:
    return [(max(a, start), min(b, end)) for a, b in intervals if a <= end and b >= start]


def _gaps(covered: list[tuple[date, date]], start: date, end: date) -> list[tuple[date, date]]:
    gaps = []
    cursor = start
    for a, b in covered:
        if a > cursor:
            gaps.append((cursor, a - timedelta(days=1)))
        cursor = max(cursor, b + timedelta(days=1))
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


def _days(intervals: Iterable[tuple[date, date]]) -> int:
    return sum((b - a).days + 1 for a, b in intervals)


# ---------------------------------------------------------------------------
# Statement records: read and write
# ---------------------------------------------------------------------------


def statements_dir(entity_path: Path | str) -> Path:
    return Path(entity_path) / STATEMENTS_DIR


def parse_statement(raw: Any, *, file: str = "") -> StatementRecord:
    """Validate one contract record. Raise ``ValueError`` with a plain reason."""
    if not isinstance(raw, Mapping):
        raise ValueError("A statement record must be a JSON object.")
    version = raw.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported statement schema_version {version!r}.")
    statement_id = str(raw.get("statement_id") or "")
    if not _STATEMENT_ID.fullmatch(statement_id):
        raise ValueError("statement_id must be 1-128 letters, digits, dots, hyphens or underscores.")
    account = str(raw.get("account") or "")
    if not account.startswith(("Assets:", "Liabilities:")):
        raise ValueError("account must be an Assets: or Liabilities: ledger account.")
    kind = str(raw.get("kind") or "statement")
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"kind must be one of {', '.join(EVIDENCE_KINDS)}.")
    start = _parse_date(raw.get("period_start"))
    end = _parse_date(raw.get("period_end"))
    if start is None or end is None:
        raise ValueError("period_start and period_end must use YYYY-MM-DD format.")
    if start > end:
        raise ValueError("period_start must not be after period_end.")
    opening = _parse_amount(raw.get("opening_balance"))
    closing = _parse_amount(raw.get("closing_balance"))
    if kind == "feed_sync" and (opening is not None or closing is not None):
        raise ValueError("feed_sync records do not carry balances.")
    digest = str(raw.get("evidence_sha256") or "")
    if digest and not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("evidence_sha256 must be a lowercase SHA-256 hex digest.")
    return StatementRecord(
        statement_id=statement_id,
        account=account,
        kind=kind,
        period_start=start,
        period_end=end,
        opening_balance=opening,
        closing_balance=closing,
        source=str(raw.get("source") or ""),
        evidence_ref=str(raw.get("evidence_ref") or ""),
        evidence_sha256=digest,
        file=file,
    )


def load_statements(entity_path: Path | str) -> tuple[list[StatementRecord], list[dict[str, Any]]]:
    """Return valid records and ``statement_record_invalid`` findings."""
    directory = statements_dir(entity_path)
    records: list[StatementRecord] = []
    findings: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    if not directory.is_dir():
        return records, findings
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            findings.append(_invalid_finding(path.name, f"The file could not be read ({type(exc).__name__})."))
            continue
        items = payload if isinstance(payload, list) else [payload]
        for index, item in enumerate(items):
            ref = path.name if len(items) == 1 else f"{path.name}#{index}"
            try:
                record = parse_statement(item, file=ref)
            except ValueError as exc:
                findings.append(_invalid_finding(ref, str(exc)))
                continue
            if record.statement_id in seen:
                findings.append(_invalid_finding(
                    ref, f"statement_id {record.statement_id} is also used in {seen[record.statement_id]}."))
                continue
            seen[record.statement_id] = ref
            records.append(record)
    return records, findings


def _invalid_finding(ref: str, reason: str) -> dict[str, Any]:
    return {
        "code": "statement_record_invalid",
        "severity": "error",
        "account": None,
        "refs": [ref],
        "message": f"Statement record {ref} was ignored: {reason}",
    }


CONTRACT_FIELDS = frozenset({
    "schema_version", "statement_id", "account", "kind", "period_start", "period_end",
    "opening_balance", "closing_balance", "source", "evidence_ref", "evidence_sha256",
})


def record_statement(entity_path: Path | str, record: Mapping[str, Any], *, replace: bool = False,
                     extra: Mapping[str, Any] | None = None, check_catalog: bool = True) -> dict[str, Any]:
    """Validate and write one statement record under ``ingestion/statements``.

    ``extra`` adds writer details (for example the import profile) next to the
    contract fields. Contract fields always win; readers ignore the extras.
    ``check_catalog=False`` skips the account-catalog check for a writer that
    already validated the account when it was mapped.
    """
    entity = load_entity(entity_path)
    parsed = parse_statement(dict(record))
    store_path = default_store_path(entity.path)
    if (check_catalog and store_path.is_file()
            and parsed.account not in LedgerStore(store_path).load_account_names()):
        raise ValueError(f"Account '{parsed.account}' is not in the account catalog.")
    directory = statements_dir(entity.path)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{parsed.statement_id}.json"
    existed = destination.exists()
    if existed and not replace:
        raise ValueError(f"Statement {parsed.statement_id} already exists; pass replace to update it.")
    document = {**{k: v for k, v in (extra or {}).items() if k not in CONTRACT_FIELDS},
                **parsed.to_document()}
    tmp = destination.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, destination)
    return {**document, "status": "updated" if existed else "created"}


def load_statement_documents(entity_path: Path | str) -> list[dict[str, Any]]:
    """Return the full JSON of each valid record, extra writer fields included."""
    records, _ = load_statements(entity_path)
    directory = statements_dir(entity_path)
    documents = []
    for record in records:
        name, _, index = record.file.partition("#")
        try:
            payload = json.loads((directory / name).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        item = payload[int(index)] if index else payload
        if isinstance(item, Mapping):
            documents.append({**item, **record.to_document()})
    return documents


def feed_window_id(account: str, start: date, end: date, source: str = "") -> str:
    """A stable ``statement_id`` for a feed window; the same window maps to one file."""
    digest = hashlib.sha256(f"{account}|{source}".encode("utf-8")).hexdigest()[:10]
    return f"feed-{digest}-{start:%Y%m%d}-{end:%Y%m%d}"


def record_feed_window(entity_path: Path | str, *, account: str, start: date, end: date,
                       source: str = "", evidence_ref: str = "", evidence_sha256: str = "",
                       extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Write (or rewrite) one ``feed_sync`` record for a window a feed returned in full."""
    return record_statement(entity_path, {
        "schema_version": SCHEMA_VERSION,
        "statement_id": feed_window_id(account, start, end, source),
        "account": account,
        "kind": "feed_sync",
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "source": source,
        "evidence_ref": evidence_ref,
        "evidence_sha256": evidence_sha256,
    }, replace=True, extra=extra)


# ---------------------------------------------------------------------------
# Expected accounts and evidence collection
# ---------------------------------------------------------------------------


def _declared(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    # Lazy import: readiness imports this module.
    from .readiness import _source_rows

    return _source_rows(config, date.min, date.max)


def expected_accounts(entity_path: Path | str) -> list[str]:
    """Accounts the company expects source data for: declared, mapped or with statements."""
    entity = load_entity(entity_path)
    accounts = {str(row["account"]) for row in _declared(entity.entity_config) if row.get("account")}
    records, _ = load_statements(entity.path)
    accounts.update(record.account for record in records)
    return sorted(account for account in accounts if account.startswith(("Assets:", "Liabilities:")))


def _transaction_dates(entity_path: Path, accounts: Sequence[str]) -> dict[str, list[date]]:
    result: dict[str, list[date]] = {account: [] for account in accounts}
    store_path = default_store_path(entity_path)
    if not store_path.is_file() or not accounts:
        return result
    store = LedgerStore(store_path)
    placeholders = ",".join("?" for _ in accounts)
    with store.connection() as conn:
        try:
            rows = conn.execute(
                f"SELECT DISTINCT p.account, e.date FROM postings p JOIN entries e ON e.id = p.entry_id "
                f"WHERE p.account IN ({placeholders}) AND e.source_id IS NOT NULL AND e.source_id != '' "
                f"ORDER BY p.account, e.date",
                list(accounts),
            ).fetchall()
        except sqlite3.DatabaseError:
            return result
    for row in rows:
        parsed = _parse_date(str(row[1]))
        if parsed is not None:
            result[str(row[0])].append(parsed)
    return result


def _transaction_runs(dates: Sequence[date], max_quiet_days: int) -> list[tuple[date, date]]:
    runs: list[tuple[date, date]] = []
    for value in sorted(set(dates)):
        if runs and (value - runs[-1][1]).days <= max_quiet_days:
            runs[-1] = (runs[-1][0], value)
        else:
            runs.append((value, value))
    return runs


# ---------------------------------------------------------------------------
# Continuity between statements
# ---------------------------------------------------------------------------


def _continuity(account: str, label: str, statements: Sequence[StatementRecord],
                start: date, end: date) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    relevant = sorted(
        (s for s in statements if s.kind == "statement"
         and s.period_end >= start - timedelta(days=1) and s.period_start <= end + timedelta(days=1)),
        key=lambda s: (s.period_start, s.period_end, s.statement_id),
    )
    for previous, current in zip(relevant, relevant[1:]):
        refs = [previous.statement_id, current.statement_id]
        if (previous.period_start, previous.period_end) == (current.period_start, current.period_end):
            same = (previous.opening_balance, previous.closing_balance) == (current.opening_balance, current.closing_balance)
            findings.append({
                "code": "statement_duplicate" if same else "statement_conflict",
                "severity": "warning" if same else "error",
                "account": account,
                "refs": refs,
                "from": current.period_start.isoformat(),
                "to": current.period_end.isoformat(),
                "message": (
                    f"{label} has two statements for {format_range(current.period_start, current.period_end)}"
                    + (" with the same balances; check for a duplicate import." if same
                       else " with different balances; keep only the correct one.")
                ),
            })
            continue
        if current.period_start <= previous.period_end:
            overlap_end = min(previous.period_end, current.period_end)
            findings.append({
                "code": "statement_overlap",
                "severity": "warning",
                "account": account,
                "refs": refs,
                "from": current.period_start.isoformat(),
                "to": overlap_end.isoformat(),
                "message": (
                    f"{label} statements {previous.statement_id} and {current.statement_id} overlap on "
                    f"{format_range(current.period_start, overlap_end)}; make sure those transactions are not imported twice."
                ),
            })
            continue
        if current.period_start > previous.period_end + timedelta(days=1):
            gap_start = previous.period_end + timedelta(days=1)
            gap_end = current.period_start - timedelta(days=1)
            findings.append({
                "code": "statement_gap",
                "severity": "warning",
                "account": account,
                "refs": refs,
                "from": gap_start.isoformat(),
                "to": gap_end.isoformat(),
                "message": f"{label} has no statement for {format_range(gap_start, gap_end)} between {previous.statement_id} and {current.statement_id}.",
            })
            continue
        if previous.closing_balance is None or current.opening_balance is None:
            continue
        difference = current.opening_balance - previous.closing_balance
        if difference != _ZERO:
            findings.append({
                "code": "balance_discontinuity",
                "severity": "error",
                "account": account,
                "refs": refs,
                "from": previous.period_end.isoformat(),
                "to": current.period_start.isoformat(),
                "previous_closing_balance": str(previous.closing_balance),
                "next_opening_balance": str(current.opening_balance),
                "difference": str(difference),
                "message": (
                    f"{label} statement ending {_fmt_day(previous.period_end)}, {previous.period_end.year} closes at "
                    f"{_money(previous.closing_balance)}, but the next statement opens at "
                    f"{_money(current.opening_balance)} (difference {_money(difference)})."
                ),
            })
    return findings


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------


def check_coverage(
    entity_path: Path | str,
    from_date: date,
    to_date: date,
    *,
    accounts: Sequence[str] | None = None,
    max_quiet_days: int = DEFAULT_MAX_QUIET_DAYS,
) -> dict[str, Any]:
    """Return the coverage document for each expected account and month."""
    if from_date > to_date:
        raise ValueError("The period start must not be after its end.")
    if type(max_quiet_days) is not int or not 1 <= max_quiet_days <= 366:
        raise ValueError("max_quiet_days must be a whole number from 1 to 366.")
    entity = load_entity(entity_path)
    declared = _declared(entity.entity_config)
    records, findings = load_statements(entity.path)
    expected = sorted(set(accounts)) if accounts else expected_accounts(entity.path)
    if not expected:
        findings.append({
            "code": "source_scope_missing",
            "severity": "error",
            "account": None,
            "refs": [],
            "message": "No expected source accounts are declared, mapped or covered by a statement record.",
        })
    tx_dates = _transaction_dates(entity.path, expected)
    months = _months(from_date, to_date)

    account_rows = []
    totals = {"complete": 0, "partial": 0, "missing": 0}
    for account in expected:
        sources = sorted({str(row["source"]) for row in declared if row.get("account") == account and row.get("source")})
        account_records = [record for record in records if record.account == account]
        label = _label(entity.entity_config, account, account_records)
        evidence: dict[str, list[tuple[date, date]]] = {basis: [] for basis in BASIS_ORDER}
        for record in account_records:
            evidence[record.kind].append((record.period_start, record.period_end))
        for row in declared:
            if row.get("account") != account:
                continue
            first, last = _parse_date(row.get("coverage_from")), _parse_date(row.get("coverage_to"))
            if first and last and first <= last:
                evidence["declared"].append((first, last))
        evidence["transactions"] = _transaction_runs(tx_dates.get(account, []), max_quiet_days)
        merged = {basis: _merge(intervals) for basis, intervals in evidence.items()}
        strong = _merge(i for basis in _STRONG_BASES for i in merged[basis])
        everything = _merge(i for intervals in merged.values() for i in intervals)

        month_rows = []
        for month, first, last in months:
            covered = _clip(everything, first, last)
            gaps = _gaps(covered, first, last)
            days = (last - first).days + 1
            covered_days = _days(covered)
            status = "complete" if not gaps else ("missing" if covered_days == 0 else "partial")
            totals[status] += 1
            bases = [basis for basis in BASIS_ORDER if _clip(merged[basis], first, last)]
            strong_days = _days(_clip(strong, first, last))
            verified = status == "complete" and strong_days == days
            noun = ("statement, feed window, declared coverage or transactions"
                    if account_records or merged["declared"] else "transactions")
            reasons = [f"{label} has no {noun} for {format_range(a, b)}." for a, b in gaps]
            notes = []
            if covered_days and strong_days < covered_days:
                weak = [(a, b) for a, b in _gaps(_clip(strong, first, last), first, last) if _clip(covered, a, b)]
                spans = ", ".join(format_range(*span) for span in _merge(
                    c for a, b in weak for c in _clip(covered, a, b)))
                notes.append(
                    f"{label} coverage for {spans} is inferred from transaction dates only; "
                    "add a statement or feed window to confirm it."
                )
            month_rows.append({
                "month": month,
                "from": first.isoformat(),
                "to": last.isoformat(),
                "status": status,
                "verified": verified,
                "basis": bases,
                "days": days,
                "covered_days": covered_days,
                "gaps": [{"from": a.isoformat(), "to": b.isoformat(), "days": (b - a).days + 1} for a, b in gaps],
                "reasons": reasons,
                "notes": notes,
            })
        continuity = _continuity(account, label, account_records, from_date, to_date)
        findings.extend(continuity)
        statuses = {row["status"] for row in month_rows}
        account_status = "complete" if statuses == {"complete"} else ("missing" if statuses == {"missing"} else "partial")
        account_rows.append({
            "account": account,
            "label": label,
            "sources": sources,
            "status": account_status,
            "verified": all(row["verified"] for row in month_rows),
            "statements": sorted(
                ({"statement_id": r.statement_id, "kind": r.kind, "from": r.period_start.isoformat(),
                  "to": r.period_end.isoformat(),
                  "opening_balance": None if r.opening_balance is None else str(r.opening_balance),
                  "closing_balance": None if r.closing_balance is None else str(r.closing_balance)}
                 for r in account_records if r.period_end >= from_date and r.period_start <= to_date),
                key=lambda item: (item["from"], item["to"], item["statement_id"]),
            ),
            "months": month_rows,
        })

    errors = [item for item in findings if item["severity"] == "error"]
    complete = bool(expected) and totals["partial"] == 0 and totals["missing"] == 0 and not errors
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "source_coverage",
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "max_quiet_days": max_quiet_days,
        "status": "complete" if complete else "incomplete",
        "summary": {
            "accounts": len(expected),
            "months_complete": totals["complete"],
            "months_partial": totals["partial"],
            "months_missing": totals["missing"],
            "months_inferred": sum(
                1 for row in account_rows for month in row["months"]
                if month["status"] == "complete" and not month["verified"]
            ),
            "errors": len(errors),
            "warnings": sum(1 for item in findings if item["severity"] == "warning"),
        },
        "accounts": account_rows,
        "findings": sorted(findings, key=lambda item: (
            str(item.get("account") or ""), str(item.get("from") or ""), item["code"], item["refs"])),
    }


def readiness_items(document: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert a coverage document into readiness blockers and warnings."""
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for account in document.get("accounts", []):
        for month in account["months"]:
            if month["status"] in {"partial", "missing"}:
                blockers.append({
                    "code": f"source_month_{month['status']}",
                    "message": " ".join(month["reasons"]),
                    "refs": [account["account"]],
                })
            elif not month["verified"]:
                warnings.append({
                    "code": "source_month_inferred",
                    "message": " ".join(month["notes"]),
                    "refs": [account["account"]],
                })
    for finding in document.get("findings", []):
        if finding["code"] == "source_scope_missing":
            continue  # readiness already reports its own source_scope_missing blocker
        target = blockers if finding["severity"] == "error" else warnings
        target.append({
            "code": f"source_{finding['code']}" if not finding["code"].startswith("statement_") else finding["code"],
            "message": finding["message"],
            "refs": sorted({*([finding["account"]] if finding.get("account") else []), *finding["refs"]}),
        })
    return blockers, warnings


# ---------------------------------------------------------------------------
# Text rendering and CLI
# ---------------------------------------------------------------------------


def render_text(document: Mapping[str, Any]) -> str:
    lines = [f"Source coverage {document['from']} to {document['to']}: {document['status'].upper()}"]
    summary = document["summary"]
    lines.append(
        f"  {summary['accounts']} account(s); months complete {summary['months_complete']}, "
        f"partial {summary['months_partial']}, missing {summary['months_missing']}, "
        f"inferred only {summary['months_inferred']}"
    )
    for account in document["accounts"]:
        lines.append("")
        lines.append(f"{account['label']} ({_account_label(account['account'])}): {account['status']}")
        for month in account["months"]:
            mark = month["status"] if month["verified"] or month["status"] != "complete" else "complete (inferred)"
            basis = ", ".join(month["basis"]) or "none"
            lines.append(f"  {month['month']}  {mark:<20} basis: {basis}")
            for reason in [*month["reasons"], *month["notes"]]:
                lines.append(f"      - {reason}")
    if document["findings"]:
        lines.append("")
        lines.append("Findings:")
        for item in document["findings"]:
            lines.append(f"  [{item['severity'].upper()}] {item['message']}")
    return "\n".join(lines) + "\n"


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "coverage", help="Check source coverage per account and month, and record statement metadata")
    sub = parser.add_subparsers(dest="coverage_command", required=True)

    check = sub.add_parser("check", help="Report complete, partial or missing coverage per account and month")
    check.add_argument("--entity", required=True, type=Path)
    check.add_argument("--from", dest="from_date", required=True, help="Period start (YYYY-MM-DD)")
    check.add_argument("--to", dest="to_date", required=True, help="Period end (YYYY-MM-DD)")
    check.add_argument("--account", action="append", default=[],
                       help="Expected source account; repeat. Default: declared, mapped and statement accounts")
    check.add_argument("--max-quiet-days", type=int, default=DEFAULT_MAX_QUIET_DAYS,
                       help="Longest run of days without transactions that still counts as covered")
    check.add_argument("--format", choices=["text", "json"], default="text", dest="fmt")
    check.add_argument("--output", type=Path, default=None, help="Write the report to this file")

    add = sub.add_parser("statement-add", help="Record statement or feed-window metadata for an account")
    add.add_argument("--entity", required=True, type=Path)
    add.add_argument("--statement-id", required=True)
    add.add_argument("--account", required=True)
    add.add_argument("--kind", choices=list(EVIDENCE_KINDS), default="statement")
    add.add_argument("--from", dest="from_date", required=True, help="First statement date (YYYY-MM-DD)")
    add.add_argument("--to", dest="to_date", required=True, help="Last statement date (YYYY-MM-DD)")
    add.add_argument("--opening-balance", default=None)
    add.add_argument("--closing-balance", default=None)
    add.add_argument("--source", default="", help="Human label, such as Operating Checking")
    add.add_argument("--evidence-ref", default="")
    add.add_argument("--evidence-sha256", default="")
    add.add_argument("--replace", action="store_true", help="Replace an existing record with the same ID")

    listing = sub.add_parser("statement-list", help="List recorded statement and feed-window metadata")
    listing.add_argument("--entity", required=True, type=Path)


def run(args: Any) -> int:
    command = args.coverage_command
    try:
        if command == "check":
            document = check_coverage(
                args.entity,
                date.fromisoformat(args.from_date),
                date.fromisoformat(args.to_date),
                accounts=args.account or None,
                max_quiet_days=args.max_quiet_days,
            )
            rendered = (json.dumps(document, indent=2, sort_keys=True) + "\n"
                        if args.fmt == "json" else render_text(document))
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered, encoding="utf-8")
                print(f"Written to {args.output}")
            else:
                print(rendered, end="")
            return 0
        if command == "statement-add":
            result = record_statement(args.entity, {
                "schema_version": SCHEMA_VERSION,
                "statement_id": args.statement_id,
                "account": args.account,
                "kind": args.kind,
                "period_start": args.from_date,
                "period_end": args.to_date,
                "opening_balance": args.opening_balance,
                "closing_balance": args.closing_balance,
                "source": args.source,
                "evidence_ref": args.evidence_ref,
                "evidence_sha256": args.evidence_sha256,
            }, replace=args.replace)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if command == "statement-list":
            records, findings = load_statements(load_entity(args.entity).path)
            print(json.dumps({
                "statements": [record.to_document() for record in records],
                "findings": findings,
            }, indent=2, sort_keys=True))
            return 0
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Unknown coverage command: {command}", file=sys.stderr)
    return 2
