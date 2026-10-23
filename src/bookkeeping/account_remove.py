"""``books entity account-remove``: remove an income or expense account that ``account-add`` created and nothing uses.

The command is narrow on purpose. It removes an account only when all of these are true:

* ``books entity account-add`` created it: the audit log has the account-add event for it
  (``is_account_added_event``), later than any ``account-removed`` event for it (``--added-event ID``
  names that event; without it the command finds the latest one);
* it is an income or expense account, and not an account that the engine creates or names itself
  (``engine_accounts.engine_owned_accounts``);
* no posting (in any period, reversals included) and no balance assertion uses it;
* no company setting or record names it (``entity.json``, ``trust-policy.json``, learned context,
  open review items, staged rows, accrual records), and no saved accrual rule selects it through a
  parent account that would then select nothing.

The command previews first and writes nothing. ``--apply --plan-hash H`` removes the account and writes
the audit event ``account-removed``. The plan hash covers the audit chain head and a fingerprint of every
company settings file that the checks read, so a changed company refuses with ``stale_preview``. Before
its checks pass, the command writes no company file: it takes the write lock without the entity.json
repair. An absent account gives ``already_applied``, also for an exact retry. No entry changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

KIND = "account_remove"
REMOVABLE_ROOTS = ("Income", "Expenses")
SETTINGS_FILES = ("entity.json", "trust-policy.json")
SETTINGS_FOLDERS = ("learned-context", "review-queue", "staging")
ACCRUAL_RULES_FILE = "learned-context/accrual-rules.json"
_SEGMENT_RE = re.compile(r"[A-Z0-9][A-Za-z0-9-]*")
_CLOSED_QUEUE_STATUSES = frozenset({"confirmed", "corrected"})
_MAX_REFS = 20


class AccountRemoveError(ValueError):
    """The account cannot be removed. ``code`` names the rule; ``refs`` lists what blocks it."""

    def __init__(self, code: str, message: str, action: str = "Leave the account in the catalog.",
                 refs: list[str] | None = None) -> None:
        self.code, self.action, self.refs = code, action, list(refs or [])
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": str(self), "action": self.action}
        if self.refs:
            error["refs"] = self.refs
        return {"status": "blocked", "errors": [error]}


def _unreadable(name: str) -> AccountRemoveError:
    return AccountRemoveError("reference_check_failed", f"{name} cannot be read safely, so the references cannot be checked.",
                              "Repair the file (no symbolic links, valid JSON, readable), then run the preview again.", [name])


def _check_name(account: Any) -> str:
    if (not isinstance(account, str) or any(ord(char) < 32 or ord(char) == 127 for char in account)
            or ":" not in account or account.split(":")[0] not in {"Assets", "Liabilities", "Equity", "Income", "Expenses"}
            or any(not _SEGMENT_RE.fullmatch(part) for part in account.split(":")[1:])):
        raise AccountRemoveError("invalid_account", f"{account!r} is not a valid account name.",
                                 "Give the account name exactly as the account catalog shows it.")
    return account


def _company_files(root: Path) -> dict[str, bytes]:
    """The bytes of every settings file that the checks read. Symbolic links and read errors fail closed."""
    files: dict[str, bytes] = {}

    def read(path: Path) -> None:
        name = path.relative_to(root).as_posix()
        try:
            if path.is_symlink():
                raise _unreadable(name)
            files[name] = path.read_bytes()
        except OSError:
            raise _unreadable(name) from None

    def fail(error: OSError) -> None:
        raise error

    for name in SETTINGS_FILES:
        path = root / name
        if path.is_symlink() or path.exists():
            read(path)
    for folder in SETTINGS_FOLDERS:
        base = root / folder
        try:
            if base.is_symlink():
                raise _unreadable(folder)
            if not base.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(base, onerror=fail):
                for child in sorted(dirnames):
                    if (Path(dirpath) / child).is_symlink():
                        raise _unreadable((Path(dirpath) / child).relative_to(root).as_posix())
                for child in sorted(filenames):
                    read(Path(dirpath) / child)
        except OSError:
            raise _unreadable(folder) from None
    return dict(sorted(files.items()))


def _matches(value: Any, account: str, path: str) -> Iterator[str]:
    """JSON paths where *account* is a string value or an object key.

    *value* is parsed JSON, so the comparison uses decoded strings: a name written with JSON escapes
    (``"Income:\\u0049nstalls"``) matches as well. Every JSON source of the scan goes through here.
    The walk uses its own stack, so a deeply nested document cannot exhaust the Python call stack.
    """
    stack: list[tuple[Any, str]] = [(value, path)]
    while stack:
        item, where = stack.pop()
        if isinstance(item, str):
            if item == account:
                yield where
        elif isinstance(item, dict):
            children = []
            for key, child in item.items():
                child_path = f"{where}.{key}" if where else str(key)
                if key == account:
                    yield child_path
                children.append((child, child_path))
            stack.extend(reversed(children))
        elif isinstance(item, list):
            stack.extend(reversed([(child, f"{where}[{index}]") for index, child in enumerate(item)]))


def _rule_prefix_refs(document: Any, account: str, catalog: set[str]) -> list[str]:
    """Saved accrual rules whose match.accounts selects *account* through a parent and would then select nothing.

    A rule account selects an account when it is equal to it or a parent of it (``account.startswith(P + ":")``).
    A rule account that is neither an account nor a parent of one fails with ``unmapped_account``.
    """
    found = []
    rules = document.get("rules") if isinstance(document, dict) else None
    for index, rule in enumerate(rules if isinstance(rules, list) else []):
        selected = (rule.get("match") or {}).get("accounts") if isinstance(rule, dict) else None
        for position, prefix in enumerate(selected if isinstance(selected, list) else []):
            if not isinstance(prefix, str) or not account.startswith(prefix + ":"):
                continue
            remaining = {name for name in catalog if name != account}
            if prefix not in remaining and not any(name.startswith(prefix + ":") for name in remaining):
                found.append(f"{ACCRUAL_RULES_FILE}:rules[{index}].match.accounts[{position}]")
    return found


def _check_shape(name: str, document: Any) -> None:
    """Refuse a settings file whose shape the readers would not accept, before any scan."""
    if name in ("entity.json", "trust-policy.json", "learned-context/counterparties.json") \
            or (name.startswith("review-queue/") and name.count("/") == 1):
        if not isinstance(document, dict):
            raise _unreadable(name)
    if name == ACCRUAL_RULES_FILE:
        rules = document.get("rules") if isinstance(document, dict) else None
        if not isinstance(rules, list):
            raise _unreadable(name)
        for rule in rules:
            match = rule.get("match", {}) if isinstance(rule, dict) else None
            selected = match.get("accounts", []) if isinstance(match, dict) else None
            if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
                raise _unreadable(name)


def parse_settings(files: dict[str, bytes]) -> dict[str, Any]:
    """Parse every JSON settings file and check its shape. Any problem gives reference_check_failed."""
    documents: dict[str, Any] = {}
    for name, data in files.items():
        if not name.endswith(".json"):
            continue
        try:
            documents[name] = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):  # Too deeply nested JSON fails closed.
            raise _unreadable(name) from None
        _check_shape(name, documents[name])
    return documents


def references(documents: dict[str, Any], store: Any, account: str, catalog: set[str]) -> list[str]:
    """Where company settings and records name *account*.

    Not scanned on purpose: the audit log (historical provenance, including earlier account-add and
    account-removed events), the narration and metadata of entries and postings (postings are checked
    by account), and files outside the settings folders (reports, ingestion, business-profile.md).
    """
    from .accrual.store import TABLES, list_records
    found: list[str] = []
    for name, document in documents.items():
        if name.startswith("review-queue/") and isinstance(document, dict) \
                and document.get("status") in _CLOSED_QUEUE_STATUSES:
            continue  # A decided item; a posted decision shows in the postings.
        found += [f"{name}:{where}" for where in _matches(document, account, "")]
        if name == ACCRUAL_RULES_FILE:
            found += _rule_prefix_refs(document, account, catalog)
    for table in TABLES:
        for record in list_records(store, table):
            found += [f"accrual {table} {record.get('id', '?')}:{where}" for where in _matches(record, account, "")]
    with store.connection() as conn:
        for table, column in (("source_transactions", "payload_json"), ("import_sessions", "metadata_json")):
            for record_id, raw in conn.execute(f"SELECT id, {column} FROM {table} ORDER BY id"):
                # Parse every row: a raw text search would miss a name written with JSON escapes.
                try:
                    record = json.loads(raw or "null")
                except (ValueError, RecursionError):
                    raise _unreadable(f"{table} {record_id}") from None
                found += [f"{table} {record_id}:{where}" for where in _matches(record, account, "")]
    return found


ACCOUNT_ADD_SESSION = "entity-account-add"


def is_account_added_event(event: dict[str, Any], account: str) -> bool:
    """True for the audit event that ``books entity account-add`` writes when it creates *account*.

    account-add writes an ``intent`` event with ``session_id`` ``entity-account-add`` and the description
    ``Add account <account>: open <date>, currency <code>`` (an update says ``Update account``).
    """
    payload = event.get("payload") or {}
    return (event.get("type") == "intent" and payload.get("session_id") == ACCOUNT_ADD_SESSION
            and str(payload.get("description", "")).startswith(f"Add account {account}: open "))


def _added_event(events: list[dict[str, Any]], account: str, added_event: int | None) -> dict[str, Any]:
    """The account-add event that created the account now in the catalog."""
    added = [e for e in events if is_account_added_event(e, account)]
    removed = [e for e in events if e["type"] == "account-removed" and e["payload"].get("account") == account]
    latest = added[-1] if added and (not removed or added[-1]["id"] > removed[-1]["id"]) else None
    if latest is None:
        raise AccountRemoveError("not_added_by_command",
                                 f"No audit event shows that books entity account-add created {account}.",
                                 "Only an account that books entity account-add created can be removed.")
    if added_event is not None and added_event != latest["id"]:
        raise AccountRemoveError("added_event_mismatch",
                                 f"Audit event {added_event} is not the account-add event of {account} "
                                 f"(that is event {latest['id']}).", "Run the preview again without --added-event.")
    return latest


def preview(entity_path: Path | str, account: str, added_event: int | None = None) -> dict[str, Any]:
    """Preview removing the account. Writes nothing, and does not repair entity.json."""
    from .accrual.intake import canonical_hash
    from .engine_accounts import engine_owned_accounts
    from .ledger.importer import check_store_integrity
    from .ledger.store import LedgerStore, default_store_path
    account = _check_name(account)
    root = Path(entity_path).resolve()
    files = _company_files(root)
    if "entity.json" not in files:
        raise AccountRemoveError("no_company", f"No entity.json found at '{root}'.", "Run books entity init first.")
    documents = parse_settings(files)
    store = LedgerStore(default_store_path(root))
    if not store.path.exists():
        raise AccountRemoveError("no_ledger", "The company has no ledger.", "Run books entity init first.")
    if check_store_integrity(store).status != "ok":
        raise AccountRemoveError("ledger_integrity", "Cannot change the account catalog while ledger integrity is invalid.",
                                 "Run the integrity check and fix the reported problem first.")
    with store.connection() as conn:
        _require_audit_history(conn)
        row = conn.execute("SELECT currency, open_date FROM accounts WHERE name=?", (account,)).fetchone()
        postings = conn.execute("SELECT COUNT(*) FROM postings WHERE account=?", (account,)).fetchone()[0]
        assertions = conn.execute("SELECT COUNT(*) FROM balance_assertions WHERE account=?", (account,)).fetchone()[0]
        catalog = {name for (name,) in conn.execute("SELECT name FROM accounts")}
    events = store.load_audit_events()
    request = {"account": account}
    body: dict[str, Any] = {
        "schema_version": 1, "kind": KIND, "request": request, "changes": row is not None,
        "ledger_digest": store.content_digest(), "audit_head": events[-1]["record_hash"] if events else "none",
        "file_fingerprints": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    added = None
    if row is not None:
        if account.split(":")[0] not in REMOVABLE_ROOTS or account in engine_owned_accounts():
            raise AccountRemoveError("protected_account",
                                     f"{account} is a system account. Only an income or expense category that "
                                     "books entity account-add created can be removed.")
        added = _added_event(events, account, added_event)
        if postings or assertions:
            raise AccountRemoveError("account_in_use",
                                     f"{account} has {postings} postings and {assertions} balance assertions. "
                                     "An account in use stays in the catalog.",
                                     "Leave the account in the catalog. Move the entries first if they belong elsewhere.")
        found = references(documents, store, account, catalog)
        if found:
            raise AccountRemoveError("account_referenced",
                                     f"{account} is named in {len(found)} company settings or records.",
                                     "Change those settings or records first, then run the preview again.",
                                     found[:_MAX_REFS])
    body["effects"] = {"account": account, "open_date": row["open_date"] if row else None,
                       "currency": row["currency"] if row else None, "added_event": added["id"] if added else None,
                       "entries_changed": 0, "accounts_removed": 1 if row else 0}
    body["request_hash"] = canonical_hash({"kind": KIND, "request": request})
    body["plan_hash"] = canonical_hash(body)
    return body


def _require_audit_history(conn: Any) -> None:
    """The guard of account-add: a ledger with entries must have audit history before its catalog changes."""
    if conn.execute("SELECT 1 FROM entries LIMIT 1").fetchone() and not conn.execute(
            "SELECT 1 FROM audit_events LIMIT 1").fetchone():
        raise AccountRemoveError("audit_history_missing",
                                 "The account catalog cannot change on a ledger with entries and no audit history.",
                                 "Repair the audit history first.")


def apply(entity_path: Path | str, account: str, plan_hash: str | None = None,
          added_event: int | None = None) -> dict[str, Any]:
    """Remove the account. An account that is not in the catalog returns already_applied."""
    from .accrual.enable import _seal
    from .ledger.importer import _entity_write_lock
    from .ledger.store import LedgerStore, default_store_path
    root = Path(entity_path).resolve()
    account = _check_name(account)
    with _entity_write_lock(root, repair=False):
        fresh = preview(root, account, added_event)
        if not fresh["changes"]:
            # The account is already absent: an exact retry or a no-op. Resolve it before the plan hash
            # check, which guards writes only.
            return {"status": "already_applied", "kind": KIND, "account": account, "request_hash": fresh["request_hash"]}
        if plan_hash is not None and plan_hash != fresh["plan_hash"]:
            raise AccountRemoveError("stale_preview", "The company changed after the preview.", "Run the preview again.")
        store = LedgerStore(default_store_path(root))
        effects = fresh["effects"]
        with store.transaction(immediate=True) as conn:
            _require_audit_history(conn)
            head = conn.execute("SELECT record_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
            if (head[0] if head else "none") != fresh["audit_head"]:
                raise AccountRemoveError("stale_preview", "The company changed after the preview.", "Run the preview again.")
            if conn.execute("SELECT 1 FROM postings WHERE account=? UNION ALL SELECT 1 FROM balance_assertions WHERE account=? "
                            "LIMIT 1", (account, account)).fetchone():
                raise AccountRemoveError("account_in_use", f"{account} is in use.", "Leave the account in the catalog.")
            conn.execute("DELETE FROM accounts WHERE name=?", (account,))
            store.append_audit_event("account-removed", {"account": account, "open_date": effects["open_date"],
                                                         "currency": effects["currency"], "added_event": effects["added_event"],
                                                         "plan_hash": fresh["plan_hash"]}, conn)
            _seal(store, conn, "entity-account-remove")
        return {"status": "applied", "kind": KIND, "account": account, "open_date": effects["open_date"],
                "currency": effects["currency"], "added_event": effects["added_event"],
                "request_hash": fresh["request_hash"], "plan_hash": fresh["plan_hash"], "entries_changed": 0}
