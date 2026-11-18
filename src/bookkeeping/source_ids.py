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

#: Earlier ledger names of a CSV or statement mapping, oldest first. ``books entity account-rename`` adds them.
FORMER_LEDGER_ACCOUNTS = "former_ledger_accounts"


class RowIds:
    """The account part of the source IDs of CSV and statement rows for one ledger account.

    A new row ID hashes the catalog row id of the account (``account-id:<id>``), which a rename keeps and a new
    account with the same name does not get. Rows imported before this rule have IDs that hash an account name.
    ``pick`` gives such an earlier ID when the company already holds it for this account: the ID hashes a name of
    this account's lineage (``account_rename.rename_lineage``), and the row belongs to this account. The owner of a
    row is the account of its CSV or statement mapping (``csvMappingKey``; a rename moves the mapping with the
    account), else the accounts that its posted entry or its held record names. A row with no owner on record (only
    in ``seen-ids.json``) counts only for a name that no other account used (``account_rename.legacy_names``).
    Without a ledger store, or for a name outside the catalog, the ID hashes the name, as before.
    """

    def __init__(self, entity_path: Any, ledger_account: str) -> None:
        from .ledger.store import LedgerStore, default_store_path
        self.entity_path = Path(entity_path)
        self.account = ledger_account
        self.basis, self.legacy, self.exclusive = ledger_account, (), frozenset()
        self._known: set[str] | None = None
        self._owners: dict[str, set[str]] = {}
        self._store = None
        path = default_store_path(self.entity_path)
        if not path.is_file():
            return
        store = LedgerStore(path)
        with store.connection() as conn:
            row = conn.execute("SELECT id FROM accounts WHERE name = ?", (ledger_account,)).fetchone()
        if row is None:
            return
        from .account_rename import legacy_names, rename_lineage
        events = store.load_audit_events()
        self._store = store
        self.basis = f"account-id:{int(row[0])}"
        self.legacy = tuple(name for name, _ in rename_lineage(events, ledger_account))
        self.exclusive = frozenset(legacy_names(events, ledger_account))

    def pick(self, make: Any) -> str:
        """``make(account_part)`` builds an ID. The ID that the company already holds for this account, else the new one."""
        new = make(self.basis)
        for name in self.legacy:
            old = make(name)
            if old != new and self._owned(old, name in self.exclusive):
                return old
        return new

    def _owned(self, source_id: str, exclusive: bool) -> bool:
        owners = self._owner(source_id)
        if owners is None:
            return exclusive and self.known(source_id)
        return self.account in owners

    def known(self, source_id: str) -> bool:
        self._load()
        spellings = source_id_variants(source_id)
        return bool(spellings & self._known) or (
            self._store is not None and any(self._store.source_exists(s) for s in spellings))

    def _owner(self, source_id: str) -> set[str] | None:
        """The current names of the accounts that hold the row, or ``None`` when no record says."""
        import json
        self._load()
        spellings = source_id_variants(source_id)
        if self._store is not None:
            marks = ",".join("?" for _ in spellings)
            with self._store.connection() as conn:
                for (raw,) in conn.execute(f"SELECT payload_json FROM source_transactions WHERE id IN ({marks})",
                                           sorted(spellings)):
                    try:
                        owner = self._mapped(json.loads(raw or "null"))
                    except ValueError:
                        owner = None
                    if owner:
                        return {owner}
                posted = {str(account) for (account,) in conn.execute(
                    f"SELECT p.account FROM postings p JOIN entries e ON e.id = p.entry_id WHERE e.source_id IN ({marks})",
                    sorted(spellings))}
            if posted:
                return posted
        found: set[str] = set()
        for spelling in spellings:
            found |= self._owners.get(spelling, set())
        return found or None

    def _mapped(self, row: Any) -> str | None:
        """The current ledger account of the row's CSV or statement mapping."""
        if not isinstance(row, dict) or not isinstance(row.get("csvMappingKey"), str):
            return None
        mapping = self._mappings.get(row["csvMappingKey"])
        account = mapping.get("ledger_account") if isinstance(mapping, dict) else None
        return account if isinstance(account, str) and account else None

    def _load(self) -> None:
        """Read the staging files once: the IDs they hold, and the owner of each row that says one."""
        if self._known is not None:
            return
        import json
        try:
            config = json.loads((self.entity_path / "entity.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        mappings = config.get("csv_account_mappings") if isinstance(config, dict) else None
        self._mappings = mappings if isinstance(mappings, dict) else {}
        known: set[str] = set()
        staging = self.entity_path / "staging"
        for name, field in (("seen-ids.json", None), ("pending.json", "id"), ("pending-categorization.json", "id"),
                            ("duplicate-candidates.json", "source_id"), ("context-history.json", "source_id")):
            try:
                data = json.loads((staging / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                data = data.get("rows", [])
            for item in data if isinstance(data, list) else []:
                value = item if field is None else (item.get(field) if isinstance(item, dict) else None)
                if not isinstance(value, str) or not value:
                    continue
                spellings = source_id_variants(value)
                known |= spellings
                if not isinstance(item, dict):
                    continue
                owner = self._mapped(item) or self._mapped(item.get("transaction"))
                if owner is None and isinstance(item.get("account"), str):
                    owner = item["account"]  # A held record names its account; a rename keeps it current.
                if owner:
                    for spelling in spellings:
                        self._owners.setdefault(spelling, set()).add(owner)
        self._known = known


def keep_former_accounts(existing: Any, ledger_account: str) -> dict[str, Any]:
    """The ``former_ledger_accounts`` field to keep when a mapping of the same account is saved again."""
    if isinstance(existing, dict) and existing.get("ledger_account") == ledger_account:
        former = existing.get(FORMER_LEDGER_ACCOUNTS)
        if isinstance(former, list) and former:
            return {FORMER_LEDGER_ACCOUNTS: list(former)}
    return {}
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

    The entry needs ``feed-provider`` and ``feed-row-digest``. Each digest (one for ``source-id``, and a second for
    ``paired-source-id`` only when the entry holds two rows) must be in the record of that source ID under that
    provider.
    """
    provider, recorded = meta.get(FEED_PROVIDER_META), meta.get(FEED_ROW_DIGEST_META)
    if not (isinstance(provider, str) and provider and isinstance(recorded, str) and recorded and source_id):
        return None
    digests = recorded.split(",")
    ids = [source_id]
    if len(digests) == 2:
        # One entry that holds two rows (a same-day transfer pair): ``source-id`` and ``paired-source-id``.
        paired = meta.get("paired-source-id")
        if not (isinstance(paired, str) and paired):
            return None
        ids.append(paired)
    elif len(digests) != 1:
        return None
    # An entry with one digest is judged by its own source ID, also when it names a ``paired-source-id`` (a
    # transfer pair on two days is two entries).
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
