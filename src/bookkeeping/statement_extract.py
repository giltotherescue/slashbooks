"""Statement extraction contract, validators and import.

A PDF or image statement cannot be read by the engine. A reader outside the
engine, either a set of rules or a model, turns the statement text into one
JSON object that follows the contract below. The engine does not trust that
object. It checks the object with code, and it records the statement only when
every check passes.

Contract (``schema_version`` 1)
-------------------------------
- ``schema_version``: ``1``.
- ``source``: ``rules`` or ``model``. Who read the statement.
- ``account_kind``: ``bank`` or ``card``. A card statement covers any
  liability account, such as a loan.
- ``period_start`` and ``period_end``: ``YYYY-MM-DD`` or ``null``. Both are
  needed before the engine records the statement.
- ``period_source``: ``printed`` (the default) or ``derived``. Use ``derived``
  when the reader did not read the start date from the statement, for example
  when it is the day after the previous statement ended.
- ``opening_balance`` and ``closing_balance``: decimal strings or ``null``,
  in the statement's own presentation: money held for a bank account, money
  owed for a card.
- ``transactions``: a list of ``{date, description, amount}``. ``amount`` is
  the effect on the printed balance. For a bank account, a deposit is positive
  and a withdrawal is negative. For a card, a purchase is positive and a
  payment is negative. ``running_balance`` (a printed balance after the row)
  and ``confidence`` (``high``, ``medium`` or ``low``) are optional.
- ``confidence``: optional, ``high``, ``medium`` or ``low``, for the whole
  statement. ``low`` always sends the statement to review.
- ``row_order``: optional. ``chronological`` when the statement prints its rows
  in date order. The engine then checks the order.
- ``excluded``: optional list of short texts for rows the reader saw and left
  out, for example pending rows. It is for the reviewer. The engine does not
  count these rows.
- ``confirmation``: optional ``{name, at}``. A person who corrected or
  confirmed the numbers. It never replaces a check.
- ``extractor``: optional ``{name, version}`` text for the audit trail.

Checks
------
``validate_extraction`` runs these checks and never changes any file:

1. The period and both balances are present.
2. The opening balance plus every transaction equals the closing balance, to
   the cent.
3. Each printed running balance equals the balance before the row plus the
   amount of the row.
4. Every date is a real date inside the period. A card row may be dated up to
   7 days before the period starts. Rows are in date order when the reader
   says they are.
5. The account kind agrees with the ledger account, so a card cannot be read
   with bank signs.
6. The reader did not mark the statement low confidence.
7. When a second extraction is given, both agree.

The model that reads a statement is not part of the engine. Its output is
untrusted data. The checks bind it, whoever wrote the numbers.

Review, not record
------------------
A statement that fails a check is never recorded. ``import_extracted`` stages
no rows and writes no coverage record. It writes a review record in
``ingestion/statements/needs-review/`` with plain reasons, and the month stays
a gap in ``books coverage check``. The statement that passes every check is
recorded through the existing statement import path, so its rows go through the
same duplicate plan and its record feeds source coverage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import statement_import as si
from .connectors import bankcsv
from .ledger.normalize import normalize_description

JsonObject = dict[str, Any]

SCHEMA_VERSION = 1
SOURCES = ("rules", "model")
ACCOUNT_KINDS = ("bank", "card")
CONFIDENCE = ("high", "medium", "low")
ROW_ORDERS = ("chronological", "unordered")
PERIOD_SOURCES = ("printed", "derived")
CARD_DATE_GRACE_DAYS = 7
MAX_ROWS = 5000
MAX_DESCRIPTION = 300
MAX_EXCLUDED = 200
EXTRACTED_FORMAT = "extracted"
REVIEW_DIR = "needs-review"

_AMOUNT = re.compile(r"^[+-]?\d{1,15}(?:\.\d{1,2})?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass
class Row:
    index: int
    day: date | None
    description: str
    amount: Decimal | None
    running_balance: Decimal | None
    confidence: str | None


@dataclass
class Extraction:
    source: str
    account_kind: str
    period_start: date | None
    period_end: date | None
    period_source: str
    opening: Decimal | None
    closing: Decimal | None
    rows: list[Row]
    confidence: str | None
    row_order: str | None
    excluded: list[str]
    confirmation: JsonObject | None
    extractor: JsonObject | None


# ---------------------------------------------------------------------------
# Parse (shape only; never raises for statement defects)
# ---------------------------------------------------------------------------

def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(bankcsv.MONEY), "f")


def _text(value: Decimal) -> str:
    return f"{value.quantize(bankcsv.MONEY):,.2f}"


def _parse_amount(value: Any) -> tuple[Decimal | None, bool]:
    """Return ``(amount, ok)``. ``None`` with ``ok`` is an absent value."""
    if value is None or value == "":
        return None, True
    if isinstance(value, bool):
        return None, False
    if isinstance(value, float):
        value = repr(value)
    if isinstance(value, (int, Decimal)):
        value = str(value)
    if not isinstance(value, str) or not _AMOUNT.fullmatch(value.strip()):
        return None, False
    try:
        return Decimal(value.strip()), True
    except InvalidOperation:  # pragma: no cover - the pattern prevents it
        return None, False


def _parse_date(value: Any) -> tuple[date | None, bool]:
    if value is None or value == "":
        return None, True
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return None, False
    try:
        return date.fromisoformat(value), True
    except ValueError:
        return None, False


def _clean(text: Any, limit: int) -> str:
    return _CONTROL.sub(" ", str(text)).strip()[:limit]


def parse_extraction(raw: Any) -> tuple[Extraction | None, list[JsonObject]]:
    """Read the contract object. Returns the extraction and reasons for every shape defect."""
    reasons: list[JsonObject] = []

    def bad(code: str, message: str, rows: list[int] | None = None) -> None:
        item: JsonObject = {"code": code, "message": message}
        if rows:
            item["rows"] = rows[:20]
        reasons.append(item)

    if not isinstance(raw, dict):
        bad("unreadable_extraction", "The extraction is not a JSON object, so nothing was recorded.")
        return None, reasons
    if raw.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        bad("unreadable_extraction", f"The extraction uses schema_version {raw.get('schema_version')!r}. "
            f"This version of the engine reads schema_version {SCHEMA_VERSION}.")
        return None, reasons
    source = raw.get("source")
    if source not in SOURCES:
        bad("unreadable_extraction", "The extraction must say who read the statement: source is rules or model.")
        source = "model"
    kind = raw.get("account_kind")
    if kind not in ACCOUNT_KINDS:
        bad("unreadable_extraction", "The extraction must say if the statement is for a bank account or a card.")
        kind = "bank"
    period_source = raw.get("period_source", "printed")
    if period_source not in PERIOD_SOURCES:
        bad("unreadable_extraction", "period_source must be printed or derived.")
        period_source = "printed"
    values: dict[str, Any] = {}
    for key in ("period_start", "period_end"):
        parsed, ok = _parse_date(raw.get(key))
        if not ok:
            bad("bad_period", f"{key} is not a valid YYYY-MM-DD date.")
        values[key] = parsed
    for key in ("opening_balance", "closing_balance"):
        parsed, ok = _parse_amount(raw.get(key))
        if not ok:
            bad("bad_balance", f"{key} is not a plain number with at most two decimals.")
        values[key] = parsed
    confidence = raw.get("confidence")
    if confidence is not None and confidence not in CONFIDENCE:
        bad("unreadable_extraction", "confidence must be high, medium or low.")
        confidence = None
    row_order = raw.get("row_order")
    if row_order is not None and row_order not in ROW_ORDERS:
        bad("unreadable_extraction", "row_order must be chronological or unordered.")
        row_order = None

    raw_rows = raw.get("transactions")
    rows: list[Row] = []
    if not isinstance(raw_rows, list):
        bad("unreadable_extraction", "The extraction has no list of transactions.")
        raw_rows = []
    elif len(raw_rows) > MAX_ROWS:
        bad("too_many_rows", f"The extraction has {len(raw_rows)} transactions. The limit is {MAX_ROWS}.")
        raw_rows = raw_rows[:MAX_ROWS]
    bad_rows: dict[str, list[int]] = {"date": [], "amount": [], "description": [], "running": [], "confidence": []}
    for index, item in enumerate(raw_rows):
        if not isinstance(item, dict):
            bad_rows["amount"].append(index + 1)
            rows.append(Row(index + 1, None, "", None, None, None))
            continue
        day, day_ok = _parse_date(item.get("date"))
        if day is None:
            day_ok = False
        amount, amount_ok = _parse_amount(item.get("amount"))
        if amount is None:
            amount_ok = False
        balance, balance_ok = _parse_amount(item.get("running_balance"))
        description = _clean(item.get("description") or "", MAX_DESCRIPTION)
        row_confidence = item.get("confidence")
        if row_confidence is not None and row_confidence not in CONFIDENCE:
            bad_rows["confidence"].append(index + 1)
            row_confidence = None
        if not day_ok:
            bad_rows["date"].append(index + 1)
        if not amount_ok:
            bad_rows["amount"].append(index + 1)
        if not balance_ok:
            bad_rows["running"].append(index + 1)
        if not description:
            bad_rows["description"].append(index + 1)
        rows.append(Row(index + 1, day, description, amount, balance, row_confidence))
    if bad_rows["date"]:
        bad("bad_date", f"{len(bad_rows['date'])} transaction(s) have no valid date (YYYY-MM-DD).", bad_rows["date"])
    if bad_rows["amount"]:
        bad("bad_amount", f"{len(bad_rows['amount'])} transaction(s) have an amount that is not a plain number "
            "with at most two decimals.", bad_rows["amount"])
    if bad_rows["running"]:
        bad("bad_balance", f"{len(bad_rows['running'])} transaction(s) have a running balance that is not a plain "
            "number.", bad_rows["running"])
    if bad_rows["description"]:
        bad("bad_description", f"{len(bad_rows['description'])} transaction(s) have no description.",
            bad_rows["description"])
    if bad_rows["confidence"]:
        bad("unreadable_extraction", "A transaction confidence must be high, medium or low.", bad_rows["confidence"])

    excluded_raw = raw.get("excluded") or []
    excluded = [_clean(x, MAX_DESCRIPTION) for x in excluded_raw[:MAX_EXCLUDED]] if isinstance(excluded_raw, list) else []
    confirmation = raw.get("confirmation")
    if confirmation is not None:
        if (isinstance(confirmation, dict) and isinstance(confirmation.get("name"), str)
                and confirmation["name"].strip()):
            confirmation = {"name": _clean(confirmation["name"], 120),
                            "at": _clean(confirmation.get("at") or "", 40) or None}
        else:
            bad("unreadable_extraction", "confirmation needs the name of the person who confirmed the numbers.")
            confirmation = None
    extractor = raw.get("extractor")
    if isinstance(extractor, dict):
        extractor = {k: _clean(extractor[k], 120) for k in ("name", "version") if isinstance(extractor.get(k), str)}
    else:
        extractor = None
    return Extraction(
        source=source, account_kind=kind, period_start=values["period_start"], period_end=values["period_end"],
        period_source=period_source, opening=values["opening_balance"], closing=values["closing_balance"],
        rows=rows, confidence=confidence, row_order=row_order, excluded=excluded,
        confirmation=confirmation, extractor=extractor), reasons


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

def _tieout(ext: Extraction) -> JsonObject:
    amounts_ok = all(r.amount is not None for r in ext.rows)
    total = sum((r.amount for r in ext.rows if r.amount is not None), Decimal("0.00"))
    tie: JsonObject = {
        "opening_balance": _money(ext.opening), "closing_balance": _money(ext.closing),
        "transactions_total": _money(total), "expected_closing": None, "difference": None,
        "balanced": False, "running_balance_rows": 0, "running_balance_breaks": 0,
        "dates_outside_period": 0, "dates_out_of_order": 0,
    }
    if ext.opening is not None:
        tie["expected_closing"] = _money(ext.opening + total)
    if ext.opening is not None and ext.closing is not None and amounts_ok:
        tie["difference"] = _money(ext.closing - ext.opening - total)
        tie["balanced"] = ext.closing == ext.opening + total
    return tie


def _row_label(row: Row) -> str:
    return f"{row.day.isoformat() if row.day else 'no date'} {row.description[:40]}".strip()


def validate_extraction(raw: Any, *, ledger_account: str | None = None,
                        cross_check: Any = None) -> JsonObject:
    """Check an extraction. Returns ``status`` (``verified`` or ``needs_review``), plain ``reasons`` and the ``tieout``.

    The function reads nothing from disk and writes nothing. ``ledger_account``
    adds the check that the statement kind agrees with the account.
    ``cross_check`` is a second extraction of the same statement.
    """
    ext, reasons = parse_extraction(raw)
    if ext is None:
        return {"status": "needs_review", "reasons": reasons, "tieout": None}

    def bad(code: str, message: str, rows: list[int] | None = None, **extra: Any) -> None:
        item: JsonObject = {"code": code, "message": message, **extra}
        if rows:
            item["rows"] = rows[:20]
        reasons.append(item)

    # 1. Completeness.
    missing = [label for label, value in (
        ("the period start", ext.period_start), ("the period end", ext.period_end),
        ("the opening balance", ext.opening), ("the closing balance", ext.closing)) if value is None]
    if missing:
        bad("incomplete", "The extraction does not have " + ", ".join(missing) + ". "
            "Read it from the statement, or ask the person for it.")
    if ext.period_start and ext.period_end and ext.period_start > ext.period_end:
        bad("period_order", f"The period starts on {ext.period_start} after it ends on {ext.period_end}.")

    # 2. Tie-out.
    tie = _tieout(ext)
    if tie["difference"] is not None and not tie["balanced"]:
        diff = Decimal(tie["difference"])
        bad("tieout", f"The opening balance {_text(ext.opening)} plus the transactions {_text(Decimal(tie['transactions_total']))} "
            f"comes to {_text(Decimal(tie['expected_closing']))}, but the statement closes at {_text(ext.closing)}. "
            f"The difference is {_text(diff)}. A transaction may be missing, extra or misread.",
            difference=tie["difference"])

    # 3. Running balances.
    balance = ext.opening
    breaks: list[int] = []
    for row in ext.rows:
        if row.running_balance is not None:
            tie["running_balance_rows"] += 1
            if balance is not None and row.amount is not None and balance + row.amount != row.running_balance:
                breaks.append(row.index)
            balance = row.running_balance
        elif balance is not None and row.amount is not None:
            balance += row.amount
    tie["running_balance_breaks"] = len(breaks)
    if breaks:
        first = ext.rows[breaks[0] - 1]
        bad("running_balance", f"{len(breaks)} printed running balance(s) do not follow from the row before. "
            f"The first is row {first.index} ({_row_label(first)}).", breaks)

    # 4. Dates.
    if ext.period_end is not None:
        grace = timedelta(days=CARD_DATE_GRACE_DAYS if ext.account_kind == "card" else 0)
        low = ext.period_start - grace if ext.period_start else None
        outside = [r.index for r in ext.rows if r.day is not None
                   and (r.day > ext.period_end or (low is not None and r.day < low))]
        tie["dates_outside_period"] = len(outside)
        if outside:
            first = ext.rows[outside[0] - 1]
            bad("dates_outside_period", f"{len(outside)} transaction date(s) are outside the statement period "
                f"{ext.period_start or '?'} to {ext.period_end}. The first is row {first.index} ({_row_label(first)}). "
                "A misread date can hide in a balance that still adds up.", outside)
    if ext.row_order == "chronological":
        dated = [r for r in ext.rows if r.day is not None]
        out_of_order = [b.index for a, b in zip(dated, dated[1:]) if b.day < a.day]
        tie["dates_out_of_order"] = len(out_of_order)
        if out_of_order:
            bad("dates_out_of_order", f"The statement prints its rows in date order, but {len(out_of_order)} row(s) "
                f"are dated before the row above. The first is row {out_of_order[0]}.", out_of_order)

    # 5. Kind agrees with the ledger account.
    if ledger_account is not None:
        expected = "card" if ledger_account.startswith("Liabilities:") else "bank"
        if ext.account_kind != expected:
            bad("account_kind", f"The statement was read as a {ext.account_kind} statement, but {ledger_account} "
                f"is a {'card or loan' if expected == 'card' else 'bank'} account. The signs would be wrong.")

    # 6. Confidence.
    if ext.confidence == "low":
        bad("low_confidence", "The reader marked this statement low confidence.")
    low_rows = [r.index for r in ext.rows if r.confidence == "low"]
    if low_rows:
        bad("low_confidence", f"The reader marked {len(low_rows)} transaction(s) low confidence.", low_rows)

    # 7. Agreement of two readers.
    if cross_check is not None:
        other, other_reasons = parse_extraction(cross_check)
        if other is None or other_reasons:
            bad("readers_disagree", "The second extraction could not be read, so the two readers cannot be compared.")
        else:
            different = _differences(ext, other)
            if different:
                bad("readers_disagree", "The two readers did not agree: " + "; ".join(different) + ".")

    return {"status": "needs_review" if reasons else "verified", "reasons": reasons, "tieout": tie}


def _differences(a: Extraction, b: Extraction) -> list[str]:
    found = []
    for label, left, right in (("period start", a.period_start, b.period_start), ("period end", a.period_end, b.period_end),
                               ("opening balance", a.opening, b.opening), ("closing balance", a.closing, b.closing)):
        if left != right:
            found.append(f"{label} {left} against {right}")
    mine = Counter((r.day, r.amount) for r in a.rows)
    theirs = Counter((r.day, r.amount) for r in b.rows)
    if mine != theirs:
        found.append(f"{sum((mine - theirs).values()) + sum((theirs - mine).values())} transaction(s) differ")
    return found


# ---------------------------------------------------------------------------
# Profile, import and review
# ---------------------------------------------------------------------------

def save_extracted_profile(entity_path: Path, *, name: str, ledger_account: str, account_name: str = "") -> JsonObject:
    """Save a confirmed profile so extracted statements can import into one account."""
    from .entity import load_entity
    from .ledger.importer import _entity_write_lock

    if not si._PROFILE_NAME.match(name or ""):
        raise si.StatementImportError("invalid_profile_name",
                                      "Profile names use lowercase letters, digits and hyphens, e.g. chase-checking.")
    with _entity_write_lock(Path(entity_path)):
        entity = load_entity(entity_path)
        si._check_ledger_account(entity, ledger_account)
        existing = si._profiles(entity.entity_config).get(name)
        if existing and existing.get("ledger_account") != ledger_account:
            raise si.StatementImportError(
                "profile_account_changed",
                f"Profile '{name}' already belongs to {existing.get('ledger_account')}. "
                "Use a new profile name for another account.")
        record = {
            "account_name": account_name or name.replace("-", " ").title(),
            "ledger_account": ledger_account,
            "boundary_date": None,
            "side": "before",
            "confirmed": True,
            "source_format": EXTRACTED_FORMAT,
            "statement_profile": {"format": EXTRACTED_FORMAT, "version": 1},
        }
        config = json.loads((entity.path / "entity.json").read_text(encoding="utf-8"))
        config.setdefault("csv_account_mappings", {})[si.MAPPING_PREFIX + name] = record
        si._write_json_atomic(entity.path / "entity.json", config)
    return {"profile": name, "mapping_key": si.MAPPING_PREFIX + name, **record}


def _core(ext: Extraction) -> str:
    """Canonical text of what was read, so the same reading has the same statement ID."""
    return json.dumps({
        "kind": ext.account_kind, "start": str(ext.period_start), "end": str(ext.period_end),
        "opening": _money(ext.opening), "closing": _money(ext.closing),
        "rows": [[str(r.day), r.description, _money(r.amount)] for r in ext.rows],
    }, sort_keys=True)


def review_dir(entity_path: Path | str) -> Path:
    return Path(entity_path) / si.STATEMENTS_DIR / REVIEW_DIR


def load_review_records(entity_path: Path | str) -> list[JsonObject]:
    """Statements that failed a check and wait for a person (oldest first)."""
    directory = review_dir(entity_path)
    records = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise si.StatementImportError("damaged_state", f"Cannot read {path}; repair it first.") from exc
            if isinstance(item, dict):
                records.append(item)
    records.sort(key=lambda r: (str(r.get("account")), str(r.get("period_end")), str(r.get("statement_id"))))
    return records


def _write_review(entity_path: Path, record: JsonObject) -> None:
    si._write_json_atomic(review_dir(entity_path) / f"{record['statement_id']}.json", record)


def _resolve_reviews(entity_path: Path, account: str, statement_id: str, period_end: str, ts: str) -> list[str]:
    """Close earlier review records for the same account and period end that this statement replaces."""
    closed = []
    for item in load_review_records(entity_path):
        if (item.get("status") == "needs_review" and item.get("account") == account
                and item.get("period_end") == period_end):
            item.update(status="resolved", resolved_by_statement_id=statement_id, resolved_at=ts)
            _write_review(entity_path, item)
            closed.append(str(item["statement_id"]))
    return closed


def import_extracted(entity_path: Path, raw: Any, *, profile: str, evidence: Path | None = None,
                     dry_run: bool = False, intake_only: bool = False, session_id: str | None = None,
                     ts: str | None = None, cross_check: Any = None) -> JsonObject:
    """Record a validated extraction through the statement import path.

    A statement that fails a check is never recorded: the result has
    ``status: needs_review`` and plain ``reasons``, no row is staged and no
    coverage record is written. A review record is kept for a person.
    """
    from .entity import load_entity
    from .ledger.importer import _entity_write_lock, import_transactions
    from . import queue as queue_module
    from .work import observe_import

    entity = load_entity(entity_path)
    profiles = si._profiles(entity.entity_config)
    if profile not in profiles:
        raise si.StatementImportError(
            "profile_not_found", f"No statement profile named '{profile}'. Save one with books statement map-extracted.")
    prof = profiles[profile]
    if not si._confirmed(prof):
        raise si.StatementImportError("profile_not_confirmed", f"Profile '{profile}' is not confirmed.")
    if prof["statement_profile"].get("format") != EXTRACTED_FORMAT:
        raise si.StatementImportError(
            "profile_format_mismatch", f"Profile '{profile}' is for CSV or OFX files. Use books statement map-extracted.")
    ledger_account = prof["ledger_account"]
    evidence_sha = None
    if evidence is not None:
        evidence = Path(evidence)
        if not evidence.is_file():
            raise si.StatementImportError("file_not_found", f"Evidence file not found: {evidence}")
        evidence_sha = si._file_sha256(evidence)

    ts = ts or si._now()
    report = validate_extraction(raw, ledger_account=ledger_account, cross_check=cross_check)
    ext, _ = parse_extraction(raw)
    mapping_key = si.MAPPING_PREFIX + profile
    statement_id = si._statement_id(mapping_key, si._hash(_core(ext)) if ext else si._hash(json.dumps(raw, sort_keys=True, default=str)))

    if report["status"] != "verified" or ext is None:
        result: JsonObject = {
            "status": "needs_review", "statement_id": statement_id, "account": ledger_account,
            "profile": profile, "reasons": report["reasons"], "tieout": report["tieout"],
            "recorded": False, "rows_staged": 0, "coverage": "not_recorded",
            "next_step": ("Show these reasons to a person. Ask for the correct statement or a corrected reading, "
                          "then run the import again. Do not record the numbers as they are."),
        }
        if not dry_run:
            record = {
                "schema_version": SCHEMA_VERSION, "status": "needs_review", "statement_id": statement_id,
                "account": ledger_account, "profile": profile, "writer": si.WRITER,
                "extraction_method": ext.source if ext else None,
                "extractor": ext.extractor if ext else None,
                "period_start": ext.period_start.isoformat() if ext and ext.period_start else None,
                "period_end": ext.period_end.isoformat() if ext and ext.period_end else None,
                "opening_balance": _money(ext.opening) if ext else None,
                "closing_balance": _money(ext.closing) if ext else None,
                "reasons": report["reasons"], "tieout": report["tieout"], "created_at": ts,
                "transaction_count": len(ext.rows) if ext else 0,
                "transactions": [{"date": r.day.isoformat() if r.day else None, "description": r.description,
                                  "amount": _money(r.amount), "running_balance": _money(r.running_balance)}
                                 for r in (ext.rows if ext else [])],
                "excluded": ext.excluded if ext else [],
            }
            if evidence_sha:
                record.update(evidence_ref=si._archive_source(entity.path, evidence, evidence_sha),
                              evidence_sha256=evidence_sha, file_name=evidence.name)
            with _entity_write_lock(entity.path):
                _write_review(entity.path, record)
            result["review_record"] = (si.STATEMENTS_DIR / REVIEW_DIR / f"{statement_id}.json").as_posix()
        return result

    sign = si.presentation_sign(ledger_account)
    session_id = session_id or f"statement-{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    occurrences: Counter[tuple] = Counter()
    txns = []
    for row in ext.rows:
        ledger_amount = sign * row.amount  # The one place where presentation becomes ledger sign.
        norm = normalize_description(row.description)
        key = (row.day.isoformat(), format(ledger_amount, "f"), norm)
        occurrences[key] += 1
        txn_id = "stmt:" + si._hash(ledger_account, row.day.isoformat(), format(ledger_amount, "f"), norm, "",
                                    str(occurrences[key]))
        txns.append(si._txn(txn_id=txn_id, day=row.day.isoformat(), description=row.description,
                            original=row.description, amount=ledger_amount, reference="", profile_name=profile,
                            profile=prof, last4=None, statement_id=statement_id, bank_id="statement-extracted",
                            currency=entity.currency))
    identical = sum(1 for n in occurrences.values() if n > 1)
    warnings: list[str] = []
    if identical:
        warnings.append(f"{identical} group(s) of identical rows were kept as separate transactions. "
                        "Check them against the statement.")
    total = sum((Decimal(t["amount"]) for t in txns), Decimal("0.00"))
    dates = sorted(t["date"] for t in txns)
    confirmed_by = ext.confirmation
    record = {
        "schema_version": 1, "statement_id": statement_id, "account": ledger_account, "kind": "statement",
        "period_start": ext.period_start.isoformat(), "period_end": ext.period_end.isoformat(),
        "opening_balance": _money(ext.opening), "closing_balance": _money(ext.closing),
        "source": prof.get("account_name") or profile, "writer": si.WRITER, "profile": profile,
        "mapping_key": mapping_key, "format": EXTRACTED_FORMAT,
        "institution_account": {"identity": None, "institution": None, "account_type": None, "account_last4": None},
        "period_source": ext.period_source, "period_confirmed": True,
        "opening_balance_date": ext.period_start.isoformat(), "closing_balance_date": ext.period_end.isoformat(),
        "balance_source": "manual_confirmed" if confirmed_by else "extracted",
        "extraction_method": ext.source, "extraction_confidence": ext.confidence, "extractor": ext.extractor,
        "tieout": report["tieout"],
        "first_transaction_date": dates[0] if dates else None, "last_transaction_date": dates[-1] if dates else None,
        "transactions_total": format(total, "f"), "transaction_count": len(txns), "currency": entity.currency,
    }
    if confirmed_by:
        record.update(confirmed_by=confirmed_by["name"], confirmed_at=confirmed_by.get("at") or ts)
    if evidence_sha:
        record.update(file_name=evidence.name, evidence_sha256=evidence_sha,
                      evidence_ref=si._archive_relative(evidence, evidence_sha).as_posix())

    with _entity_write_lock(entity.path):
        entity = load_entity(entity_path)
        plan = si.plan_duplicates(entity, txns, ledger_account, mapping_key)
        counts: JsonObject = {
            "rows_in_statement": len(txns), "held_for_review": len(plan["held"]),
            **{k: v for k, v in plan["skipped"].items()}, "to_import": len(plan["import"]),
        }
        # Reuse the import plan for month-end review. All amounts use ledger signs,
        # including cards; callers must not negate balances or sum rows themselves.
        from .reports.cache import get_account_balance, open_cache
        books_balance = None
        if (entity.path / "ledger.sqlite").exists() or (entity.path / "books.beancount").exists():
            conn = open_cache(entity.path)
            try:
                books_balance = get_account_balance(conn, ledger_account, ext.period_end)
            finally:
                conn.close()
        reconciliation = {
            "account": ledger_account, "period_start": ext.period_start.isoformat(),
            "as_of": ext.period_end.isoformat(), "opening": _money(sign * ext.opening),
            "closing": _money(sign * ext.closing), "activity": _money(total),
            "books_balance": _money(books_balance),
            "difference": _money(sign * ext.closing - books_balance) if books_balance is not None else None,
            "missing": [{k: t[k] for k in ("date", "description", "amount")} for t in plan["import"]],
            "possible_matches": len(plan["held"]),
        }
        resolved: list[str] = []
        if not dry_run:
            observe_import(plan["skipped"].get("already_imported", 0) + plan["skipped"].get("confirmed_duplicate", 0))
            if plan["import"]:
                outcome = import_transactions(
                    entity, plan["import"], session_id=session_id,
                    categorizer=queue_module.make_categorizer(entity), ts=ts, intake_only=intake_only)
                if outcome.errors:
                    raise si.StatementImportError("import_failed", "; ".join(outcome.errors[:5]))
                counts.update(posted=outcome.new_entries, queued_for_review=outcome.pending_categorization,
                              importer_duplicates=outcome.skipped_duplicate)
            else:
                counts.update(posted=0, queued_for_review=0, importer_duplicates=0)
            si._record_held(entity, plan["held"], ts)
            if evidence is not None:
                si._archive_source(entity.path, evidence, evidence_sha)
            record["imported_at"] = ts
            record["import_session"] = session_id
            si._store_record(entity.path, record)
            resolved = _resolve_reviews(entity.path, ledger_account, statement_id, record["period_end"], ts)
    return {
        "status": "dry_run" if dry_run else "imported", "statement_id": statement_id,
        "session_id": None if dry_run else session_id, "recorded": not dry_run, "reasons": [],
        "statement": record, "counts": counts, "warnings": warnings, "coverage": "recorded",
        "held": [{k: v for k, v in h.items() if k != "transaction"} for h in plan["held"]],
        "resolved_reviews": resolved, "reconciliation": reconciliation,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), parse_float=Decimal)
    except OSError as exc:
        raise si.StatementImportError("file_not_found", f"Cannot read {path}.") from exc
    except ValueError as exc:
        raise si.StatementImportError("invalid_json", f"{path} is not valid JSON.") from exc


def add_parsers(sub: Any) -> None:
    validate = sub.add_parser("validate", help="Check a statement extraction (JSON) without recording anything")
    validate.add_argument("file", type=Path, help="Extraction JSON that follows the statement extraction contract")
    validate.add_argument("--ledger-account", default=None, help="Also check that the statement kind fits this account")
    validate.add_argument("--cross-check", type=Path, default=None, help="A second extraction of the same statement")

    map_p = sub.add_parser("map-extracted", help="Save a confirmed profile for extracted (PDF or image) statements")
    map_p.add_argument("--entity", type=Path, required=True)
    map_p.add_argument("--profile", required=True, help="Profile name, for example chase-checking-pdf")
    map_p.add_argument("--ledger-account", required=True)
    map_p.add_argument("--account-name", default="")

    imp = sub.add_parser("import-extracted", help="Record a statement extraction that passes every check")
    imp.add_argument("file", type=Path, help="Extraction JSON that follows the statement extraction contract")
    imp.add_argument("--entity", type=Path, required=True)
    imp.add_argument("--profile", required=True)
    imp.add_argument("--evidence", type=Path, default=None, help="The statement file (PDF or image), kept as evidence")
    imp.add_argument("--cross-check", type=Path, default=None, help="A second extraction of the same statement")
    imp.add_argument("--dry-run", action="store_true")
    imp.add_argument("--intake-only", action="store_true")
    imp.add_argument("--session-id", default=None)


def run(args: argparse.Namespace) -> JsonObject:
    command = args.statement_command
    if command == "validate":
        cross = _load_json(args.cross_check) if args.cross_check else None
        return {**validate_extraction(_load_json(args.file), ledger_account=args.ledger_account, cross_check=cross),
                "recorded": False}
    if command == "map-extracted":
        return {"status": "saved", **save_extracted_profile(
            args.entity, name=args.profile, ledger_account=args.ledger_account, account_name=args.account_name)}
    cross = _load_json(args.cross_check) if args.cross_check else None
    return import_extracted(args.entity, _load_json(args.file), profile=args.profile, evidence=args.evidence,
                            dry_run=args.dry_run, intake_only=args.intake_only, session_id=args.session_id,
                            cross_check=cross)
