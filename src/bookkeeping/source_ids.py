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
# The digest of the source row, as the ``feed-intake`` record holds it. An entry made from a connected-feed row has
# ``feed-provider`` and ``feed-row-digest`` (the digests of its rows, in the order of ``source-id`` and
# ``paired-source-id``, joined by a comma). The proof counts the row only when the intake record, after the newest
# trust boundary and in a valid chain, still holds that provider, source ID and digest. A row with no digest never counts.
FEED_ROW_DIGEST_META = "feed-row-digest"

# The trust boundary for a connection proof. Books that came from another store (a restored snapshot, a Beancount
# migration) carry entries, marks and ``feed-intake`` events that this store never saw arrive. Such a source is
# untrusted for the proof. ``books ledger mark-migrated`` writes ``MIGRATED_EVENT`` with ``max_entry_id``, the newest
# entry at that point; the older ``migration`` event of ``books ledger migrate`` counts too (its ``entries`` is the
# count of a new store). The newest boundary decides: a ``feed-intake`` event before it is ignored, and an entry
# with an ID up to its ``max_entry_id`` never counts as delivered by the bank connection.
MIGRATED_EVENT = "store-migrated"
BOUNDARY_EVENTS = (MIGRATED_EVENT, "migration")


def intake_boundary(conn: Any) -> tuple[int, int]:
    """``(event ID, max entry ID)`` of the newest trust boundary in the audit log, or ``(0, 0)`` if there is none."""
    import json
    marks = ",".join("?" for _ in BOUNDARY_EVENTS)
    event_id, max_entry_id = 0, 0
    for row_id, kind, raw in conn.execute(
            f"SELECT id, type, payload_json FROM audit_events WHERE type IN ({marks}) ORDER BY id", BOUNDARY_EVENTS):
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            payload = {}
        key = "max_entry_id" if kind == MIGRATED_EVENT else "entries"
        value = payload.get(key) if isinstance(payload, dict) else None
        event_id = int(row_id)
        # A boundary that cannot be read covers every entry, the safe side.
        max_entry_id = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 2 ** 62
    return event_id, max_entry_id

FeedIntake = dict[str, dict[str, set[str]]]


def _row_digest(txn: Any) -> str:
    from .source_disposition import row_identity
    return row_identity(txn)["row_digest"]


def feed_intake_payload(provider: str, session_id: str, txns: list[dict]) -> dict[str, Any]:
    """The audit payload that ``books ingest`` records for the rows of a connected feed."""
    return {"provider": provider, "session": session_id,
            "rows": {str(txn["id"]): _row_digest(txn) for txn in txns if isinstance(txn, dict) and txn.get("id")}}


def feed_intake_index(entity_path: Any, *, chain_verified: bool = False) -> FeedIntake:
    """Source ID -> provider -> the row digests that a connected-feed ingest recorded for it.

    The index is empty when the audit hash chain of the ledger store has an error. Each call reads the store and
    verifies the chain again; nothing is kept between calls. A caller that needs the index for many rows calls
    this once and passes the result to ``feed_provider_meta``. ``chain_verified`` says that the caller verified the
    chain just before and found no error, so the index is not verified again.
    """
    return _read_feed_intake(entity_path, chain_verified)


def _read_feed_intake(entity_path: Any, chain_verified: bool = False) -> FeedIntake:
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
        if not chain_verified and store.verify_audit_chain():
            return index
        with store.connection() as conn:
            boundary_event, _ = intake_boundary(conn)
            rows = conn.execute("SELECT payload_json FROM audit_events WHERE type = ? AND id > ? ORDER BY id",
                                (FEED_INTAKE_EVENT, boundary_event)).fetchall()
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
    """The ``feed-provider`` and ``feed-row-digest`` entry metadata for an entry made from *txns*.

    Each row must be in a connected-feed ingest's audit record with its current digest, under exactly one provider,
    and every row must name the same provider. Otherwise the entry gets no mark.
    """
    providers: set[str] = set()
    digests: list[str] = []
    for txn in txns:
        if not isinstance(txn, dict):
            return ()
        recorded = index.get(str(txn.get("id") or ""), {})
        digest = _row_digest(txn)
        matching = {provider for provider, held in recorded.items() if digest in held}
        if len(matching) != 1:
            return ()
        providers |= matching
        digests.append(digest)
    if len(providers) != 1:
        return ()
    return ((FEED_PROVIDER_META, providers.pop()), (FEED_ROW_DIGEST_META, ",".join(digests)))


def verified_feed_provider(index: FeedIntake, meta: dict[str, Any], source_id: str | None) -> str | None:
    """The provider of an entry whose mark the intake record still backs, else ``None``.

    The entry needs ``feed-provider`` and ``feed-row-digest``. The digests (one for ``source-id`` and one for
    ``paired-source-id`` when the entry has it) must each be in the record of that source ID under that provider.
    """
    provider, recorded = meta.get(FEED_PROVIDER_META), meta.get(FEED_ROW_DIGEST_META)
    if not (isinstance(provider, str) and provider and isinstance(recorded, str) and recorded and source_id):
        return None
    ids = [source_id]
    paired = meta.get("paired-source-id")
    if isinstance(paired, str) and paired:
        ids.append(paired)
    digests = recorded.split(",")
    if len(digests) != len(ids):
        return None
    for row_id, digest in zip(ids, digests):
        if digest not in index.get(row_id, {}).get(provider, ()):
            return None
    return provider


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
