"""The books scope of a company (``books_from``) and where each account's open date came from.

Four things are different:

* The fiscal year (``entity.json`` ``fiscal_year_start``): the calendar of reports and tax views. A fiscal-year
  boundary is not a posting start. A business that starts its books on 2026-01-01 can still have to record its
  2025 history.
* The books scope (``books_from``, this module): the start of the historical scope that the host or a person
  states for the company (``books entity init --books-from``), and the source of the default open date of the
  generic accounts (``is_generic``). It is not a posting fence. No ledger write reads it.
* The open date of each account (``ledger.open_dates``): the only open-date posting fence. A bank, card or other
  balance sheet account gets its open date from evidence about that account (an explicit ``--open-date``, a
  cutover, an opening), never from the books scope.
* The books start policy (``ledger.books_policy``): in enforced mode, the host's fence for each in-scope account.
  This module does not change it.

Before this module, a company created with no cutover opened every starter account on a fixed fallback date,
2026-01-01, and ``books entity account-add`` gave a new category the same date. Because the ledger write refuses
a posting before an account's open date, such a company could not record a 2025 entry on any category.

Generic accounts. The books scope opens income and expense accounts and the ordinary owner equity accounts of
the engine (``ORDINARY_EQUITY``: owner contributions and owner draws). The account model has no equity subtype,
so the engine lists them by exact name, as it lists starter financial kinds. Every other equity account keeps
its own date: ``Equity:Opening-Balances`` and the other opening equity accounts (an opening operation sets their
date), retained earnings and prior-period earnings (they summarize history), the accrual-to-cash adjustment, and
any equity account a person adds (its role is not known).

Provenance. The ledger meta key ``open_date_provenance`` (``LedgerStore.load_open_date_provenance``) records the
books scope and, for each account, the source of its current open date:

* ``default``: the fixed fallback date, given because no scope and no date were stated;
* ``scope``: the books scope; ``cutover``: the company cutover;
* ``user``: an explicit ``--open-date``; ``import``: an import, a connector or another ledger write;
  ``opening``: an opening operation (statement, QuickBooks or accrual opening, accrual tracking start);
* ``legacy``: no reliable source (in the catalog before the record existed, or a stale marker).

Every catalog writer of this engine keeps the record in the same transaction as its change, and a ledger made
before the record gets one at its first write with a source. A marker is evidence only while its date is the
account's current open date and the record's catalog fingerprint matches the catalog. A catalog change by a
writer that does not keep the record (an earlier engine version after a rollback, or a direct edit) changes the
fingerprint, and then no marker of the record is evidence (``stale``). The fingerprint covers the row id, so a
removal and a new add of the same account on the same date also shows. An earlier engine command that writes
nothing (for example ``account-add --open-date`` with the date the account already has) leaves no trace at all,
so no record can show it; this is the reason why a ledger without reliable markers never gets automatic
evidence. The meta key is part of the ledger digest, so the audit seal covers it.

Repair (``preview`` and ``apply``, ``books entity set-books-from``). A person states a books scope. The preview
lists each account that moves and each account that stays, with the reason and what is known about it, and a
``plan_hash``. ``apply`` runs only the plan with that hash. Only a generic account moves, and only when:

* its open date has the marker ``default`` or ``scope`` and the record is not stale; or
* it has no reliable marker (``legacy``), it opens on the fixed fallback date, and a person names it in the
  request (``--account``) after review. Its name and date alone are never evidence, also for a starter account.

An account with ``user``, ``import``, ``opening`` or ``cutover`` provenance never moves. Balance sheet accounts,
equity accounts other than ``ORDINARY_EQUITY``, and accounts that a bank or CSV mapping names never move. The
preview refuses a scope before the company cutover, before a company opening (a QuickBooks or accrual opening
summarizes the whole company before its day), later than the current scope (an open date only moves earlier),
or over a closed period. A statement opening is for one account; the statement opening and intake controls of
that account keep its earlier rows out of the books, so the preview lists it and does not refuse. No entry
changes. A repeat of an applied plan returns ``already_applied`` and writes nothing.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

KIND = "entity_books_from"
SET_EVENT = "books-from-set"
SESSION = "entity-set-books-from"
DEFAULT_OPEN_DATE = date(2026, 1, 1)
GENERIC_ROOTS = ("Income", "Expenses")
# The ordinary owner equity accounts of the engine's starter chart. Every other equity account keeps its own date.
ORDINARY_EQUITY = frozenset({"Equity:Owner-Contributions", "Equity:Owner-Draws"})
REPAIRABLE_SOURCES = ("default", "scope")
UNCERTAIN_SOURCES = ("legacy", "unknown")
# Openings that summarize the whole company on their day (``quickbooks.OTHER_OPENING_SOURCES``).
COMPANY_OPENINGS = ("quickbooks-opening", "accrual-opening", "accrual-tracking-opening")


class BooksFromError(ValueError):
    """The books scope cannot be set as requested. ``code`` names the rule. Nothing was written."""

    def __init__(self, code: str, message: str, action: str = "Nothing was changed.", **detail: Any) -> None:
        self.code, self.action, self.detail = code, action, detail
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"status": "blocked", "errors": [{"code": self.code, "message": str(self), "action": self.action,
                                                 **self.detail}]}


def is_generic(account: str) -> bool:
    """True for an account that the books scope opens: income, expense, and ``ORDINARY_EQUITY``."""
    return account.partition(":")[0] in GENERIC_ROOTS or account in ORDINARY_EQUITY


def parse_day(value: Any, what: str = "books_from") -> date:
    """A strict YYYY-MM-DD date."""
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        day = date.fromisoformat(value)
    except ValueError:
        raise BooksFromError("invalid_date", f"{what} must be a date in YYYY-MM-DD form. Got {value!r}.",
                             "Give the date as YYYY-MM-DD.") from None
    return day


def new_provenance(books_from: date | None, source: str | None) -> dict[str, Any]:
    """An empty provenance record with the books scope."""
    return {"version": 1, "books_from": books_from.isoformat() if books_from else None,
            "books_from_source": source if books_from else None, "accounts": {}, "catalog_sha256": ""}


def starter_open(account: str, cutover: date | None, books_from: date | None) -> tuple[date, str]:
    """The open date of a starter account and its provenance.

    A cutover opens every starter account, as before. Otherwise the books scope opens a generic account
    (``is_generic``); any other account keeps the fixed fallback date.
    """
    if cutover is not None:
        return cutover, "cutover"
    if books_from is not None and is_generic(account):
        return books_from, "scope"
    return DEFAULT_OPEN_DATE, "default"


def recorded_scope(store: Any, conn: Any, config: Mapping[str, Any]) -> tuple[date, str] | None:
    """The company's books scope and its source (``scope`` or ``cutover``), or ``None`` when none is known.

    The ledger record wins. A company created before the record uses its ``entity.json`` cutover.
    """
    record = store.load_open_date_provenance(conn)
    if record and record.get("books_from"):
        return date.fromisoformat(record["books_from"]), record.get("books_from_source") or "scope"
    cutover = config.get("cutover_date")
    if isinstance(cutover, str) and cutover:
        try:
            return date.fromisoformat(cutover), "cutover"
        except ValueError:
            return None
    return None


def added_open(account: str, scope: tuple[date, str] | None) -> tuple[date, str]:
    """The open date of a new account that ``books entity account-add`` adds without ``--open-date``."""
    if scope is not None and is_generic(account):
        return scope
    return DEFAULT_OPEN_DATE, "default"


def _cutover(config: Mapping[str, Any]) -> date | None:
    value = config.get("cutover_date")
    if value in (None, ""):
        return None
    return parse_day(value, "entity.json cutover_date")


def _starter_names(store: Any, conn: Any, config: Mapping[str, Any]) -> frozenset[str]:
    from .entity import _ACCRUAL_STARTER_ACCOUNTS, resolve_basis, starter_chart
    row = conn.execute("SELECT value FROM meta WHERE key='business_type'").fetchone()
    business_type = str(row[0]) if row else str(config.get("business_type") or "consulting")
    names = set(starter_chart(business_type))
    if resolve_basis(dict(config)) == "accrual":
        names.update(_ACCRUAL_STARTER_ACCOUNTS)
    return frozenset(names)


def _openings(conn: Any) -> list[dict[str, str]]:
    """Opening entries in force: a statement, QuickBooks or accrual opening that no entry reverses."""
    from .opening_presentation import OPENING_IMPORT_SOURCES
    from .source_ids import source_id_variants
    reversed_ids: set[str] = set()
    found = []
    for entry_date, source_id, raw in conn.execute(
            "SELECT date, source_id, metadata_json FROM entries ORDER BY date, id"):
        try:
            pairs = json.loads(raw or "[]")
        except ValueError:
            continue
        meta = {str(pair[0]): str(pair[1]) for pair in pairs if isinstance(pair, (list, tuple)) and len(pair) == 2}
        if "reverses" in meta:
            reversed_ids |= source_id_variants(meta["reverses"])
            continue
        if meta.get("import-source") in OPENING_IMPORT_SOURCES:
            found.append({"date": str(entry_date)[:10], "source_id": str(source_id or ""),
                          "import_source": meta["import-source"]})
    return [item for item in found if not (source_id_variants(item["source_id"]) & reversed_ids)]


def _closed_days(entity: Any, store: Any, start: date, end: date) -> list[str]:
    """Closed periods that the days ``start`` to ``end`` (inclusive) touch."""
    from .ledger.reclassify import ReclassifyError, _LOCKED_STATUSES, _period_state
    try:
        months, through = _period_state(entity.entity_config)
    except ReclassifyError as exc:
        raise BooksFromError("invalid_period_state", str(exc), exc.action) from None
    periods: dict[str, Any] = {}
    if store.schema_version() in {2, 3}:
        from .accrual.store import list_records
        periods = {str(row.get("id")): row.get("status") for row in list_records(store, "periods")}
    found = []
    if through is not None and start.isoformat() <= through:
        found.append(f"closed_through {through}")
    month = date(start.year, start.month, 1)
    while month <= end:
        key = month.isoformat()[:7]
        if key in months:
            found.append(f"closed_periods {key}")
        if periods.get(key) in _LOCKED_STATUSES:
            found.append(f"accrual period {key} {periods[key]}")
        month = (month + timedelta(days=32)).replace(day=1)
    return found


def _account_history(store: Any) -> dict[str, list[dict[str, Any]]]:
    """For each account, the audit events that name it: account-add and account-remove. Facts for a review."""
    from .account_remove import ACCOUNT_ADD_SESSION
    found: dict[str, list[dict[str, Any]]] = {}
    for event in store.load_audit_events():
        payload = event.get("payload") or {}
        name = None
        if event["type"] == "intent" and payload.get("session_id") == ACCOUNT_ADD_SESSION:
            description = str(payload.get("description", ""))
            for verb in ("Add account ", "Update account "):
                if description.startswith(verb) and ": open " in description:
                    name = description[len(verb):description.index(": open ")]
        elif event["type"] == "account-removed":
            name = payload.get("account")
        if isinstance(name, str):
            found.setdefault(name, []).append({"id": event["id"], "type": event["type"], "ts": event["ts"],
                                               "description": payload.get("description") or "account removed"})
    return found


def _check_names(accounts: Iterable[Any]) -> list[str]:
    names = []
    for account in accounts:
        if not isinstance(account, str) or not account or any(ord(c) < 32 or ord(c) == 127 for c in account):
            raise BooksFromError("invalid_account", f"{account!r} is not a valid account name.",
                                 "Give the account name exactly as the account catalog shows it.")
        names.append(account)
    return sorted(set(names))


def preview(entity_path: Path | str, books_from: Any, accounts: Sequence[str] = ()) -> dict[str, Any]:
    """Preview setting the books scope to ``books_from``. Writes nothing. See the module documentation."""
    from .accrual.intake import canonical_hash
    from .entity import load_entity
    from .ledger.importer import _source_accounts, check_integrity
    from .ledger.store import LedgerStore, default_store_path

    day = parse_day(books_from)
    named = _check_names(accounts)
    try:
        entity = load_entity(Path(entity_path))
    except FileNotFoundError as exc:
        raise BooksFromError("no_company", str(exc), "Run books entity init first.") from None
    store = LedgerStore(default_store_path(entity.path))
    if not store.path.exists():
        raise BooksFromError("no_ledger", "The company has no ledger.", "Run books entity init first.")
    if check_integrity(entity).status != "ok":
        raise BooksFromError("ledger_integrity", "Cannot change open dates while ledger integrity is invalid.",
                             "Run the integrity check and fix the reported problem first.")
    config = entity.entity_config
    cutover = _cutover(config)
    with store.connection() as conn:
        if conn.execute("SELECT 1 FROM entries LIMIT 1").fetchone() and not conn.execute(
                "SELECT 1 FROM audit_events LIMIT 1").fetchone():
            raise BooksFromError("audit_history_missing",
                                 "The account catalog cannot change on a ledger with entries and no audit history.",
                                 "Repair the audit history first.")
        record = store.load_open_date_provenance(conn)
        catalog = [(str(name), str(opened)[:10]) for name, opened in
                   conn.execute("SELECT name, open_date FROM accounts ORDER BY name")]
        usage = {str(account): (int(count), str(first)[:10]) for account, count, first in conn.execute(
            "SELECT p.account, COUNT(*), MIN(e.date) FROM postings p JOIN entries e ON e.id = p.entry_id "
            "GROUP BY p.account")}
        kinds = store.load_financial_kinds(conn)
        starters = _starter_names(store, conn, config)
        openings = _openings(conn)
        digest = store.content_digest(conn)
    current = date.fromisoformat(record["books_from"]) if record and record.get("books_from") else None
    current_source = (record or {}).get("books_from_source") if current else None
    if current is None and cutover is not None:
        current, current_source = cutover, "cutover"
    effective = current or DEFAULT_OPEN_DATE
    request = {"books_from": day.isoformat(), "accounts": named}

    start = cutover if cutover is not None else current if current_source == "cutover" else None
    if start is not None and day < start:
        raise BooksFromError("scope_before_cutover",
                             f"The company has a cutover on {start}. Its books start there, and the opening balance "
                             f"on that day summarizes the earlier history. A books scope of {day} would count that "
                             "history again.", "Keep the cutover as the books scope.", cutover=str(start))
    if day > effective:
        raise BooksFromError("scope_not_earlier",
                             f"The books scope is {effective}. A new scope can only be earlier, because an open date "
                             "only moves earlier.", "Give a date on or before the current scope.",
                             current_books_from=effective.isoformat())
    # A QuickBooks or accrual opening is a trial balance of the whole company on its day: its equity line already
    # holds the earlier income and expense. Entries on categories before it would count that history again. A
    # statement opening is the balance of one account. The statement opening and intake controls of that account
    # keep its earlier rows out of the books, so the preview lists it for review and does not refuse.
    later = [item for item in openings if item["date"] > day.isoformat() and item["import_source"] in COMPANY_OPENINGS]
    if later:
        raise BooksFromError("scope_before_opening",
                             f"The books hold a company opening balance dated {later[0]['date']} "
                             f"({later[0]['source_id']}). It summarizes the history before its day, so entries "
                             "before it would count that history again.",
                             "Give a books scope on or after the opening day, or replace the opening first.",
                             openings=later[:20])
    if store.schema_version() in {2, 3}:
        from .accrual.store import load_record
        coverage = load_record(store, "coverage", "opening")
        start = (coverage or {}).get("cutover_date")
        if isinstance(start, str) and start > day.isoformat():
            raise BooksFromError("scope_before_opening",
                                 f"The accrual opening of the books covers the history before {start}.",
                                 "Give a books scope on or after the accrual opening day.", cutover=start)

    mapped = set(_source_accounts(entity)) | set(kinds)
    in_catalog = {name for name, _ in catalog}
    unknown = [name for name in named if name not in in_catalog]
    if unknown:
        raise BooksFromError("unknown_account", f"{unknown[0]} is not in the account catalog.",
                             "Give the account name exactly as the account catalog shows it.", accounts=unknown)
    history = _account_history(store)
    moved: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for name, opened in catalog:
        marker = (record or {}).get("accounts", {}).get(name)
        if marker is not None and marker.get("open_date") != opened:
            marker = None  # A marker is evidence only for the date it names.
        source = marker["source"] if marker else ("legacy" if record is None else "unknown")
        count, first = usage.get(name, (0, None))
        row = {"account": name, "open_date": opened, "source": source, "postings": count, "first_posting": first}
        reason = None
        evidence = None
        if name.partition(":")[0] == "Equity" and not is_generic(name):
            reason = "control_equity_account"
        elif not is_generic(name):
            reason = "balance_sheet_account"
        elif name in mapped:
            reason = "mapped_source"
        elif opened <= day.isoformat():
            reason = "already_open"
        elif source in REPAIRABLE_SOURCES:
            evidence = f"recorded_{source}"
        elif source in UNCERTAIN_SOURCES:
            # No reliable marker. A name and a date are never evidence, also for a starter account: an earlier
            # engine wrote no record of an explicit date, so only a person's review can decide.
            if opened != DEFAULT_OPEN_DATE.isoformat():
                reason = "not_fallback_date"
            else:
                row["known"] = {"in_starter_chart": name in starters, "audit_events": history.get(name, [])}
                if name in named:
                    evidence = "named_by_person"
                else:
                    reason = "uncertain_provenance"
        else:
            reason = f"provenance_{source}"
        if evidence:
            moved.append({**row, "to": day.isoformat(), "evidence": evidence})
        else:
            kept.append({**row, "reason": reason})
    refused = [item for item in kept if item["account"] in named and item["reason"] != "already_open"]
    if refused:
        raise BooksFromError("account_not_repairable",
                             f"{refused[0]['account']} keeps its open date ({refused[0]['reason']}).",
                             "Leave the account out of the request. Change its open date with books entity "
                             "account-add --open-date only when the books must hold its earlier activity.",
                             accounts=[{"account": item["account"], "reason": item["reason"]} for item in refused])
    if moved:
        latest = date.fromisoformat(max(item["open_date"] for item in moved)) - timedelta(days=1)
        closed = _closed_days(entity, store, day, latest)
        if closed:
            raise BooksFromError("closed_period",
                                 f"The days from {day} to {latest} include a closed period ({closed[0]}).",
                                 "Reopen the period first, or give a books scope after it.", closed=closed)
    # A change moves an account, or records a scope that the ledger does not hold yet.
    changes = bool(moved) or (record or {}).get("books_from") != day.isoformat()
    body: dict[str, Any] = {
        "schema_version": 1, "kind": KIND, "request": request, "changes": changes,
        "ledger_digest": digest, "configuration_hash": canonical_hash(config),
        "provenance": "legacy" if record is None else "stale" if record.get("stale") else "recorded",
        "books_from": {"from": current.isoformat() if current else None, "from_source": current_source,
                       "to": day.isoformat()},
        "accounts": moved, "kept": kept, "openings": openings[:100],
        "effects": {"accounts_moved": len(moved), "entries_changed": 0,
                    "uncertain": [item["account"] for item in kept if item["reason"] == "uncertain_provenance"],
                    "note": "The books scope is the start of the historical scope and the default open date of "
                            "income, expense and ordinary owner equity accounts. It is not a posting fence: each "
                            "account's open date stays the open-date fence. Bank, card, other balance sheet and "
                            "control equity accounts keep their own open dates."}}
    body["plan_hash"] = canonical_hash(body)
    return body


def apply(entity_path: Path | str, books_from: Any, plan_hash: str | None,
          accounts: Sequence[str] = ()) -> dict[str, Any]:
    """Apply the previewed plan with ``plan_hash``. A replay of an applied plan returns ``already_applied``."""
    from .accrual.enable import _seal
    from .ledger.importer import _entity_write_lock
    from .ledger.store import LedgerStore, default_store_path

    if not plan_hash:
        raise BooksFromError("plan_hash_required", "--apply needs the plan_hash of the preview.",
                             "Run the preview, review it, then apply with --plan-hash.")
    root = Path(entity_path).resolve()
    if not (root / "entity.json").is_file():
        raise BooksFromError("no_company", f"No entity.json found at '{root}'.", "Run books entity init first.")
    store = LedgerStore(default_store_path(root))
    with _entity_write_lock(root):
        if store.path.exists():
            for event in store.load_audit_events():
                if event["type"] == SET_EVENT and event["payload"].get("plan_hash") == plan_hash:
                    payload = event["payload"]
                    return {"status": "already_applied", "kind": KIND, "replayed": True, "plan_hash": plan_hash,
                            "books_from": {"from": payload.get("from"), "to": payload.get("to")},
                            "accounts": payload.get("accounts", []), "effects": {"entries_changed": 0}}
        fresh = preview(root, books_from, accounts)
        if plan_hash != fresh["plan_hash"]:
            raise BooksFromError("stale_preview", "The company changed after the preview, or the request differs.",
                                 "Run the preview again and review it.")
        if not fresh["changes"]:
            return {"status": "already_applied", **fresh}
        day = fresh["books_from"]["to"]
        with store.transaction(immediate=True) as conn:
            if store.content_digest(conn) != fresh["ledger_digest"]:
                raise BooksFromError("stale_preview", "The company changed after the preview.",
                                     "Run the preview again and review it.")
            # Read the record before the change, so its fingerprint is checked against the catalog the preview saw.
            record = store.load_open_date_provenance(conn)
            if record is None:
                record = new_provenance(None, None)
                for name, opened in conn.execute("SELECT name, open_date FROM accounts ORDER BY name"):
                    record["accounts"][str(name)] = {"source": "legacy", "open_date": str(opened)[:10]}
            for item in fresh["accounts"]:
                result = conn.execute("UPDATE accounts SET open_date=? WHERE name=? AND open_date=?",
                                      (day, item["account"], item["open_date"]))
                if result.rowcount != 1:
                    raise BooksFromError("stale_preview", "The company changed after the preview.",
                                         "Run the preview again and review it.")
            for item in fresh["accounts"]:
                record["accounts"][item["account"]] = {"source": "scope", "open_date": day}
            record["books_from"], record["books_from_source"] = day, "scope"
            store.save_open_date_provenance(record, conn)
            store.append_audit_event(SET_EVENT, {
                "session_id": SESSION, "entries": 0, "from": fresh["books_from"]["from"], "to": day,
                "accounts": [{"account": item["account"], "from": item["open_date"], "to": day,
                              "evidence": item["evidence"]} for item in fresh["accounts"]],
                "plan_hash": fresh["plan_hash"]}, conn)
            _seal(store, conn, SESSION)
        return {"status": "applied", **fresh}
