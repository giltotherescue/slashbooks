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
from datetime import date, timedelta
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


def live_openings(conn: Any, account: str | None = None) -> list[JsonObject]:
    """Opening entries this module wrote that are still in force, oldest first.

    An opening that a later entry reverses (``reverses`` metadata) is not in force, and the reversal is
    not an opening. ``account`` limits the list to one account.
    """
    rows = conn.execute(
        "SELECT e.source_id, e.date, e.narration, e.metadata_json, p.account, p.amount FROM entries e "
        "JOIN postings p ON p.entry_id = e.id WHERE e.metadata_json LIKE ? OR e.metadata_json LIKE ? "
        "ORDER BY e.date, e.id", (f"%{IMPORT_SOURCE}%", "%reverses%")).fetchall()
    reversed_ids = set()
    found = []
    for source_id, day, narration, raw, posted_account, posted in rows:
        meta = _metadata(raw)
        if "reverses" in meta:
            reversed_ids.add(meta["reverses"])
            continue
        if meta.get("import-source") != IMPORT_SOURCE or posted_account == OPENING_EQUITY:
            continue
        if account is not None and posted_account != account:
            continue
        found.append({"source_id": source_id, "date": day, "account": posted_account, "narration": narration,
                      "statement_id": meta.get("statement-id"),
                      "opening_source": meta.get("opening-source", "printed"),
                      "amount": format(Decimal(str(posted)).quantize(Decimal("0.01")), "f")})
    return [item for item in found if item["source_id"] not in reversed_ids]


def opening_dates(entity_path: Path | str) -> dict[str, date]:
    """The day of each account's opening entry in force (read-only; empty without a ledger)."""
    from .ledger.store import LedgerStore, default_store_path

    path = default_store_path(Path(entity_path))
    if not path.is_file():
        return {}
    with LedgerStore(path).connection() as conn:
        return {item["account"]: date.fromisoformat(item["date"]) for item in reversed(live_openings(conn))}


def before_opening_refusal(entity_path: Path | str, account: str, days: list[str],
                           openings: dict[str, date] | None = None, **detail: Any) -> JsonObject | None:
    """The refusal for rows dated before the account's opening balance, or ``None`` when every row is on or after it."""
    opening = (opening_dates(entity_path) if openings is None else openings).get(account)
    if opening is None:
        return None
    early = sorted(day for day in days if day < opening.isoformat())
    if not early:
        return None
    return {
        "code": "before_opening_balance",
        "message": (f"{account} has an opening balance on {opening.isoformat()}. {len(early)} row(s) are dated before "
                    f"it, from {early[0]}. The opening balance already includes them, so they would count twice. "
                    "Nothing was imported."),
        "action": (f"To add the earlier history, move the opening balance to the first day of the earlier statement: "
                   f"books statement record-opening --entity PATH --account {account} --on YYYY-MM-DD "
                   "--amount AMOUNT --replace, then import again."),
        "account": account, "opening_date": opening.isoformat(), "first_row_date": early[0],
        "rows_before_opening": len(early), **detail,
    }


def _books_facts(store: Any, account: str, start: date) -> JsonObject:
    """What the books already hold for the account: earlier postings, its opening entry, the first entry day."""
    with store.connection() as conn:
        earlier = conn.execute(
            "SELECT MIN(e.date) FROM postings p JOIN entries e ON e.id = p.entry_id "
            "WHERE p.account = ? AND e.date < ?", (account, start.isoformat())).fetchone()[0]
        openings = live_openings(conn, account)
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
                   on: str | None = None, amount: str | None = None, replace: bool = False,
                   dry_run: bool = False) -> JsonObject:
    """Record (or, with ``dry_run``, show) an account's opening entry.

    Three forms:

    * ``statement_id``: the printed opening balance of that saved statement (``opening_source: printed``).
    * ``statement_id`` and ``amount``: a person's amount for a saved statement that has no printed opening
      balance, dated on its first day (``opening_source: person``). A printed opening balance wins, so the
      amount is refused when the statement has one.
    * ``account``, ``on`` and ``amount``: a person's amount for the account on the first day the books cover
      for it (``opening_source: person``), with no saved statement.

    ``amount`` uses the statement presentation: money held for a bank account, money owed for a card or loan.

    With ``replace`` the account's opening entry in force is reversed and the new one is recorded on the same
    or an earlier day, in one write. The usual rules apply to the new day, so nothing else for the account
    may be dated before it.
    """
    from . import coverage
    from .entity import load_entity
    from .ledger.importer import _entity_write_lock
    from .ledger.model import Entry, Open, Posting, _validate_account_name
    from .ledger.store import LedgerStore, default_store_path
    from .ledger.validator import validate
    from .ledger.writer import render_ledger
    from .tieout import load_waiting, presentation_sign

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

        facts = _books_facts(store, account, start)
        base_id = (f"{IMPORT_SOURCE}-{statement_id}" if statement_id else
                   f"{IMPORT_SOURCE}-{start.isoformat()}-{hashlib.sha256(account.encode()).hexdigest()[:12]}")
        replaced = None
        if facts["openings"]:
            found = facts["openings"][0]
            if found["date"] == start.isoformat() and found["amount"] == format(amount_ledger, "f") \
                    and found["opening_source"] == source:
                return {"status": "already_recorded", "statement_id": statement_id, "account": account,
                        "date": found["date"], "source_id": found["source_id"], "opening_source": found["opening_source"]}
            if not replace:
                return _not_eligible(ref, account, "opening_exists",
                                     f"{account} already has an opening balance entry dated {found['date']}. "
                                     "To move it to an earlier day or change it, run the command again with --replace.",
                                     opening_entry=found)
            if start.isoformat() > found["date"]:
                return _not_eligible(ref, account, "replace_not_earlier",
                                     f"The opening balance can move only to {found['date']} or earlier. Entries "
                                     f"from {found['date']} already count from the current opening balance.",
                                     opening_entry=found)
            replaced = found
        elif replace:
            return _not_eligible(ref, account, "no_opening_to_replace",
                                 f"{account} has no opening balance entry to replace. Run the command without --replace.")
        earlier_statements = sorted(
            r.period_start.isoformat() for r in coverage.load_statements(entity_path)[0]
            if r.kind == "statement" and r.account == account and r.statement_id != statement_id
            and r.period_start < start)
        if earlier_statements:
            return _not_eligible(ref, account, "not_first_statement",
                                 f"A statement for {account} starts earlier, on {earlier_statements[0]}. "
                                 "The opening balance belongs to the first day the books cover for the account.",
                                 first_statement_start=earlier_statements[0])

        if facts["earlier_posting"]:
            return _not_eligible(ref, account, "books_before_statement",
                                 f"The books have entries for {account} from {facts['earlier_posting']}, before "
                                 f"{start.isoformat()}. The balance on that day comes from those entries, not from "
                                 "an opening balance.", first_posting=facts["earlier_posting"])
        waiting = load_waiting(entity, [account])[account]
        if waiting.count_between(None, start - timedelta(days=1)):
            return _not_eligible(ref, account, "rows_waiting_before",
                                 f"Rows for {account} dated before {start.isoformat()} wait in review. The balance on "
                                 "that day comes from those rows, not from an opening balance.")
        source_id = base_id

        result: JsonObject = {
            "statement_id": statement_id, "account": account, "date": start.isoformat(),
            "opening_balance": format(presented, "f"), "opening_source": source, "currency": currency,
            "source_id": source_id,
            "postings": [{"account": account, "amount": format(amount_ledger, "f")},
                         {"account": OPENING_EQUITY, "amount": format(-amount_ledger + Decimal("0.00"), "f")}],
            "books_start": facts["books_start"],
            **({"replaces": replaced} if replaced else {}),
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
            taken = {row[0] for row in conn.execute("SELECT source_id FROM entries WHERE source_id LIKE ?",
                                                     (f"{base_id}%",))}
            number = 1
            while source_id in taken:
                number += 1
                source_id = f"{base_id}-{number}"
            if source_id != base_id:
                meta[0] = ("source-id", source_id)
                entry = Entry(date=entry.date, narration=entry.narration, meta=tuple(meta), postings=entry.postings)
                result.update(source_id=source_id)
            written = [entry]
            if replaced:
                old_meta = [(k, v) for k, v in json.loads(conn.execute(
                    "SELECT metadata_json FROM entries WHERE source_id = ?", (replaced["source_id"],)).fetchone()[0])
                            if k not in ("source-id", "import-session")]
                old_postings = conn.execute(
                    "SELECT p.account, p.amount, p.currency FROM postings p JOIN entries e ON e.id = p.entry_id "
                    "WHERE e.source_id = ? ORDER BY p.id", (replaced["source_id"],)).fetchall()
                written.insert(0, Entry(
                    date=date.fromisoformat(replaced["date"]), narration=f"Reversal of: {replaced['narration']}",
                    meta=tuple(old_meta) + (("reverses", replaced["source_id"]), ("import-session", SESSION)),
                    postings=tuple(Posting(account=a, amount=-Decimal(str(v)), currency=c) for a, v, c in old_postings)))
            store.append_audit_event("intent", {"session_id": SESSION, "statement_id": statement_id,
                                                "opening_source": source,
                                                "replaces": replaced["source_id"] if replaced else None,
                                                "description": f"record opening balance for {account}",
                                                "entries": len(written)}, conn)
            store.insert_opens(opens, conn)
            store.insert_entries(written, conn)
            store.set_meta("canonical", "true", conn)
            for item in written:
                store.append_audit_event("entry-written", {"session_id": SESSION,
                                                           "source_id": item.source_id or replaced["source_id"]}, conn)
            store.append_audit_event("ledger-store-sealed", {"session_id": SESSION, "entries": len(written),
                                                             "source_ids": [source_id],
                                                             "store_sha256": store.content_digest(conn)}, conn)
        return {**result, "status": "replaced" if replaced else "recorded"}
