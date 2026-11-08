"""A books start policy: from which day live postings may go into the books, for each account.

Three dates are different things:

* The open date of an account (the ``accounts`` catalog, ``ledger.open_dates``): the account exists from
  this day.
* An opening balance: one entry that summarizes the history before a day. The opening operations write it
  (``books qb sync-opening --apply``, ``books statement record-opening``).
* The authorized books start (this module): live sources may add postings to the account from this day.

A host (for example a hosted service) that keeps an authorized start for each account passes the policy to
a mutating command in the environment variable ``SLASHBOOKS_BOOKS_POLICY``. Its value is the path of a JSON
document::

    {"schema_version": 1, "mode": "enforced", "epoch": 4,
     "accounts": {"Assets:Bank:Checking": {"state": "authorized", "start_day": "2026-03-15", "version": 2},
                  "Liabilities:Card-1234": {"state": "unset"}}}

Without the variable, nothing changes. When the variable is set, the document must load: a missing or bad
document refuses every ledger write (``BOOKS_POLICY_REQUIRED``, ``BOOKS_POLICY_INVALID``). It never falls
back to the earlier behavior. ``mode: legacy`` turns the check off.

In ``enforced`` mode the atomic ledger write checks each posting line of each new entry on an account in
scope. The scope is every account that the policy names, every ledger account of a bank or CSV mapping
(``bank_account_mappings``, ``csv_account_mappings``) and the ledger account of each source row in the
write. Account names are never used to decide the scope. A transfer has two in-scope lines, so both sides
are checked. Account states:

* ``unset``: no start yet. Every live posting is refused (``BOOKS_START_UNSET``).
* ``authorized_pending_opening``: a start exists, but the opening that it needs is not recorded. Every live
  posting is refused, on the start day and after it too (``OPENING_REQUIRED``).
* ``authorized``: a posting before ``start_day`` is refused (``BOOKS_DATE_BEFORE_AUTHORIZED_START``); a
  posting on or after it is allowed under the usual rules.

An in-scope account that the policy does not name is refused (``BOOKS_POLICY_REQUIRED``).

The exact reversal of an entry in the books that is not reversed yet is not refused (the same predicate as
the open-date rule). It removes activity and adds none. An entry that only has ``reverses`` metadata is
checked. Entry metadata such as ``import-source`` or a source ID never makes an entry exempt.

This check inside a command is early feedback, not the fence. A session that runs the command controls the
variable and the file. The opening operations (``qb sync-opening --apply``, ``qb import-opening``,
``statement record-opening`` and ``accrual opening apply``) also write outside this check or under their own
rules. The fence is ``ledger.books_policy_check``: the host runs it on the proposed ledger change with its own
immutable policy, outside the session, before it publishes. The engine does not store the policy and does
not compare epochs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

ENV_VAR = "SLASHBOOKS_BOOKS_POLICY"
SCHEMA_VERSION = 1
MAX_BYTES = 1_000_000

UNSET = "unset"
PENDING_OPENING = "authorized_pending_opening"
AUTHORIZED = "authorized"
STATES = (UNSET, PENDING_OPENING, AUTHORIZED)

START_UNSET = "BOOKS_START_UNSET"
OPENING_REQUIRED = "OPENING_REQUIRED"
BEFORE_START = "BOOKS_DATE_BEFORE_AUTHORIZED_START"
POLICY_REQUIRED = "BOOKS_POLICY_REQUIRED"
POLICY_INVALID = "BOOKS_POLICY_INVALID"
CODES = (START_UNSET, OPENING_REQUIRED, BEFORE_START, POLICY_REQUIRED, POLICY_INVALID)


class BooksPolicyError(ValueError):
    """A write is outside the authorized books start, or the policy is missing or bad. Nothing was written."""

    def __init__(self, code: str, message: str, action: str, *, account: str | None = None,
                 state: str | None = None, start_day: date | None = None, entry_date: date | None = None,
                 source_id: str = "", policy_epoch: int | None = None, account_version: int | None = None) -> None:
        self.code = code
        self.action = action
        self.account = account
        self.state = state
        self.start_day = start_day
        self.entry_date = entry_date
        self.source_id = source_id
        self.policy_epoch = policy_epoch
        self.account_version = account_version
        super().__init__(f"{message} ({code}). Nothing was recorded. {action}")

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "action": self.action, "account": self.account,
                "state": self.state, "start_day": self.start_day.isoformat() if self.start_day else None,
                "entry_date": self.entry_date.isoformat() if self.entry_date else None,
                "source_id": self.source_id, "policy_epoch": self.policy_epoch,
                "account_version": self.account_version}


def _invalid(detail: str) -> BooksPolicyError:
    return BooksPolicyError(POLICY_INVALID, f"The books start policy is not valid: {detail}",
                            "The host must give a valid books start policy. The row stays in review.")


@dataclass(frozen=True)
class AccountStart:
    state: str
    start_day: date | None
    version: int | None


@dataclass(frozen=True)
class BooksPolicy:
    mode: str
    epoch: int
    accounts: Mapping[str, AccountStart]

    @property
    def enforced(self) -> bool:
        return self.mode == "enforced"

    def refusal(self, account: str, day: date, source_id: str = "") -> BooksPolicyError | None:
        """The refusal for one in-scope posting line on ``day``, or ``None`` when the line is allowed."""
        if not self.enforced:
            return None
        found = self.accounts.get(account)
        row = f"Row {source_id}" if source_id else "The entry"
        common = dict(account=account, entry_date=day, source_id=source_id, policy_epoch=self.epoch)
        if found is None:
            return BooksPolicyError(
                POLICY_REQUIRED, f"{row} posts to {account}, and the books start policy has no state for it",
                f"Add {account} to the books start policy. The row stays in review.", **common)
        common.update(state=found.state, start_day=found.start_day, account_version=found.version)
        if found.state == UNSET:
            return BooksPolicyError(
                START_UNSET, f"{row} is dated {day.isoformat()}. {account} has no books start",
                f"Choose when the books start for {account}. The row stays in review.", **common)
        assert found.start_day is not None
        start = found.start_day.isoformat()
        if found.state == PENDING_OPENING:
            return BooksPolicyError(
                OPENING_REQUIRED, f"{row} is dated {day.isoformat()}. The books for {account} start on {start}, "
                f"but the opening balance of {account} on {start} is not recorded",
                f"Record the opening balance of {account} on {start} with its opening operation first. "
                "The row stays in review.", **common)
        if day < found.start_day:
            return BooksPolicyError(
                BEFORE_START, f"{row} is dated {day.isoformat()}. The books for {account} start on {start}",
                f"A row before {start} is outside the books. The opening balance on {start} can include its "
                "activity. Do not record this row. The row stays in review.", **common)
        return None


def _day(value: Any, where: str) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise _invalid(f"{where} must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _invalid(f"{where} must be a YYYY-MM-DD date") from None


def _integer(value: Any, where: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _invalid(f"{where} must be an integer of {minimum} or more")
    return value


def parse_policy(document: Any) -> BooksPolicy:
    """Validate a decoded policy document. Unknown fields, states or modes are refused."""
    if not isinstance(document, dict):
        raise _invalid("the document must be a JSON object")
    unknown = set(document) - {"schema_version", "mode", "epoch", "accounts"}
    if unknown:
        raise _invalid(f"unknown field {sorted(unknown)[0]!r}")
    if document.get("schema_version") != SCHEMA_VERSION or isinstance(document.get("schema_version"), bool):
        raise _invalid(f"schema_version must be {SCHEMA_VERSION}")
    mode = document.get("mode")
    if mode not in ("legacy", "enforced"):
        raise _invalid("mode must be 'legacy' or 'enforced'")
    if "epoch" not in document:
        raise _invalid("epoch is required")
    epoch = _integer(document["epoch"], "epoch", 0)
    raw_accounts = document.get("accounts")
    if not isinstance(raw_accounts, dict):
        raise _invalid("accounts must be an object")
    accounts: dict[str, AccountStart] = {}
    for account, raw in raw_accounts.items():
        where = f"accounts[{account!r}]"
        if not account or not isinstance(raw, dict):
            raise _invalid(f"{where} must be an object")
        extra = set(raw) - {"state", "start_day", "version"}
        if extra:
            raise _invalid(f"unknown field {sorted(extra)[0]!r} in {where}")
        state = raw.get("state")
        if state not in STATES:
            raise _invalid(f"{where}.state must be one of {', '.join(STATES)}")
        if state == UNSET:
            if raw.get("start_day") is not None:
                raise _invalid(f"{where} is unset, so it has no start_day")
            version = None if raw.get("version") is None else _integer(raw["version"], f"{where}.version", 1)
            accounts[account] = AccountStart(state, None, version)
            continue
        accounts[account] = AccountStart(state, _day(raw.get("start_day"), f"{where}.start_day"),
                                         _integer(raw.get("version"), f"{where}.version", 1))
    return BooksPolicy(mode, epoch, accounts)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _invalid("a field appears twice")
    return dict(pairs)


def _reject_constant(name: str) -> Any:
    raise _invalid(f"{name} is not a JSON number")


def active_policy() -> BooksPolicy | None:
    """The policy of this run: ``None`` when ``SLASHBOOKS_BOOKS_POLICY`` is not set; else the loaded document.

    A set variable that does not lead to a valid document raises ``BooksPolicyError``. It never means
    "no policy".
    """
    value = os.environ.get(ENV_VAR)
    if value is None:
        return None
    path = Path(value)
    if not value or not path.is_file():
        raise BooksPolicyError(POLICY_REQUIRED, f"{ENV_VAR} is set, but no policy document is at {value!r}",
                               "The host must give the books start policy document. The row stays in review.")
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_BYTES:
            raise _invalid(f"the document is larger than {MAX_BYTES} bytes")
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates,
                              parse_constant=_reject_constant)
    except BooksPolicyError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _invalid(f"the document cannot be read as JSON ({type(exc).__name__})") from None
    return parse_policy(document)


def first_policy_conflict(policy: BooksPolicy | None, rows: Iterable[tuple[date, Iterable[str], str]],
                          scope: Iterable[str]) -> BooksPolicyError | None:
    """The first in-scope posting line that the policy refuses, or ``None``.

    ``rows`` are ``(day, accounts, source_id)`` for each entry, in write order. ``scope`` is the set of mapped
    and source-row accounts; every account that the policy names is in scope too.
    """
    if policy is None or not policy.enforced:
        return None
    in_scope = set(scope) | set(policy.accounts)
    for day, accounts, source_id in rows:
        for account in accounts:
            if account in in_scope:
                refusal = policy.refusal(account, day, source_id)
                if refusal is not None:
                    return refusal
    return None
