"""Source IDs for connected bank feeds: one namespace per provider.

A feed transaction's source ID used to be the provider's raw ID. Two providers
can use the same raw ID, and a company that switches provider must be able to
tell their rows apart. New feed rows therefore get ``<provider>:<id>``, for
example ``banksync:abc``. Statement rows keep their ``stmt:``, ``ofx:`` and
``csv:`` prefixes.

Rows that were imported before this change keep their raw BankSync IDs in the
ledger. They are never rewritten. ``source_id_variants`` gives every spelling
that names the same BankSync row, so a lookup for ``banksync:abc`` also finds a
stored ``abc``, and a lookup for ``abc`` also finds a stored ``banksync:abc``.
Every duplicate lookup (seen, posted, queued, accrual ownership) uses this one
function, so the two spellings never disagree.
"""

from __future__ import annotations

from typing import Any

# Providers that can be named as a connected bank feed. ``books ingest --source``
# uses one of these, or ``--feed-provider`` names another.
CONNECTED_FEED_PROVIDERS = ("banksync", "plaid", "stripe-financial-connections")

# The only provider whose rows were stored without a prefix before namespacing.
LEGACY_UNPREFIXED_PROVIDER = "banksync"


def namespaced_feed_id(provider: str, raw_id: Any) -> str:
    """``<provider>:<id>``. An ID that already has this prefix is returned as is."""
    text = str(raw_id or "")
    if not text:
        return ""
    prefix = f"{provider}:"
    return text if text.startswith(prefix) else prefix + text


def source_id_variants(source_id: Any) -> frozenset[str]:
    """Every spelling of *source_id* that names the same feed row.

    ``banksync:abc`` and the legacy ``abc`` name one row. Any other ID, such as
    ``plaid:abc``, ``stmt:...``, ``stripe:acct:...`` or an ID with its own
    colon, has no alias and returns itself.
    """
    text = str(source_id or "")
    prefix = f"{LEGACY_UNPREFIXED_PROVIDER}:"
    if text.startswith(prefix) and len(text) > len(prefix):
        return frozenset({text, text[len(prefix):]})
    if text and ":" not in text:
        return frozenset({text, prefix + text})
    return frozenset({text})


UNPREFIXED_FAMILY = "unprefixed"


def source_family(source_id: Any) -> str:
    """Which source produced *source_id*, for the duplicate plan.

    A connected feed names itself in the ID prefix (``plaid``, ``banksync``).
    Statement and CSV IDs are ``other``. An ID with no prefix could come from
    any connector, so it is ``unprefixed``; it counts as BankSync only when a
    BankSync feed is the one being checked (see ``same_family``).
    """
    head, separator, _ = str(source_id or "").partition(":")
    if not separator:
        return UNPREFIXED_FAMILY
    return "other" if head in {"stmt", "ofx", "csv"} else head


def same_family(existing: str, incoming: str) -> bool:
    """Whether an existing row and an incoming row come from the same source.

    The old raw-ID rows are treated as BankSync only for an incoming BankSync
    feed. For any other provider they are a different source, so overlap with
    them is held for review.
    """
    if existing == incoming:
        return True
    return {existing, incoming} == {UNPREFIXED_FAMILY, LEGACY_UNPREFIXED_PROVIDER}
