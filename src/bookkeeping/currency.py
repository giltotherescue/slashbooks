"""Company currency: the one currency a company keeps its books in.

Each company keeps its books in one base currency. Slashbooks does not convert
between currencies, keep exchange rates or compute exchange gains. Two keys in
``entity.json`` hold the setting:

``currency``
    An ISO 4217 code such as ``"USD"`` or ``"EUR"``. Absent means ``"USD"``, so
    every company made before this setting existed is unchanged.
``currency_source``
    ``"default"`` (absent also means default), ``"inferred"`` (the first import
    that carried a currency set it) or ``"set"`` (a person ran
    ``books entity set-currency``).

This first version supports only currencies with two decimal places (cents).
A zero-decimal currency such as JPY or a three-decimal currency such as KWD is
known to the table below but refused with a clear message, because the money
math in the engine rounds to two decimals. Refusing is safer than silently
rounding a yen amount to hundredths or a dinar amount to hundredths.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_CURRENCY = "USD"
CURRENCY_KEY = "currency"
SOURCE_KEY = "currency_source"
SOURCES = ("default", "inferred", "set")
SUPPORTED_MINOR_UNITS = 2


@dataclass(frozen=True)
class CurrencyInfo:
    code: str
    minor_units: int
    symbol: str | None  # Printed before the amount; None prints the code, as in "CHF 1,234.50".


# A small built-in ISO 4217 table: code, minor-unit digits, display symbol.
_TABLE: tuple[CurrencyInfo, ...] = tuple(CurrencyInfo(*row) for row in (
    ("USD", 2, "$"), ("EUR", 2, "€"), ("GBP", 2, "£"), ("CAD", 2, "CA$"), ("AUD", 2, "A$"),
    ("NZD", 2, "NZ$"), ("HKD", 2, "HK$"), ("SGD", 2, "S$"), ("MXN", 2, "MX$"), ("BRL", 2, "R$"),
    ("TWD", 2, "NT$"), ("INR", 2, "₹"), ("ILS", 2, "₪"), ("PHP", 2, "₱"), ("NGN", 2, "₦"),
    ("TRY", 2, "₺"), ("UAH", 2, "₴"), ("CHF", 2, None), ("SEK", 2, None), ("NOK", 2, None),
    ("DKK", 2, None), ("PLN", 2, None), ("CZK", 2, None), ("HUF", 2, None), ("RON", 2, None),
    ("BGN", 2, None), ("ZAR", 2, None), ("CNY", 2, None), ("AED", 2, None), ("SAR", 2, None),
    ("QAR", 2, None), ("MYR", 2, None), ("THB", 2, None), ("IDR", 2, None), ("EGP", 2, None),
    ("KES", 2, None), ("MAD", 2, None), ("COP", 2, None), ("PEN", 2, None), ("ARS", 2, None),
    ("JPY", 0, "¥"), ("KRW", 0, "₩"), ("CLP", 0, None), ("ISK", 0, None), ("VND", 0, None),
    ("UGX", 0, None), ("XOF", 0, None), ("XAF", 0, None),
    ("KWD", 3, None), ("BHD", 3, None), ("OMR", 3, None), ("JOD", 3, None), ("TND", 3, None),
))
CURRENCIES: dict[str, CurrencyInfo] = {info.code: info for info in _TABLE}

# Symbols that name exactly one currency when they appear in a file. "$" is not
# here: it is used by many dollar currencies, so it is not evidence of any one.
UNAMBIGUOUS_SYMBOLS = {"€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₩": "KRW", "₪": "ILS",
                       "₱": "PHP", "₦": "NGN", "₺": "TRY", "₴": "UAH"}
# Currencies whose amounts a bank file may print with a plain "$".
DOLLAR_CURRENCIES = frozenset({"USD", "CAD", "AUD", "NZD", "HKD", "SGD", "MXN", "TWD", "BRL"})


class CurrencyError(ValueError):
    """A currency problem with a stable ``code`` for callers."""

    def __init__(self, code: str, message: str, action: str = "", **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.action = action
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        error = {"code": self.code, "message": str(self)}
        if self.action:
            error["action"] = self.action
        error.update(self.detail)
        return error


def normalize_code(value: Any) -> str:
    """Return a known ISO 4217 code in upper case, or raise CurrencyError."""
    text = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", text):
        raise CurrencyError("invalid_currency", f"{value!r} is not a three-letter currency code such as USD or EUR.",
                            "Use an ISO 4217 code such as USD, EUR, GBP or CAD.")
    if text not in CURRENCIES:
        raise CurrencyError("unknown_currency", f"{text} is not a currency that Slashbooks knows.",
                            "Use an ISO 4217 code such as USD, EUR, GBP or CAD.")
    return text


def minor_units(currency: str) -> int:
    """The number of decimal places of the currency (USD 2, JPY 0, KWD 3)."""
    return CURRENCIES[normalize_code(currency)].minor_units


def require_supported(currency: Any) -> str:
    """Return the code when this version can keep books in it; refuse other decimal places."""
    code = normalize_code(currency)
    digits = CURRENCIES[code].minor_units
    if digits != SUPPORTED_MINOR_UNITS:
        raise CurrencyError(
            "unsupported_currency_decimals",
            f"{code} has {digits} decimal places. This version of Slashbooks keeps books only in currencies "
            "with two decimal places, such as USD, EUR, GBP or CAD.",
            "Keep these books in a two-decimal currency, or wait for a version that supports this currency.",
            currency=code, minor_units=digits)
    return code


def company_currency(config: Mapping[str, Any] | Any) -> str:
    """The company's currency from entity.json (or an Entity). Absent means USD."""
    if hasattr(config, "entity_config"):
        config = config.entity_config
    value = config.get(CURRENCY_KEY) if isinstance(config, Mapping) else None
    if value is None:
        return DEFAULT_CURRENCY
    try:
        return require_supported(value)
    except CurrencyError as exc:
        raise ValueError(f"entity.json currency is not valid: {exc} Run books entity set-currency to fix it.") from None


def currency_source(config: Mapping[str, Any] | Any) -> str:
    if hasattr(config, "entity_config"):
        config = config.entity_config
    value = config.get(SOURCE_KEY, "default") if isinstance(config, Mapping) else "default"
    return value if value in SOURCES else "default"


def is_default_usd(config: Mapping[str, Any] | Any) -> bool:
    return company_currency(config) == DEFAULT_CURRENCY


def format_money(amount: Any, currency: str = DEFAULT_CURRENCY) -> str:
    """Display text for an amount: "$1,234.50", "€1,234.50" or "CHF 1,234.50".

    USD output is exactly ``f"${amount:,.2f}"``, the format the engine always used.
    """
    code = normalize_code(currency)
    info = CURRENCIES[code]
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    text = f"{value:,.{info.minor_units}f}"
    return f"{info.symbol}{text}" if info.symbol else f"{code} {text}"


def amount_text(amount: Any, currency: str = DEFAULT_CURRENCY) -> str:
    """Plain text "12.00 USD" form used in engine messages."""
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return f"{value:.2f} {currency}"


# ---------------------------------------------------------------------------
# Detection in text
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted(CURRENCIES)) + r")(?![A-Za-z])", re.IGNORECASE)


def marks_in(value: str) -> set[str]:
    """Currency codes named in a text cell by an ISO code or an unambiguous symbol."""
    found = {code for mark, code in UNAMBIGUOUS_SYMBOLS.items() if mark in value}
    found.update(match.upper() for match in _CODE_RE.findall(value))
    return found


def dollar_allowed(currency: str) -> bool:
    return currency in DOLLAR_CURRENCIES


# ---------------------------------------------------------------------------
# Company state
# ---------------------------------------------------------------------------

def books_activity(entity_path: Path | str) -> list[str]:
    """Name what the company already holds. An empty list means a company with no entries at all.

    Counts ledger entries, balance assertions, imported source rows, accrual
    records (the accrual-enable receipt does not count), the review queue and
    staged rows.
    """
    from .ledger.store import ACCRUAL_SCHEMA_VERSION, LedgerStore, default_store_path
    path = Path(entity_path)
    found: list[str] = []
    store_path = default_store_path(path)
    if store_path.exists():
        store = LedgerStore(store_path)
        with store.connection() as conn:
            if store.schema_version(conn) is not None:
                for table, label in (("entries", "ledger entries"), ("balance_assertions", "balance assertions"),
                                     ("source_transactions", "imported source rows")):
                    if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                        found.append(label)
                if store.schema_version(conn) == ACCRUAL_SCHEMA_VERSION:
                    from .accrual.store import TABLES
                    for table in TABLES:
                        rows = conn.execute(f"SELECT payload_json FROM accrual_{table}").fetchall()
                        if table == "intents":
                            rows = [r for r in rows if json.loads(r[0]).get("kind") != "accrual_enable"]
                        if rows:
                            found.append(f"accrual {table}")
    if (path / "books.beancount").exists() and (path / "books.beancount").read_text(encoding="utf-8").strip():
        found.append("legacy ledger file")
    staging = path / "staging"
    for name, label in (("pending-categorization.json", "review queue items"), ("pending.json", "staged pending rows"),
                        ("seen-ids.json", "imported source IDs")):
        target = staging / name
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8") or "[]")
            except (json.JSONDecodeError, OSError):
                found.append(label)
                continue
            if data:
                found.append(label)
    return found


def _write_currency(entity: Any, code: str, source: str, event: str, payload: dict[str, Any],
                    session: str) -> None:
    """Write entity.json currency, re-label account currencies and append an audit event in one transaction."""
    from .ledger.store import LedgerStore, default_store_path
    previous = company_currency(entity.entity_config)
    config = {**entity.entity_config, CURRENCY_KEY: code, SOURCE_KEY: source}
    temp = entity.path / "entity.json.currency.tmp"
    temp.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    store = LedgerStore(default_store_path(entity.path))
    try:
        if store.path.exists() and store.schema_version() is not None:
            with store.transaction(immediate=True) as conn:
                # The company has no entries, so only the account catalog names the old currency.
                conn.execute("UPDATE accounts SET currency=? WHERE currency=?", (code, previous))
                # The store records a non-USD company currency so that new accounts open in it.
                if code == DEFAULT_CURRENCY:
                    conn.execute("DELETE FROM meta WHERE key='currency'")
                else:
                    store.set_meta("currency", code, conn)
                store.append_audit_event(event, payload, conn)
                store.append_audit_event("ledger-store-sealed", {"session_id": session, "entries": 0,
                                                                 "store_sha256": store.content_digest(conn)}, conn)
                os.replace(temp, entity.path / "entity.json")
        else:
            os.replace(temp, entity.path / "entity.json")
    finally:
        temp.unlink(missing_ok=True)
    entity.entity_config.clear()
    entity.entity_config.update(config)


# ---------------------------------------------------------------------------
# Import-time adoption
# ---------------------------------------------------------------------------

def mismatch_error(company: str, found: str, source: str, *, source_set: bool) -> CurrencyError:
    why = ("a person set the company currency" if source_set else "the company already has entries")
    return CurrencyError(
        "unsupported_currency",
        f"The {source} is in {found}, but the company keeps its books in {company} and {why}. "
        "Slashbooks does not convert currencies. Nothing was imported.",
        f"Ask for a file in {company}, or, if the company keeps its books in {found} and has no entries yet, "
        f"run books entity set-currency PATH --currency {found} before importing.",
        reason="currency_mismatch", company_currency=company, file_currency=found)


def mixed_error(found: Iterable[str], source: str) -> CurrencyError:
    codes = sorted(found)
    return CurrencyError(
        "unsupported_currency",
        f"The {source} has amounts in more than one currency ({', '.join(codes)}). Each company keeps its books "
        "in one currency and Slashbooks does not convert currencies. Nothing was imported.",
        "Ask for a separate file for each currency, or a file already converted to the company currency.",
        reason="mixed_currency", currencies=codes)


def resolve_import_currency(entity: Any, found: Iterable[str], source: str, *, adopt: bool = True) -> str:
    """Check the currencies an import carries against the company currency.

    Returns the company currency to book in. A company with no entries whose
    currency is still the default adopts the one currency the import carries:
    entity.json gets ``currency`` and ``currency_source="inferred"`` and the ledger
    gets an audit event, under the company write lock, before any posting. More
    than one currency, or a different currency than a company that has entries
    or a person-set currency, raises CurrencyError. Nothing converts. With
    ``adopt=False`` (a dry run) the same checks run and nothing is written.
    """
    from .ledger.importer import _entity_write_lock
    codes = {str(code).strip().upper() for code in found if str(code or "").strip()}
    company = company_currency(entity.entity_config)
    if not codes:
        return company
    if len(codes) > 1:
        raise mixed_error(codes, source)
    code = next(iter(codes))
    if code == company:
        return company
    try:
        normalize_code(code)
    except CurrencyError:
        raise CurrencyError("unsupported_currency", f"The {source} names the currency {code}, which Slashbooks does not know. "
                            "Nothing was imported.", "Ask for a file in the company currency.",
                            reason="unknown_currency", file_currency=code) from None
    with _entity_write_lock(entity.path):
        from .entity import load_entity
        fresh = load_entity(entity.path)
        company = company_currency(fresh.entity_config)
        if code == company:
            entity.entity_config.clear()
            entity.entity_config.update(fresh.entity_config)
            return company
        source_set = currency_source(fresh.entity_config) == "set"
        if source_set or books_activity(entity.path):
            raise mismatch_error(company, code, source, source_set=source_set)
        require_supported(code)
        if not adopt:
            return code
        _write_currency(fresh, code, "inferred", "currency-inferred",
                        {"from": company, "to": code, "source": source}, "entity-currency-inferred")
        entity.entity_config.clear()
        entity.entity_config.update(fresh.entity_config)
        return code


def check_rows(entity: Any, rows: Iterable[Mapping[str, Any]], source: str) -> str:
    """Resolve the currencies named by normalized rows (their ``currency`` field)."""
    return resolve_import_currency(entity, {str(r.get("currency")) for r in rows if r.get("currency")}, source)


# ---------------------------------------------------------------------------
# books entity set-currency (person-only; preview, then --apply)
# ---------------------------------------------------------------------------

SET_CURRENCY_KIND = "entity_set_currency"
SET_CURRENCY_NOTE = ("Sets the one currency the company keeps its books in. Slashbooks does not convert currencies, keep "
                     "exchange rates or compute exchange gains. Every import, report and entry uses this currency. "
                     "It can change only while the company has no entries.")


def preview_set_currency(entity: Any, currency: Any) -> dict[str, Any]:
    """Preview setting the company currency. Writes nothing."""
    from .accrual.intake import canonical_hash
    from .entity import load_entity
    from .ledger.store import LedgerStore, default_store_path
    entity = load_entity(entity.path)
    code = require_supported(currency)
    config = entity.entity_config
    current = company_currency(config)
    current_source = currency_source(config)
    activity = books_activity(entity.path)
    changes = (current, current_source) != (code, "set")
    if activity and code != current:
        raise CurrencyError("company_has_entries",
                            f"The company already has {', '.join(activity)} in {current}. The company currency can change "
                            "only while the company has no entries. Slashbooks does not convert currencies.",
                            "Start a new company folder for books in another currency.",
                            company_currency=current, requested_currency=code)
    store = LedgerStore(default_store_path(entity.path))
    digest = store.content_digest() if store.path.exists() and store.schema_version() is not None else None
    body = {"schema_version": 1, "kind": SET_CURRENCY_KIND, "request": {"currency": code},
            "changes": changes, "ledger_digest": digest, "configuration_hash": canonical_hash(config),
            "effects": {"from": {"currency": current, "currency_source": current_source},
                        "to": {"currency": code, "currency_source": "set"},
                        "minor_units": CURRENCIES[code].minor_units, "entries_changed": 0,
                        "published_statements_changed": False, "note": SET_CURRENCY_NOTE}}
    body["request_hash"] = canonical_hash({"kind": SET_CURRENCY_KIND, "request": body["request"]})
    body["plan_hash"] = canonical_hash(body)
    return body


def apply_set_currency(entity: Any, currency: Any, plan_hash: str | None = None) -> dict[str, Any]:
    """Set the company currency (person-only). Repeating the same value returns already_applied."""
    from .accrual.journals import JournalError
    from .entity import load_entity
    from .ledger.importer import _entity_write_lock
    from .preparation_runtime import is_preparation
    if is_preparation():
        raise JournalError("currency_person_only", "Setting the company currency requires the owner or bookkeeper.")
    with _entity_write_lock(entity.path):
        fresh = preview_set_currency(entity, currency)
        if plan_hash is not None and plan_hash != fresh["plan_hash"]:
            raise JournalError("stale_preview", "The company changed after the preview.", "Run the preview again.")
        if not fresh["changes"]:
            return {"status": "already_applied", "kind": SET_CURRENCY_KIND, **fresh["effects"]["to"],
                    "request_hash": fresh["request_hash"]}
        entity = load_entity(entity.path)
        code = fresh["request"]["currency"]
        _write_currency(entity, code, "set", "currency-set",
                        {"from": fresh["effects"]["from"], "to": fresh["effects"]["to"], "plan_hash": fresh["plan_hash"]},
                        "entity-set-currency")
        return {"status": "applied", "kind": SET_CURRENCY_KIND, **fresh["effects"]["to"], "from": fresh["effects"]["from"],
                "request_hash": fresh["request_hash"], "plan_hash": fresh["plan_hash"], "entries_changed": 0,
                "published_statements_changed": False}


# ---------------------------------------------------------------------------
# books entity infer-currency (read-only)
# ---------------------------------------------------------------------------

def _file_evidence(path: Path) -> tuple[set[str], str]:
    """Currencies a file carries and the kind of file. Reads only."""
    from .connectors import bankcsv, ofx
    text = bankcsv.read_text(path)
    stripped = text.lstrip()
    if ofx.looks_like_ofx(text):
        return ofx.transaction_currencies(text), "OFX statement"
    if stripped.startswith(("{", "[")):
        data = json.loads(text)
        return json_currencies(data), "JSON export"
    return bankcsv.file_currencies(text), "bank CSV"


def json_currencies(data: Any) -> set[str]:
    """Currency codes in a JSON export: Stripe balance/charges, QuickBooks home currency, BankSync accounts and rows."""
    found: set[str] = set()

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"currency", "default_currency", "defaultCurrency", "HomeCurrency", "home_currency",
                           "CurrencyRef"} and isinstance(item, (str, Mapping)):
                    text = item.get("value") if isinstance(item, Mapping) else item
                    if isinstance(text, str) and re.fullmatch(r"[A-Za-z]{3}", text.strip()):
                        found.add(text.strip().upper())
                else:
                    walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:5000]:
                walk(item, depth + 1)
    walk(data)
    return found


def infer_from_files(paths: Iterable[Path | str]) -> dict[str, Any]:
    """Propose a company currency from files. Pure: writes nothing.

    Returns ``{"currency": code | None, "source": ..., "evidence": [...]}``. The
    currency is null when no file names a currency, or when the files name more
    than one.
    """
    evidence = []
    union: set[str] = set()
    for raw in paths:
        path = Path(raw)
        try:
            codes, kind = _file_evidence(path)
        except Exception as exc:  # A file that cannot be read gives no evidence; say why.
            evidence.append({"file": path.name, "kind": "unreadable", "currencies": [], "note": str(exc)[:200]})
            continue
        evidence.append({"file": path.name, "kind": kind, "currencies": sorted(codes)})
        union |= codes
    result: dict[str, Any] = {"currency": None, "source": "none", "evidence": evidence}
    if len(union) == 1:
        code = next(iter(union))
        result.update(currency=code, source="inferred")
        try:
            require_supported(code)
        except CurrencyError as exc:
            result.update(currency=None, source="unsupported", note=str(exc))
    elif len(union) > 1:
        result.update(source="mixed", currencies=sorted(union),
                      note="The files name more than one currency. A person chooses the company currency.")
    else:
        result["note"] = "No file names a currency. The company stays in its current currency (USD by default)."
    return result
