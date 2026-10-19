"""`books statement`: import historical bank and card statement files.

Supported files
---------------
- Bank and card CSV exports in any common layout (``connectors/bankcsv.py``).
- OFX, QFX and QBO Web Connect files (``connectors/ofx.py``).

Workflow
--------
1. ``books statement inspect FILE`` detects the format and proposes a
   mapping. The result is JSON with a ``status``:

   - ``ready``: a saved profile matches the file; it can be imported.
   - ``ready_to_confirm``: detection decided every field; save it with
     ``books statement map`` after a person checks the proposal.
   - ``needs_mapping``: one or more fields are listed in ``uncertain``; the
     caller must supply each one explicitly to ``books statement map``.
   - ``error``: the file cannot be read; ``error.code`` says why.

2. ``books statement map FILE --profile NAME --ledger-account ACCOUNT`` saves a
   confirmed per-account profile in ``entity.json`` under
   ``csv_account_mappings["statement:NAME"]``. The importer, review queue and
   readiness checks already use that mapping table, so the saved ledger
   account is the only authority for the cash side of each posting.

3. ``books statement import FILE --profile NAME`` posts new transactions
   through the shared importer and records the statement metadata. Use
   ``--dry-run`` to see the plan without changes.

Duplicate rules
---------------
Rules apply in this order. A row is never posted twice and is never dropped
without a count in the result.

1. Same source ID. OFX rows use ``ofx:`` plus a hash of the account identity
   and ``FITID``. CSV rows use ``stmt:`` plus a hash of the ledger account,
   date, amount, normalized description, reference and the occurrence number
   of identical rows in the file. A source ID that is already in the ledger,
   the review queue or the duplicate-candidate list is skipped.
2. Same ledger account, same date, same amount and same normalized
   description as an existing entry or review item with another source ID:
   the row is held as a duplicate candidate.
3. Same ledger account, same amount and a date within three days of an
   existing entry or review item that came from another source (a bank feed or
   another statement profile): the row is held as a duplicate candidate.

Held rows are not posted. A person resolves each one with
``books queue resolve-duplicate --decision duplicate|distinct``.

Statement records
-----------------
Each import writes one statement record in the source-coverage contract
(``coverage.py``; ``books coverage check`` reads it) to
``ingestion/statements/<statement_id>.json``. Importer details (profile,
account identity, balance dates, transaction total) are extra fields. Balances
use the statement presentation: money held for a bank account, money owed for
a card.

The record counts as coverage only when the statement period is known: the OFX
``DTSTART``/``DTEND``, a printed CSV summary with dates, or
``--period-start``/``--period-end`` from a person. Otherwise the record waits
in ``ingestion/statements/unconfirmed/`` until the same file is imported again
with the period. An OFX ``LEDGERBAL`` is the closing balance only when its
``DTASOF`` is the last statement day; the opening balance is then derived from
the file's transactions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .connectors import bankcsv, ofx
from .connectors.bankcsv import StatementFileError
from .ledger.normalize import normalize_description
from .tieout import presentation_sign, statement_tieout
from .work import observe_import

JsonObject = dict[str, Any]

MAPPING_PREFIX = "statement:"
NEAR_DATE_DAYS = 3
STATEMENTS_DIR = Path("ingestion") / "statements"
WRITER = "books statement import"
_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_OVERRIDE_FIELDS = ("date_column", "description_column", "amount_column", "debit_column",
                    "credit_column", "balance_column", "reference_column", "status_column",
                    "direction_column", "date_format", "decimal", "sign", "header_row")
_FIELD_FOR_UNCERTAIN = {
    "date_format": ("date_format",),
    "decimal": ("decimal",),
    "sign": ("sign",),
    "columns": ("date_column",),
    "description_column": ("description_column",),
    "amount_columns": ("amount_column", "debit_column"),
    "debit_credit_signs": ("sign",),
}


class StatementImportError(ValueError):
    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_dict(self) -> JsonObject:
        return {"code": self.code, "message": str(self), **self.detail}


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> tuple[str, str]:
    """Return (format, text) for a statement file."""
    if not path.is_file():
        raise StatementImportError("file_not_found", f"File not found: {path}")
    text = bankcsv.read_text(path)
    return ("ofx" if ofx.looks_like_ofx(text) else "csv"), text


def _profiles(config: JsonObject) -> dict[str, JsonObject]:
    mappings = config.get("csv_account_mappings") or {}
    found = {}
    for key, value in mappings.items():
        if (isinstance(key, str) and key.startswith(MAPPING_PREFIX) and isinstance(value, dict)
                and isinstance(value.get("statement_profile"), dict)):
            found[key[len(MAPPING_PREFIX):]] = value
    return found


def _confirmed(profile: JsonObject) -> bool:
    return profile.get("confirmed") is True and not profile.get("proposed")


def _ofx_profile_for(profiles: dict[str, JsonObject], identity: str) -> str | None:
    for name, profile in sorted(profiles.items()):
        spec = profile["statement_profile"]
        if spec.get("format") == "ofx" and spec.get("account_identity") == identity and _confirmed(profile):
            return name
    return None


def _kind(ledger_account: str | None) -> str | None:
    try:
        return bankcsv.account_kind(ledger_account)
    except StatementFileError as exc:
        raise StatementImportError(exc.code, str(exc)) from exc


def _ofx_summary(stmt: ofx.OfxStatement, profiles: dict[str, JsonObject]) -> JsonObject:
    return {
        "account_identity": stmt.account_identity,
        "account_kind": stmt.kind,
        "account_type": stmt.account_type,
        "institution": stmt.institution,
        "account_last4": stmt.account_last4,
        "period_start": stmt.period_start,
        "period_end": stmt.period_end,
        "ledger_balance": _money(stmt.ledger_balance),
        "ledger_balance_date": stmt.ledger_balance_date,
        "transaction_count": len(stmt.transactions),
        "in_file_duplicates": stmt.in_file_duplicates,
        "profile": _ofx_profile_for(profiles, stmt.account_identity),
    }


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


# ---------------------------------------------------------------------------
# Inspect
# ---------------------------------------------------------------------------

def inspect_file(path: Path, *, entity_config: JsonObject | None = None,
                 profile: str | None = None, ledger_account: str | None = None) -> JsonObject:
    """Detect a statement file and say what the caller must decide."""
    path = Path(path)
    config = entity_config or {}
    profiles = _profiles(config)
    try:
        fmt, text = _load(path)
        result: JsonObject = {"file": path.name, "sha256": _file_sha256(path), "format": fmt}
        if fmt == "ofx":
            statements = ofx.parse_ofx(text)
            accounts = [_ofx_summary(s, profiles) for s in statements]
            result["accounts"] = accounts
            result["row_count"] = sum(a["transaction_count"] for a in accounts)
            result["uncertain"] = []
            if all(a["profile"] for a in accounts):
                result["status"] = "ready"
                result["next_step"] = "books statement import FILE --entity ENTITY"
            else:
                result["status"] = "ready_to_confirm"
                result["next_step"] = ("Confirm which ledger account each unmapped account belongs to, then run "
                                       "books statement map FILE --entity ENTITY --profile NAME "
                                       "--ledger-account ACCOUNT [--ofx-account LAST4]")
            return result

        saved = profiles.get(profile) if profile else None
        if profile and saved is None:
            raise StatementImportError("profile_not_found", f"No statement profile named '{profile}'.")
        if saved is not None:
            layout = saved["statement_profile"]
            kind = _kind(saved.get("ledger_account"))
            try:
                rows, _ = bankcsv.read_rows(text)
                parsed = bankcsv.parse_rows(rows, layout, kind=kind)
            except StatementFileError as exc:
                if exc.code != "layout_changed":
                    raise
                detected = bankcsv.detect_layout(text, kind=kind)
                result.update(status="needs_mapping", reason=exc.as_dict(), **_detection_view(detected))
                return result
            result.update(
                status="ready", profile=profile, layout=layout, uncertain=[],
                preview=[bankcsv._preview(r) for r in parsed.rows[:5]],
                statement=_serialize_meta(parsed.metadata), transaction_count=len(parsed.rows),
                row_count=len(parsed.rows),
                pending_rows=parsed.pending_rows, warnings=parsed.warnings,
                next_step=f"books statement import FILE --entity ENTITY --profile {profile}")
            return result

        detected = bankcsv.detect_layout(text, kind=_kind(ledger_account))
        result.update(_detection_view(detected))
        if detected["uncertain"]:
            result["status"] = "needs_mapping"
            result["next_step"] = ("Ask the person to decide each uncertain field, then pass it to "
                                   "books statement map with the matching option.")
            result["options_for_uncertain"] = {
                field: ["--" + name.replace("_", "-") for name in _FIELD_FOR_UNCERTAIN.get(field, (field,))]
                for field in detected["uncertain"]}
        else:
            result["status"] = "ready_to_confirm"
            result["next_step"] = ("Show the proposal and preview to the person, then run "
                                   "books statement map FILE --entity ENTITY --profile NAME "
                                   "--ledger-account ACCOUNT")
        return result
    except (StatementFileError, StatementImportError) as exc:
        return {"file": path.name, "status": "error", "error": exc.as_dict()}


def _detection_view(detected: JsonObject) -> JsonObject:
    layout = dict(detected["layout"])
    header = layout.get("header") or []
    layout["column_names"] = {
        role: (header[index] if index < len(header) else f"column {index + 1}")
        for role, index in layout.get("columns", {}).items()}
    return {"layout": layout, "uncertain": detected["uncertain"], "evidence": detected["evidence"],
            "warnings": detected["warnings"], "preview": detected["preview"],
            "row_count": detected["row_count"]}


def _serialize_meta(meta: JsonObject) -> JsonObject:
    return {k: (_money(v) if isinstance(v, Decimal) else v) for k, v in meta.items()}


# ---------------------------------------------------------------------------
# Map (save a confirmed profile)
# ---------------------------------------------------------------------------

def _column_index(value: str, header: list[str]) -> int:
    value = value.strip()
    if value.isdigit():
        index = int(value) - 1
        if index < 0:
            raise StatementImportError("invalid_column", "Column numbers start at 1.")
        return index
    for index, name in enumerate(header):
        if name.strip().lower() == value.lower():
            return index
    raise StatementImportError("invalid_column", f"The file has no column named '{value}'.",
                               columns=header)


def _apply_overrides(text: str, detected: JsonObject, overrides: JsonObject) -> JsonObject:
    layout = dict(detected["layout"])
    columns = dict(layout.get("columns") or {})
    rows, _ = bankcsv.read_rows(text)
    if overrides.get("header_row") is not None:
        index = int(overrides["header_row"])
        if index == 0:
            layout["header"], layout["header_row"] = [], None
        else:
            if index > len(rows):
                raise StatementImportError("invalid_header_row", "The header row is past the end of the file.")
            layout["header"], layout["header_row"] = rows[index - 1], index - 1
    header = layout.get("header") or []
    for role in ("date", "description", "amount", "debit", "credit", "balance", "reference",
                 "status", "direction"):
        value = overrides.get(f"{role}_column")
        if value is None:
            continue
        if str(value).lower() == "none":
            columns.pop(role, None)
        else:
            columns[role] = _column_index(str(value), header)
    if overrides.get("debit_column") or overrides.get("credit_column"):
        columns.pop("amount", None)
        overrides.setdefault("sign", "debit_credit")
    layout["columns"] = columns
    for key in ("date_format", "decimal", "sign"):
        if overrides.get(key):
            layout[key] = overrides[key]
    if layout.get("date_format") and layout["date_format"] not in bankcsv.DATE_FORMATS:
        raise StatementImportError("invalid_date_format",
                                   "Unsupported date format.", supported=list(bankcsv.DATE_FORMATS))
    if layout.get("sign") not in bankcsv.SIGN_MODES:
        raise StatementImportError("invalid_sign", "Unsupported sign convention.",
                                   supported=list(bankcsv.SIGN_MODES))
    if layout["sign"] == "debit_credit" and not ("debit" in columns and "credit" in columns):
        raise StatementImportError("invalid_columns", "The debit_credit sign needs a debit and a credit column.")
    if layout["sign"] != "debit_credit" and "amount" not in columns:
        raise StatementImportError("invalid_columns", "This sign convention needs an amount column.")
    if layout["sign"] == "direction_column" and "direction" not in columns:
        raise StatementImportError("invalid_columns", "The direction_column sign needs a direction column.")
    for role in ("date", "description"):
        if role not in columns:
            raise StatementImportError("invalid_columns", f"The layout needs a {role} column.")
    return layout


def _unresolved(uncertain: list[str], overrides: JsonObject) -> list[str]:
    missing = []
    for field in uncertain:
        options = _FIELD_FOR_UNCERTAIN.get(field, (field,))
        if not any(overrides.get(option) for option in options):
            missing.append(field)
    return missing


def _check_ledger_account(entity: Any, ledger_account: str) -> None:
    _kind(ledger_account)
    from .ledger.store import LedgerStore, default_store_path
    store_path = default_store_path(entity.path)
    if not store_path.exists():
        return
    names = LedgerStore(store_path).load_account_names()
    if names and ledger_account not in names:
        raise StatementImportError(
            "unknown_ledger_account",
            f"'{ledger_account}' is not in the account catalog. Add it with "
            "'books entity account-add' or choose an existing account.")


def save_profile(entity_path: Path, path: Path, *, name: str, ledger_account: str,
                 account_name: str = "", overrides: JsonObject | None = None,
                 ofx_account: str | None = None, boundary_date: str | None = None,
                 side: str = "before") -> JsonObject:
    """Save a confirmed statement profile for one account."""
    from .entity import load_entity
    from .ledger.importer import _entity_write_lock

    overrides = {k: v for k, v in (overrides or {}).items() if v not in (None, "")}
    if not _PROFILE_NAME.match(name or ""):
        raise StatementImportError("invalid_profile_name",
                                   "Profile names use lowercase letters, digits and hyphens, e.g. chase-checking.")
    if boundary_date:
        try:
            date.fromisoformat(boundary_date)
        except ValueError as exc:
            raise StatementImportError("invalid_boundary", "The boundary must be YYYY-MM-DD.") from exc
    with _entity_write_lock(Path(entity_path)):
        entity = load_entity(entity_path)
        _check_ledger_account(entity, ledger_account)
        existing = _profiles(entity.entity_config).get(name)
        if existing and existing.get("ledger_account") != ledger_account:
            raise StatementImportError(
                "profile_account_changed",
                f"Profile '{name}' already belongs to {existing.get('ledger_account')}. "
                "Use a new profile name for another account.")
        fmt, text = _load(Path(path))
        try:
            if fmt == "ofx":
                spec, label = _ofx_spec(text, ofx_account, overrides, name, entity.entity_config)
            else:
                detected = bankcsv.detect_layout(text, kind=_kind(ledger_account))
                missing = _unresolved(detected["uncertain"], overrides)
                if missing:
                    raise StatementImportError(
                        "needs_mapping",
                        "Detection could not decide these fields; pass each one explicitly: "
                        + ", ".join(missing) + ".",
                        uncertain=missing, evidence=detected["evidence"])
                spec = _apply_overrides(text, detected, overrides)
                rows, _ = bankcsv.read_rows(text)
                bankcsv.parse_rows(rows, spec, kind=_kind(ledger_account))  # Prove it reads.
                spec = {k: spec[k] for k in ("format", "delimiter", "header", "columns",
                                             "date_format", "decimal", "sign", "currency")}
                label = ""
        except StatementFileError as exc:
            raise StatementImportError(exc.code, str(exc), **({"rows": exc.rows[:20]} if exc.rows else {})) from exc
        spec["version"] = 1
        record = {
            "account_name": account_name or label or name.replace("-", " ").title(),
            "ledger_account": ledger_account,
            "boundary_date": boundary_date,
            "side": side,
            "confirmed": True,
            "source_format": spec["format"],
            "statement_profile": spec,
        }
        config = json.loads((entity.path / "entity.json").read_text(encoding="utf-8"))
        config.setdefault("csv_account_mappings", {})[MAPPING_PREFIX + name] = record
        _write_json_atomic(entity.path / "entity.json", config)
    return {"profile": name, "mapping_key": MAPPING_PREFIX + name, **record}


def _ofx_spec(text: str, ofx_account: str | None, overrides: JsonObject, name: str,
              config: JsonObject) -> tuple[JsonObject, str]:
    statements = ofx.parse_ofx(text)
    if ofx_account:
        statements = [s for s in statements if s.account_last4 == ofx_account[-4:]]
    if len(statements) != 1:
        raise StatementImportError(
            "choose_ofx_account",
            "The file has more than one account or none that match; pass --ofx-account with the last "
            "four characters of the account.",
            accounts=[_ofx_summary(s, _profiles(config)) for s in ofx.parse_ofx(text)])
    stmt = statements[0]
    owner = _ofx_profile_for(_profiles(config), stmt.account_identity)
    if owner and owner != name:
        raise StatementImportError("account_already_mapped",
                                   f"This account already has the profile '{owner}'.")
    sign = overrides.get("sign") or "standard"
    if sign not in {"standard", "inverted"}:
        raise StatementImportError("invalid_sign", "OFX profiles accept only standard or inverted signs.")
    spec = {"format": "ofx", "account_identity": stmt.account_identity, "account_kind": stmt.kind,
            "account_type": stmt.account_type, "institution": stmt.institution,
            "account_last4": stmt.account_last4, "sign": sign, "currency": "USD"}
    label = f"{'Card' if stmt.kind == 'creditcard' else 'Account'} ending {stmt.account_last4}"
    return spec, label


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Normalize
# ---------------------------------------------------------------------------

def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


def _txn(*, txn_id: str, day: str, description: str, original: str, amount: Decimal,
         reference: str, profile_name: str, profile: JsonObject, last4: str | None,
         statement_id: str, bank_id: str, extra: JsonObject | None = None) -> JsonObject:
    ledger_account = profile["ledger_account"]
    account_type = "checking" if ledger_account.startswith("Assets:") else "credit_card"
    txn = {
        "id": txn_id,
        "date": day,
        "description": description,
        "originalDescription": original or description,
        "amount": format(amount, "f"),
        "creditAmount": format(amount, "f") if amount > 0 else None,
        "debitAmount": format(-amount, "f") if amount < 0 else None,
        "currency": "USD",
        "category": None,
        "type": account_type,
        "accountType": account_type,
        "reference": reference or None,
        "pending": False,
        "pendingTransactionId": None,
        "accountId": ledger_account,
        "accountName": profile.get("account_name") or profile_name,
        "accountNumberLast4": last4,
        "bankId": bank_id,
        "bank": None,
        "csvMappingKey": MAPPING_PREFIX + profile_name,
        "statementId": statement_id,
    }
    txn.update(extra or {})
    return txn


def _statement_id(mapping_key: str, file_sha: str, identity: str = "") -> str:
    return "st-" + _hash(mapping_key, file_sha, identity)[:16]


def _csv_statement(path: Path, text: str, file_sha: str, name: str, profile: JsonObject) -> JsonObject:
    layout = profile["statement_profile"]
    kind = _kind(profile["ledger_account"])
    rows, _ = bankcsv.read_rows(text)
    parsed = bankcsv.parse_rows(rows, layout, kind=kind)
    statement_id = _statement_id(MAPPING_PREFIX + name, file_sha)
    occurrences: Counter[tuple] = Counter()
    txns = []
    identical_groups = 0
    for row in parsed.rows:
        norm = normalize_description(row.description)
        key = (row.date, format(row.amount, "f"), norm, row.reference)
        occurrences[key] += 1
        if occurrences[key] == 2:
            identical_groups += 1
        txn_id = "stmt:" + _hash(profile["ledger_account"], row.date, format(row.amount, "f"),
                                 norm, row.reference, str(occurrences[key]))
        original = " ".join(part for part in (row.description, row.memo) if part)
        txns.append(_txn(txn_id=txn_id, day=row.date, description=row.description, original=original,
                         amount=row.amount, reference=row.reference, profile_name=name,
                         profile=profile, last4=None, statement_id=statement_id,
                         bank_id="statement-csv"))
    warnings = list(parsed.warnings)
    if identical_groups:
        warnings.append(f"{identical_groups} group(s) of identical rows were kept as separate "
                        "transactions. Check them against the statement.")
    meta = _serialize_meta(parsed.metadata)
    return {
        "statement_id": statement_id, "profile": name, "transactions": txns,
        "pending_rows": parsed.pending_rows, "non_transaction_rows": len(parsed.non_transaction_rows),
        "in_file_duplicates": 0, "identical_row_groups": identical_groups, "warnings": warnings,
        "account": {"identity": None, "institution": None, "account_type": None, "account_last4": None},
        "metadata": meta,
    }


def _ofx_statement(stmt: ofx.OfxStatement, file_sha: str, name: str, profile: JsonObject) -> JsonObject:
    spec = profile["statement_profile"]
    factor = -1 if spec.get("sign") == "inverted" else 1
    statement_id = _statement_id(MAPPING_PREFIX + name, file_sha, stmt.account_identity)
    txns = []
    for t in stmt.transactions:
        description = t.name or t.memo or t.trntype
        original = " ".join(part for part in (t.name, t.memo) if part)
        txns.append(_txn(
            txn_id="ofx:" + _hash(stmt.account_identity, t.fitid), day=t.date,
            description=description, original=original, amount=factor * t.amount,
            reference=t.reference or t.fitid, profile_name=name, profile=profile,
            last4=stmt.account_last4, statement_id=statement_id, bank_id="statement-ofx",
            extra={"fitid": t.fitid, "ofxType": t.trntype}))
    dates = sorted(t["date"] for t in txns)
    balance = factor * stmt.ledger_balance if stmt.ledger_balance is not None else None
    period_start = stmt.period_start or (dates[0] if dates else None)
    period_end = stmt.period_end or (dates[-1] if dates else None)
    warnings: list[str] = []
    opening = closing = None
    if balance is not None and stmt.ledger_balance_date == period_end:
        # LEDGERBAL is the balance at DTASOF. It is the closing balance only
        # when DTASOF is the last statement day; the opening balance is then
        # that balance less every transaction in the statement.
        closing = balance
        if all(period_start <= day <= period_end for day in dates):
            opening = balance - sum((Decimal(t["amount"]) for t in txns), Decimal("0.00"))
        else:
            warnings.append("Some transactions are outside the statement dates; the opening balance "
                            "was not derived.")
    elif balance is not None:
        warnings.append(f"The ledger balance is dated {stmt.ledger_balance_date or 'with no date'}, "
                        f"not on the last statement day {period_end}; it was not recorded as the "
                        "closing balance. Pass --closing-balance from the printed statement.")
    meta = {
        "period_start": period_start,
        "period_end": period_end,
        "period_source": "statement" if stmt.period_start and stmt.period_end else "transactions",
        "opening_balance": _money(opening),
        "opening_balance_date": period_start if opening is not None else None,
        "closing_balance": _money(closing),
        "closing_balance_date": period_end if closing is not None else None,
        "balance_source": "ledger_balance" if closing is not None else None,
        "ledger_balance": _money(balance),
        "ledger_balance_date": stmt.ledger_balance_date,
        "first_transaction_date": dates[0] if dates else None,
        "last_transaction_date": dates[-1] if dates else None,
    }
    return {
        "statement_id": statement_id, "profile": name, "transactions": txns, "pending_rows": 0,
        "non_transaction_rows": 0, "in_file_duplicates": stmt.in_file_duplicates,
        "identical_row_groups": 0, "warnings": warnings,
        "account": {"identity": stmt.account_identity, "institution": stmt.institution,
                    "account_type": stmt.account_type, "account_last4": stmt.account_last4},
        "metadata": meta,
    }


# ---------------------------------------------------------------------------
# Duplicate plan
# ---------------------------------------------------------------------------

def _txn_amount(txn: JsonObject) -> Decimal:
    if txn.get("amount") is not None:
        return Decimal(str(txn["amount"])).quantize(bankcsv.MONEY)
    credit = Decimal(str(txn.get("creditAmount") or "0"))
    debit = Decimal(str(txn.get("debitAmount") or "0"))
    return (credit - debit).quantize(bankcsv.MONEY)


def _existing_pool(entity: Any, ledger_account: str) -> list[JsonObject]:
    """Entries and review items already recorded on *ledger_account*."""
    from .ledger.importer import _ledger_account_for_txn, _load_pending_categorization
    from .ledger.store import LedgerStore, default_store_path

    pool: list[JsonObject] = []
    store_path = default_store_path(entity.path)
    if store_path.exists():
        store = LedgerStore(store_path)
        families: dict[str, str] = {}
        with store.connection() as conn:
            for source_id, payload in conn.execute("SELECT id, payload_json FROM source_transactions"):
                try:
                    families[source_id] = str(json.loads(payload).get("csvMappingKey") or "other")
                except (ValueError, AttributeError):
                    families[source_id] = "other"
        for entry in store.load_entries():
            if not entry.source_id or "reverses" in {k for k, _ in entry.meta}:
                continue
            for posting in entry.postings:
                if posting.account == ledger_account:
                    pool.append({"source_id": entry.source_id, "date": entry.date,
                                 "amount": posting.amount.quantize(bankcsv.MONEY),
                                 "desc": normalize_description(entry.narration),
                                 "family": families.get(entry.source_id, "other")})
                    break
    mappings = entity.entity_config.get("bank_account_mappings")
    csv_mappings = entity.entity_config.get("csv_account_mappings")
    for txn in _load_pending_categorization(entity):
        try:
            if _ledger_account_for_txn(txn, mappings, csv_mappings) != ledger_account:
                continue
            pool.append({"source_id": str(txn["id"]), "date": date.fromisoformat(str(txn["date"])[:10]),
                         "amount": _txn_amount(txn),
                         "desc": normalize_description(str(txn.get("description") or "")),
                         "family": str(txn.get("csvMappingKey") or "other")})
        except (ValueError, KeyError, ArithmeticError):
            continue
    return pool


def _load_list(path: Path) -> list[JsonObject]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise StatementImportError("damaged_state", f"Cannot read {path.name}; repair it before importing.") from exc
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def plan_duplicates(entity: Any, txns: list[JsonObject], ledger_account: str,
                    mapping_key: str) -> JsonObject:
    """Split *txns* into rows to import, rows to skip and rows to hold."""
    from .ledger.importer import _load_pending_categorization
    from .ledger.staging import StagingStore
    from .ledger.store import LedgerStore, default_store_path

    staging = StagingStore(entity.staging_dir)
    store_path = default_store_path(entity.path)
    store = LedgerStore(store_path) if store_path.exists() else None
    queued = {str(t.get("id")) for t in _load_pending_categorization(entity)}
    candidates = _load_list(entity.staging_dir / "duplicate-candidates.json")
    held_ids = {str(c.get("source_id")) for c in candidates if c.get("status") == "duplicate-candidate"}
    aliases: dict[str, str] = {}
    for alias in _load_list(entity.staging_dir / "source-id-aliases.json"):
        aliases[str(alias.get("source_id") or "")] = str(alias.get("status") or "")

    batch_ids = {t["id"] for t in txns}
    pool = [p for p in _existing_pool(entity, ledger_account) if p["source_id"] not in batch_ids]
    used: set[int] = set()
    plan: JsonObject = {"import": [], "held": [], "skipped": Counter()}
    for txn in sorted(txns, key=lambda t: (t["date"], t["id"])):
        sid = txn["id"]
        if staging.is_seen(sid) or sid in queued or (store is not None and store.source_exists(sid)):
            plan["skipped"]["already_imported"] += 1
            continue
        if sid in held_ids:
            plan["skipped"]["already_held_for_review"] += 1
            continue
        status = aliases.get(sid, "")
        if status == "confirmed-duplicate":
            plan["skipped"]["confirmed_duplicate"] += 1
            continue
        if status == "confirmed-distinct":
            plan["import"].append(txn)
            continue
        day = date.fromisoformat(txn["date"])
        amount = _txn_amount(txn)
        desc = normalize_description(txn["description"])
        match, rule = None, None
        for index, item in enumerate(pool):
            if index not in used and item["date"] == day and item["amount"] == amount and item["desc"] == desc:
                match, rule = index, "same_date_amount_description"
                break
        if match is None:
            near = [(abs((item["date"] - day).days), index) for index, item in enumerate(pool)
                    if index not in used and item["amount"] == amount and item["family"] != mapping_key
                    and abs((item["date"] - day).days) <= NEAR_DATE_DAYS]
            if near:
                match, rule = min(near)[1], f"same_amount_within_{NEAR_DATE_DAYS}_days_other_source"
        if match is None:
            plan["import"].append(txn)
            continue
        used.add(match)
        existing = pool[match]
        plan["held"].append({
            "source_id": sid,
            "existing_source_id": existing["source_id"],
            "account": ledger_account,
            "date": txn["date"],
            "amount": format(amount, "f"),
            "description": txn["description"],
            "existing_date": existing["date"].isoformat(),
            "match_rule": rule,
            "transaction": txn,
            "status": "duplicate-candidate",
        })
    plan["skipped"] = dict(plan["skipped"])
    return plan


def _record_held(entity: Any, held: list[JsonObject], ts: str) -> None:
    if not held:
        return
    path = entity.staging_dir / "duplicate-candidates.json"
    candidates = _load_list(path)
    known = {str(c.get("source_id")) for c in candidates}
    aliases_path = entity.staging_dir / "source-id-aliases.json"
    aliases = _load_list(aliases_path)
    for item in held:
        if item["source_id"] in known:
            continue
        candidates.append({**item, "created_at": ts})
        aliases.append({"source_id": item["source_id"], "canonical_source_id": item["existing_source_id"],
                        "status": "candidate"})
    _write_json_atomic(path, candidates)
    _write_json_atomic(aliases_path, aliases)


# ---------------------------------------------------------------------------
# Statement records (the coverage contract in coverage.py is the only format)
# ---------------------------------------------------------------------------

UNCONFIRMED_DIR = "unconfirmed"


def _unconfirmed_path(entity_path: Path, statement_id: str) -> Path:
    return Path(entity_path) / STATEMENTS_DIR / UNCONFIRMED_DIR / f"{statement_id}.json"


def load_statement_records(entity_path: Path) -> list[JsonObject]:
    """Return the statement records this importer wrote (oldest first).

    Records with a confirmed period are coverage contract records in
    ``ingestion/statements/<statement_id>.json``. Records whose period nobody
    confirmed are in ``ingestion/statements/unconfirmed/`` with
    ``period_confirmed: false``; the coverage check does not read them.
    An extracted statement that failed a check is listed with
    ``status: needs_review``; it is in ``ingestion/statements/needs-review/``
    and is not coverage either.
    """
    from . import coverage

    records = [doc for doc in coverage.load_statement_documents(entity_path)
               if doc.get("writer") == WRITER]
    directory = Path(entity_path) / STATEMENTS_DIR / UNCONFIRMED_DIR
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise StatementImportError("damaged_state", f"Cannot read {path}; repair it first.") from exc
            if isinstance(item, dict):
                records.append(item)
    from . import statement_extract

    records.extend(r for r in statement_extract.load_review_records(entity_path)
                   if r.get("status") == "needs_review")
    records.sort(key=lambda r: (str(r.get("account")), str(r.get("period_start")), str(r.get("statement_id"))))
    return records


def _store_record(entity_path: Path, record: JsonObject) -> None:
    from . import coverage

    pending = _unconfirmed_path(entity_path, record["statement_id"])
    if record["period_confirmed"]:
        contract = {k: record[k] for k in coverage.CONTRACT_FIELDS if k in record}
        coverage.record_statement(entity_path, contract, replace=True, extra=record, check_catalog=False)
        if pending.exists():
            pending.unlink()
    else:
        _write_json_atomic(pending, record)


_CONFIRMED_FIELDS = ("period_start", "period_end", "opening_balance", "closing_balance",
                     "opening_balance_date", "closing_balance_date", "balance_source", "period_source")


def _keep_confirmed_period(entity_path: Path, record: JsonObject) -> None:
    """A repeat import without dates keeps the period a person already confirmed."""
    from . import coverage

    existing = coverage.statements_dir(entity_path) / f"{record['statement_id']}.json"
    try:
        previous = json.loads(existing.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(previous, dict) and previous.get("period_confirmed"):
        record.update({key: previous.get(key) for key in _CONFIRMED_FIELDS}, period_confirmed=True)


def _archive_relative(path: Path, file_sha: str) -> Path:
    suffix = path.suffix.lower() if re.fullmatch(r"\.[a-z0-9]{1,5}", path.suffix.lower()) else ".dat"
    return STATEMENTS_DIR / "files" / f"{file_sha}{suffix}"


def _archive_source(entity_path: Path, path: Path, file_sha: str) -> str:
    relative = _archive_relative(path, file_sha)
    destination = Path(entity_path) / relative
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copyfile(path, tmp)
        os.replace(tmp, destination)
    return relative.as_posix()


def _presented(ledger_sign: str | None, ledger_account: str) -> str | None:
    """Ledger-sign balance (card debt negative) to statement presentation (debt positive)."""
    if ledger_sign is None:
        return None
    amount = Decimal(str(ledger_sign)).quantize(bankcsv.MONEY) * presentation_sign(ledger_account)
    return format(amount + Decimal("0.00"), "f")


def _parse_override_balance(value: str | None, label: str) -> str | None:
    if value in (None, ""):
        return None
    from .coverage import _parse_amount

    try:
        return format(_parse_amount(str(value)), "f")
    except ValueError as exc:
        raise StatementImportError("invalid_balance", f"{label}: {exc}") from exc


def _parse_override_date(value: str | None, label: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise StatementImportError("invalid_period", f"{label} must be YYYY-MM-DD.") from exc


def _contract_record(statement: JsonObject, *, fmt: str, ledger_account: str, label: str,
                     confirmed_period: tuple[str, str] | None,
                     balances: tuple[str | None, str | None]) -> tuple[JsonObject, list[str]]:
    """Build the coverage contract record (plus importer details) for one statement.

    Balances in the record use the statement presentation: money held for a
    bank account, money owed for a card or loan.
    """
    meta = dict(statement["metadata"])
    warnings: list[str] = []
    dates = sorted(t["date"] for t in statement["transactions"])
    period_start, period_end = meta.get("period_start"), meta.get("period_end")
    opening = _presented(meta.get("opening_balance"), ledger_account)
    closing = _presented(meta.get("closing_balance"), ledger_account)
    period_source = meta.get("period_source")
    confirmed = (period_source == "statement" or (
        period_source == "file_summary" and bool(meta.get("opening_balance_date"))
        and bool(meta.get("closing_balance_date"))))
    if confirmed_period is not None:
        start, end = confirmed_period
        if start > end:
            raise StatementImportError("invalid_period", "The period start is after the period end.")
        outside = [day for day in dates if not start <= day <= end]
        if outside:
            raise StatementImportError(
                "period_mismatch",
                f"{len(outside)} transaction(s) are outside {start} to {end}, the first on {outside[0]}. "
                "Check the period printed on the statement.", outside=outside[:10])
        if (start, end) != (period_start, period_end) and meta.get("balance_source") in {
                "file_summary", "ledger_balance"}:
            if opening is not None or closing is not None:
                warnings.append("The confirmed period differs from the dates of the balances in the file; "
                                "those balances were not recorded.")
            opening = closing = None
            meta.update(opening_balance_date=None, closing_balance_date=None, balance_source=None)
        period_start, period_end, period_source, confirmed = start, end, "person", True
    if balances[0] is not None:
        opening, meta["opening_balance_date"] = balances[0], period_start
    if balances[1] is not None:
        closing, meta["closing_balance_date"] = balances[1], period_end
    if any(b is not None for b in balances):
        meta["balance_source"] = "person" if all(b is not None for b in balances) else "person_and_file"
    total = sum((Decimal(t["amount"]) for t in statement["transactions"]), Decimal("0.00"))
    tie = statement_tieout(
        {"opening_balance": opening, "closing_balance": closing, "transactions_total": format(total, "f"),
         "transaction_count": len(statement["transactions"])}, presentation_sign(ledger_account))
    if tie["status"] == "differs":
        warnings.append(f"The balances change by {Decimal(closing) - Decimal(opening)} but the transactions "
                        f"in the file total {tie['rows_total']}; check for missing or extra rows.")
    if not period_start or not period_end:
        confirmed = False
    record: JsonObject = {
        "schema_version": 1,
        "statement_id": statement["statement_id"],
        "account": ledger_account,
        "kind": "statement",
        "period_start": period_start,
        "period_end": period_end,
        "opening_balance": opening,
        "closing_balance": closing,
        "source": label,
        "writer": WRITER,
        "profile": statement["profile"],
        "mapping_key": MAPPING_PREFIX + statement["profile"],
        "format": fmt,
        "institution_account": statement["account"],
        "period_source": period_source,
        "period_confirmed": confirmed,
        "opening_balance_date": meta.get("opening_balance_date") if opening is not None else None,
        "closing_balance_date": meta.get("closing_balance_date") if closing is not None else None,
        "balance_source": meta.get("balance_source") if opening is not None or closing is not None else None,
        "first_transaction_date": dates[0] if dates else None,
        "last_transaction_date": dates[-1] if dates else None,
        "transactions_total": format(total, "f"),
        "transaction_count": len(statement["transactions"]),
        "currency": "USD",
        "tieout": tie,
    }
    if fmt == "ofx":
        record["ledger_balance"] = _presented(meta.get("ledger_balance"), ledger_account)
        record["ledger_balance_date"] = meta.get("ledger_balance_date")
    return record, warnings


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _apply_boundary(txns: list[JsonObject], profile: JsonObject) -> tuple[list[JsonObject], int]:
    boundary = profile.get("boundary_date")
    if not boundary:
        return txns, 0
    side = profile.get("side", "before")
    kept = [t for t in txns if (t["date"] <= boundary if side == "before" else t["date"] > boundary)]
    return kept, len(txns) - len(kept)


def _tieout_blocked(path: Path, fmt: str, selected: list[JsonObject], profiles: JsonObject,
                    confirmed_period: tuple[str, str] | None,
                    balances: tuple[str | None, str | None]) -> JsonObject | None:
    """Return a ``needs_review`` result when any statement fails its tie-out, else None."""
    entries = []
    for statement in selected:
        prof = profiles[statement["profile"]]
        record, warnings = _contract_record(
            statement, fmt=fmt, ledger_account=prof["ledger_account"],
            label=prof.get("account_name") or statement["profile"], confirmed_period=confirmed_period,
            balances=balances)
        entries.append({"profile": statement["profile"], "statement_id": record["statement_id"],
                        "tieout": record["tieout"], "warnings": warnings})
    differing = [e for e in entries if e["tieout"]["status"] == "differs"]
    if not differing:
        return None
    names = ", ".join(e["profile"] for e in differing)
    return {"status": "needs_review", "file": path.name, "format": fmt, "session_id": None,
            "error": {"code": "tieout_differs",
                      "message": f"Opening balance plus the rows does not equal the closing balance for {names}. "
                                 "Nothing was imported. Check for missing or extra rows, or the balances."},
            "statements": entries}


def _run_import_check(entity: Any, record: JsonObject, prof: JsonObject, statement: JsonObject,
                      ts: str, entry: JsonObject) -> JsonObject | None:
    """Check the finished import against the books and store the result next to its record.

    The check is read-only apart from that stored file. A failure of the check
    never undoes the import; it is reported as a warning.
    """
    from . import import_check

    try:
        result = import_check.check_parsed(entity, record, prof, statement, ts=ts)
        import_check.store_result(entity.path, result)
        return result
    except (OSError, ValueError, ArithmeticError, sqlite3.Error) as exc:
        entry["warnings"].append(f"The import check could not run: {exc}")
        return None


def import_file(entity_path: Path, path: Path, *, profile: str | None = None, dry_run: bool = False,
                intake_only: bool = False, session_id: str | None = None,
                ts: str | None = None, output: Path | None = None,
                period_start: str | None = None, period_end: str | None = None,
                opening_balance: str | None = None, closing_balance: str | None = None,
                require_tieout: bool = False) -> JsonObject:
    """Import one statement file. Returns a JSON-ready result with ``status``.

    ``period_start``/``period_end`` are the statement dates a person confirmed
    from the printed statement; both are needed. ``opening_balance`` and
    ``closing_balance`` use the statement presentation (money owed is positive
    for a card). They apply only when the file holds one account.

    Each statement entry carries a ``tieout`` (opening balance plus the file's
    rows against the closing balance). A mismatch is a warning by default.
    With ``require_tieout`` a mismatch returns ``needs_review`` before any
    write, and a dry run reports the same.
    """
    from .entity import load_entity
    from .ledger.importer import _entity_write_lock, import_transactions
    from . import queue as queue_module

    path = Path(path)
    entity = load_entity(entity_path)
    profiles = _profiles(entity.entity_config)
    fmt, text = _load(path)
    file_sha = _file_sha256(path)
    confirmed_period = None
    if period_start or period_end:
        if not (period_start and period_end):
            raise StatementImportError("invalid_period", "Pass both --period-start and --period-end.")
        confirmed_period = (_parse_override_date(period_start, "--period-start"),
                            _parse_override_date(period_end, "--period-end"))
    balances = (_parse_override_balance(opening_balance, "--opening-balance"),
                _parse_override_balance(closing_balance, "--closing-balance"))

    try:
        if fmt == "ofx":
            selected = []
            unmapped = []
            for stmt in ofx.parse_ofx(text):
                name = _ofx_profile_for(profiles, stmt.account_identity)
                if profile and name != profile:
                    continue
                if name is None:
                    unmapped.append(_ofx_summary(stmt, profiles))
                else:
                    selected.append(_ofx_statement(stmt, file_sha, name, profiles[name]))
            if profile and not selected:
                raise StatementImportError("profile_not_in_file",
                                           f"The file has no account for profile '{profile}'.")
            if unmapped:
                return {"status": "needs_mapping", "file": path.name, "format": "ofx",
                        "unmapped_accounts": unmapped,
                        "next_step": "Map each account with books statement map, then import again."}
        else:
            if not profile:
                inspection = inspect_file(path, entity_config=entity.entity_config)
                inspection["status"] = "error" if inspection["status"] == "error" else "needs_mapping"
                inspection["next_step"] = ("CSV imports need --profile. Save one with "
                                           "books statement map, then import again.")
                return inspection
            if profile not in profiles:
                raise StatementImportError("profile_not_found", f"No statement profile named '{profile}'.")
            if not _confirmed(profiles[profile]):
                raise StatementImportError("profile_not_confirmed", f"Profile '{profile}' is not confirmed.")
            if profiles[profile]["statement_profile"].get("format") != "csv":
                raise StatementImportError("profile_format_mismatch",
                                           f"Profile '{profile}' is for OFX files, and this file is a CSV.")
            selected = [_csv_statement(path, text, file_sha, profile, profiles[profile])]
    except StatementFileError as exc:
        status = "needs_mapping" if exc.code == "layout_changed" else "error"
        return {"status": status, "file": path.name, "error": exc.as_dict()}
    if (confirmed_period or any(b is not None for b in balances)) and len(selected) != 1:
        raise StatementImportError(
            "choose_one_account",
            "The period and balance options apply to one account; pass --profile for that account.")

    if require_tieout:
        blocked = _tieout_blocked(path, fmt, selected, profiles, confirmed_period, balances)
        if blocked is not None:
            return blocked

    ts = ts or _now()
    session_id = session_id or f"statement-{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    results = []
    all_txns: list[JsonObject] = []
    with _entity_write_lock(entity.path):
        entity = load_entity(entity_path)
        for statement in selected:
            name = statement["profile"]
            prof = profiles[name]
            mapping_key = MAPPING_PREFIX + name
            txns, outside = _apply_boundary(statement["transactions"], prof)
            plan = plan_duplicates(entity, txns, prof["ledger_account"], mapping_key)
            counts: JsonObject = {
                "rows_in_file": len(statement["transactions"]) + statement["pending_rows"]
                + statement["in_file_duplicates"],
                "pending_rows_skipped": statement["pending_rows"],
                "in_file_duplicates_skipped": statement["in_file_duplicates"],
                "outside_boundary": outside,
                "held_for_review": len(plan["held"]),
                **{k: v for k, v in plan["skipped"].items()},
                "to_import": len(plan["import"]),
            }
            record, record_warnings = _contract_record(
                statement, fmt=fmt, ledger_account=prof["ledger_account"],
                label=prof.get("account_name") or name, confirmed_period=confirmed_period,
                balances=balances)
            record.update(file_name=path.name, evidence_ref=_archive_relative(path, file_sha).as_posix(),
                          evidence_sha256=file_sha)
            if not record["period_confirmed"]:
                _keep_confirmed_period(entity.path, record)
            entry: JsonObject = {"statement": record, "counts": counts, "tieout": record["tieout"],
                                 "warnings": statement["warnings"] + record_warnings,
                                 "held": [{k: v for k, v in h.items() if k != "transaction"} for h in plan["held"]],
                                 "coverage": "recorded" if record["period_confirmed"] else "needs_period"}
            if not record["period_confirmed"]:
                entry["next_step"] = (
                    "Ask the person for the statement period printed on the statement, then run "
                    f"books statement import FILE --entity ENTITY --profile {name} "
                    "--period-start YYYY-MM-DD --period-end YYYY-MM-DD. Until then this file adds "
                    "no statement coverage.")
            if not dry_run:
                # Rows skipped before the importer runs are still skipped duplicates for work events.
                observe_import(plan["skipped"].get("already_imported", 0)
                               + plan["skipped"].get("confirmed_duplicate", 0)
                               + statement["in_file_duplicates"])
                if plan["import"]:
                    outcome = import_transactions(
                        entity, plan["import"], session_id=session_id,
                        categorizer=queue_module.make_categorizer(entity), ts=ts,
                        intake_only=intake_only)
                    if outcome.errors:
                        raise StatementImportError("import_failed", "; ".join(outcome.errors[:5]))
                    counts.update(posted=outcome.new_entries, queued_for_review=outcome.pending_categorization,
                                  importer_duplicates=outcome.skipped_duplicate)
                else:
                    counts.update(posted=0, queued_for_review=0, importer_duplicates=0)
                _record_held(entity, plan["held"], ts)
                _archive_source(entity.path, path, file_sha)
                record["imported_at"] = ts
                record["import_session"] = session_id
                _store_record(entity.path, record)
                entry["import_check"] = _run_import_check(entity, record, prof, statement, ts, entry)
            all_txns.extend(plan["import"])
            results.append(entry)

    if output is not None:
        _write_json_atomic(Path(output), {"transactions": all_txns, "count": len(all_txns)})
    return {"status": "dry_run" if dry_run else "imported", "file": path.name, "format": fmt,
            "session_id": None if dry_run else session_id, "statements": results}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "statement", help="Import historical bank and card statements (CSV, OFX, QFX, QBO)")
    sub = parser.add_subparsers(dest="statement_command", required=True)

    inspect_p = sub.add_parser("inspect", help="Detect a statement file and propose a mapping (no changes)")
    inspect_p.add_argument("file", type=Path)
    inspect_p.add_argument("--entity", type=Path, default=None, help="Company books directory")
    inspect_p.add_argument("--profile", default=None, help="Check the file against a saved profile")
    inspect_p.add_argument("--ledger-account", default=None,
                           help="Intended Assets: or Liabilities: account; improves sign detection")

    map_p = sub.add_parser("map", help="Save a confirmed per-account mapping profile")
    map_p.add_argument("file", type=Path)
    map_p.add_argument("--entity", type=Path, required=True)
    map_p.add_argument("--profile", required=True, help="Profile name, e.g. chase-checking")
    map_p.add_argument("--ledger-account", required=True)
    map_p.add_argument("--account-name", default="")
    map_p.add_argument("--ofx-account", default=None, help="Last four characters of the OFX account")
    map_p.add_argument("--boundary", default=None, help="Optional YYYY-MM-DD boundary with another source")
    map_p.add_argument("--side", choices=["before", "after"], default="before")
    map_p.add_argument("--header-row", type=int, default=None,
                       help="1-based header line; 0 means the file has no header")
    for option in ("date", "description", "amount", "debit", "credit", "balance", "reference",
                   "status", "direction"):
        map_p.add_argument(f"--{option}-column", default=None,
                           help=f"{option.title()} column name or 1-based number ('none' removes it)")
    map_p.add_argument("--date-format", choices=list(bankcsv.DATE_FORMATS), default=None)
    map_p.add_argument("--decimal", choices=[".", ","], default=None)
    map_p.add_argument("--sign", choices=list(bankcsv.SIGN_MODES), default=None,
                       help="standard: positive is money in; inverted: positive is a charge or withdrawal")

    import_p = sub.add_parser("import", help="Import a statement file with its saved profile")
    import_p.add_argument("file", type=Path)
    import_p.add_argument("--entity", type=Path, required=True)
    import_p.add_argument("--profile", default=None, help="Required for CSV; optional filter for OFX")
    import_p.add_argument("--dry-run", action="store_true", help="Show the plan without changes")
    import_p.add_argument("--intake-only", action="store_true",
                          help="Stage transactions for review without learned auto-posting")
    import_p.add_argument("--session-id", default=None)
    import_p.add_argument("--output", type=Path, default=None,
                          help="Also write the normalized transactions to this JSON file")
    import_p.add_argument("--period-start", default=None,
                          help="First statement day (YYYY-MM-DD) as printed on the statement")
    import_p.add_argument("--period-end", default=None,
                          help="Last statement day (YYYY-MM-DD) as printed on the statement")
    import_p.add_argument("--opening-balance", default=None,
                          help="Opening balance as printed (money owed is positive for a card)")
    import_p.add_argument("--closing-balance", default=None,
                          help="Closing balance as printed (money owed is positive for a card)")
    import_p.add_argument("--require-tieout", action="store_true",
                          help="Exit 4 and import nothing when opening balance plus the rows "
                               "does not equal the closing balance")

    from . import statement_extract
    statement_extract.add_parsers(sub)

    list_p = sub.add_parser("list", help="List recorded statements and their periods and balances")
    list_p.add_argument("--entity", type=Path, required=True)
    list_p.add_argument("--account", default=None, help="Only this ledger account")


def run(args: argparse.Namespace) -> int:
    from .entity import load_entity

    command = args.statement_command
    try:
        if command == "inspect":
            config = load_entity(args.entity).entity_config if args.entity else None
            result = inspect_file(args.file, entity_config=config, profile=args.profile,
                                  ledger_account=args.ledger_account)
        elif command == "map":
            overrides = {key: getattr(args, key) for key in _OVERRIDE_FIELDS if hasattr(args, key)}
            result = {"status": "saved", **save_profile(
                args.entity, args.file, name=args.profile, ledger_account=args.ledger_account,
                account_name=args.account_name, overrides=overrides, ofx_account=args.ofx_account,
                boundary_date=args.boundary, side=args.side)}
        elif command == "import":
            result = import_file(args.entity, args.file, profile=args.profile, dry_run=args.dry_run,
                                 intake_only=args.intake_only, session_id=args.session_id,
                                 output=args.output, period_start=args.period_start,
                                 period_end=args.period_end, opening_balance=args.opening_balance,
                                 closing_balance=args.closing_balance,
                                 require_tieout=args.require_tieout)
        elif command in {"validate", "map-extracted", "import-extracted"}:
            from . import statement_extract
            result = statement_extract.run(args)
        elif command == "list":
            load_entity(args.entity)
            records = load_statement_records(args.entity)
            if args.account:
                records = [r for r in records if r.get("account") == args.account]
            result = {"status": "ok", "statements": records}
        else:
            raise StatementImportError("unknown_command", f"Unknown statement command: {command}")
    except StatementImportError as exc:
        result = {"status": "needs_mapping" if exc.code == "needs_mapping" else "error",
                  "error": exc.as_dict()}
    except StatementFileError as exc:
        result = {"status": "error", "error": exc.as_dict()}
    except FileNotFoundError as exc:
        result = {"status": "error", "error": {"code": "not_found", "message": str(exc)}}
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if result.get("status") == "error":
        return 1
    if result.get("status") == "needs_review":
        return 4  # The statement failed a check. Nothing was recorded.
    # Inspect succeeds when it reports what is uncertain; map and import stop.
    return 3 if result.get("status") == "needs_mapping" and command != "inspect" else 0
