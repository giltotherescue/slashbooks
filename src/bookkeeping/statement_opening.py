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

The entry is recorded only when it is the account's opening:

* the statement is saved with a confirmed period and an opening balance, in
  the company currency;
* no other statement for the account starts earlier;
* nothing in the books for the account is dated before the statement starts;
* the account has no opening entry yet.

Anything else returns ``not_eligible`` with the reason, and writes nothing.
"""
from __future__ import annotations

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
        for source_id, day, raw in conn.execute(
                "SELECT DISTINCT e.source_id, e.date, e.metadata_json FROM postings p JOIN entries e ON e.id = p.entry_id "
                "WHERE p.account = ?", (account,)):
            meta = _metadata(raw)
            if meta.get("import-source") == IMPORT_SOURCE:
                openings.append({"source_id": source_id, "date": day, "statement_id": meta.get("statement-id")})
        first_entry = conn.execute("SELECT MIN(date) FROM entries").fetchone()[0]
    return {"earlier_posting": earlier, "openings": openings, "books_start": first_entry}


def record_opening(entity_path: Path | str, statement_id: str, *, dry_run: bool = False) -> JsonObject:
    """Record (or, with ``dry_run``, show) the opening entry from one saved statement."""
    from . import coverage
    from .entity import load_entity
    from .ledger.importer import _entity_write_lock
    from .ledger.model import Entry, Open, Posting
    from .ledger.store import LedgerStore, default_store_path
    from .ledger.validator import validate
    from .ledger.writer import render_ledger
    from .tieout import presentation_sign

    entity_path = Path(entity_path)
    with _entity_write_lock(entity_path, repair=False):
        entity = load_entity(entity_path)
        record = _find_record(entity_path, statement_id)
        account = str(record.get("account") or "")
        if record.get("kind", "statement") != "statement":
            return _not_eligible(statement_id, account, "not_a_statement",
                                 "Only a bank or card statement has a printed opening balance.")
        if record.get("coverage_basis") == "transactions" or record.get("period_confirmed") is False:
            return _not_eligible(statement_id, account, "period_unconfirmed",
                                 "The statement period is not confirmed from the printed statement.")
        if record.get("opening_balance") in (None, ""):
            return _not_eligible(statement_id, account, "no_opening_balance",
                                 "The saved statement has no opening balance.")
        currency = str(record.get("currency") or entity.currency)
        if currency != entity.currency:
            return _not_eligible(statement_id, account, "currency_differs",
                                 f"The statement is in {currency} and the books are in {entity.currency}.")
        start = date.fromisoformat(str(record["period_start"]))
        presented = Decimal(str(record["opening_balance"]))
        amount = (presented * presentation_sign(account)).quantize(Decimal("0.01"))

        earlier_statements = sorted(
            r.period_start.isoformat() for r in coverage.load_statements(entity_path)[0]
            if r.kind == "statement" and r.account == account and r.statement_id != statement_id
            and r.period_start < start)
        if earlier_statements:
            return _not_eligible(statement_id, account, "not_first_statement",
                                 f"Another statement for {account} starts earlier, on {earlier_statements[0]}. "
                                 "The opening balance belongs to the first statement.",
                                 first_statement_start=earlier_statements[0])

        store = LedgerStore(default_store_path(entity_path))
        store.initialize()
        facts = _books_facts(store, account, start)
        if facts["openings"]:
            found = facts["openings"][0]
            if found["statement_id"] == statement_id:
                return {"status": "already_recorded", "statement_id": statement_id, "account": account,
                        "date": found["date"], "source_id": found["source_id"]}
            return _not_eligible(statement_id, account, "opening_exists",
                                 f"{account} already has an opening balance entry dated {found['date']}.",
                                 opening_entry=found)
        if facts["earlier_posting"]:
            return _not_eligible(statement_id, account, "books_before_statement",
                                 f"The books have entries for {account} from {facts['earlier_posting']}, before the "
                                 f"statement starts on {start.isoformat()}. The balance on that day comes from those "
                                 "entries, not from an opening balance.",
                                 first_posting=facts["earlier_posting"])

        source_id = f"{IMPORT_SOURCE}-{statement_id}"
        result: JsonObject = {
            "statement_id": statement_id, "account": account, "date": start.isoformat(),
            "opening_balance": format(presented, "f"), "balance_source": record.get("balance_source"),
            "currency": currency, "source_id": source_id,
            "postings": [{"account": account, "amount": format(amount, "f")},
                         {"account": OPENING_EQUITY, "amount": format(-amount + Decimal("0.00"), "f")}],
            "books_start": facts["books_start"],
        }
        if facts["books_start"] and facts["books_start"] < start.isoformat():
            result["warnings"] = [
                f"Other accounts have entries from {facts['books_start']}, before this statement starts on "
                f"{start.isoformat()}. A cash flow statement that starts before {start.isoformat()} cannot use "
                "this opening balance."]
        if amount == 0:
            return {**result, "status": "nothing_to_record", "postings": []}
        if dry_run:
            return {**result, "status": "dry_run"}

        label = str(record.get("source") or account.split(":")[-1])
        opens = [Open(date=start, account=account), Open(date=start, account=OPENING_EQUITY)]
        entry = Entry(
            date=start,
            narration=f"Opening balance from the {label} statement",
            meta=(("source-id", source_id), ("import-source", IMPORT_SOURCE), ("statement-id", statement_id),
                  ("opening-balance", format(presented, "f"))),
            postings=(Posting(account=account, amount=amount, currency=currency),
                      Posting(account=OPENING_EQUITY, amount=-amount, currency=currency)),
        )
        errors = validate(render_ledger(opens=opens, entries=[entry], balances=[], title="Opening balance",
                                        currency=currency))
        if errors:
            raise StatementOpeningError("invalid_entry", "; ".join(str(e) for e in errors))
        with store.transaction(immediate=True) as conn:
            if conn.execute("SELECT 1 FROM entries WHERE source_id = ? LIMIT 1", (source_id,)).fetchone():
                return {**result, "status": "already_recorded"}
            store.append_audit_event("intent", {"session_id": SESSION, "statement_id": statement_id,
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
