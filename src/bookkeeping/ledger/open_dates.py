"""No posting is dated before its account's open date.

The account catalog (the ``accounts`` table, written by ``open`` directives) gives each
account an open date: the first day the books record activity for it. The books often
start an account with an opening balance on that day, and that balance may already
include the earlier activity. So a posting dated before the open date is refused; the
ledger validator also reports it as "used ... before open date".

The saved-rule path (``routine``) refuses such a row with ``source_catalog_conflict``
(the row's own bank or card account) or ``category_catalog_conflict`` (the category).
Every ledger write uses the same rule and the same codes through ``first_conflict``:
queue confirmation by a person or by standing automation, group confirmation, splits,
transfer pairs, and corrections that post a replacement entry.

The exact reversal of an entry in the books is not refused: it removes activity on the
day of the original, which is how a person takes out a posting that came in before this
rule. A ledger write cannot move an open date earlier (``OpenDateChangeError``). The
commands that set open dates do that: ``books entity account-add --open-date`` (it writes
the catalog directly, and it can only move a date earlier) and ``books accrual opening
apply`` (the open dates of its previewed plan). A confirmation never moves an open date.
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
        move = (f"move the open date earlier first with books entity account-add --entity PATH --account {account} "
                "--open-date YYYY-MM-DD")
        if code == SOURCE_CODE:
            self.action = (f"Check whether the books already include this activity, for example in an opening "
                           f"balance of {account}. If they do, do not record this row. If the books must record "
                           f"{account} before {open_date.isoformat()}, {move}.")
        else:
            self.action = f"Choose an account that is open on {entry_date.isoformat()}, or {move}."
        super().__init__(
            f"{row} is dated {entry_date.isoformat()}. {account} opens on {open_date.isoformat()} ({code}). "
            f"The books do not record activity for an account before its open date, because an opening balance "
            f"on that date may already include earlier activity. Nothing was recorded. The row stays in review. "
            f"{self.action}")

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "action": self.action, "account": self.account,
                "open_date": self.open_date.isoformat(), "entry_date": self.entry_date.isoformat(),
                "source_id": self.source_id}


class OpenDateChangeError(ValueError):
    """A write tried to move an account's open date earlier. Nothing was written."""

    code = "open_date_change_not_authorized"

    def __init__(self, account: str, open_date: date, requested: date) -> None:
        self.account, self.open_date, self.requested = account, open_date, requested
        self.action = (f"A ledger write does not change an open date. To move it earlier, run books entity "
                       f"account-add --entity PATH --account {account} --open-date {requested.isoformat()}. Check "
                       f"first whether an opening balance already includes the earlier activity.")
        super().__init__(f"{account} opens on {open_date.isoformat()}. This write tried to open it on "
                         f"{requested.isoformat()} ({self.code}). Nothing was recorded. {self.action}")

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "action": self.action, "account": self.account,
                "open_date": self.open_date.isoformat(), "requested_open_date": self.requested.isoformat()}


def merged_open_dates(existing: Mapping[str, date], new_opens: Sequence[Open] = ()) -> dict[str, date]:
    """Open dates after ``new_opens`` are inserted. The catalog keeps the earliest date of an account.

    The atomic ledger write refuses an earlier date for an account in the catalog before it uses this,
    unless the caller is an explicit open-date command.
    """
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
