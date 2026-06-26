from __future__ import annotations

"""Beancount v2-compatible writer.

All functions are pure string-in / string-out.  File I/O (temp+rename) belongs
to the importer (U4), not here.

ESCAPING CONTRACT (injection-safety KTD):
    Narration, payee, and metadata values are opaque data.  We:
    - Reject (strip) newlines, carriage returns, and all other control characters
      (U+0000..U+001F, U+007F) from string data before embedding it.
    - Escape embedded double-quote characters as \\".
    Bank/LLM text must never become ledger syntax.

OUTPUT CONTRACT:
    - Directives are date-ordered (ties preserve insertion order).
    - Open directives sort before transactions on the same date.
    - Session blocks are wrapped in pushtag/poptag.
    - Session ID is caller-supplied; never generated here.
    - Output is deterministic byte-for-byte given the same inputs.
"""

import re
from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

from .model import Balance, Entry, Ledger, Open, Posting

# ---------------------------------------------------------------------------
# Escaping helpers
# ---------------------------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize(text: str) -> str:
    """Strip control characters (including newlines/CR) then escape quotes."""
    cleaned = _CONTROL_RE.sub("", text)
    return cleaned.replace('"', '\\"')


def _quote(text: str) -> str:
    """Return *text* wrapped in double quotes, sanitized."""
    return f'"{_sanitize(text)}"'


# ---------------------------------------------------------------------------
# Amount formatting
# ---------------------------------------------------------------------------


def _fmt_amount(amount: Decimal) -> str:
    """Format *amount* to exactly two decimal places with a leading minus if negative."""
    # quantize to ensure .00 suffix on integers
    from decimal import ROUND_HALF_EVEN
    q = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    # format with sign
    if q < 0:
        return f"-{abs(q):.2f}"
    return f"{q:.2f}"


# ---------------------------------------------------------------------------
# Metadata key validation
# ---------------------------------------------------------------------------

_META_KEY_RE = re.compile(r"^[a-z][a-z0-9\-_]*$")


def _validate_meta_key(key: str) -> None:
    if not _META_KEY_RE.match(key):
        raise ValueError(
            f"Metadata key '{key}' is invalid: must start with a lowercase letter, "
            "then letters/digits/hyphens/underscores"
        )


# ---------------------------------------------------------------------------
# Individual directive renderers
# ---------------------------------------------------------------------------


def render_header(title: str) -> str:
    """Return the standard beancount v2 file header block (trailing newline)."""
    t = _sanitize(title)
    lines = [
        f'option "title" "{t}"',
        'option "operating_currency" "USD"',
        'option "inferred_tolerance_default" "USD:0.005"',
        "",
    ]
    return "\n".join(lines)


def render_open(directive: Open) -> str:
    """Return a single ``open`` directive line (no trailing newline)."""
    d = directive.date.strftime("%Y-%m-%d")
    if directive.currencies:
        cur = ",".join(directive.currencies)
        return f"{d} open {directive.account} {cur}"
    return f"{d} open {directive.account}"


def render_balance(directive: Balance) -> str:
    """Return a single ``balance`` directive line (no trailing newline)."""
    d = directive.date.strftime("%Y-%m-%d")
    amt = _fmt_amount(directive.amount)
    return f"{d} balance {directive.account} {amt} {directive.currency}"


def render_entry(entry: Entry) -> str:
    """Return a full transaction block (trailing newline)."""
    d = entry.date.strftime("%Y-%m-%d")

    # Header line: date flag [payee] narration [#tags] [^links]
    if entry.payee is not None:
        header = f'{d} {entry.flag} {_quote(entry.payee)} {_quote(entry.narration)}'
    else:
        header = f'{d} {entry.flag} {_quote(entry.narration)}'

    # Tags and links on the header line
    for tag in entry.tags:
        header += f" #{tag}"
    for link in entry.links:
        header += f" ^{link}"

    lines = [header]

    # Transaction-level metadata. Beancount rejects duplicate keys on one
    # entry, so dedupe with last-wins — the most recently attached value
    # (e.g. a correction's session id) is the one that should survive.
    for key, val in dict(entry.meta).items():
        _validate_meta_key(key)
        lines.append(f"  {key}: {_quote(val)}")

    # Postings
    for posting in entry.postings:
        amt = _fmt_amount(posting.amount)
        lines.append(f"  {posting.account}  {amt} {posting.currency}")
        # Posting-level metadata (if any)
        for key, val in posting.meta:
            _validate_meta_key(key)
            lines.append(f"    {key}: {_quote(val)}")

    lines.append("")  # blank line after each entry
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session block
# ---------------------------------------------------------------------------


def render_session(
    entries: Sequence[Entry],
    session_id: str,
) -> str:
    """Return a pushtag/poptag-wrapped block of entries, date-ordered.

    *session_id* is a caller-supplied opaque string (e.g. 'import-2026-06-12-001').
    Never generated here — output is deterministic.
    """
    tag = f"import-{session_id}"
    sorted_entries = sorted(entries, key=lambda e: e.date)

    parts = [f"pushtag #{tag}", ""]
    for entry in sorted_entries:
        parts.append(render_entry(entry).rstrip("\n"))
        parts.append("")
    parts.append(f"poptag #{tag}")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Full ledger renderer
# ---------------------------------------------------------------------------


def render_ledger(
    opens: Sequence[Open],
    entries: Sequence[Entry],
    balances: Sequence[Balance],
    title: str = "Books",
) -> str:
    """Return the full text of a beancount v2 ledger file.

    Ordering:
      1. Header options block
      2. Open directives (sorted by date, then account name)
      3. Entries and balance directives interleaved, date-ordered
         (opens sort before entries on the same date)
    """
    parts: list[str] = []

    # Header
    parts.append(render_header(title))

    # Open directives: sort by (date, account)
    sorted_opens = sorted(opens, key=lambda o: (o.date, o.account))
    if sorted_opens:
        for o in sorted_opens:
            parts.append(render_open(o))
        parts.append("")

    # Interleave entries and balance assertions, sorted by date.
    # On the same date, opens already rendered above; among entries and
    # balances, preserve insertion order (stable sort).
    combined: list[tuple[date, int, object]] = []
    for i, e in enumerate(entries):
        combined.append((e.date, i, e))
    for i, b in enumerate(balances):
        # balance assertions sort after entries on same day (standard)
        combined.append((b.date, len(entries) + i, b))
    combined.sort(key=lambda x: (x[0], x[1]))

    for _, _, directive in combined:
        if isinstance(directive, Entry):
            parts.append(render_entry(directive).rstrip("\n"))
            parts.append("")
        elif isinstance(directive, Balance):
            parts.append(render_balance(directive))
            parts.append("")

    return "\n".join(parts)
