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

from pathlib import Path
from typing import Any

# Providers that can be named as a connected bank feed. ``books ingest --source``
# uses one of these, or ``--feed-provider`` names another.
CONNECTED_FEED_PROVIDERS = ("banksync", "plaid", "stripe-financial-connections")

# The only provider whose rows were stored without a prefix before namespacing.
LEGACY_UNPREFIXED_PROVIDER = "banksync"

# ``books ingest`` with a connected-feed provider writes one ``FEED_INTAKE_EVENT`` audit event in the ledger store
# before it imports: the provider, the session and the digest of every row. An entry made from such a row, now or
# when a person confirms it later, carries the provider in ``FEED_PROVIDER_META`` only when the row still has that
# digest. The mark is read from the hash-chained audit log, never from a staged row, so an edit of the staging files
# cannot create it, and an edited row loses it.
FEED_PROVIDER_META = "feed-provider"
FEED_INTAKE_EVENT = "feed-intake"

FeedIntake = dict[str, dict[str, set[str]]]


def _row_digest(txn: Any) -> str:
    from .source_disposition import row_identity
    return row_identity(txn)["row_digest"]


def feed_intake_payload(provider: str, session_id: str, txns: list[dict]) -> dict[str, Any]:
    """The audit payload that ``books ingest`` records for the rows of a connected feed."""
    return {"provider": provider, "session": session_id,
            "rows": {str(txn["id"]): _row_digest(txn) for txn in txns if isinstance(txn, dict) and txn.get("id")}}


def feed_intake_index(entity_path: Any) -> FeedIntake:
    """Source ID -> provider -> the row digests that a connected-feed ingest recorded for it.

    The index is empty when the audit hash chain of the ledger store has an error. Each call reads the store and
    verifies the chain again; nothing is kept between calls. A caller that needs the index for many rows calls
    this once and passes the result to ``feed_provider_meta``.
    """
    return _read_feed_intake(entity_path)


def _read_feed_intake(entity_path: Any) -> FeedIntake:
    import json
    import sqlite3
    from .ledger.store import LedgerStore, default_store_path

    index: FeedIntake = {}
    path = default_store_path(Path(entity_path))
    if not path.is_file():
        return index
    try:
        store = LedgerStore(path)
        # An edited or inserted audit record breaks the chain. Then no record can be trusted, so no row has a mark.
        if store.verify_audit_chain():
            return index
        with store.connection() as conn:
            rows = conn.execute("SELECT payload_json FROM audit_events WHERE type = ? ORDER BY id",
                                (FEED_INTAKE_EVENT,)).fetchall()
    except sqlite3.DatabaseError:
        return index
    for (raw,) in rows:
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            continue
        provider, digests = payload.get("provider"), payload.get("rows")
        if not isinstance(provider, str) or not provider or not isinstance(digests, dict):
            continue
        for source_id, digest in digests.items():
            if isinstance(digest, str):
                index.setdefault(str(source_id), {}).setdefault(provider, set()).add(digest)
    return index


def feed_provider_meta(index: FeedIntake, *txns: Any) -> tuple[tuple[str, str], ...]:
    """The ``feed-provider`` entry metadata for an entry made from *txns*.

    Each row must be in a connected-feed ingest's audit record with its current digest, under exactly one provider,
    and every row must name the same provider. Otherwise the entry gets no mark.
    """
    providers: set[str] = set()
    for txn in txns:
        if not isinstance(txn, dict):
            return ()
        recorded = index.get(str(txn.get("id") or ""), {})
        digest = _row_digest(txn)
        matching = {provider for provider, digests in recorded.items() if digest in digests}
        if len(matching) != 1:
            return ()
        providers |= matching
    return ((FEED_PROVIDER_META, providers.pop()),) if len(providers) == 1 else ()


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
