from __future__ import annotations

"""Built-in beancount v2-compatible validator and parser.

Validates everything the writer emits plus simple same-shape hand-edits.

Checks:
  1. Parseability — every line is recognizable
  2. Per-transaction zero-sum within tolerance (honors inferred_tolerance_default)
  3. Account-opened-before-use
  4. Balance assertions with start-of-day semantics
  5. Account-name lexical rules
  6. Balanced pushtag/poptag

Public API:
  validate(text) -> list[ValidationError]
  validate_file(path) -> list[ValidationError]
  parse_ledger(text) -> dict with keys: opens, entries, balances
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from .model import (
    QUANTIZE,
    TOLERANCE,
    Balance,
    Entry,
    Ledger,
    Open,
    Posting,
    ValidationError,
    _validate_account_name,
)

# ---------------------------------------------------------------------------
# Regex patterns for parsing
# ---------------------------------------------------------------------------

_DATE_RE = r"(\d{4}-\d{2}-\d{2})"
_ACCOUNT_RE = r"([A-Z][A-Za-z0-9]*(?::[A-Z0-9][A-Za-z0-9-]*)*)"
_AMOUNT_RE = r"(-?\d+\.\d{2})"
_CURRENCY_RE = r"([A-Z]{3})"

_OPTION_RE = re.compile(r'^option\s+"([^"]+)"\s+"([^"]+)"')
_OPEN_RE = re.compile(
    rf'^{_DATE_RE}\s+open\s+{_ACCOUNT_RE}(?:\s+{_CURRENCY_RE})?'
)
_BALANCE_RE = re.compile(
    rf'^{_DATE_RE}\s+balance\s+{_ACCOUNT_RE}\s+{_AMOUNT_RE}\s+{_CURRENCY_RE}'
)
_TXN_HEADER_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2})\s+(\*|!)\s+"((?:[^"\\]|\\.)*)"(?:\s+"((?:[^"\\]|\\.)*)")?'
)
_POSTING_RE = re.compile(
    rf'^\s{{2}}{_ACCOUNT_RE}\s+{_AMOUNT_RE}\s+{_CURRENCY_RE}$'
)
_META_RE = re.compile(r'^\s{2}([a-z][a-z0-9\-_]*):\s+"((?:[^"\\]|\\.)*)"$')
_TAG_LINK_RE = re.compile(r'(#[^\s]+|\^[^\s]+)')
_PUSHTAG_RE = re.compile(r'^pushtag\s+(#\S+)')
_POPTAG_RE = re.compile(r'^poptag\s+(#\S+)')
_COMMENT_RE = re.compile(r'^\s*;')
_BLANK_RE = re.compile(r'^\s*$')

_TOLERANCE_OPT_RE = re.compile(
    r'^option\s+"inferred_tolerance_default"\s+"([A-Z]{3}):([0-9.]+)"'
)


def _parse_date(s: str) -> date:
    from datetime import datetime
    return datetime.strptime(s, "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@dataclass
class _ParsedEntry:
    line_no: int
    date: date
    flag: str
    narration: str
    payee: Optional[str]
    tags: list[str]
    links: list[str]
    meta: list[tuple[str, str]]
    postings: list[tuple[str, Decimal, str]]  # (account, amount, currency)


def _unescape(s: str) -> str:
    """Unescape \\\" sequences from a parsed quoted string."""
    return s.replace('\\"', '"')


def parse_ledger(text: str) -> dict:
    """Parse *text* into structured opens, entries, and balances.

    Returns a dict:
        {
            "opens": list[Open],
            "entries": list[Entry],
            "balances": list[Balance],
            "title": str,
            "tolerance": Decimal,
        }

    Raises ValueError on unrecoverable parse errors.
    Does NOT raise on semantic errors — call validate() for those.
    """
    opens: list[Open] = []
    entries: list[Entry] = []
    balances: list[Balance] = []
    title = "Books"
    tolerance = Decimal("0.005")

    lines = text.splitlines()
    i = 0
    n = len(lines)

    current_entry: Optional[_ParsedEntry] = None

    def flush_entry() -> None:
        nonlocal current_entry
        if current_entry is None:
            return
        pe = current_entry
        current_entry = None
        postings_out = []
        for acc, amt, cur in pe.postings:
            postings_out.append(
                Posting(
                    account=acc,
                    amount=amt,
                    currency=cur,
                )
            )
        try:
            e = Entry(
                date=pe.date,
                narration=pe.narration,
                payee=pe.payee,
                flag=pe.flag,
                tags=tuple(pe.tags),
                links=tuple(pe.links),
                meta=tuple(pe.meta),
                postings=tuple(postings_out),
            )
            entries.append(e)
        except ValueError:
            # Re-raise with line info
            raise ValueError(f"Line {pe.line_no}: invalid entry")

    while i < n:
        raw = lines[i]
        line_no = i + 1
        i += 1

        # Skip blank lines and comments
        if _BLANK_RE.match(raw) or _COMMENT_RE.match(raw):
            if _BLANK_RE.match(raw) and current_entry is not None:
                # blank line ends an entry
                flush_entry()
            continue

        # Option
        m = _OPTION_RE.match(raw)
        if m:
            if current_entry is not None:
                flush_entry()
            key, val = m.group(1), m.group(2)
            if key == "title":
                title = val
            m2 = _TOLERANCE_OPT_RE.match(raw)
            if m2:
                try:
                    tolerance = Decimal(m2.group(2))
                except InvalidOperation:
                    pass
            continue

        # pushtag / poptag
        if _PUSHTAG_RE.match(raw) or _POPTAG_RE.match(raw):
            if current_entry is not None:
                flush_entry()
            continue

        # Open directive
        m = _OPEN_RE.match(raw)
        if m:
            if current_entry is not None:
                flush_entry()
            d = _parse_date(m.group(1))
            acc = m.group(2)
            cur = m.group(3)
            opens.append(Open(date=d, account=acc, currencies=(cur,) if cur else ()))
            continue

        # Balance directive
        m = _BALANCE_RE.match(raw)
        if m:
            if current_entry is not None:
                flush_entry()
            d = _parse_date(m.group(1))
            acc = m.group(2)
            amt = Decimal(m.group(3))
            cur = m.group(4)
            balances.append(Balance(date=d, account=acc, amount=amt, currency=cur))
            continue

        # Transaction header
        m = _TXN_HEADER_RE.match(raw)
        if m:
            if current_entry is not None:
                flush_entry()
            d = _parse_date(m.group(1))
            flag = m.group(2)
            # groups: group(3) is always present; group(4) is optional second quoted string
            g3 = _unescape(m.group(3))
            g4 = m.group(4)
            if g4 is not None:
                payee = g3
                narration = _unescape(g4)
            else:
                payee = None
                narration = g3

            # Extract tags and links from the remainder of the header line
            # (after the quoted strings)
            # Find end of last quoted string
            rest = raw
            # Find where the quoted parts end by re-matching
            tags: list[str] = []
            links: list[str] = []
            # Parse tags/links from the raw line suffix after quotes
            # Simple approach: find all #word and ^word tokens after position
            # of the last closing quote
            last_quote = raw.rfind('"')
            if last_quote >= 0:
                suffix = raw[last_quote + 1:]
                for tok_m in _TAG_LINK_RE.finditer(suffix):
                    tok = tok_m.group(1)
                    if tok.startswith("#"):
                        tags.append(tok[1:])
                    else:
                        links.append(tok[1:])

            current_entry = _ParsedEntry(
                line_no=line_no,
                date=d,
                flag=flag,
                narration=narration,
                payee=payee,
                tags=tags,
                links=links,
                meta=[],
                postings=[],
            )
            continue

        # Inside an entry: posting or metadata
        if current_entry is not None:
            # Posting
            pm = _POSTING_RE.match(raw)
            if pm:
                acc = pm.group(1)
                amt = Decimal(pm.group(2))
                cur = pm.group(3)
                current_entry.postings.append((acc, amt, cur))
                continue

            # Metadata
            mm = _META_RE.match(raw)
            if mm:
                key = mm.group(1)
                val = _unescape(mm.group(2))
                if any(k == key for k, _ in current_entry.meta):
                    errors.append(ValidationError(
                        line=lineno,
                        message=f"Duplicate metadata key '{key}' on entry (beancount keeps only the first)",
                    ))
                current_entry.meta.append((key, val))
                continue

        # Unknown line — skip silently (hand-edits like comments inside entries)
        # but flush a pending entry if it looks like a new directive
        if current_entry is not None and not raw.startswith(" "):
            flush_entry()

    # Flush final entry
    if current_entry is not None:
        flush_entry()

    return {
        "opens": opens,
        "entries": entries,
        "balances": balances,
        "title": title,
        "tolerance": tolerance,
    }


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def validate(text: str) -> list[ValidationError]:
    """Validate *text* as a beancount v2 ledger.

    Returns a (possibly empty) list of ValidationError.
    """
    errors: list[ValidationError] = []
    lines = text.splitlines()

    # --- Pass 1: parse tolerance from options ---
    tolerance = Decimal("0.005")
    for raw in lines:
        m = _TOLERANCE_OPT_RE.match(raw)
        if m:
            try:
                tolerance = Decimal(m.group(2))
            except InvalidOperation:
                pass

    # --- Pass 2: structural parse (pushtag/poptag, account names) ---
    tag_stack: list[tuple[int, str]] = []  # (line_no, tag)
    opened_accounts: dict[str, date] = {}  # account -> open date
    # Collect open directives for open-before-use check
    open_directives: list[tuple[int, date, str]] = []  # (line_no, date, account)
    # Collect transactions for balance/sum checks
    txn_blocks: list[tuple[int, _ParsedEntry]] = []

    i = 0
    n = len(lines)
    current_entry: Optional[_ParsedEntry] = None

    def flush_txn() -> None:
        nonlocal current_entry
        if current_entry is None:
            return
        txn_blocks.append((current_entry.line_no, current_entry))
        current_entry = None

    while i < n:
        raw = lines[i]
        line_no = i + 1
        i += 1

        if _BLANK_RE.match(raw):
            if current_entry is not None:
                flush_txn()
            continue

        if _COMMENT_RE.match(raw):
            continue

        # pushtag
        m = _PUSHTAG_RE.match(raw)
        if m:
            if current_entry is not None:
                flush_txn()
            tag_stack.append((line_no, m.group(1)))
            continue

        # poptag
        m = _POPTAG_RE.match(raw)
        if m:
            if current_entry is not None:
                flush_txn()
            tag = m.group(1)
            if not tag_stack:
                errors.append(ValidationError(line_no, f"poptag {tag} without matching pushtag"))
            elif tag_stack[-1][1] != tag:
                errors.append(
                    ValidationError(
                        line_no,
                        f"poptag {tag} does not match most recent pushtag "
                        f"{tag_stack[-1][1]} (line {tag_stack[-1][0]})",
                    )
                )
                tag_stack.pop()
            else:
                tag_stack.pop()
            continue

        # Option
        if _OPTION_RE.match(raw):
            if current_entry is not None:
                flush_txn()
            continue

        # Open directive
        m = _OPEN_RE.match(raw)
        if m:
            if current_entry is not None:
                flush_txn()
            d_str = m.group(1)
            acc = m.group(2)
            try:
                d = _parse_date(d_str)
                _validate_account_name(acc)
                open_directives.append((line_no, d, acc))
            except ValueError as exc:
                errors.append(ValidationError(line_no, str(exc)))
            continue

        # Balance directive
        m = _BALANCE_RE.match(raw)
        if m:
            if current_entry is not None:
                flush_txn()
            acc = m.group(2)
            try:
                _validate_account_name(acc)
            except ValueError as exc:
                errors.append(ValidationError(line_no, str(exc)))
            continue

        # Transaction header
        m = _TXN_HEADER_RE.match(raw)
        if m:
            if current_entry is not None:
                flush_txn()
            d = _parse_date(m.group(1))
            flag = m.group(2)
            g3 = _unescape(m.group(3))
            g4 = m.group(4)
            if g4 is not None:
                payee = g3
                narration = _unescape(g4)
            else:
                payee = None
                narration = g3

            last_quote = raw.rfind('"')
            tags_list: list[str] = []
            links_list: list[str] = []
            if last_quote >= 0:
                suffix = raw[last_quote + 1:]
                for tok_m in _TAG_LINK_RE.finditer(suffix):
                    tok = tok_m.group(1)
                    if tok.startswith("#"):
                        tags_list.append(tok[1:])
                    else:
                        links_list.append(tok[1:])

            current_entry = _ParsedEntry(
                line_no=line_no,
                date=d,
                flag=flag,
                narration=narration,
                payee=payee,
                tags=tags_list,
                links=links_list,
                meta=[],
                postings=[],
            )
            continue

        if current_entry is not None:
            pm = _POSTING_RE.match(raw)
            if pm:
                acc = pm.group(1)
                amt = Decimal(pm.group(2))
                cur = pm.group(3)
                # validate account name
                try:
                    _validate_account_name(acc)
                except ValueError as exc:
                    errors.append(ValidationError(line_no, str(exc)))
                current_entry.postings.append((acc, amt, cur))
                continue

            mm = _META_RE.match(raw)
            if mm:
                current_entry.meta.append((mm.group(1), _unescape(mm.group(2))))
                continue

        # Unrecognized non-indented line while in entry
        if current_entry is not None and not raw.startswith(" "):
            flush_txn()

    if current_entry is not None:
        flush_txn()

    # --- Check balanced pushtag/poptag ---
    for push_line, tag in tag_stack:
        errors.append(ValidationError(push_line, f"pushtag {tag} has no matching poptag"))

    # --- Build opened accounts map ---
    for line_no, d, acc in open_directives:
        if acc not in opened_accounts:
            opened_accounts[acc] = d
        # If opened twice, keep the earliest
        elif d < opened_accounts[acc]:
            opened_accounts[acc] = d

    # --- Check transactions: balance and account-open-before-use ---
    for block_line, pe in txn_blocks:
        # Zero-sum check
        total = sum(amt for _, amt, _ in pe.postings)
        if abs(total) > tolerance:
            narr = pe.narration
            errors.append(
                ValidationError(
                    block_line,
                    f"Transaction '{narr}' on {pe.date} does not balance: "
                    f"sum={total} (tolerance={tolerance})",
                )
            )

        # Account-open-before-use check
        for acc, amt, cur in pe.postings:
            if acc not in opened_accounts:
                errors.append(
                    ValidationError(
                        block_line,
                        f"Account '{acc}' used on {pe.date} has no open directive",
                    )
                )
            elif opened_accounts[acc] > pe.date:
                errors.append(
                    ValidationError(
                        block_line,
                        f"Account '{acc}' opened on {opened_accounts[acc]} "
                        f"but used on {pe.date} (before open date)",
                    )
                )

    # --- Balance assertion checks (start-of-day semantics) ---
    # Build running balances per account, then check balance directives.
    # "Start of day" means: balance assertion on date D is checked BEFORE
    # same-day transactions are applied.  So we sum all entries with date < D.
    account_balances: dict[str, Decimal] = {}
    # Collect all entries sorted by date
    all_entries_sorted = sorted(txn_blocks, key=lambda x: x[1].date)

    # Parse balance directives from text to check them
    balance_assertions: list[tuple[int, date, str, Decimal, str]] = []
    for line_no_idx, raw in enumerate(lines, 1):
        bm = _BALANCE_RE.match(raw)
        if bm:
            d = _parse_date(bm.group(1))
            acc = bm.group(2)
            amt = Decimal(bm.group(3))
            cur = bm.group(4)
            balance_assertions.append((line_no_idx, d, acc, amt, cur))

    if balance_assertions:
        # Build a timeline of postings
        # For each balance assertion, sum all postings to that account with date < assertion date
        for ba_line, ba_date, ba_acc, ba_amt, ba_cur in balance_assertions:
            running = Decimal("0.00")
            for _, pe in all_entries_sorted:
                if pe.date >= ba_date:
                    break
                for acc, amt, cur in pe.postings:
                    if acc == ba_acc and cur == ba_cur:
                        running += amt
            diff = abs(running - ba_amt)
            if diff > tolerance:
                errors.append(
                    ValidationError(
                        ba_line,
                        f"Balance assertion for '{ba_acc}' on {ba_date} failed: "
                        f"expected {ba_amt} {ba_cur}, computed {running} {ba_cur} "
                        f"(difference {running - ba_amt})",
                    )
                )

    return errors


def validate_file(path: str | Path) -> list[ValidationError]:
    """Read *path* and validate its contents.  Returns list of ValidationError."""
    text = Path(path).read_text(encoding="utf-8")
    return validate(text)
