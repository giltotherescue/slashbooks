"""The opening balance of each account of an opening entry, in the presentation sign.

A host shows the opening balance of each account after a person records an opening. The host does no
accounting math, so it takes the amount and its sign from this module.

:func:`opening_presentation` is pure. It reads no file, no environment variable and no clock, writes nothing,
and does not change its arguments. It takes:

- ``entry``: the opening entry. Either an ``Entry`` or the entry document that the engine prints
  (``proposed_entry`` of an opening preview, or ``entry`` of ``recorded_opening``). A document is read with the
  strict reader ``source_disposition.entry_from_document``. The entry must come from a statement opening, a
  QuickBooks opening, or an accrual opening (``import-source`` ``statement-opening``, ``quickbooks-opening``,
  ``accrual-opening`` or ``accrual-tracking-opening``). It must not be a reversal, and it must post only to
  Assets, Liabilities and Equity accounts.
- ``accounts``: the names of the accounts to show. They are unique and sorted by their UTF-8 bytes. They must
  be exactly the Assets and Liabilities accounts that the entry posts to. Equity accounts are not in the list.
- ``financial_kinds``: the explicit financial kind of each catalog account, as
  ``LedgerStore.load_financial_kinds`` returns it. An account without a key is unclassified.

It returns one row for each name in ``accounts``, in the same order. A row is presented or refused:

    {"account": "Liabilities:CreditCard:Visa", "status": "presented", "financial_kind": "credit_card",
     "amount": "2000.25", "currency": "USD"}
    {"account": "Liabilities:Other:Loan", "status": "refused", "reason": "financial_kind_missing",
     "financial_kind": null, "message": "..."}

``amount`` is the sum of the ledger amounts of the account's lines, multiplied by ``tieout.presentation_sign``
(money held for an asset account, money owed for a liability account), with exactly two decimals, no
thousands separator, a leading ``-`` when negative, and ``0.00`` for zero. ``currency`` is the currency of the
lines, as in the entry. For a statement opening, ``amount`` equals the ``opening-balance`` metadata exactly.

The supported financial kinds are ``ledger.store.FINANCIAL_KINDS``, on the roots that
``ledger.store.validate_financial_kind`` accepts: ``bank`` and ``cash`` on Assets, ``credit_card`` on
Liabilities, and ``other`` on Assets or Liabilities (for example a receivable, a payable or a loan).

A refused row has one reason (the first that applies, in this order):

- ``financial_kind_missing``: the account has no financial kind.
- ``financial_kind_unsupported``: the kind is not one of the supported kinds.
- ``financial_kind_mismatch``: the kind is not allowed on the account's root.
- ``mixed_currency``: the account's lines are in more than one currency, so no single amount exists.
- ``opening_balance_differs``: a statement opening whose ``opening-balance`` metadata is not the amount.

Errors for the whole call raise :class:`OpeningPresentationError` with a ``code``: ``invalid_entry``,
``not_an_opening``, ``invalid_accounts``, ``invalid_financial_kinds`` or ``accounts_mismatch`` (with
``not_posted`` and ``not_listed``, each sorted by UTF-8 bytes, in ``detail``). No row is returned for an account that is not in the list.

``python -I -m bookkeeping.opening_presentation show --request R.json --entity DIR`` runs the same check.
``R.json`` is ``{"schema_version": 1, "entry": <entry document>, "accounts": [...]}``. The command reads the
financial kinds from the ledger of ``DIR`` read-only (no ledger means no kinds). It prints
``{"status": "ok" | "refused", "accounts": [<rows>]}`` and exits with 0 when every row is presented, or 1 when
a row is refused. For a bad request it prints ``{"status": "error", "error": {"code": ..., "message": ...}}``
and exits with 2.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, TypedDict, Union

from .ledger.model import Entry
from .ledger.store import FINANCIAL_KINDS, LedgerStore, default_store_path, validate_financial_kind
from .quickbooks import OTHER_OPENING_SOURCES
from .source_disposition import DispositionInputError, entry_from_document
from .statement_opening import IMPORT_SOURCE as STATEMENT_OPENING_SOURCE
from .tieout import presentation_sign

REQUEST_SCHEMA_VERSION = 1
OPENING_IMPORT_SOURCES = (STATEMENT_OPENING_SOURCE, *OTHER_OPENING_SOURCES)
BALANCE_ROOTS = ("Assets", "Liabilities")

RefusalReason = Literal["financial_kind_missing", "financial_kind_unsupported", "financial_kind_mismatch",
                        "mixed_currency", "opening_balance_differs"]
REFUSAL_REASONS: tuple[str, ...] = RefusalReason.__args__  # type: ignore[attr-defined]
ErrorCode = Literal["invalid_entry", "not_an_opening", "invalid_accounts", "invalid_financial_kinds",
                    "accounts_mismatch", "invalid_request"]


class PresentedAccount(TypedDict):
    account: str
    status: Literal["presented"]
    financial_kind: str
    amount: str
    currency: str


class RefusedAccount(TypedDict):
    account: str
    status: Literal["refused"]
    reason: RefusalReason
    financial_kind: str | None
    message: str


AccountRow = Union[PresentedAccount, RefusedAccount]


class OpeningPresentationError(ValueError):
    """The whole call is refused. ``code`` is one of ``ErrorCode``; ``detail`` holds the facts."""

    def __init__(self, code: ErrorCode, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.detail}


def _entry(value: Entry | Mapping[str, Any]) -> Entry:
    if isinstance(value, Entry):
        return value
    try:
        return entry_from_document(value)
    except DispositionInputError as exc:
        raise OpeningPresentationError("invalid_entry", f"The entry is not a valid entry document: {exc}") from None


def _check_opening(entry: Entry) -> None:
    meta = dict(entry.meta)
    if meta.get("import-source") not in OPENING_IMPORT_SOURCES or "reverses" in meta:
        raise OpeningPresentationError(
            "not_an_opening", "The entry is not a statement, QuickBooks or accrual opening entry.",
            import_source=meta.get("import-source"))
    others = sorted({p.account for p in entry.postings
                     if p.account.partition(":")[0] not in (*BALANCE_ROOTS, "Equity")})
    if others:
        raise OpeningPresentationError(
            "not_an_opening", "An opening entry posts only to Assets, Liabilities and Equity accounts.",
            accounts=others)


def _check_accounts(accounts: Any) -> list[str]:
    if isinstance(accounts, (str, bytes)) or not isinstance(accounts, Sequence) \
            or not all(isinstance(name, str) for name in accounts):
        raise OpeningPresentationError("invalid_accounts", "accounts must be a list of account names.")
    names = list(accounts)
    encoded = [name.encode("utf-8") for name in names]
    if len(set(names)) != len(names) or encoded != sorted(encoded):
        raise OpeningPresentationError("invalid_accounts",
                                       "accounts must be unique and sorted by their UTF-8 bytes.")
    return names


def _refused(account: str, reason: RefusalReason, kind: str | None, message: str) -> RefusedAccount:
    return {"account": account, "status": "refused", "reason": reason, "financial_kind": kind, "message": message}


def _row(entry: Entry, account: str, raw_kind: Any) -> AccountRow:
    if raw_kind is None:
        return _refused(account, "financial_kind_missing", None,
                        f"{account} has no financial kind. Review the account's financial kind first.")
    kind = raw_kind if isinstance(raw_kind, str) else None
    if kind not in FINANCIAL_KINDS:
        return _refused(account, "financial_kind_unsupported", kind,
                        f"The financial kind of {account} is not one of {', '.join(FINANCIAL_KINDS)}.")
    try:
        validate_financial_kind(account, kind)
    except ValueError:
        return _refused(account, "financial_kind_mismatch", kind,
                        f"The financial kind {kind} is not allowed for {account}.")
    lines = [p for p in entry.postings if p.account == account]
    currencies = sorted({p.currency for p in lines})
    if len(currencies) != 1:
        return _refused(account, "mixed_currency", kind,
                        f"The lines of {account} are in more than one currency: {', '.join(currencies)}.")
    ledger_total = sum((p.amount for p in lines), Decimal("0"))
    presented = (ledger_total * presentation_sign(account)).quantize(Decimal("0.01")) + Decimal("0.00")
    meta = dict(entry.meta)
    if meta.get("import-source") == STATEMENT_OPENING_SOURCE and "opening-balance" in meta:
        try:
            recorded = Decimal(meta["opening-balance"])
        except (InvalidOperation, ValueError):
            recorded = None
        if recorded is None or not recorded.is_finite() or recorded != presented:
            return _refused(account, "opening_balance_differs", kind,
                            f"The opening-balance metadata of the entry is not the amount of {account}.")
    return {"account": account, "status": "presented", "financial_kind": kind, "amount": format(presented, "f"),
            "currency": currencies[0]}


def opening_presentation(entry: Entry | Mapping[str, Any], accounts: Sequence[str],
                         financial_kinds: Mapping[str, str]) -> list[AccountRow]:
    """One presented or refused row for each name in ``accounts``, in order. See the module documentation."""
    parsed = _entry(entry)
    _check_opening(parsed)
    names = _check_accounts(accounts)
    if not isinstance(financial_kinds, Mapping):
        raise OpeningPresentationError("invalid_financial_kinds",
                                       "financial_kinds must map an account name to its financial kind.")
    posted = list(dict.fromkeys(p.account for p in parsed.postings if p.account.partition(":")[0] in BALANCE_ROOTS))
    not_posted = [name for name in names if name not in posted]
    not_listed = sorted((name for name in posted if name not in names), key=lambda name: name.encode("utf-8"))
    if not_posted or not_listed:
        raise OpeningPresentationError(
            "accounts_mismatch", "accounts must be exactly the Assets and Liabilities accounts that the entry "
            "posts to.", not_posted=not_posted, not_listed=not_listed)
    return [_row(parsed, name, financial_kinds.get(name)) for name in names]


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def _read_kinds(entity: Path) -> dict[str, str]:
    """The explicit financial kinds of the entity's ledger, read-only. No ledger means no kinds."""
    path = default_store_path(entity)
    if not path.is_file():
        return {}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return LedgerStore(path).load_financial_kinds(conn)
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -I -m bookkeeping.opening_presentation")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser(
        "show", help="Show the opening balance of each account of an opening entry (read-only).",
        description="Show the opening balance of each account of an opening entry (read-only). The entry document "
                    "is presentation input only. It is not proof that the entry is committed or current in the "
                    "ledger. A caller that needs that proof must check it separately, for example with a trusted "
                    "verification of the live ledger entry.")
    show.add_argument("--request", type=Path, required=True)
    show.add_argument("--entity", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        try:
            request = json.loads(args.request.read_text(encoding="utf-8"), parse_constant=_reject_constant)
        except (OSError, ValueError) as exc:
            raise OpeningPresentationError("invalid_request", f"The request is not readable JSON: {exc}") from None
        if not isinstance(request, dict) or request.get("schema_version") != REQUEST_SCHEMA_VERSION \
                or set(request) != {"schema_version", "entry", "accounts"}:
            raise OpeningPresentationError(
                "invalid_request", "The request must be exactly {schema_version: 1, entry, accounts}.")
        rows = opening_presentation(request["entry"], request["accounts"], _read_kinds(args.entity))
    except OpeningPresentationError as exc:
        print(json.dumps({"status": "error", "error": exc.as_dict()}, sort_keys=True))
        return 2
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "error": {"code": "invalid_request",
                                                       "message": f"{type(exc).__name__}: {exc}"}}, sort_keys=True))
        return 2
    refused = any(row["status"] == "refused" for row in rows)
    print(json.dumps({"status": "refused" if refused else "ok", "accounts": rows}, sort_keys=True))
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
