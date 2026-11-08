"""No posting is dated before its account's open date.

The account catalog (the ``accounts`` table, written by ``open`` directives) gives each
account an open date: the first day the books cover for it. An opening balance on that
day (for example a QuickBooks opening or a statement opening) already contains the
activity before it. A posting dated before the open date would count that activity
twice, and the ledger validator reports it as "used ... before open date".

The saved-rule path (``routine``) refuses such a row with ``source_catalog_conflict``
(the row's own bank or card account) or ``category_catalog_conflict`` (the category).
Every ledger write uses the same rule and the same codes through ``first_conflict``:
queue confirmation by a person or by standing automation, group confirmation, splits,
transfer pairs, and corrections that post a replacement entry.

A reversal of an entry already in the books is not refused: it removes activity on
the day of the original, which is how a person takes out a posting that came in
before this rule. A legitimate change of an account's open date is a different,
explicit command (``books entity account-add --open-date``, or ``books statement
record-opening --replace`` for a statement opening); a confirmation never moves it.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from .model import Entry, Open

SOURCE_CODE = "source_catalog_conflict"
CATEGORY_CODE = "category_catalog_conflict"


class AccountNotOpenError(ValueError):
    """A posting is dated before its account's open date. Nothing was written."""

    def __init__(self, code: str, account: str, open_date: date, entry_date: date, source_id: str = "") -> None:
        self.code = code
        self.account = account
        self.open_date = open_date
        self.entry_date = entry_date
        self.source_id = source_id
        row = f"Row {source_id}" if source_id else "The entry"
        move = (f"a person moves the open date first: books entity account-add --entity PATH --account {account} "
                "--open-date YYYY-MM-DD")
        if code == SOURCE_CODE:
            why = "The opening balance on that day already includes earlier activity, so the row would count twice."
            self.action = (f"If the opening balance of {account} already includes this activity, do not record it. "
                           f"If the books must start earlier, {move} (check the opening balance before you do).")
        else:
            why = "The books do not use the account before that day."
            self.action = f"Choose an account that is open on {entry_date.isoformat()}, or {move}."
        super().__init__(
            f"{row} is dated {entry_date.isoformat()}, before {account} opens on {open_date.isoformat()} "
            f"({code}). {why} Nothing was recorded. The row stays in review. {self.action}")

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "action": self.action, "account": self.account,
                "open_date": self.open_date.isoformat(), "entry_date": self.entry_date.isoformat(),
                "source_id": self.source_id}


def merged_open_dates(existing: Mapping[str, date], new_opens: Sequence[Open] = ()) -> dict[str, date]:
    """Open dates after ``new_opens`` are inserted. The catalog keeps the earliest date of an account."""
    merged = dict(existing)
    for item in new_opens:
        if item.account not in merged or item.date < merged[item.account]:
            merged[item.account] = item.date
    return merged


def catalog_open_dates(conn: Any) -> dict[str, date]:
    """Each account's open date from the catalog, read on ``conn``."""
    return {row[0]: date.fromisoformat(str(row[1])[:10]) for row in conn.execute("SELECT name, open_date FROM accounts")}


def opened_after(open_dates: Mapping[str, date], account: str, day: date) -> date | None:
    """The open date of ``account`` when it is after ``day``, else ``None``. An account not in the catalog is not judged here."""
    opened = open_dates.get(account)
    return opened if opened is not None and opened > day else None


def first_conflict(entries: Iterable[Entry], open_dates: Mapping[str, date], source_accounts: Iterable[str] = (),
                   is_reversal: Any = None) -> AccountNotOpenError | None:
    """The first posting dated before its account's open date, as an error; ``None`` when every posting is valid.

    ``source_accounts`` are the bank or card accounts of the rows (``source_catalog_conflict``); every other
    account is a category (``category_catalog_conflict``). ``is_reversal(entry)`` returns True for a reversal
    of an entry already in the books; such an entry is not refused.
    """
    return first_posting_conflict(
        ((entry.date, [posting.account for posting in entry.postings], entry.source_id or "")
         for entry in entries if is_reversal is None or not is_reversal(entry)),
        open_dates, source_accounts)


def first_posting_conflict(rows: Iterable[tuple[date, Iterable[str], str]], open_dates: Mapping[str, date],
                           source_accounts: Iterable[str] = ()) -> AccountNotOpenError | None:
    """``first_conflict`` for planned postings: ``(day, accounts, source_id)`` for each entry, before it is built."""
    sources = set(source_accounts)
    for day, accounts, source_id in rows:
        # The source account first, so the reason names the row's own account when both are early.
        for account in sorted(accounts, key=lambda name: name not in sources):
            opened = opened_after(open_dates, account, day)
            if opened is not None:
                code = SOURCE_CODE if account in sources else CATEGORY_CODE
                return AccountNotOpenError(code, account, opened, day, source_id)
    return None
