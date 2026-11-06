"""Account register: every posting line of an account in ledger order, with a running balance.

The register is the view people know from an accounting program. Each line is one posting to the account. It
shows the entry, the date, the amount, the other side of the entry (the split), and the balance after the line.

Everything here is read-only, deterministic and dependency-free. Sums are exact ``Decimal`` values, with one
running balance for each currency. A host (the CLI or a service that builds a list) calls these functions and
never adds the amounts itself.

Order
-----
Lines are in date order. Lines on the same date keep the order of the entries given. The ledger store gives
entries by date, then id, so that is the order of a day. Lines of one entry keep their posting order.

Sign
----
``presentation="ledger"`` keeps the ledger sign: a debit balance is positive. ``presentation="natural"`` shows a
balance the way people read it. Assets and expenses are positive when they are debit balances. Liabilities,
equity and income are positive when they are credit balances, so a card that is owed shows as a positive
balance. ``natural_sign`` is the one place that decides this. It differs from ``tieout.presentation_sign``,
which serves statement imports and only flips liabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

LEDGER = "ledger"
NATURAL = "natural"
PRESENTATIONS = (LEDGER, NATURAL)
# The split of a line whose entry has other lines on more than one account.
SPLIT_MARKER = "-Split-"

_CREDIT_ROOTS = ("Liabilities", "Equity", "Income")
_ZERO = Decimal("0")


@dataclass(frozen=True)
class RegisterPosting:
    """One line of an entry: the account, the amount in the ledger sign, and the currency."""

    account: str
    amount: Decimal
    currency: str = "USD"


@dataclass(frozen=True)
class RegisterEntry:
    """What the register needs from an entry. ``key`` is the id the host uses for the entry."""

    key: str
    date: date
    description: str
    postings: tuple[RegisterPosting, ...]

    @classmethod
    def from_entry(cls, key: Any, entry: Any) -> "RegisterEntry":
        """Build from a ledger ``Entry``. The description is the payee and the narration, as the journal shows them."""
        description = " ".join(part for part in (entry.payee, entry.narration) if part)
        return cls(str(key), entry.date, description,
                   tuple(RegisterPosting(p.account, p.amount, p.currency) for p in entry.postings))


@dataclass(frozen=True)
class RegisterLine:
    """One posting line. ``amount`` is in the ledger sign. ``change`` and ``balance`` are in the chosen presentation."""

    entry_key: str
    posting_index: int
    date: date
    description: str
    account: str
    currency: str
    amount: Decimal
    change: Decimal
    split: str
    balance: Decimal

    def to_dict(self) -> dict[str, str | int]:
        return {"entry_id": self.entry_key, "posting_index": self.posting_index, "date": self.date.isoformat(),
                "description": self.description, "account": self.account, "currency": self.currency,
                "amount": _fmt(self.amount), "change": _fmt(self.change), "split": self.split,
                "balance": _fmt(self.balance)}


@dataclass(frozen=True)
class LineBalance:
    """The running balance after one posting of an entry, for a host that builds a list of entries."""

    posting_index: int
    currency: str
    balance: Decimal


@dataclass(frozen=True)
class Register:
    """The register of one account. ``opening`` is the balance of each currency before the first line shown."""

    account: str
    presentation: str
    from_date: Optional[date]
    to_date: Optional[date]
    opening: Mapping[str, Decimal]
    lines: tuple[RegisterLine, ...]
    closing: Mapping[str, Decimal]

    def to_dict(self) -> dict[str, Any]:
        return {"account": self.account, "presentation": self.presentation,
                "from": self.from_date.isoformat() if self.from_date else None,
                "to": self.to_date.isoformat() if self.to_date else None,
                "opening": {c: _fmt(v) for c, v in sorted(self.opening.items())},
                "lines": [line.to_dict() for line in self.lines],
                "closing": {c: _fmt(v) for c, v in sorted(self.closing.items())}}


def _fmt(value: Decimal) -> str:
    return format(value + _ZERO, "f")


def _present(value: Decimal, sign: int) -> Decimal:
    return value * sign + _ZERO  # adding zero turns -0.00 into 0.00


def natural_sign(account: str) -> int:
    """-1 for a liability, equity or income account, else 1. Multiply a ledger value by it to present it."""
    return -1 if account.split(":", 1)[0] in _CREDIT_ROOTS else 1


def _sign(account: str, presentation: str) -> int:
    if presentation not in PRESENTATIONS:
        raise ValueError(f"presentation must be one of {PRESENTATIONS}, got {presentation!r}")
    return natural_sign(account) if presentation == NATURAL else 1


def _split(postings: Sequence[RegisterPosting], index: int) -> str:
    others = {p.account for i, p in enumerate(postings) if i != index}
    return next(iter(others)) if len(others) == 1 else SPLIT_MARKER if others else ""


def _in_order(entries: Iterable[RegisterEntry]) -> list[RegisterEntry]:
    return sorted(entries, key=lambda entry: entry.date)  # stable: a day keeps the order given


def account_register(entries: Iterable[RegisterEntry], account: str, *, from_date: Optional[date] = None,
                     to_date: Optional[date] = None, presentation: str = LEDGER) -> Register:
    """The register of ``account``, optionally limited to ``from_date`` to ``to_date`` (both included).

    Activity before ``from_date`` counts in the opening balance, so a balance in a range equals the balance in the
    full register. Each currency has its own running balance.
    """
    sign = _sign(account, presentation)
    running: dict[str, Decimal] = {}
    opening: Optional[dict[str, Decimal]] = None
    lines: list[RegisterLine] = []
    for entry in _in_order(entries):
        if to_date is not None and entry.date > to_date:
            break
        before_range = from_date is not None and entry.date < from_date
        if not before_range and opening is None:
            opening = {c: _present(v, sign) for c, v in running.items()}
        for index, posting in enumerate(entry.postings):
            if posting.account != account:
                continue
            running[posting.currency] = running.get(posting.currency, _ZERO) + posting.amount
            if before_range:
                continue
            lines.append(RegisterLine(entry.key, index, entry.date, entry.description, account, posting.currency,
                                      posting.amount, _present(posting.amount, sign), _split(entry.postings, index),
                                      _present(running[posting.currency], sign)))
    if opening is None:
        opening = {c: _present(v, sign) for c, v in running.items()}
    return Register(account, presentation, from_date, to_date, opening, tuple(lines),
                    {c: _present(v, sign) for c, v in running.items()})


def running_balances(entries: Iterable[RegisterEntry], *,
                     presentation: str = LEDGER) -> dict[tuple[str, str], tuple[LineBalance, ...]]:
    """The running balance after every posting of every account, from one pass over the entries.

    The key is ``(entry key, account)``. The value has one ``LineBalance`` for each posting of that entry to that
    account, in posting order. Each account and currency has its own running balance.
    """
    running: dict[tuple[str, str], Decimal] = {}
    result: dict[tuple[str, str], list[LineBalance]] = {}
    for entry in _in_order(entries):
        for index, posting in enumerate(entry.postings):
            key = (posting.account, posting.currency)
            running[key] = running.get(key, _ZERO) + posting.amount
            balance = _present(running[key], _sign(posting.account, presentation))
            result.setdefault((entry.key, posting.account), []).append(LineBalance(index, posting.currency, balance))
    return {key: tuple(value) for key, value in result.items()}


def load_register_entries(conn: Any) -> list[RegisterEntry]:
    """Every entry in a company's report cache in ledger order (date, then id). The key is the entry id.

    ``conn`` is the connection that ``reports.cache.open_cache`` gives. The description is the payee and the
    narration, as the journal shows them.
    """
    rows = conn.execute(
        """SELECT e.id, e.date, e.payee, e.narration, p.account, p.amount, p.currency
           FROM entries e JOIN postings p ON p.entry_id = e.id
           ORDER BY e.date, e.id, p.id""")
    entries: list[RegisterEntry] = []
    postings: list[RegisterPosting] = []
    head: Any = None
    for row in rows:
        if head is None or head[0] != row[0]:
            if head is not None:
                entries.append(_entry(head, postings))
            head, postings = row, []
        postings.append(RegisterPosting(row[4], Decimal(row[5]), row[6]))
    if head is not None:
        entries.append(_entry(head, postings))
    return entries


def _entry(head: Any, postings: list[RegisterPosting]) -> RegisterEntry:
    description = " ".join(part for part in (head[2], head[3]) if part)
    return RegisterEntry(str(head[0]), date.fromisoformat(str(head[1])[:10]), description, tuple(postings))


def entity_register(entity_path: Path | str, account: str, *, from_date: Optional[date] = None,
                    to_date: Optional[date] = None, presentation: str = NATURAL) -> Register:
    """The register of one account of a company. Raises ``ValueError`` if the ledger has no such account."""
    from .cache import list_accounts, open_cache

    conn = open_cache(entity_path)
    try:
        if account not in {row["name"] for row in list_accounts(conn)}:
            raise ValueError(f"Unknown account: {account}")
        return account_register(load_register_entries(conn), account, from_date=from_date, to_date=to_date,
                                presentation=presentation)
    finally:
        conn.close()


def to_text(register: Register) -> str:
    """A plain table. A line shows the date, description, split, change and balance."""
    out = [f"Register: {register.account}"]
    for currency, value in sorted(register.opening.items()):
        out.append(f"Opening balance {currency}: {_fmt(value)}")
    for line in register.lines:
        out.append(f"{line.date}  {line.description}  [{line.split}]  {_fmt(line.change)}  "
                   f"{_fmt(line.balance)} {line.currency}")
    for currency, value in sorted(register.closing.items()):
        out.append(f"Closing balance {currency}: {_fmt(value)}")
    return "\n".join(out)
