"""``books entity account-rename``: give an account a new name and keep its history.

A rename changes a label, never an amount. Every posting and balance assertion of the account moves to the new
name in one ledger transaction, together with the catalog row, its open-date marker and the chart records. The
audit event ``account-renamed`` records the old and the new name of each account, and a new seal covers the result.
Past entries show under the new name. The audit log keeps the old name in its earlier events, and the
``account-renamed`` event connects the two names (``rename_lineage``).

What changes
------------

* Ledger (one transaction): ``accounts.name`` (the row keeps its id, open date, currency and financial kind),
  ``postings.account``, ``balance_assertions.account``, the open-date provenance markers, and the JSON meta
  records that name accounts (``chart_presentation_v1``, ``chart_adoption_v1``, ``reporting_goals`` and others).
  An adopted display name of the renamed account is cleared, so the new name shows; its number stays.
* Company settings and records (after the ledger commit, one atomic file replace each): ``entity.json`` (bank and
  CSV mapping targets, tax line overrides, account numbers, rules), ``trust-policy.json``, ``learned-context/``
  JSON files (payee memory, accrual rules), open review items in ``review-queue/``, the staging files that are
  not source rows (``split-templates.json``, ``context-history.json``, ``duplicate-candidates.json``; the
  ``transaction`` object inside them is a source row and stays), ``reports/reconciliation.json`` and the
  statement and feed-window records in ``ingestion/statements/``.
* A child account moves with its parent: ``Expenses:Office`` to ``Expenses:Office-Costs`` also renames
  ``Expenses:Office:Supplies`` to ``Expenses:Office-Costs:Supplies``. A reference to a parent name (an accrual
  rule that selects ``Expenses:Office`` and its children) moves too, so it selects the same accounts.

What stays
----------

* Source evidence: staged source rows (``staging/pending.json``, ``staging/pending-categorization.json``), the
  ``source_transactions`` and ``import_sessions`` rows, raw files in ``ingestion/``, entry narration and entry or
  posting metadata, and decided review items. Their digests bind them, and they record what arrived.
* Mapping keys: the keys of ``bank_account_mappings`` and ``compare.qbo_account_crosswalk`` are provider or
  QuickBooks identifiers, not ledger names.
* A CSV or statement row carries the ledger account of its mapping at parse time. The rename adds the old name to
  ``former_ledger_accounts`` of each confirmed CSV mapping that named it, so a row parsed before the rename still
  matches the mapping and posts to the new name.

Rules
-----

* Only the last part of the name changes: the root and the parent stay. A move to another parent is not a rename.
* The new name, and the new name of each child, must not exist (also not with other letter case), and must not
  be an engine account name.
* A system account keeps its name (``engine_accounts.rename_protected_accounts``). A starter income or expense
  category is the company's own and can be renamed.
* A balance sheet or equity account can be renamed only when it has no child accounts. Engine code reads the
  group names (``Assets:Bank``, ``Liabilities:CreditCard``, ``Liabilities:Loans``), so a group stays.
* A rename that moves an account into or out of cost of sales (``reports.structure.is_cost_of_sales``) changes
  the profit and loss structure, not a label, and is refused.
* An account that accrual or business records name is refused: those records bind facts with digests.
* A staged source row that names the account outside ``accountId`` is refused: post or clear it first.
* Closed and locked periods do not block a rename. Amounts, dates and the number of entries stay the same, so
  every closed report keeps its totals; only the label changes. The preview lists the locked months that have
  postings on the account, and the audit event is the record of the change.

Flow
----

The command previews first and writes nothing. ``--apply --plan-hash H`` makes the change. The plan hash covers
the request, the ledger digest, the audit chain head and a fingerprint of every company file that the checks
read, so a changed company refuses with ``stale_preview``. Inside the ledger transaction the command checks that
every balance is the same under the new name; if not, the transaction rolls back.

A retry of a finished rename (the old name is absent, the new name is present, and the newest
``account-renamed`` event of the old name names the new one) returns ``already_applied``. When the command stopped
after the ledger commit and before every file was replaced, the retry finishes the file updates.
"""
from __future__ import annotations

import json
import os
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .account_remove import AccountRemoveError, _company_files, parse_settings

KIND = "account_rename"
EVENT = "account-renamed"
SESSION = "entity-account-rename"
ROOTS = ("Assets", "Liabilities", "Equity", "Income", "Expenses")
GROUP_ROOTS = ("Income", "Expenses")
_CLOSED_QUEUE_STATUSES = frozenset({"confirmed", "corrected"})
# Staging files that hold source rows. Their digests bind them, so they are read, never rewritten.
SOURCE_ROW_FILES = frozenset({"staging/pending.json", "staging/pending-categorization.json", "staging/seen-ids.json"})
# Keys whose objects are source rows, inside files that are otherwise rewritten.
_SOURCE_ROW_KEYS = frozenset({"transaction"})
_ALIAS_KEY = "former_ledger_accounts"
# entity.json maps whose keys are provider or QuickBooks identifiers. Their values are still renamed.
_SOURCE_KEYED_MAPS = frozenset({"bank_account_mappings", "compare.qbo_account_crosswalk"})
_PENDING_DIRS = ("review-queue/.transactions", "reports/.reconciliation-transactions")
_MAX_REFS = 20


class AccountRenameError(AccountRemoveError):
    """The account cannot be renamed. ``code`` names the rule; ``refs`` lists what blocks it."""

    def __init__(self, code: str, message: str, action: str = "Keep the current name.",
                 refs: list[str] | None = None) -> None:
        super().__init__(code, message, action, refs)


def _unreadable(name: str) -> AccountRenameError:
    return AccountRenameError("reference_check_failed",
                              f"{name} cannot be read safely, so the references cannot be checked.",
                              "Repair the file (no symbolic links, valid JSON, readable), then run the preview again.",
                              [name])


def _valid(name: Any) -> bool:
    from .ledger.model import _validate_account_name
    if not isinstance(name, str) or ":" not in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return False
    try:
        _validate_account_name(name)
    except ValueError:
        return False
    return True


def _check_names(old: Any, new: Any) -> tuple[str, str]:
    if not _valid(old):
        raise AccountRenameError("invalid_account", f"{old!r} is not a valid account name.",
                                 "Give the account name exactly as the account catalog shows it.")
    if not _valid(new):
        raise AccountRenameError("invalid_name", f"{new!r} is not a valid account name.",
                                 "Use the same root and parent, and a last part of letters, digits and hyphens that "
                                 "starts with a capital letter or a digit, for example Expenses:Office-Costs.")
    if old.rsplit(":", 1)[0] != new.rsplit(":", 1)[0]:
        raise AccountRenameError("parent_change",
                                 f"{new} has another parent than {old}. A rename changes only the last part of the name.",
                                 f"Keep the part before the last colon: {old.rsplit(':', 1)[0]}:<new name>.")
    if old == new:
        raise AccountRenameError("same_name", f"{old} already has this name.", "Give a different new name.")
    return old, new


def renamed(value: Any, old: str, new: str) -> str | None:
    """The new spelling of *value* when it names *old* or a child of it, else ``None``."""
    if not isinstance(value, str):
        return None
    if value == old:
        return new
    if value.startswith(old + ":") and _valid(value):
        return new + value[len(old):]
    return None


def _rewrite(node: Any, old: str, new: str, path: str, changes: list[str], *,
             keep_keys: frozenset[str] = frozenset(), skip_keys: frozenset[str] = frozenset()) -> Any:
    """A copy of the JSON *node* with every name of *old* (and its children) renamed; *changes* gets each path."""
    value = renamed(node, old, new)
    if value is not None:
        changes.append(path or "$")
        return value
    if isinstance(node, list):
        return [_rewrite(child, old, new, f"{path}[{index}]", changes, keep_keys=keep_keys, skip_keys=skip_keys)
                for index, child in enumerate(node)]
    if not isinstance(node, dict):
        return node
    result: dict[str, Any] = {}
    for key, child in node.items():
        child_path = f"{path}.{key}" if path else str(key)
        if key in skip_keys:
            result[key] = child
            continue
        new_key = None if path in keep_keys else renamed(key, old, new)
        if new_key is not None:
            if new_key in node or new_key in result:
                raise AccountRenameError("name_in_use", f"{child_path} and {new_key} would be one setting after the rename.",
                                         "Remove the setting of the new name first, then run the preview again.",
                                         [child_path])
            changes.append(child_path)
        result[new_key or key] = _rewrite(child, old, new, child_path, changes, keep_keys=keep_keys, skip_keys=skip_keys)
    return result


def _find(node: Any, old: str, path: str = "") -> list[str]:
    """JSON paths where *node* names *old* or a child of it, as a value or a key."""
    found: list[str] = []
    stack: list[tuple[Any, str]] = [(node, path)]
    while stack:
        item, where = stack.pop()
        if renamed(item, old, old) is not None:
            found.append(where or "$")
        elif isinstance(item, dict):
            for key, child in item.items():
                child_path = f"{where}.{key}" if where else str(key)
                if renamed(key, old, old) is not None:
                    found.append(child_path)
                stack.append((child, child_path))
        elif isinstance(item, list):
            stack.extend((child, f"{where}[{index}]") for index, child in enumerate(item))
    return sorted(found)


def _add_csv_aliases(config: Any, original: Any, old: str, new: str, changes: list[str]) -> None:
    """Add the old ledger name to each confirmed CSV mapping that the rename moved."""
    mappings = config.get("csv_account_mappings") if isinstance(config, dict) else None
    before = original.get("csv_account_mappings") if isinstance(original, dict) else None
    if not isinstance(mappings, dict) or not isinstance(before, dict):
        return
    for key, mapping in mappings.items():
        prior = before.get(key)
        if not isinstance(mapping, dict) or not isinstance(prior, dict):
            continue
        earlier = prior.get("ledger_account")
        if renamed(earlier, old, new) is None or mapping.get("ledger_account") == earlier:
            continue
        former = mapping.get(_ALIAS_KEY)
        former = list(former) if isinstance(former, list) else []
        if earlier not in former:
            mapping[_ALIAS_KEY] = former + [earlier]
            changes.append(f"csv_account_mappings.{key}.{_ALIAS_KEY}")


def _plan_file(name: str, document: Any, old: str, new: str) -> tuple[Any, list[str]] | None:
    """The new content of one company file and the changed paths, or ``None`` when the file stays as it is."""
    if name in SOURCE_ROW_FILES or name.startswith("review-queue/.transactions/") \
            or name.startswith("reports/.reconciliation-transactions/"):
        return None
    if name.startswith("review-queue/"):
        if name.count("/") != 1 or not isinstance(document, dict) or document.get("status") in _CLOSED_QUEUE_STATUSES:
            return None  # A decided item is history; its posted decision shows in the postings.
    if not name.endswith(".json"):
        return None
    changes: list[str] = []
    keep = _SOURCE_KEYED_MAPS if name == "entity.json" else frozenset()
    skip = frozenset({_ALIAS_KEY}) | (_SOURCE_ROW_KEYS if name.startswith("staging/") else frozenset())
    result = _rewrite(document, old, new, "", changes, keep_keys=keep, skip_keys=skip)
    if name == "entity.json":
        _add_csv_aliases(result, document, old, new, changes)
    return (result, changes) if changes else None


def _staged_refs(documents: dict[str, Any], old: str) -> list[str]:
    """Places where a staged source row names the account outside ``accountId``."""
    refs = []
    for name in sorted(SOURCE_ROW_FILES & set(documents)):
        refs += [f"{name}:{where}" for where in _find(documents[name], old)
                 if not (where.endswith(".accountId") or where == "accountId")]
    return refs


def rename_lineage(events: list[dict[str, Any]], account: str) -> list[tuple[str, int | None]]:
    """The names of *account* through ``account-renamed`` events: ``(name, last event id with that name)``.

    The current name comes first with ``None``. Each earlier name comes with the id of the event that renamed it,
    so a caller can ignore what a later account with the earlier name did.
    """
    lineage: list[tuple[str, int | None]] = [(account, None)]
    current, before = account, None
    for event in reversed(events):
        if event.get("type") != EVENT or (before is not None and event["id"] >= before):
            continue
        for pair in (event.get("payload") or {}).get("accounts") or []:
            if isinstance(pair, list) and len(pair) == 2 and pair[1] == current and isinstance(pair[0], str):
                current, before = pair[0], event["id"]
                lineage.append((current, before))
                break
    return lineage


def _latest_rename(events: list[dict[str, Any]], old: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("type") == EVENT and (event.get("payload") or {}).get("from") == old:
            return event
    return None


def _locked_months(root: Path, store: Any, conn: Any, accounts: list[str]) -> list[str]:
    """Locked or closed months that have postings on the accounts. Facts for the preview; they do not block."""
    from .accrual.store import list_records
    from .ledger.reclassify import ReclassifyError, _lock_reason, _period_state
    try:
        config = json.loads((root / "entity.json").read_text(encoding="utf-8"))
        closed, through = _period_state(config if isinstance(config, dict) else {})
    except (OSError, ValueError, ReclassifyError):
        return []
    periods = {row.get("id"): row.get("status") for row in list_records(store, "periods", conn)}
    marks = ",".join("?" for _ in accounts)
    months = {str(day)[:7] for (day,) in conn.execute(
        f"SELECT DISTINCT e.date FROM postings p JOIN entries e ON e.id = p.entry_id WHERE p.account IN ({marks})",
        accounts)}
    # The first day of a month is locked when any lock covers the month (a month lock, a closed accrual period,
    # or a closed-through date on or after that day).
    return sorted(month for month in months if _lock_reason(closed, through, periods, f"{month}-01"))


def _record_refs(store: Any, conn: Any, old: str) -> list[str]:
    """Accrual and business records that name the account. They bind facts with digests, so they block."""
    from .accrual.store import TABLES, list_records
    from .ledger.store import BUSINESS_SCHEMA_VERSION
    tables = list(TABLES)
    if store.schema_version(conn) == BUSINESS_SCHEMA_VERSION:
        from .business import BUSINESS_TABLES
        tables += [f"business_{table}" for table in BUSINESS_TABLES]
    found = []
    for table in tables:
        for record in list_records(store, table, conn):
            found += [f"{table} {record.get('id', '?')}:{where}" for where in _find(record, old)]
    return found


def _tax_line_changes(documents: dict[str, Any], pairs: list[list[str]]) -> list[dict[str, Any]]:
    """Accounts whose suggested tax line (a name rule, not a saved override) changes with the name."""
    from . import taxmap
    config = documents.get("entity.json") if isinstance(documents.get("entity.json"), dict) else {}
    try:
        form, _ = taxmap.resolve_form(config)
    except ValueError:
        return []
    overrides = ((config.get("tax_handoff") or {}).get("line_overrides") or {}).get(form) or {}
    changes = []
    for before, after in pairs:
        if before in overrides:
            continue  # The saved override moves with the name.
        line_before, line_after = taxmap.map_account(before, form).line, taxmap.map_account(after, form).line
        if line_before != line_after:
            changes.append({"from": before, "to": after, "form": form,
                            "line_before": line_before.code if line_before else None,
                            "line_after": line_after.code if line_after else None})
    return changes


def _balances(conn: Any, accounts: list[str]) -> dict[tuple[str, str], tuple[Decimal, int]]:
    marks = ",".join("?" for _ in accounts)
    totals: dict[tuple[str, str], tuple[Decimal, int]] = {}
    for table in ("postings", "balance_assertions"):
        for account, amount, currency in conn.execute(
                f"SELECT account, amount, currency FROM {table} WHERE account IN ({marks})", accounts):
            key = (f"{table}:{account}", str(currency))
            total, count = totals.get(key, (Decimal("0"), 0))
            totals[key] = (total + Decimal(str(amount)), count + 1)
    return totals


def _ledger_names(conn: Any, old: str) -> list[str]:
    """The old name and every name under it in the catalog, the postings and the balance assertions."""
    names: set[str] = set()
    for table, column in (("accounts", "name"), ("postings", "account"), ("balance_assertions", "account")):
        names |= {str(name) for (name,) in conn.execute(
            f"SELECT DISTINCT {column} FROM {table} WHERE {column} = ? OR substr({column}, 1, ?) = ?",
            (old, len(old) + 1, old + ":"))}
    return sorted(names)


def _meta_updates(conn: Any, old: str, new: str) -> tuple[dict[str, str], list[str], Any]:
    """New values of the JSON meta records that name the account, the changed paths, and a cleared display name."""
    from . import chart_presentation
    from .ledger.store import OPEN_DATE_PROVENANCE_KEY
    updates: dict[str, str] = {}
    changes: list[str] = []
    cleared = None
    for key, raw in conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall():
        if key == OPEN_DATE_PROVENANCE_KEY:
            continue  # The store API rekeys the markers.
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not isinstance(value, (dict, list)):
            continue
        found: list[str] = []
        try:
            result = _rewrite(value, old, new, "", found)
        except RecursionError:
            raise _unreadable(f"ledger meta {key}") from None
        if key == chart_presentation.KEY and isinstance(result, dict) and isinstance(result.get(new), dict) \
                and result[new].get("display_name"):
            cleared = result[new]["display_name"]
            result[new] = {**result[new], "display_name": None}
            found.append(f"{new}.display_name")
        if found:
            updates[key] = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            changes += [f"meta {key}:{where}" for where in found]
    return updates, changes, cleared


def _serialize(document: Any) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _file_plans(documents: dict[str, Any], old: str, new: str) -> dict[str, tuple[bytes, list[str]]]:
    plans: dict[str, tuple[bytes, list[str]]] = {}
    for name in sorted(documents):
        try:
            plan = _plan_file(name, documents[name], old, new)
        except RecursionError:
            raise _unreadable(name) from None
        if plan is not None:
            plans[name] = (_serialize(plan[0]), plan[1])
    return plans


def _check_pending(root: Path) -> None:
    for folder in _PENDING_DIRS:
        path = root / folder
        if path.is_dir() and any(path.iterdir()):
            raise AccountRenameError("operation_pending", f"An unfinished operation is in {folder}.",
                                     "Finish it first (run the queue or reconciliation command again), "
                                     "then run the preview again.", [folder])


def _open_store(root: Path, files: dict[str, bytes]) -> Any:
    from .ledger.importer import check_store_integrity
    from .ledger.store import SUPPORTED_SCHEMA_VERSIONS, LedgerStore, default_store_path
    if "entity.json" not in files:
        raise AccountRenameError("no_company", f"No entity.json found at '{root}'.", "Run books entity init first.")
    store = LedgerStore(default_store_path(root))
    if not store.path.is_file() or store.path.is_symlink():
        raise AccountRenameError("no_ledger", "The company has no SQLite ledger.",
                                 "Run books entity init, or migrate the ledger explicitly, first.")
    if store.schema_version() not in SUPPORTED_SCHEMA_VERSIONS:
        raise AccountRenameError("unsupported_schema", "The ledger schema is not supported by this engine.",
                                 "Use a compatible engine.")
    if check_store_integrity(store).status != "ok":
        raise AccountRenameError("ledger_integrity", "Cannot rename an account while ledger integrity is invalid.",
                                 "Run the integrity check and fix the reported problem first.")
    return store


def _require_audit_history(conn: Any) -> None:
    from .ledger.store import has_ledger_audit_history
    if conn.execute("SELECT 1 FROM entries LIMIT 1").fetchone() and not has_ledger_audit_history(conn):
        raise AccountRenameError("audit_history_missing",
                                 "The account catalog cannot change on a ledger with entries and no audit history.",
                                 "Repair the audit history first.")


def _check_rename(conn: Any, store: Any, documents: dict[str, Any], old: str, new: str,
                  names: list[str], pairs: list[list[str]]) -> None:
    from .engine_accounts import rename_protected_accounts
    from .reports.structure import is_cost_of_sales
    protected = rename_protected_accounts()
    blocked = sorted(name for name in names if name in protected)
    if blocked:
        raise AccountRenameError("protected_account", f"{blocked[0]} is a system account, so it keeps its name.",
                                 refs=blocked[:_MAX_REFS])
    reserved = sorted(after for _, after in pairs if after in protected)
    if reserved:
        raise AccountRenameError("reserved_name", f"{reserved[0]} is the name of a system account.",
                                 "Choose another name.", reserved[:_MAX_REFS])
    if old.split(":")[0] not in GROUP_ROOTS and len(names) > 1:
        raise AccountRenameError("has_children",
                                 f"{old} has child accounts. A balance sheet or equity group keeps its name.",
                                 "Rename each child account instead.", [n for n in names if n != old][:_MAX_REFS])
    catalog = {str(name) for (name,) in conn.execute("SELECT name FROM accounts")}
    moving = set(names)
    folded = {name.casefold(): name for name in catalog - moving}
    taken = sorted(after for _, after in pairs if after.casefold() in folded)
    if taken:
        raise AccountRenameError("name_exists", f"An account named {folded[taken[0].casefold()]} already exists.",
                                 "Choose another name, or move the entries with books ledger reclassify.",
                                 [folded[name.casefold()] for name in taken][:_MAX_REFS])
    changed = sorted(before for before, after in pairs if is_cost_of_sales(before) != is_cost_of_sales(after))
    if changed:
        raise AccountRenameError("classification_change",
                                 f"The new name moves {changed[0]} into or out of cost of sales. That changes the "
                                 "profit and loss structure, not only a label.",
                                 "Keep the cost of sales group name, or choose a name in the same group.",
                                 changed[:_MAX_REFS])
    records = _record_refs(store, conn, old)
    if records:
        raise AccountRenameError("records_use_account",
                                 f"Accrual or business records name {old} ({len(records)} places). They bind their "
                                 "facts with digests, so a rename is not available for this account.",
                                 refs=records[:_MAX_REFS])
    staged = _staged_refs(documents, old)
    if staged:
        raise AccountRenameError("staged_rows_name_account",
                                 f"Staged source rows name {old} ({len(staged)} places). Source rows are not rewritten.",
                                 "Post or clear the staged rows first, then run the preview again.", staged[:_MAX_REFS])


def _open_item_intents(store: Any, plans: dict[str, Any], documents: dict[str, Any]) -> list[str]:
    """Open review items that the rename changes and that have a queue finalization record (an unfinished confirm)."""
    items = {str(documents[name].get("source_id") or ""): name for name in plans
             if name.startswith("review-queue/") and isinstance(documents.get(name), dict)}
    if not items:
        return []
    found = set()
    for event in store.load_audit_events():
        payload = event.get("payload") or {}
        if event.get("type") == "queue-finalization-intent" and isinstance(payload, dict) \
                and payload.get("item_id") in items:
            found.add(items[payload["item_id"]])
    return sorted(found)


def _context(root: Path, old: str, new: str) -> dict[str, Any]:
    """Read the company and decide the rename. Writes nothing."""
    from .accrual.intake import canonical_hash
    _check_pending(root)
    files = _company_files(root, loan=True)
    try:
        documents = parse_settings(files)
    except AccountRemoveError as exc:
        raise _unreadable(exc.refs[0] if exc.refs else "company files") from None
    store = _open_store(root, files)
    events = store.load_audit_events()
    request = {"from": old, "to": new}
    with store.connection() as conn:
        _require_audit_history(conn)
        in_catalog = {name for name in (old, new)
                      if conn.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone()}
        names = _ledger_names(conn, old)
        done = None
        if old not in in_catalog:
            latest = _latest_rename(events, old)
            target = (latest or {}).get("payload", {}).get("to")
            if latest is None or (target == new and new not in in_catalog):
                raise AccountRenameError("unknown_account", f"{old} is not in the account catalog.",
                                         "Give the account name exactly as the account catalog shows it.")
            if target != new:
                raise AccountRenameError("renamed_elsewhere", f"{old} was renamed to {target}.",
                                         f"Rename {target} instead.", [f"audit event {latest['id']}"])
            if names:
                raise AccountRenameError("ledger_integrity", f"The ledger still names {old} after its rename.",
                                         "Run the integrity check and fix the reported problem first.", names[:_MAX_REFS])
            done = latest
            pairs = [list(pair) for pair in latest["payload"].get("accounts") or []]
        else:
            pairs = [[name, new + name[len(old):]] for name in names]
            _check_rename(conn, store, documents, old, new, names, pairs)
        plans = _file_plans(documents, old, new)
        if done is None:
            meta, meta_changes, cleared = _meta_updates(conn, old, new)
            counts = {before: (conn.execute("SELECT COUNT(*) FROM postings WHERE account=?", (before,)).fetchone()[0],
                               conn.execute("SELECT COUNT(*) FROM balance_assertions WHERE account=?",
                                            (before,)).fetchone()[0]) for before, _ in pairs}
            locked = _locked_months(root, store, conn, [before for before, _ in pairs])
        busy = _open_item_intents(store, plans, documents)
        if busy:
            raise AccountRenameError("operation_pending", "A review item that names the account has an unfinished "
                                     "confirmation.", "Finish or retry the confirmation first.", busy[:_MAX_REFS])
    body: dict[str, Any] = {
        "schema_version": 1, "kind": KIND, "request": request, "changes": done is None,
        "ledger_digest": store.content_digest(), "audit_head": events[-1]["record_hash"] if events else "none",
        "file_fingerprints": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    if done is None:
        body["effects"] = {
            "from": old, "to": new,
            "accounts": [{"from": before, "to": after, "postings": counts[before][0],
                          "balance_assertions": counts[before][1]} for before, after in pairs],
            "postings_relabelled": sum(count[0] for count in counts.values()),
            "balance_assertions_relabelled": sum(count[1] for count in counts.values()),
            "entries_changed": 0, "amounts_changed": 0,
            "files": sorted(plans), "ledger_records": sorted({c.split(":", 1)[0] for c in meta_changes}),
            "display_name_cleared": cleared, "locked_months_with_postings": locked,
            "tax_line_changes": _tax_line_changes(documents, pairs)}
    body["request_hash"] = canonical_hash({"kind": KIND, "request": request})
    body["plan_hash"] = canonical_hash(body)
    return {"body": body, "plans": plans, "pairs": pairs, "done": done,
            "meta": meta if done is None else {}, "store": store}


def preview(entity_path: Path | str, old: str, new: str) -> dict[str, Any]:
    """Preview the rename. Writes nothing, and does not repair entity.json."""
    old, new = _check_names(old, new)
    context = _context(Path(entity_path).resolve(), old, new)
    body = context["body"]
    if context["done"] is not None:
        return {"status": "already_applied", "kind": KIND, "from": old, "to": new,
                "event": context["done"]["id"], "files_pending": sorted(context["plans"]),
                "request_hash": body["request_hash"]}
    return {"status": "preview", **body}


def _write_files(root: Path, plans: dict[str, tuple[bytes, list[str]]]) -> list[str]:
    from .file_safety import sync_directory
    written = []
    for name, (data, _) in sorted(plans.items()):
        path = root / name
        tmp = path.with_name(path.name + ".rename.tmp")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        sync_directory(path.parent)
        written.append(name)
    return written


def apply(entity_path: Path | str, old: str, new: str, plan_hash: str | None = None) -> dict[str, Any]:
    """Rename the account. A finished rename returns ``already_applied`` and finishes any file left behind."""
    from .accrual.enable import _seal
    from .ledger.importer import _entity_write_lock
    root = Path(entity_path).resolve()
    old, new = _check_names(old, new)
    with _entity_write_lock(root, repair=False):
        context = _context(root, old, new)
        body, store = context["body"], context["store"]
        if context["done"] is not None:
            # An exact retry, or a rename that stopped after the ledger commit: finish the files. The plan hash
            # guards new writes only.
            written = _write_files(root, context["plans"])
            return {"status": "already_applied", "kind": KIND, "from": old, "to": new, "event": context["done"]["id"],
                    "files_completed": written, "request_hash": body["request_hash"]}
        if plan_hash is not None and plan_hash != body["plan_hash"]:
            raise AccountRenameError("stale_preview", "The company changed after the preview.", "Run the preview again.")
        pairs, effects = context["pairs"], body["effects"]
        with store.transaction(immediate=True) as conn:
            _require_audit_history(conn)
            head = conn.execute("SELECT record_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
            if (head[0] if head else "none") != body["audit_head"]:
                raise AccountRenameError("stale_preview", "The company changed after the preview.", "Run the preview again.")
            before = _balances(conn, [b for b, _ in pairs])
            record = store.load_open_date_provenance(conn)
            for old_name, new_name in pairs:
                conn.execute("UPDATE accounts SET name=? WHERE name=?", (new_name, old_name))
                conn.execute("UPDATE postings SET account=? WHERE account=?", (new_name, old_name))
                conn.execute("UPDATE balance_assertions SET account=? WHERE account=?", (new_name, old_name))
                if record is not None and old_name in record["accounts"]:
                    record["accounts"][new_name] = record["accounts"].pop(old_name)
            if record is not None:
                store.save_open_date_provenance(record, conn)
            for key, value in context["meta"].items():
                conn.execute("UPDATE meta SET value=? WHERE key=?", (value, key))
            after = _balances(conn, [a for _, a in pairs])
            moved = dict(pairs)
            expected = {(f"{key.split(':', 1)[0]}:{moved[key.split(':', 1)[1]]}", currency): total
                        for (key, currency), total in before.items()}
            if after != expected:
                raise AccountRenameError("balance_changed", "A balance changed during the rename, so nothing was written.",
                                         "Report this problem; the ledger was not changed.")
            store.append_audit_event(EVENT, {
                "from": old, "to": new, "accounts": [list(pair) for pair in pairs],
                "postings": effects["postings_relabelled"], "balance_assertions": effects["balance_assertions_relabelled"],
                "files": effects["files"], "display_name_cleared": effects["display_name_cleared"],
                "request_hash": body["request_hash"], "plan_hash": body["plan_hash"]}, conn)
            _seal(store, conn, SESSION)
        written = _write_files(root, context["plans"])
        return {"status": "applied", "kind": KIND, "from": old, "to": new, "accounts": effects["accounts"],
                "postings_relabelled": effects["postings_relabelled"],
                "balance_assertions_relabelled": effects["balance_assertions_relabelled"],
                "entries_changed": 0, "amounts_changed": 0, "files_updated": written,
                "display_name_cleared": effects["display_name_cleared"],
                "locked_months_with_postings": effects["locked_months_with_postings"],
                "tax_line_changes": effects["tax_line_changes"],
                "request_hash": body["request_hash"], "plan_hash": body["plan_hash"]}


def run(args: Any, resolve: Callable[[Path], Path]) -> int:
    """CLI entry: print the result as JSON (``--json``) or as text. Exit 1 on a refusal."""
    try:
        target = resolve(args.path)
        if args.apply:
            result = apply(target, args.account, args.to, args.plan_hash)
        else:
            result = preview(target, args.account, args.to)
        code = 0
    except (ValueError, OSError) as exc:
        if hasattr(exc, "as_dict"):
            result = exc.as_dict()
        elif isinstance(exc, OSError):
            result = _unreadable(getattr(exc, "filename", None) or "company files").as_dict()
        else:
            result = {"status": "blocked", "errors": [{"code": "invalid_operation", "message": str(exc)}]}
        code = 1
    if getattr(args, "json", False):
        print(json.dumps(result, sort_keys=True, indent=2))
    else:
        print(_text(result))
    return code


def _text(result: dict[str, Any]) -> str:
    status = result.get("status")
    if status == "blocked":
        error = result["errors"][0]
        lines = [f"Refused ({error['code']}): {error['message']}", f"Action: {error.get('action', '')}"]
        lines += [f"  {ref}" for ref in error.get("refs", [])]
        return "\n".join(lines)
    if status == "already_applied":
        return f"Already renamed: {result['from']} -> {result['to']}."
    effects = result.get("effects", result)
    lines = [f"{'Preview' if status == 'preview' else 'Renamed'}: {effects['from']} -> {effects['to']}"]
    for item in effects["accounts"]:
        lines.append(f"  {item['from']} -> {item['to']} ({item['postings']} postings, "
                     f"{item['balance_assertions']} balance assertions)")
    lines.append("Amounts and entries do not change.")
    if effects.get("locked_months_with_postings"):
        lines.append("Locked months with postings (labels change, totals stay): "
                     + ", ".join(effects["locked_months_with_postings"]))
    if status == "preview":
        lines.append(f"Apply with --apply --plan-hash {result['plan_hash']}")
    return "\n".join(lines)
