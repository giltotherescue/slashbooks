"""Record a bank or card account's opening balance from its first saved statement.

A company whose books start at a statement has no history before it, so the
account's balance on the first statement day is the statement's printed
opening balance. Without an entry for it, every later balance of the account
is off by that amount. This module writes one entry for it, dated on the
first statement day:

    Assets:Bank:Checking        7907.82
    Equity:Opening-Balances    -7907.82

The entry is the account's opening balance, so it is part of the beginning
balances of a cash flow statement that starts on that day, and a statement
tie-out leaves it out of the period movement.

The amount is the statement's printed opening balance (``opening-source:
printed``) or, when the file has none (most CSV exports), an amount a person
enters (``opening-source: person``). A printed balance wins: a typed amount is
refused when the statement has one.

The entry is recorded only when it is the account's opening:

* a statement is saved with a confirmed period, in the company currency, or a
  person names the account and the day;
* no statement for the account starts earlier;
* nothing in the books for the account is dated before that day;
* the account has no opening entry yet.

Anything else returns ``not_eligible`` with the reason, and writes nothing.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

IMPORT_SOURCE = "statement-opening"
OPENING_EQUITY = "Equity:Opening-Balances"
SESSION = "statement-opening"


class StatementOpeningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def as_dict(self) -> JsonObject:
        return {"code": self.code, "message": str(self)}


def _not_eligible(statement_id: str, account: str | None, reason: str, message: str, **detail: Any) -> JsonObject:
    return {"status": "not_eligible", "statement_id": statement_id, "account": account,
            "reason": reason, "message": message, **detail}


def _find_record(entity_path: Path, statement_id: str) -> JsonObject:
    from . import coverage

    for doc in coverage.load_statement_documents(entity_path):
        if doc.get("statement_id") == statement_id:
            return doc
    if any(doc.get("statement_id") == statement_id
           for doc in coverage.load_statement_documents(entity_path, include_replaced=True)):
        raise StatementOpeningError("statement_replaced",
                                    f"Statement {statement_id} was replaced by another statement for the same period.")
    raise StatementOpeningError("statement_not_found",
                                f"No saved statement with a confirmed period has the ID {statement_id}.")


def _metadata(raw: Any) -> dict[str, str]:
    try:
        decoded = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return {}
    if isinstance(decoded, dict):
        return {str(k): str(v) for k, v in decoded.items()}
    return {str(pair[0]): str(pair[1]) for pair in decoded
            if isinstance(pair, (list, tuple)) and len(pair) == 2}


def _books_facts(store: Any, account: str, start: date) -> JsonObject:
    """What the books already hold for the account: earlier postings, its opening entry, the first entry day."""
    with store.connection() as conn:
        earlier = conn.execute(
            "SELECT MIN(e.date) FROM postings p JOIN entries e ON e.id = p.entry_id "
            "WHERE p.account = ? AND e.date < ?", (account, start.isoformat())).fetchone()[0]
        openings = []
        for source_id, day, raw, posted in conn.execute(
                "SELECT e.source_id, e.date, e.metadata_json, p.amount FROM postings p JOIN entries e ON e.id = p.entry_id "
                "WHERE p.account = ?", (account,)):
            meta = _metadata(raw)
            if meta.get("import-source") == IMPORT_SOURCE:
                openings.append({"source_id": source_id, "date": day, "statement_id": meta.get("statement-id"),
                                 "opening_source": meta.get("opening-source", "printed"),
                                 "amount": format(Decimal(str(posted)).quantize(Decimal("0.01")), "f")})
        first_entry = conn.execute("SELECT MIN(date) FROM entries").fetchone()[0]
    return {"earlier_posting": earlier, "openings": openings, "books_start": first_entry}


def _amount(value: Any) -> Decimal:
    from .coverage import _parse_amount

    try:
        parsed = _parse_amount(value)
    except ValueError as exc:
        raise StatementOpeningError("invalid_amount", f"--amount: {exc}") from exc
    if parsed is None:
        raise StatementOpeningError("invalid_amount", "--amount needs a decimal amount such as 7907.82.")
    return parsed


def _day(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise StatementOpeningError("invalid_date", "--on must be a date in YYYY-MM-DD form.") from exc


def _known_account(store: Any, entity_path: Path, account: str) -> bool:
    from .statement_import import load_statement_records

    if account in store.load_account_names():
        return True
    return any(r.get("account") == account for r in load_statement_records(entity_path, include_all=True))


def record_opening(entity_path: Path | str, statement_id: str | None = None, *, account: str | None = None,
                   on: str | None = None, amount: str | None = None, dry_run: bool = False) -> JsonObject:
    """Record (or, with ``dry_run``, show) an account's opening entry.

    Three forms:

    * ``statement_id``: the printed opening balance of that saved statement (``opening_source: printed``).
    * ``statement_id`` and ``amount``: a person's amount for a saved statement that has no printed opening
      balance, dated on its first day (``opening_source: person``). A printed opening balance wins, so the
      amount is refused when the statement has one.
    * ``account``, ``on`` and ``amount``: a person's amount for the account on the first day the books cover
      for it (``opening_source: person``), with no saved statement.

    ``amount`` uses the statement presentation: money held for a bank account, money owed for a card or loan.
    """
    from . import coverage
    from .entity import load_entity
    from .ledger.importer import _entity_write_lock
    from .ledger.model import Entry, Open, Posting, _validate_account_name
    from .ledger.store import LedgerStore, default_store_path
    from .ledger.validator import validate
    from .ledger.writer import render_ledger
    from .tieout import presentation_sign

    if statement_id and (account or on):
        raise StatementOpeningError("choose_one_form", "Pass --statement-id, or --account with --on, not both.")
    if not statement_id and not (account and on and amount is not None):
        raise StatementOpeningError("choose_one_form",
                                    "Pass --statement-id, or --account, --on and --amount for an amount a person enters.")
    typed = None if amount is None else _amount(amount)
    entity_path = Path(entity_path)
    with _entity_write_lock(entity_path, repair=False):
        entity = load_entity(entity_path)
        store = LedgerStore(default_store_path(entity_path))
        store.initialize()
        label = ""
        if statement_id:
            record = _find_record(entity_path, statement_id)
            account = str(record.get("account") or "")
            label = str(record.get("source") or "")
            ref = statement_id
            if record.get("kind", "statement") != "statement":
                return _not_eligible(ref, account, "not_a_statement",
                                     "Only a bank or card statement has an opening balance.")
            if record.get("coverage_basis") == "transactions" or record.get("period_confirmed") is False:
                return _not_eligible(ref, account, "period_unconfirmed",
                                     "The statement period is not confirmed from the printed statement.")
            printed = record.get("opening_balance")
            if typed is not None and printed not in (None, ""):
                return _not_eligible(ref, account, "printed_opening_exists",
                                     f"The statement has a printed opening balance of {printed}. The printed "
                                     "balance is used; run the command without --amount.", printed_opening=str(printed))
            if typed is None and printed in (None, ""):
                return _not_eligible(ref, account, "no_opening_balance",
                                     "The saved statement has no printed opening balance. Ask the person for the "
                                     "balance on the first statement day and pass it with --amount.")
            currency = str(record.get("currency") or entity.currency)
            if currency != entity.currency:
                return _not_eligible(ref, account, "currency_differs",
                                     f"The statement is in {currency} and the books are in {entity.currency}.")
            start = date.fromisoformat(str(record["period_start"]))
            presented = typed if typed is not None else Decimal(str(printed))
        else:
            assert account is not None
            try:
                _validate_account_name(account)
            except ValueError as exc:
                raise StatementOpeningError("invalid_account", str(exc)) from exc
            ref = None
            if not account.startswith(("Assets:", "Liabilities:")):
                return _not_eligible(None, account, "not_a_balance_account",
                                     "An opening balance is for an Assets: or Liabilities: account.")
            if not _known_account(store, entity_path, account):
                return _not_eligible(None, account, "unknown_account",
                                     f"{account} is not in the chart of accounts. Add it first.")
            start = _day(on)
            currency = entity.currency
            assert typed is not None
            presented = typed
            starting = [r for r in coverage.load_statements(entity_path)[0]
                        if r.kind == "statement" and r.account == account and r.period_start == start
                        and r.opening_balance is not None]
            if starting:
                return _not_eligible(starting[0].statement_id, account, "printed_opening_exists",
                                     f"The statement {starting[0].statement_id} starts on {start.isoformat()} with a "
                                     f"printed opening balance of {starting[0].opening_balance}. The printed balance "
                                     f"is used; run the command with --statement-id {starting[0].statement_id}.",
                                     printed_opening=str(starting[0].opening_balance))
        source = "person" if typed is not None else "printed"
        amount_ledger = (presented * presentation_sign(account)).quantize(Decimal("0.01"))

        earlier_statements = sorted(
            r.period_start.isoformat() for r in coverage.load_statements(entity_path)[0]
            if r.kind == "statement" and r.account == account and r.statement_id != statement_id
            and r.period_start < start)
        if earlier_statements:
            return _not_eligible(ref, account, "not_first_statement",
                                 f"A statement for {account} starts earlier, on {earlier_statements[0]}. "
                                 "The opening balance belongs to the first day the books cover for the account.",
                                 first_statement_start=earlier_statements[0])

        facts = _books_facts(store, account, start)
        if source == "person" and not statement_id:
            source_id = f"{IMPORT_SOURCE}-{start.isoformat()}-{hashlib.sha256(account.encode()).hexdigest()[:12]}"
        else:
            source_id = f"{IMPORT_SOURCE}-{statement_id}"
        if facts["openings"]:
            found = facts["openings"][0]
            if found["source_id"] == source_id and found["amount"] == format(amount_ledger, "f"):
                return {"status": "already_recorded", "statement_id": statement_id, "account": account,
                        "date": found["date"], "source_id": found["source_id"], "opening_source": found["opening_source"]}
            return _not_eligible(ref, account, "opening_exists",
                                 f"{account} already has an opening balance entry dated {found['date']}.",
                                 opening_entry=found)
        if facts["earlier_posting"]:
            return _not_eligible(ref, account, "books_before_statement",
                                 f"The books have entries for {account} from {facts['earlier_posting']}, before "
                                 f"{start.isoformat()}. The balance on that day comes from those entries, not from "
                                 "an opening balance.", first_posting=facts["earlier_posting"])

        result: JsonObject = {
            "statement_id": statement_id, "account": account, "date": start.isoformat(),
            "opening_balance": format(presented, "f"), "opening_source": source, "currency": currency,
            "source_id": source_id,
            "postings": [{"account": account, "amount": format(amount_ledger, "f")},
                         {"account": OPENING_EQUITY, "amount": format(-amount_ledger + Decimal("0.00"), "f")}],
            "books_start": facts["books_start"],
        }
        if facts["books_start"] and facts["books_start"] < start.isoformat():
            result["warnings"] = [
                f"Other accounts have entries from {facts['books_start']}, before {start.isoformat()}. A cash flow "
                f"statement that starts before {start.isoformat()} cannot use this opening balance."]
        if amount_ledger == 0:
            return {**result, "status": "nothing_to_record", "postings": []}
        if dry_run:
            return {**result, "status": "dry_run"}

        name = label or account.split(":")[-1].replace("-", " ")
        narration = (f"Opening balance from the {name} statement" if source == "printed"
                     else f"Opening balance for {name}, entered by a person")
        meta = [("source-id", source_id), ("import-source", IMPORT_SOURCE), ("opening-source", source),
                ("opening-balance", format(presented, "f"))]
        if statement_id:
            meta.append(("statement-id", statement_id))
        opens = [Open(date=start, account=account), Open(date=start, account=OPENING_EQUITY)]
        entry = Entry(
            date=start, narration=narration, meta=tuple(meta),
            postings=(Posting(account=account, amount=amount_ledger, currency=currency),
                      Posting(account=OPENING_EQUITY, amount=-amount_ledger, currency=currency)),
        )
        errors = validate(render_ledger(opens=opens, entries=[entry], balances=[], title="Opening balance",
                                        currency=currency))
        if errors:
            raise StatementOpeningError("invalid_entry", "; ".join(str(e) for e in errors))
        with store.transaction(immediate=True) as conn:
            if conn.execute("SELECT 1 FROM entries WHERE source_id = ? LIMIT 1", (source_id,)).fetchone():
                return {**result, "status": "already_recorded"}
            store.append_audit_event("intent", {"session_id": SESSION, "statement_id": statement_id,
                                                "opening_source": source,
                                                "description": f"record opening balance for {account}",
                                                "entries": 1}, conn)
            store.insert_opens(opens, conn)
            store.insert_entries([entry], conn)
            store.set_meta("canonical", "true", conn)
            store.append_audit_event("entry-written", {"session_id": SESSION, "source_id": source_id}, conn)
            store.append_audit_event("ledger-store-sealed", {"session_id": SESSION, "entries": 1,
                                                             "source_ids": [source_id],
                                                             "store_sha256": store.content_digest(conn)}, conn)
        return {**result, "status": "recorded"}
