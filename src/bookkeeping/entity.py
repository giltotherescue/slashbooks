"""Entity directory scaffolding, configuration, and loader.

Provides:
  - ``entity init <path>`` CLI subcommand
  - ``load_entity(path)`` for reading an initialised entity directory
  - ``add_parser(subparsers)`` / ``run(args)`` — wired into cli.py by the
    orchestrator; this module is self-contained.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

ACCRUAL_CAPABILITY_VERSION = 1


def _locked_entity_operation(function: Callable[..., Any] | None = None, *, shared: bool = False) -> Callable[..., Any]:
    """Keep a complete workspace read or configuration update under its common lock.

    A writer takes the exclusive lock. A read passes ``shared=True``: readers share
    the lock, so a read needs no write access and does not block another read.
    """
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        def wrapped(entity_path: Path | str, *args: Any, **kwargs: Any) -> Any:
            from .ledger.importer import _entity_read_lock, _entity_write_lock

            # Preserve a missing company's read/validation error without scaffolding a directory.
            if not (Path(entity_path) / "entity.json").exists():
                return function(entity_path, *args, **kwargs)
            with (_entity_read_lock if shared else _entity_write_lock)(Path(entity_path)):
                return function(entity_path, *args, **kwargs)
        return wrapped
    return decorate(function) if function is not None else decorate


def resolve_basis(config: dict[str, Any], *, authoritative_basis: str | None = None) -> str:
    """Resolve the canonical basis, rejecting conflicting legacy aliases."""
    values = []
    for key in ("basis", "accounting_basis"):
        if key in config:
            value = config[key]
            if not isinstance(value, str) or value not in {"cash", "accrual"}:
                raise ValueError(f"Unsupported accounting basis in {key}.")
            values.append(value)
    if len(set(values)) > 1:
        raise ValueError("Conflicting accounting basis aliases.")
    basis = values[0] if values else "cash"
    if authoritative_basis is not None and basis != authoritative_basis:
        raise ValueError("Company accounting basis does not match authoritative basis.")
    version = config.get("accrual_capability_version", 0)
    if type(version) is not int or version not in {0, ACCRUAL_CAPABILITY_VERSION}:
        raise ValueError("Unsupported accrual capability version; use a compatible engine.")
    return basis


# The value this resolves is the default report view. For capability and
# discipline use bookkeeping.accrual.scope.
resolve_view = resolve_basis


def require_accrual(entity_or_config: Any) -> None:
    """Admit accrual writes only after explicit basis and version opt-in."""
    config = getattr(entity_or_config, "entity_config", entity_or_config)
    if resolve_basis(config) != "accrual" or config.get("accrual_capability_version") != ACCRUAL_CAPABILITY_VERSION:
        raise ValueError("Accrual writes require explicit accrual basis and capability version 1.")

# ---------------------------------------------------------------------------
# Template resolution
# ---------------------------------------------------------------------------

_ENV_TEMPLATES_DIR = "BOOKKEEPING_TEMPLATES_DIR"

# Account for the case where the package lives at various depths:
#   repo/src/bookkeeping/entity.py  ->  repo root is 2 parents up
_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parents[1]  # src/bookkeeping -> src -> repo root
_BUILTIN_TEMPLATES = _REPO_ROOT / "skills" / "books-onboard" / "templates"
_PACKAGED_TEMPLATES = _PACKAGE_DIR / "templates"


def _templates_dir() -> Path:
    """Prefer the env override and repo/plugin templates over wheel resources."""
    override = os.environ.get(_ENV_TEMPLATES_DIR)
    if override:
        return Path(override)
    if _BUILTIN_TEMPLATES.is_dir():
        return _BUILTIN_TEMPLATES
    return _PACKAGED_TEMPLATES


# ---------------------------------------------------------------------------
# Package-repo detection
# ---------------------------------------------------------------------------

def _is_package_repo(path: Path) -> bool:
    """Return True when *path* (or any ancestor) looks like the package repo.

    A directory is treated as the package repo when it contains both
    ``pyproject.toml`` *and* a ``src/bookkeeping`` sub-tree.
    """
    for candidate in [path, *path.parents]:
        if (candidate / "pyproject.toml").exists() and (candidate / "src" / "bookkeeping").is_dir():
            return True
    return False


def _resolve_target(raw: str | Path) -> Path:
    """Resolve a raw path argument to an absolute Path."""
    return Path(raw).resolve()


def _refuse_if_inside_package_repo(target: Path) -> None:
    """Raise SystemExit with a plain-English error when *target* is inside the
    package repo, to prevent accidentally init-ing inside the source tree."""
    # Walk up from target to find a package-repo ancestor
    for candidate in [target, *target.parents]:
        if (candidate / "pyproject.toml").exists() and (candidate / "src" / "bookkeeping").is_dir():
            print(
                f"Error: '{target}' is inside the books package repo at '{candidate}'.\n"
                "Choose a separate directory outside the package, for example:\n"
                "  ~/Documents/books/my-entity/",
                file=sys.stderr,
            )
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# Git detection and gitignore helpers
# ---------------------------------------------------------------------------

def _git_root(path: Path) -> Path | None:
    """Return the git work-tree root for *path*, or None if not git-managed."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path if path.is_dir() else path.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        git_dir = result.stdout.strip()
        # Resolve the work tree from the git dir
        if git_dir == ".git":
            return (path if path.is_dir() else path.parent).resolve()
        # Bare repo or unusual layout — treat as non-git
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path if path.is_dir() else path.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if top.returncode == 0:
            return Path(top.stdout.strip()).resolve()
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _gitignore_covers_secrets(gitignore_path: Path) -> bool:
    """Return True when the .gitignore at *gitignore_path* ignores local secrets."""
    if not gitignore_path.exists():
        return False
    text = gitignore_path.read_text(encoding="utf-8")
    return ".env" in text and ".secrets/" in text


# ---------------------------------------------------------------------------
# File-copy helpers
# ---------------------------------------------------------------------------

def _copy_if_absent(src: Path, dst: Path) -> bool:
    """Copy *src* to *dst* only when *dst* does not exist.

    Returns True when the file was created, False when it was already present.
    """
    if dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return True


def _mkdir_if_absent(path: Path) -> bool:
    """Create *path* directory when absent.  Returns True when created."""
    if path.exists():
        return False
    path.mkdir(parents=True, exist_ok=True)
    return True


# ---------------------------------------------------------------------------
# Init logic
# ---------------------------------------------------------------------------

_DIRS = [
    "learned-context",
    "review-queue",
    "staging",
    "ingestion",
    "ingestion/quickbooks",
    "ingestion/stripe",
    "ingestion/mercury",
    "ingestion/custom",
    "reports",
]

_CORE_ACCOUNTS: tuple[str, ...] = (
    "Assets:Bank:Checking",
    "Liabilities:CreditCard",
    "Equity:Opening-Balances",
    "Equity:Owner-Contributions",
    "Equity:Owner-Draws",
)
# Overhead that every small business has, whatever it sells. A chart adds these after its own accounts.
_COMMON_EXPENSE_ACCOUNTS: tuple[str, ...] = (
    "Expenses:Rent",
    "Expenses:Payroll",
    "Expenses:Payroll-Taxes",
    "Expenses:Utilities",
    "Expenses:Insurance",
    "Expenses:Bank-Fees",
    "Expenses:Professional-Fees",
)
_LEGACY_SERVICE_ACCOUNTS: tuple[str, ...] = (
    "Expenses:Software",
    "Expenses:Subcontractors",
    "Expenses:Meals",
    "Expenses:Marketing",
    "Expenses:Travel",
    "Expenses:Office",
    "Expenses:Fees",
    "Expenses:Taxes",
)
_SHOP_OVERHEAD_ACCOUNTS: tuple[str, ...] = (
    "Expenses:Software",
    "Expenses:Meals",
    "Expenses:Marketing",
    "Expenses:Travel",
    "Expenses:Office",
    "Expenses:Taxes",
)
# The first part of each chart keeps its earlier order, so an existing business type opens the same
# accounts as before and gains the common accounts at the end.
_STARTER_ACCOUNTS: dict[str, tuple[str, ...]] = {
    "services": _CORE_ACCOUNTS + ("Income:Consulting",) + _LEGACY_SERVICE_ACCOUNTS
    + ("Income:Services",) + _COMMON_EXPENSE_ACCOUNTS,
    "saas": _CORE_ACCOUNTS + ("Income:Subscriptions",) + _LEGACY_SERVICE_ACCOUNTS
    + ("Expenses:Hosting", "Expenses:Payment-Fees") + _COMMON_EXPENSE_ACCOUNTS,
    "services_and_subscriptions": _CORE_ACCOUNTS + ("Income:Consulting", "Income:Subscriptions")
    + _LEGACY_SERVICE_ACCOUNTS + ("Expenses:Hosting", "Expenses:Payment-Fees")
    + ("Income:Services",) + _COMMON_EXPENSE_ACCOUNTS,
    "retail": _CORE_ACCOUNTS + (
        "Income:Sales",
        "Liabilities:Sales-Tax-Payable",
        "Assets:Other:Inventory",
        "Expenses:Cost-of-Goods-Sold",
        "Expenses:Shipping",
        "Expenses:Payment-Fees",
    ) + _SHOP_OVERHEAD_ACCOUNTS + _COMMON_EXPENSE_ACCOUNTS,
    "restaurant": _CORE_ACCOUNTS + (
        "Income:Sales",
        "Liabilities:Sales-Tax-Payable",
        "Assets:Other:Inventory",
        "Expenses:Cost-of-Goods-Sold",
        "Expenses:Supplies",
        "Expenses:Repairs",
        "Expenses:Licenses-and-Permits",
        "Expenses:Payment-Fees",
    ) + _SHOP_OVERHEAD_ACCOUNTS + _COMMON_EXPENSE_ACCOUNTS,
    "trades": _CORE_ACCOUNTS + (
        "Income:Services",
        "Expenses:Cost-of-Sales:Materials",
        "Expenses:Cost-of-Sales:Subcontractors",
        "Expenses:Tools-and-Equipment",
        "Expenses:Vehicle",
        "Expenses:Licenses-and-Permits",
        "Expenses:Payment-Fees",
    ) + _SHOP_OVERHEAD_ACCOUNTS + _COMMON_EXPENSE_ACCOUNTS,
    "real_estate": _CORE_ACCOUNTS + (
        "Liabilities:Loans:Mortgage",
        "Assets:Fixed:Property",
        "Income:Rental-Income",
        "Expenses:Repairs",
        "Expenses:Property-Taxes",
        "Expenses:Mortgage-Interest",
        "Expenses:Property-Management",
        "Expenses:Payment-Fees",
    ) + _SHOP_OVERHEAD_ACCOUNTS + _COMMON_EXPENSE_ACCOUNTS,
    "other": _CORE_ACCOUNTS + (
        "Income:Sales",
        "Income:Services",
        "Expenses:Payment-Fees",
    ) + _SHOP_OVERHEAD_ACCOUNTS + _COMMON_EXPENSE_ACCOUNTS,
}
# Older names for a business type. The alias opens the chart of the type it names.
_BUSINESS_TYPE_ALIASES: dict[str, str] = {"consulting": "services"}
DEFAULT_BUSINESS_TYPE = "consulting"
BUSINESS_TYPES: tuple[str, ...] = tuple(_STARTER_ACCOUNTS) + tuple(_BUSINESS_TYPE_ALIASES)


def starter_chart(business_type: str) -> tuple[str, ...]:
    """The cash starter chart for ``business_type``; an alias or an unknown type opens the services chart."""
    key = _BUSINESS_TYPE_ALIASES.get(business_type, business_type)
    return _STARTER_ACCOUNTS.get(key, _STARTER_ACCOUNTS["services"])


# Added to the starter chart of an accrual company only; cash companies are unchanged.
_ACCRUAL_STARTER_ACCOUNTS: tuple[str, ...] = (
    "Assets:Receivable:Accounts-Receivable",
    "Assets:Other:Prepaid-Expenses",
    "Assets:Fixed:Equipment",
    "Assets:Fixed:Accumulated-Depreciation",
    "Liabilities:Payable:Accounts-Payable",
    "Liabilities:Other:Accrued-Liabilities",
    "Liabilities:Other:Deferred-Revenue",
    "Liabilities:Other:Customer-Deposits",
    "Expenses:Depreciation",
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DEFAULT_ACCOUNT_OPEN_DATE = date(2026, 1, 1)


def _starter_account_opens(business_type: str, open_date: date = _DEFAULT_ACCOUNT_OPEN_DATE,
                           basis: str = "cash") -> list[Any]:
    """Return starter SQLite account definitions for a business type and basis."""
    from .ledger.model import Open

    accounts = starter_chart(business_type)
    if basis == "accrual":
        accounts = accounts + _ACCRUAL_STARTER_ACCOUNTS
    return [
        Open(
            date=open_date,
            account=account,
            currencies=("USD",) if not account.startswith(("Equity:", "Income:")) else (),
        )
        for account in accounts
    ]


def accrual_starter_accounts(business_type: str) -> tuple[str, ...]:
    """Names of the accounts a new accrual company of ``business_type`` gets on top of the cash starter chart."""
    opens = _starter_account_opens(business_type, _DEFAULT_ACCOUNT_OPEN_DATE, "accrual")
    return tuple(opened.account for opened in opens[-len(_ACCRUAL_STARTER_ACCOUNTS):])


def _normalize_optional_date(value: str) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if not _DATE_RE.match(normalized):
        raise ValueError("Cutover date must use YYYY-MM-DD format.")
    return normalized


def _entity_json_for_name(
    name: str,
    business_type: str,
    legal_structure: str,
    cutover_date: str | None,
    template_json: dict[str, Any],
) -> dict[str, Any]:
    """Merge CLI arguments into the entity.json template."""
    merged = dict(template_json)
    if name:
        merged["name"] = name
    merged["business_type"] = business_type
    if legal_structure:
        merged["legal_structure"] = legal_structure.strip()
    if cutover_date:
        merged["cutover_date"] = cutover_date
    return merged


def normalize_website(value: str) -> str:
    """Validate business context without fetching the site."""
    value = value.strip()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
        parsed.port
    except ValueError:
        valid = False
    if not valid or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Use a website URL such as https://example.com without sign-in details.")
    return value


def init_entity(
    target: Path,
    name: str = "",
    business_type: str = "consulting",
    legal_structure: str = "",
    cutover_date: str = "",
    website_url: str = "",
    basis: str | None = None,
) -> dict[str, list[str]]:
    """Initialise (or safely re-init) an entity directory at *target*.

    Returns a report dict with keys ``"created"`` and ``"existed"``, each a
    list of relative path strings.
    """
    _refuse_if_inside_package_repo(target)
    selected_basis = basis or "cash"
    resolve_basis({"basis": selected_basis})
    if (target / "entity.json").exists() and basis is not None:
        existing_config = json.loads((target / "entity.json").read_text(encoding="utf-8"))
        if resolve_basis(existing_config) != basis:
            raise ValueError("Re-init preserves accounting basis; use configure_basis for an empty company.")
    website_url = normalize_website(website_url)
    normalized_cutover_date = _normalize_optional_date(cutover_date)
    starter_open_date = date.fromisoformat(normalized_cutover_date) if normalized_cutover_date else _DEFAULT_ACCOUNT_OPEN_DATE

    templates = _templates_dir()
    target.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    existed: list[str] = []

    is_reinit = (target / "entity.json").exists()

    def _track(path: Path, was_created: bool) -> None:
        rel = str(path.relative_to(target))
        (created if was_created else existed).append(rel)

    # --- Directories ---------------------------------------------------------
    for dir_name in _DIRS:
        d = target / dir_name
        _track(d, _mkdir_if_absent(d))

    # --- ledger.sqlite ------------------------------------------------------
    store_path = target / "ledger.sqlite"
    books_path = target / "books.beancount"
    if is_reinit and store_path.exists():
        existed.append("ledger.sqlite")
    elif books_path.exists():
        existed.append("books.beancount")
    else:
        if not store_path.exists():
            from .ledger.store import LedgerStore

            store = LedgerStore(store_path)
            store.initialize()
            with store.transaction() as conn:
                store.set_meta("canonical", "true", conn)
                store.set_meta("account_catalog", "sqlite", conn)
                store.set_meta("business_type", business_type, conn)
                store.set_meta("basis", selected_basis, conn)
                if name:
                    store.set_meta("title", name, conn)
                store.insert_opens(_starter_account_opens(business_type, starter_open_date, selected_basis), conn)
            created.append("ledger.sqlite")
            if selected_basis == "accrual":
                store.upgrade()
        else:
            existed.append("ledger.sqlite")

    # --- business-profile.md -------------------------------------------------
    if is_reinit:
        # Re-init: never overwrite business profile
        existed.append("business-profile.md")
    else:
        bp_src = templates / "business-profile.md"
        bp_dst = target / "business-profile.md"
        _track(bp_dst, _copy_if_absent(bp_src, bp_dst))

    # --- entity.json ---------------------------------------------------------
    entity_json_dst = target / "entity.json"
    if is_reinit:
        existed.append("entity.json")
    else:
        if not entity_json_dst.exists():
            template_data: dict[str, Any] = json.loads((templates / "entity.json").read_text(encoding="utf-8"))
            merged = _entity_json_for_name(
                name,
                business_type,
                legal_structure,
                normalized_cutover_date,
                template_data,
            )
            if website_url:
                merged["website_url"] = website_url
            merged["basis"] = selected_basis
            merged["accrual_capability_version"] = ACCRUAL_CAPABILITY_VERSION if selected_basis == "accrual" else 0
            entity_json_dst.write_text(
                json.dumps(merged, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            created.append("entity.json")
        else:
            existed.append("entity.json")

    # --- trust-policy.json ---------------------------------------------------
    trust_dst = target / "trust-policy.json"
    _track(trust_dst, _copy_if_absent(templates / "trust-policy.json", trust_dst))

    # --- .gitignore (credential hygiene) -------------------------------------
    git_root = _git_root(target)
    if git_root is not None:
        gitignore_path = target / ".gitignore"
        if not gitignore_path.exists():
            gitignore_src = templates / "gitignore-template"
            gitignore_path.write_text(gitignore_src.read_text(encoding="utf-8"), encoding="utf-8")
            created.append(".gitignore")
        else:
            existed.append(".gitignore")
        # Warn when local credential files would not be ignored
        if not _gitignore_covers_secrets(target / ".gitignore"):
            print(
                "Warning: this entity directory is inside a git repository but "
                ".env and .secrets/ are not both listed in .gitignore.\n"
                "Run: printf '\\n.env\\n.secrets/\\n' >> " + str(target / ".gitignore"),
                file=sys.stderr,
            )

    return {"created": created, "existed": existed}


@_locked_entity_operation
def configure_basis(entity_path: Path | str, basis: str) -> None:
    """Select the basis for an empty company; conversion is not supported.

    Publish the ledger authority first, then atomically replace configuration.
    An interrupted publication fails closed on the basis mismatch when reopened.
    """
    from .ledger.importer import _entity_write_lock, check_integrity
    from .ledger.store import ACCRUAL_SCHEMA_VERSION, LedgerStore, default_store_path
    from .accrual.store import list_records

    resolve_basis({"basis": basis})
    entity = load_entity(entity_path)
    with _entity_write_lock(entity.path):
        store = LedgerStore(default_store_path(entity.path))
        if check_integrity(entity).status != "ok":
            raise ValueError("Cannot change basis while ledger integrity is invalid.")
        with store.connection() as conn:
            populated = any(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                            for table in ("entries", "balance_assertions", "source_transactions"))
            if store.schema_version(conn) in {ACCRUAL_SCHEMA_VERSION, 3}:
                populated = populated or any(list_records(store, table, conn)
                                             for table in ("intents", "schedules", "items", "allocations", "coverage"))
        if populated:
            raise ValueError(
                "A populated company (one that already has entries) cannot change accounting basis, and Slashbooks does not convert in place. "
                "To keep these books and add accrual records from a date, run books accrual enable preview --entity THIS_DIR --tracking-start YYYY-MM-DD. "
                "Prior periods stay in this company. Or start a new accrual company at the cutover date and open it with balances: "
                "1) books entity init NEW_DIR --name NAME --basis accrual --cutover-date YYYY-MM-DD; "
                "2) books accrual opening draft --entity THIS_DIR --cutover YYYY-MM-DD > draft.json (then add open invoices and bills); "
                "3) books accrual opening preview --entity NEW_DIR --input draft.json > plan.json; "
                "4) books accrual opening apply --entity NEW_DIR --input plan.json.")
        if basis == "accrual":
            store.upgrade()
        config = dict(entity.entity_config)
        config.pop("accounting_basis", None)
        config["basis"] = basis
        config["accrual_capability_version"] = ACCRUAL_CAPABILITY_VERSION if basis == "accrual" else 0
        with store.transaction(immediate=True) as conn:
            store.set_meta("basis", basis, conn)
            if basis == "accrual":
                open_day = date.fromisoformat(_normalize_optional_date(str(config.get("cutover_date") or "")) or _DEFAULT_ACCOUNT_OPEN_DATE.isoformat())
                store.insert_opens(_starter_account_opens(entity.business_type, open_day, "accrual")[-len(_ACCRUAL_STARTER_ACCOUNTS):], conn)
            store.append_audit_event("basis-configured", {"basis": basis}, conn)
            store.append_audit_event("ledger-store-sealed", {
                "session_id": "entity-basis", "entries": 0, "store_sha256": store.content_digest(conn),
            }, conn)
        temp = entity.path / "entity.json.basis.tmp"
        temp.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, entity.path / "entity.json")


_CATEGORY_SEGMENT_RE = re.compile(r"[A-Z0-9][A-Za-z0-9-]*")
CATEGORY_SHAPES = ("Income:<Line>", "Income:<Group>:<Line>", "Expenses:<Name>", "Expenses:Cost-of-Sales:<Line>")


def check_category_account(account: str) -> None:
    """Raise ValueError unless *account* is an income or expense category of an approved shape.

    The shapes are ``CATEGORY_SHAPES``: an income line, an income line one level under an
    income group, an expense, or a line under the cost-of-sales group. A cost-of-sales
    group is found by name, as the profit and loss structure finds it (Cost-of-Sales,
    COGS, Cost-of-Goods-Sold, Cost-of-Revenue or Cost-of-Goods).
    """
    from .reports.structure import is_cost_of_sales
    # A full match of each name part: `$` in the general account check also matches before a final
    # newline, so "Income:Consulting\n" would pass it and open a second account.
    if not isinstance(account, str) or any(not _CATEGORY_SEGMENT_RE.fullmatch(part) for part in account.split(":")[1:]) \
            or any(ord(char) < 32 or ord(char) == 127 for char in account):
        raise ValueError(f"A category name may use letters, digits and hyphens only, with each part starting with a "
                         f"capital letter or a digit. Got {account!r}.")
    parts = account.split(":")
    allowed = ((parts[0] == "Income" and len(parts) in (2, 3))
               or (parts[0] == "Expenses" and (len(parts) == 2 or (len(parts) == 3 and is_cost_of_sales(account)))))
    if not allowed:
        raise ValueError(f"A category must have one of these shapes: {', '.join(CATEGORY_SHAPES)}. Got {account!r}.")


def add_account(
    entity_path: Path,
    account: str,
    open_date: str = "",
    currency: str | None = None,
    category: bool = False,
) -> dict[str, str]:
    """Add an account to the SQLite account catalog for an existing entity.

    The currency defaults to the company currency (USD unless entity.json says otherwise).
    With ``category`` the account must also be an income or expense category of an
    approved shape (``check_category_account``). The usual checks apply in both cases.
    """
    if category:
        check_category_account(account)
    entity = load_entity(entity_path)
    if currency is None:
        from .currency import company_currency
        currency = company_currency(entity.entity_config)
    normalized_open_date = _normalize_optional_date(open_date) or _DEFAULT_ACCOUNT_OPEN_DATE.isoformat()

    from .ledger.model import Open
    import sqlite3
    from .ledger.store import LedgerStore, SUPPORTED_SCHEMA_VERSIONS, default_store_path
    from .ledger.importer import _entity_write_lock, check_integrity

    opened = Open(
        date=date.fromisoformat(normalized_open_date),
        account=account,
        currencies=(currency,) if currency else (),
    )
    store = LedgerStore(default_store_path(entity.path))
    with _entity_write_lock(entity.path):
        if not store.path.exists():
            if entity.books_path.exists():
                raise ValueError("Migrate the legacy ledger explicitly before changing the account catalog.")
            raise ValueError("Account catalog requires an initialized SQLite ledger; run books entity init first.")
        effective_currency = currency or "USD"
        with store.transaction(immediate=True) as conn:
            try:
                schema = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            except sqlite3.DatabaseError:
                raise ValueError("Account catalog requires an initialized supported-schema SQLite ledger.") from None
            if schema is None or schema[0] not in {str(v) for v in SUPPORTED_SCHEMA_VERSIONS}:
                raise ValueError("Unsupported ledger schema; upgrade or migrate explicitly before changing the account catalog.")
            integrity = check_integrity(entity)
            if integrity.status != "ok":
                raise ValueError("Account catalog cannot change while ledger integrity is invalid.")
            if conn.execute("SELECT 1 FROM entries LIMIT 1").fetchone() and not conn.execute(
                "SELECT 1 FROM audit_events LIMIT 1"
            ).fetchone():
                raise ValueError("Account catalog cannot change on a populated ledger without audit history.")
            existing = conn.execute("SELECT currency,open_date FROM accounts WHERE name=?", (account,)).fetchone()
            if existing and existing["currency"] != effective_currency:
                conflict = conn.execute(
                    "SELECT 1 FROM postings WHERE account=? AND currency<>? "
                    "UNION ALL SELECT 1 FROM balance_assertions WHERE account=? AND currency<>? LIMIT 1",
                    (account, effective_currency, account, effective_currency),
                ).fetchone()
                if conflict:
                    raise ValueError("Account currency conflicts with existing postings or balance assertions.")
            existed_before = existing is not None
            effective_date = min(normalized_open_date, existing["open_date"]) if existing else normalized_open_date
            if not existing or (existing["currency"], existing["open_date"]) != (effective_currency, effective_date):
                # Catalog, intent and seal share one transaction without
                # initializing the store or rewriting its metadata.
                store.append_audit_event("intent", {
                    "session_id": "entity-account-add", "entries": 0,
                    "description": (f"{'Update' if existing else 'Add'} account {account}: "
                                    f"open {effective_date}, currency {effective_currency}"),
                }, conn)
                store.insert_opens([opened], conn)
                store.append_audit_event("ledger-store-sealed", {
                    "session_id": "entity-account-add", "entries": 0, "source_ids": [],
                    "store_sha256": store.content_digest(conn),
                }, conn)

    return {
        "account": account,
        "open_date": normalized_open_date,
        "currency": currency,
        "status": "existed" if existed_before else "created",
    }


@_locked_entity_operation
def apply_starter_accounts(entity_path: Path | str, business_type: str) -> dict[str, Any]:
    """Set the business type of a company that has no entries yet and add the starter accounts it lacks.

    This is for a company that was created before its business type was known. An account that
    already exists is not changed. It
    refuses a company that has entries, balance assertions or source transactions, and a
    company whose ledger integrity is invalid. A repeat with the same type changes nothing.

    It also removes an account of the company's previous starter chart that the new chart lacks,
    when no posting or balance assertion uses it and entity.json does not name it. An account
    outside the previous starter chart is never removed.
    """
    from .currency import company_currency
    from .ledger.importer import _entity_write_lock, check_integrity
    from .ledger.model import Open
    from .ledger.store import SUPPORTED_SCHEMA_VERSIONS, LedgerStore, default_store_path

    if business_type not in BUSINESS_TYPES:
        raise ValueError(f"Unknown business type {business_type!r}. Choose one of: {', '.join(BUSINESS_TYPES)}.")
    entity = load_entity(entity_path)
    currency = company_currency(entity.entity_config)
    open_day = date.fromisoformat(_normalize_optional_date(str(entity.entity_config.get("cutover_date") or ""))
                                  or _DEFAULT_ACCOUNT_OPEN_DATE.isoformat())
    with _entity_write_lock(entity.path):
        store = LedgerStore(default_store_path(entity.path))
        if not store.path.exists():
            raise ValueError("Starter accounts need an initialized SQLite ledger; run books entity init first.")
        if check_integrity(entity).status != "ok":
            raise ValueError("Starter accounts cannot be added while ledger integrity is invalid.")
        with store.transaction(immediate=True) as conn:
            schema = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if schema is None or schema[0] not in {str(v) for v in SUPPORTED_SCHEMA_VERSIONS}:
                raise ValueError("Unsupported ledger schema; upgrade or migrate explicitly before changing the account catalog.")
            if any(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                   for table in ("entries", "postings", "balance_assertions", "source_transactions")):
                raise ValueError("This company already has entries, so its starter accounts are not changed. "
                                 "Add single accounts with books entity account-add.")
            chart = starter_chart(business_type)
            if resolve_basis(entity.entity_config) == "accrual":
                chart = chart + tuple(a for a in _ACCRUAL_STARTER_ACCOUNTS if a not in chart)
            existing = {str(row[0]).lower() for row in conn.execute("SELECT name FROM accounts")}
            missing = [account for account in chart if account.lower() not in existing]
            # Remove an unused account of the previous starter chart that the new chart lacks. An account
            # that has postings or assertions, or that entity.json names, or that is not in the previous chart, stays.
            config_text = json.dumps(entity.entity_config, sort_keys=True)
            wanted = {a.lower() for a in chart}
            removable = [a for a in starter_chart(entity.business_type) if a.lower() not in wanted]
            removed: list[str] = []
            for account in removable:
                row = conn.execute("SELECT name FROM accounts WHERE name=?", (account,)).fetchone()
                if row is None or json.dumps(account) in config_text:
                    continue
                if conn.execute("SELECT 1 FROM postings WHERE account=? UNION ALL SELECT 1 FROM balance_assertions "
                                "WHERE account=? LIMIT 1", (account, account)).fetchone():
                    continue
                removed.append(account)
            opens = [Open(date=open_day, account=account,
                          currencies=(currency,) if not account.startswith(("Equity:", "Income:")) else ())
                     for account in missing]
            previous_type = conn.execute("SELECT value FROM meta WHERE key='business_type'").fetchone()
            type_changed = previous_type is None or previous_type[0] != business_type
            changed = bool(opens or removed or type_changed)
            if changed:
                store.append_audit_event("intent", {
                    "session_id": "entity-starter-accounts", "entries": 0,
                    "description": f"Add {len(opens)} and remove {len(removed)} starter accounts for business type {business_type}",
                }, conn)
                store.insert_opens(opens, conn)
                for account in removed:
                    conn.execute("DELETE FROM accounts WHERE name=?", (account,))
            if type_changed:
                store.set_meta("business_type", business_type, conn)
            if changed:
                store.append_audit_event("ledger-store-sealed", {
                    "session_id": "entity-starter-accounts", "entries": 0, "source_ids": [],
                    "store_sha256": store.content_digest(conn),
                }, conn)
        if entity.entity_config.get("business_type") != business_type:
            config = dict(entity.entity_config)
            config["business_type"] = business_type
            temp = entity.path / "entity.json.starter.tmp"
            temp.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, entity.path / "entity.json")
    return {"business_type": business_type, "added": missing, "removed": removed,
            "already_present": [a for a in chart if a not in missing]}


# Fields a mapped bank account can carry besides the ledger account. The key of
# the mapping stays the provider's account ID, so a flat ``{id: account}`` entry
# and a rich entry are both found by the same lookup.
BANK_IDENTITY_FIELDS = (
    "provider", "provider_account_id", "institution_id", "institution_name",
    "last4", "account_type", "currency", "cutover_date",
)


def bank_mapping_account(value: Any) -> str | None:
    """Ledger account of one ``bank_account_mappings`` value, in either form.

    The flat form is the account name. The rich form is an object with a
    ``ledger_account``. Anything else is not a usable mapping.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        account = value.get("ledger_account")
        return account if isinstance(account, str) and account else None
    return None


def flat_bank_account_mappings(config: Any) -> dict[str, str]:
    """``{provider account ID: ledger account}`` for either mapping form.

    Read code that only needs the ledger account uses this, so a rich entry is
    never mistaken for a missing one.
    """
    raw = config.get("bank_account_mappings") if isinstance(config, dict) else None
    if not isinstance(raw, dict):
        return {}
    flat: dict[str, str] = {}
    for key, value in raw.items():
        account = bank_mapping_account(value)
        if account:
            flat[str(key)] = account
    return flat


def bank_mapping_details(value: Any) -> dict[str, Any]:
    """Identity fields of one mapping value. A flat value has none."""
    if not isinstance(value, dict):
        return {}
    return {name: value[name] for name in BANK_IDENTITY_FIELDS if value.get(name) not in (None, "")}


def normalize_last4(value: Any) -> str:
    """Last four letters or digits of an account number or mask. ``****0021`` gives ``0021``.

    Returns an empty string when fewer than four remain.
    """
    kept = re.sub(r"[^A-Za-z0-9]", "", str(value or ""))
    return kept[-4:].upper() if len(kept) >= 4 else ""


def _clean_identity(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise the identity fields a caller gave."""
    clean: dict[str, Any] = {}
    for name, value in fields.items():
        if name not in BANK_IDENTITY_FIELDS:
            raise ValueError(f"Unknown bank account field '{name}'.")
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if name == "last4":
            text = normalize_last4(text)
            if not text:
                raise ValueError("last4 must be the last four letters or digits of the account number.")
        elif name == "currency":
            if not re.fullmatch(r"[A-Za-z]{3}", text):
                raise ValueError("currency must be a three-letter code such as USD.")
            text = text.upper()
        elif name == "account_type":
            text = text.lower().replace("-", "_").replace(" ", "_")
        elif name == "cutover_date":
            try:
                text = date.fromisoformat(text).isoformat()
            except ValueError:
                raise ValueError("cutover_date must be a date in YYYY-MM-DD form.") from None
        clean[name] = text
    return clean


@_locked_entity_operation
def map_bank_account(entity_path: Path, feed_account_id: str, account: str,
                     clear: tuple[str, ...] = (), **identity: Any) -> dict[str, str]:
    """Persist a provider-account-ID to an existing ledger account mapping.

    With no identity fields the mapping is written in the flat form, exactly as
    before. With any of ``BANK_IDENTITY_FIELDS`` it is written as an object that
    also carries the account identity (provider, institution, last four digits,
    type, currency, cutover date). Re-mapping a rich entry without new fields
    keeps its identity and changes only the ledger account. ``clear`` names
    identity fields to remove, for example ``("cutover_date",)``.
    """
    entity = load_entity(entity_path)
    stable_id = str(feed_account_id).strip()
    if not stable_id:
        raise ValueError("Feed account ID is required.")
    fields = _clean_identity(identity)

    from .ledger.store import LedgerStore, default_store_path

    store = LedgerStore(default_store_path(entity.path))
    if account not in store.load_account_names():
        raise ValueError(
            f"Account '{account}' is not in the account catalog. Add or select an existing account first."
        )
    mappings = dict(entity.entity_config.get("bank_account_mappings") or {})
    previous = mappings.get(stable_id)
    previous_fields = bank_mapping_details(previous)
    for name in clear:
        if name not in BANK_IDENTITY_FIELDS or name in {"provider_account_id"}:
            raise ValueError(f"Cannot clear '{name}'.")
        previous_fields.pop(name, None)
    if fields or previous_fields:
        merged = {**previous_fields, **fields}
        merged.setdefault("provider_account_id", stable_id)
        mappings[stable_id] = {"ledger_account": account, **merged}
    else:
        mappings[stable_id] = account
    entity.entity_config["bank_account_mappings"] = mappings
    destination = entity.path / "entity.json"
    tmp = destination.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entity.entity_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, destination)
    return {"feed_account_id": stable_id, "account": account, "status": "updated" if previous else "created"}


def coverage_ranges(bounds: Any) -> list[tuple[date, date]]:
    """Return the covered (from, to) date ranges recorded for one source.

    A source has a single `coverage_from`/`coverage_to` pair, plus an optional
    `ranges` list when separate ranges were recorded with a gap between them.
    """
    if not isinstance(bounds, dict):
        return []
    found: list[tuple[date, date]] = []
    for item in [*(bounds.get("ranges") or []), {"from": bounds.get("coverage_from"), "to": bounds.get("coverage_to")}]:
        try:
            first, last = date.fromisoformat(str(item["from"])), date.fromisoformat(str(item["to"]))
        except (KeyError, TypeError, ValueError):
            continue
        if first <= last:
            found.append((first, last))
    return found


def merge_coverage_ranges(ranges: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """Union of date ranges; overlapping or touching ranges become one."""
    merged: list[tuple[date, date]] = []
    for first, last in sorted(ranges):
        if merged and first <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def coverage_spans(bounds: Any, start: date, end: date) -> bool:
    """True when one recorded range covers the whole of start..end."""
    return any(first <= start and last >= end for first, last in coverage_ranges(bounds))


@_locked_entity_operation
def record_source_coverage(
    entity_path: Path,
    source_name: str,
    coverage_from: str,
    coverage_to: str,
    account: str | None = None,
) -> dict[str, str]:
    """Record an owner-confirmed covered date range for one declared source."""
    entity = load_entity(entity_path)
    name = source_name.strip()
    if not name:
        raise ValueError("Source name is required.")
    try:
        start = date.fromisoformat(coverage_from)
        end = date.fromisoformat(coverage_to)
    except ValueError as exc:
        raise ValueError("Source coverage dates must use YYYY-MM-DD format.") from exc
    if start > end:
        raise ValueError("Source coverage start date must not be after its end date.")

    sources = list(entity.entity_config.get("declared_sources") or [])
    coverage = dict(entity.entity_config.get("source_coverage") or {})
    existing_coverage = coverage.get(name)
    # Coverage is additive: a new range joins the ranges already recorded and
    # never shrinks them. Replace-by-latest-call silently re-blocked earlier months.
    previous_ranges = merge_coverage_ranges(coverage_ranges(existing_coverage))
    merged = merge_coverage_ranges([*previous_ranges, (start, end)])
    block = max(merged, key=lambda r: r[1])
    replacement: dict[str, Any] = {"coverage_from": block[0].isoformat(), "coverage_to": block[1].isoformat()}
    if len(merged) > 1:
        replacement["ranges"] = [{"from": a.isoformat(), "to": b.isoformat()} for a, b in merged]
    if (not account and isinstance(existing_coverage, dict)
            and isinstance(existing_coverage.get("account"), str)):
        replacement["account"] = existing_coverage["account"]
    if account:
        from .ledger.store import LedgerStore, default_store_path
        if account not in LedgerStore(default_store_path(entity.path)).load_account_names():
            raise ValueError(f"Source account '{account}' is not in the account catalog.")
        if not account.startswith(("Assets:", "Liabilities:")):
            raise ValueError("A statement source must map to an asset or liability account.")
        replacement["account"] = account
    status = "updated" if name in coverage else "created"
    for source in sources:
        existing_name = source if isinstance(source, str) else source.get("name", "")
        if str(existing_name) != name:
            continue
        status = "updated"
        break
    else:
        sources.append(name)

    entity.entity_config["declared_sources"] = sources
    coverage[name] = replacement
    entity.entity_config["source_coverage"] = coverage
    destination = entity.path / "entity.json"
    tmp = destination.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entity.entity_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, destination)
    return {
        "source": name,
        "coverage_from": start.isoformat(),
        "coverage_to": end.isoformat(),
        "account": str(replacement.get("account") or ""),
        "status": status,
        "ranges": [{"from": a.isoformat(), "to": b.isoformat()} for a, b in merged],
        "added": {"from": start.isoformat(), "to": end.isoformat()},
        "already_covered": any(a <= start and b >= end for a, b in previous_ranges),
    }


_INBOUND_RELATED_POLICIES = {"income", "settle-receivable"}
_OUTBOUND_RELATED_POLICIES = {"create-receivable", "settle-payable"}


def _write_entity_config(entity: "Entity") -> None:
    destination = entity.path / "entity.json"
    tmp = destination.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entity.entity_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, destination)


ONBOARDING_VIAS = ("agent", "meeting", "staff")


def onboarding_completed(config: dict[str, Any]) -> dict[str, str] | None:
    """The recorded onboarding completion of a company, or None.

    ``entity.json`` keeps it as ``onboarding_completed``: ``{"on": "YYYY-MM-DD", "via": "agent"}``.
    ``via`` says how the business was set up: ``agent`` (the onboarding workflow of an AI
    agent), ``meeting`` (a hosted setup meeting) or ``staff`` (a person marked it done).
    A missing or malformed value means onboarding is not recorded as complete.
    """
    value = config.get("onboarding_completed")
    if not isinstance(value, dict) or value.get("via") not in ONBOARDING_VIAS or not isinstance(value.get("on"), str):
        return None
    try:
        day = date.fromisoformat(value["on"])
    except ValueError:
        return None
    return {"on": day.isoformat(), "via": value["via"]}


def _latest_today() -> date:
    """The latest date that counts as today: the later of the machine date and the UTC date.

    The engine keeps no company time zone. The default ``--on`` is the machine date, and a
    machine east of UTC can be one day ahead of UTC, so the limit accepts the later of the two.
    """
    return max(date.today(), datetime.now(timezone.utc).date())


@_locked_entity_operation
def record_onboarding_complete(entity_path: Path, via: str, on: str = "") -> dict[str, Any]:
    """Record that onboarding of the company is complete. A valid first record is kept.

    A second call changes nothing and returns the record that is already there, so a
    workflow can call it again safely. It changes only entity.json, never ledger data.
    ``on`` defaults to the machine date (``date.today()``); the engine keeps no company time
    zone. A date after today (the later of the machine date and the UTC date) is refused.
    """
    if via not in ONBOARDING_VIAS:
        raise ValueError(f"Onboarding completion --via must be one of: {', '.join(ONBOARDING_VIAS)}.")
    try:
        day = date.fromisoformat(on) if on else date.today()
    except ValueError as exc:
        raise ValueError("Onboarding completion --on must use YYYY-MM-DD format.") from exc
    if day > _latest_today():
        raise ValueError("Onboarding completion --on cannot be after today.")
    entity = load_entity(entity_path)
    existing = onboarding_completed(entity.entity_config)
    if existing is not None:
        return {"status": "already_complete", "onboarding_completed": existing}
    record = {"on": day.isoformat(), "via": via}
    entity.entity_config["onboarding_completed"] = record
    _write_entity_config(entity)
    return {"status": "completed", "onboarding_completed": record}


@_locked_entity_operation
def record_related_entity(
    entity_path: Path,
    name: str,
    receivable_account: str,
    payable_account: str,
    inbound_policy: str,
    outbound_policy: str,
    inbound_income_account: str = "",
) -> dict[str, str]:
    """Record a pending related-entity migration policy.

    The configuration itself never posts transactions. It declares proposed
    chart accounts and direction-specific fallbacks. A separate approval action
    must activate them before a queue proposal can use the policy.
    """
    entity = load_entity(entity_path)
    related_name = name.strip()
    if not related_name:
        raise ValueError("Related entity name is required.")
    if inbound_policy not in _INBOUND_RELATED_POLICIES:
        raise ValueError("Inbound policy must be income or settle-receivable.")
    if outbound_policy not in _OUTBOUND_RELATED_POLICIES:
        raise ValueError("Outbound policy must be create-receivable or settle-payable.")

    from .ledger.store import LedgerStore, default_store_path
    accounts = LedgerStore(default_store_path(entity.path)).load_account_names()
    required = {receivable_account, payable_account}
    if inbound_policy == "income":
        if not inbound_income_account:
            raise ValueError("An inbound income account is required when inbound policy is income.")
        required.add(inbound_income_account)
    missing = sorted(account for account in required if account not in accounts)
    if missing:
        raise ValueError("Related-entity accounts must exist first: " + ", ".join(missing))

    record = {
        "name": related_name,
        "receivable_account": receivable_account,
        "payable_account": payable_account,
        "inbound_policy": inbound_policy,
        "outbound_policy": outbound_policy,
        "inbound_income_account": inbound_income_account if inbound_policy == "income" else "",
        "owner_authorized": False,
    }
    records = list(entity.entity_config.get("related_entities") or [])
    status = "created"
    for index, existing in enumerate(records):
        if isinstance(existing, dict) and str(existing.get("name") or "").casefold() == related_name.casefold():
            records[index] = record
            status = "updated"
            break
    else:
        records.append(record)
    entity.entity_config["related_entities"] = records
    _write_entity_config(entity)
    return {"name": related_name, "status": status, "authorization": "pending", "inbound_policy": inbound_policy, "outbound_policy": outbound_policy}


@_locked_entity_operation
def approve_related_entity(entity_path: Path, name: str, approval_note: str) -> dict[str, str]:
    """Activate a pending related-entity policy with explicit approval provenance."""
    entity = load_entity(entity_path)
    related_name = name.strip()
    note = approval_note.strip()
    if not related_name or not note:
        raise ValueError("Related entity name and owner approval note are required.")
    records = list(entity.entity_config.get("related_entities") or [])
    for index, record in enumerate(records):
        if not isinstance(record, dict) or str(record.get("name") or "").casefold() != related_name.casefold():
            continue
        approved = dict(record)
        approved["owner_authorized"] = True
        approved["approval_note"] = note
        approved["approved_at"] = datetime.now(timezone.utc).isoformat()
        records[index] = approved
        entity.entity_config["related_entities"] = records
        _write_entity_config(entity)
        return {"name": str(approved["name"]), "status": "approved"}
    raise ValueError(f"No pending related entity named '{related_name}' is configured.")


def list_related_entities(entity_path: Path) -> list[dict[str, Any]]:
    """Return configured related-entity policies without changing the books."""
    entity = load_entity(entity_path)
    return [dict(record) for record in entity.entity_config.get("related_entities") or [] if isinstance(record, dict)]


def get_related_entity(entity: "Entity", name: str) -> dict[str, Any]:
    """Find one configured policy by name, preserving an explicit owner boundary."""
    for record in entity.entity_config.get("related_entities") or []:
        if isinstance(record, dict) and str(record.get("name") or "").casefold() == name.strip().casefold():
            if record.get("owner_authorized") is not True:
                raise ValueError(f"Related entity '{name}' has not been owner-authorized.")
            return dict(record)
    raise ValueError(f"No owner-authorized related entity named '{name}' is configured.")


# ---------------------------------------------------------------------------
# Entity data object
# ---------------------------------------------------------------------------

_DEFAULT_TRUST_POLICY: dict[str, Any] = {
    "auto_post_threshold": 3,
    "queue_all_until_confirmed": True,
}


@dataclass
class Entity:
    """Parsed state of an initialised entity directory."""

    path: Path
    entity_config: dict[str, Any]
    trust_policy: dict[str, Any]

    # Convenience path accessors
    @property
    def books_path(self) -> Path:
        return self.path / "books.beancount"

    @property
    def coa_path(self) -> Path:
        return self.path / "chart-of-accounts.beancount"

    @property
    def learned_context_dir(self) -> Path:
        return self.path / "learned-context"

    @property
    def review_queue_dir(self) -> Path:
        return self.path / "review-queue"

    @property
    def staging_dir(self) -> Path:
        return self.path / "staging"

    @property
    def ingestion_dir(self) -> Path:
        return self.path / "ingestion"

    @property
    def reports_dir(self) -> Path:
        return self.path / "reports"

    @property
    def currency(self) -> str:
        """The one currency the company keeps its books in (USD unless entity.json says otherwise)."""
        from .currency import company_currency
        return company_currency(self.entity_config)

    @property
    def name(self) -> str:
        return str(self.entity_config.get("name", ""))

    @property
    def business_type(self) -> str:
        return str(self.entity_config.get("business_type", "consulting"))

    @property
    def basis(self) -> str:
        return resolve_basis(self.entity_config)

    @property
    def auto_post_threshold(self) -> int:
        return int(self.trust_policy.get("auto_post_threshold", 3))


def load_entity(path: str | Path) -> Entity:
    """Load an entity from *path*.

    Raises ``FileNotFoundError`` when ``entity.json`` is absent (directory not
    initialised).  ``trust-policy.json`` is optional — missing file falls back
    to the default policy (threshold 3, queue_all_until_confirmed True).
    """
    p = Path(path).resolve()
    entity_json_path = p / "entity.json"
    if not entity_json_path.exists():
        raise FileNotFoundError(
            f"No entity.json found at '{p}'. "
            "Run 'books entity init <path>' to initialise."
        )
    entity_config: dict[str, Any] = json.loads(entity_json_path.read_text(encoding="utf-8"))
    resolve_basis(entity_config)
    from .ledger.store import LedgerStore, default_store_path
    store_path = default_store_path(p)
    currency_meta: dict[str, str] = {}
    if store_path.exists():
        store = LedgerStore(store_path)
        with store.connection() as conn:
            if store.schema_version(conn) is not None:
                row = conn.execute("SELECT value FROM meta WHERE key='basis'").fetchone()
                recorded_basis = row[0] if row else None
                if recorded_basis is None and conn.execute("SELECT 1 FROM entries LIMIT 1").fetchone():
                    recorded_basis = "cash"
                if recorded_basis is not None:
                    resolve_basis(entity_config, authoritative_basis=recorded_basis)
                # A currency change commits the ledger first; finish entity.json if a stop came in between.
                currency_meta = dict(conn.execute(
                    "SELECT key, value FROM meta WHERE key IN ('currency', 'currency_source')").fetchall())
    if store_path.exists() and currency_meta:
        from .currency import repair_config
        repair_config(p, entity_config, currency_meta)

    trust_path = p / "trust-policy.json"
    if trust_path.exists():
        trust_policy: dict[str, Any] = json.loads(trust_path.read_text(encoding="utf-8"))
        # Fill in any missing keys with defaults
        for k, v in _DEFAULT_TRUST_POLICY.items():
            trust_policy.setdefault(k, v)
    else:
        trust_policy = dict(_DEFAULT_TRUST_POLICY)

    return Entity(path=p, entity_config=entity_config, trust_policy=trust_policy)


# ---------------------------------------------------------------------------
# CLI surface (wired in by orchestrator)
# ---------------------------------------------------------------------------

def add_parser(subparsers: Any) -> None:
    """Register the ``entity`` subcommand onto *subparsers*."""
    entity_parser = subparsers.add_parser("entity", help="Manage entity data directories")
    entity_sub = entity_parser.add_subparsers(dest="entity_command", required=True)

    init_parser = entity_sub.add_parser(
        "init",
        help="Initialise (or safely re-init) an entity directory",
    )
    init_parser.add_argument("path", type=Path, help="Path to the entity directory")
    init_parser.add_argument("--website-url", default="", help="Optional public business website")
    init_parser.add_argument("--name", default="", help="Business name (stored in entity.json)")
    init_parser.add_argument("--basis", choices=("cash", "accrual"), default=None,
                             help="Accounting basis for a new company; defaults to cash")
    init_parser.add_argument(
        "--legal-structure",
        default="",
        help="Legal structure context, such as sole proprietorship, LLC, S corporation, or partnership",
    )
    init_parser.add_argument(
        "--business-type",
        choices=list(BUSINESS_TYPES),
        default=DEFAULT_BUSINESS_TYPE,
        help="Business type; selects the starter account catalog",
    )
    init_parser.add_argument(
        "--cutover-date",
        default="",
        help="Books start/cutover date in YYYY-MM-DD format",
    )

    add_account_parser = entity_sub.add_parser(
        "account-add",
        help="Add an account to the SQLite account catalog",
    )
    add_account_parser.add_argument("path", type=Path, help="Path to the entity directory")
    add_account_parser.add_argument("--account", required=True, help="Account to add, such as Expenses:Marketing")
    add_account_parser.add_argument(
        "--open-date",
        default="",
        help="Account open date in YYYY-MM-DD format; defaults to the starter catalog date",
    )
    add_account_parser.add_argument("--currency", default=None, help="Currency, default the company currency (USD unless set)")
    add_account_parser.add_argument(
        "--category", action="store_true",
        help="Refuse an account that is not an income or expense category: Income:<Line>, Income:<Group>:<Line>, "
             "Expenses:<Name> or Expenses:Cost-of-Sales:<Line>",
    )

    starter_parser = entity_sub.add_parser(
        "starter-accounts",
        help="Set the business type of a company with no entries and add the starter accounts it lacks",
    )
    starter_parser.add_argument("path", type=Path, help="Path to the entity directory")
    starter_parser.add_argument("--business-type", required=True, choices=list(BUSINESS_TYPES),
                                help="Business type; selects the starter account catalog")
    starter_parser.add_argument("--json", action="store_true", help="Print the result as JSON")

    remove_account_parser = entity_sub.add_parser(
        "account-remove",
        help="Remove an unused income or expense account that account-add created. Previews unless --apply is given",
    )
    remove_account_parser.add_argument("path", type=Path, help="Path to the entity directory")
    remove_account_parser.add_argument("--account", required=True, help="Account to remove, such as Income:Installs")
    remove_account_parser.add_argument("--added-event", type=int, default=None, metavar="ID",
                                       help="The id of the audit event that account-add wrote when it created the account; default: the latest one")
    remove_account_parser.add_argument("--apply", action="store_true", help="Make the change. Without it the command only previews")
    remove_account_parser.add_argument("--plan-hash", default=None, help="With --apply: the plan_hash from the preview, to refuse a changed company")

    set_view_parser = entity_sub.add_parser(
        "set-view",
        help="Choose the default report view, cash or accrual (person-only). Previews unless --apply is given",
    )
    set_view_parser.add_argument("path", type=Path, help="Path to the entity directory")
    set_view_parser.add_argument("--view", required=True, choices=("cash", "accrual"),
                                 help="Default view for report, ask and export. Accrual needs `books accrual enable` first")
    set_view_parser.add_argument("--apply", action="store_true", help="Make the change. Without it the command only previews")
    set_view_parser.add_argument("--plan-hash", default=None, help="With --apply: the plan_hash from the preview, to refuse a changed company")

    fiscal_parser = entity_sub.add_parser("set-fiscal-year", help="Set the fiscal-year start (person-only); previews unless --apply is given")
    fiscal_parser.add_argument("path", type=Path, help="Path to the entity directory")
    fiscal_parser.add_argument("--start", required=True, help="First day of the fiscal year, MM-01 (for example 07-01)")
    fiscal_parser.add_argument("--apply", action="store_true", help="Apply the previewed setting")
    fiscal_parser.add_argument("--plan-hash", default=None, help="Refuse a changed company using the preview's plan_hash")

    tax_method_parser = entity_sub.add_parser(
        "set-tax-method",
        help="Record the tax method, cash or accrual, that tax outputs follow (person-only). Previews unless --apply is given",
    )
    tax_method_parser.add_argument("path", type=Path, help="Path to the entity directory")
    tax_method_parser.add_argument("--method", required=True, choices=("cash", "accrual"),
                                   help="The method the company files its tax return on")
    # Removed option. It stays here so an old script gets a clear refusal, not a silent change.
    tax_method_parser.add_argument("--tax-view", nargs="?", const="removed", default=None, help=argparse.SUPPRESS)
    tax_method_parser.add_argument("--apply", action="store_true", help="Make the change. Without it the command only previews")
    tax_method_parser.add_argument("--plan-hash", default=None, help="With --apply: the plan_hash from the preview, to refuse a changed company")

    profile_parser = entity_sub.add_parser(
        "set-tax-profile",
        help="Record the method of the last tax return and the entity type, for the tax preparer (person-only). Previews unless --apply is given",
    )
    profile_parser.add_argument("path", type=Path, help="Path to the entity directory")
    profile_parser.add_argument("--prior-return-method", choices=("cash", "accrual", "unknown", "none"), default=None,
                                help="The method of the last tax return filed; none for a first-year business")
    profile_parser.add_argument("--entity-type", default=None,
                                choices=("sole_proprietor", "single_member_llc", "partnership", "s_corp", "c_corp", "nonprofit", "other", "unknown"),
                                help="The kind of business entity")
    profile_parser.add_argument("--apply", action="store_true", help="Make the change. Without it the command only previews")
    profile_parser.add_argument("--plan-hash", default=None, help="With --apply: the plan_hash from the preview, to refuse a changed company")

    goals_parser = entity_sub.add_parser(
        "set-reporting-goals",
        help="Record what the owner wants the reports to show (entity.json reporting_goals). Changes no entries. Previews unless --apply is given",
    )
    goals_parser.add_argument("path", type=Path, help="Path to the entity directory")
    goals_parser.add_argument("--goal", action="append", default=[], metavar="CODE",
                              help="A reporting goal; repeat in the owner's order. One of: profit_by_line, profit_by_job, "
                                   "cash_runway, spend_vs_last_year, sales_tax_owed, owner_pay, recurring_revenue, basics")
    goals_parser.add_argument("--line", action="append", default=[], metavar="NAME",
                              help="A service or product line the owner named (only with profit_by_line or profit_by_job); "
                                   "repeat as needed within the 65536-byte reporting goals payload limit")
    goals_parser.add_argument("--clear", action="store_true", help="Remove the recorded goals")
    goals_parser.add_argument("--apply", action="store_true", help="Make the change. Without it the command only previews")
    goals_parser.add_argument("--plan-hash", default=None, help="With --apply: the plan_hash from the preview, to refuse a changed company")

    currency_parser = entity_sub.add_parser(
        "set-currency",
        help="Set the one currency the company keeps its books in (person-only; only while the company has no entries). Previews unless --apply is given",
    )
    currency_parser.add_argument("path", type=Path, help="Path to the entity directory")
    currency_parser.add_argument("--currency", required=True, help="ISO 4217 code, such as USD, EUR, GBP or CAD")
    currency_parser.add_argument("--apply", action="store_true", help="Make the change. Without it the command only previews")
    currency_parser.add_argument("--plan-hash", default=None, help="With --apply: the plan_hash from the preview, to refuse a changed company")

    infer_parser = entity_sub.add_parser(
        "infer-currency",
        help="Propose a company currency from files (OFX, bank CSV, Stripe, QuickBooks or BankSync JSON). Reads only; writes nothing",
    )
    infer_parser.add_argument("--input", action="append", type=Path, required=True,
                              help="A file to read; repeat for more files")

    map_parser = entity_sub.add_parser(
        "bank-account-map",
        help="Map a stable connected-bank account ID to an existing ledger account",
    )
    map_parser.add_argument("path", type=Path, help="Path to the entity directory")
    map_parser.add_argument("--feed-account-id", required=True, help="Stable provider account ID")
    map_parser.add_argument("--account", required=True, help="Existing ledger account to use")
    map_parser.add_argument("--provider", default=None, help="Bank data provider, e.g. banksync or plaid")
    map_parser.add_argument("--institution-id", default=None, help="Provider's institution ID")
    map_parser.add_argument("--institution-name", default=None, help="Institution name as the person knows it")
    map_parser.add_argument("--last4", default=None, help="Last four characters of the account number")
    map_parser.add_argument("--account-type", default=None, help="checking, savings or credit_card")
    map_parser.add_argument("--currency", default=None, help="Three-letter currency code")
    map_parser.add_argument("--clear-cutover-date", action="store_true",
                            help="Remove the cutover date from this mapping")
    map_parser.add_argument("--cutover-date", default=None,
                            help="YYYY-MM-DD. A feed import sends rows on or before this day to review, not the books, unless --backfill")

    coverage_parser = entity_sub.add_parser(
        "source-coverage",
        help="Record the confirmed covered date range for a bank, card, or fallback source",
    )
    coverage_parser.add_argument("path", type=Path, help="Path to the entity directory")
    coverage_parser.add_argument("--source", required=True, dest="source_name",
                                 help="Declared source name, such as a card or CSV fallback")
    coverage_parser.add_argument("--from", required=True, dest="coverage_from",
                                 help="First covered date in YYYY-MM-DD format")
    coverage_parser.add_argument("--to", required=True, dest="coverage_to",
                                 help="Last covered date in YYYY-MM-DD format")
    coverage_parser.add_argument("--account", default=None,
                                 help="Existing asset or liability account covered by this source")

    onboarding_parser = entity_sub.add_parser(
        "onboarding-complete",
        help="Record that onboarding of the company is complete (entity.json onboarding_completed). The first record is kept",
    )
    onboarding_parser.add_argument("path", type=Path, help="Path to the entity directory")
    onboarding_parser.add_argument("--via", required=True, choices=ONBOARDING_VIAS,
                                   help="How the business was set up: agent (an AI agent's onboarding workflow), "
                                        "meeting (a hosted setup meeting) or staff (a person marked it done)")
    onboarding_parser.add_argument("--on", default="",
                                   help="Completion date in YYYY-MM-DD format; defaults to the machine date. A date after today is refused")

    related_parser = entity_sub.add_parser(
        "related-entity",
        help="Record or inspect owner-authorized related-entity settlement policies",
    )
    related_sub = related_parser.add_subparsers(dest="related_entity_command", required=True)
    related_set = related_sub.add_parser("set", help="Set a related-entity migration policy")
    related_set.add_argument("path", type=Path, help="Path to the entity directory")
    related_set.add_argument("--name", required=True, help="Related legal entity or operating name")
    related_set.add_argument("--receivable-account", required=True, help="Existing due-from account")
    related_set.add_argument("--payable-account", required=True, help="Existing due-to account")
    related_set.add_argument("--inbound-policy", required=True, choices=sorted(_INBOUND_RELATED_POLICIES))
    related_set.add_argument("--inbound-income-account", default="", help="Required when inbound policy is income")
    related_set.add_argument("--outbound-policy", required=True, choices=sorted(_OUTBOUND_RELATED_POLICIES))
    related_approve = related_sub.add_parser("approve", help="Approve a pending related-entity policy")
    related_approve.add_argument("path", type=Path, help="Path to the entity directory")
    related_approve.add_argument("--name", required=True, help="Configured related entity name")
    related_approve.add_argument("--approval-note", required=True, help="Owner approval provenance")
    related_list = related_sub.add_parser("list", help="List configured related-entity policies")
    related_list.add_argument("path", type=Path, help="Path to the entity directory")


def run(args: Any) -> int:
    """Execute the entity subcommand described by *args*."""
    if args.entity_command == "init":
        target = _resolve_target(args.path)
        try:
            report = init_entity(
                target,
                name=args.name,
                business_type=args.business_type,
                legal_structure=args.legal_structure,
                cutover_date=args.cutover_date,
                website_url=getattr(args, "website_url", ""),
                basis=getattr(args, "basis", None),
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except SystemExit:
            return 1

        is_reinit = bool(report["existed"])
        mode = "Re-init" if is_reinit else "Init"
        print(f"{mode}: {target}")
        if report["created"]:
            print("  Created:")
            for item in report["created"]:
                print(f"    + {item}")
        if report["existed"]:
            print("  Already present (not modified):")
            for item in report["existed"]:
                print(f"    = {item}")
        return 0

    if args.entity_command == "account-add":
        target = _resolve_target(args.path)
        try:
            report = add_account(
                target,
                account=args.account,
                open_date=args.open_date,
                currency=args.currency,
                category=args.category,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        verb = "Added" if report["status"] == "created" else "Already present"
        print(f"{verb}: {report['account']} (open {report['open_date']})")
        return 0

    if args.entity_command == "starter-accounts":
        target = _resolve_target(args.path)
        try:
            report = apply_starter_accounts(target, args.business_type)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"Business type: {report['business_type']}")
            print(f"Added {len(report['added'])} accounts, removed {len(report['removed'])}; {len(report['already_present'])} already present.")
            for account in report["added"]:
                print(f"  + {account}")
            for account in report["removed"]:
                print(f"  - {account}")
        return 0

    if args.entity_command == "account-remove":
        # The command reads the company files itself (no load_entity, no entity.json repair), so a refusal
        # writes nothing and an unreadable settings file gives a structured reference_check_failed.
        from . import account_remove
        try:
            target = _resolve_target(args.path)
            if args.apply:
                result = account_remove.apply(target, args.account, args.plan_hash, args.added_event)
            else:
                result = account_remove.preview(target, args.account, args.added_event)
        except (ValueError, OSError) as exc:
            if hasattr(exc, "as_dict"):
                result = exc.as_dict()
            elif isinstance(exc, OSError):
                result = account_remove._unreadable(getattr(exc, "filename", None) or "company files").as_dict()
            else:
                result = {"status": "blocked", "errors": [{"code": "invalid_operation", "message": str(exc)}]}
            print(json.dumps(result, sort_keys=True, indent=2))
            return 1
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0

    if args.entity_command == "set-view":
        target = _resolve_target(args.path)
        try:
            from .accrual import enable as accrual_enable
            entity = load_entity(target)
            if args.apply:
                result = accrual_enable.apply_set_view(entity, args.view, args.plan_hash)
            else:
                result = accrual_enable.preview_set_view(entity, args.view)
        except (FileNotFoundError, ValueError) as exc:
            result = exc.as_dict() if hasattr(exc, "as_dict") else {"status": "blocked", "errors": [{"code": "invalid_operation", "message": str(exc)}]}
            print(json.dumps(result, sort_keys=True, indent=2))
            return 1
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0

    if args.entity_command == "set-fiscal-year":
        try:
            from . import fiscal
            entity = load_entity(_resolve_target(args.path))
            result = fiscal.apply(entity, args.start, args.plan_hash) if args.apply else fiscal.preview(entity, args.start)
        except (FileNotFoundError, ValueError) as exc:
            result = exc.as_dict() if hasattr(exc, "as_dict") else {"status": "blocked", "errors": [{"code": "invalid_operation", "message": str(exc)}]}
            print(json.dumps(result, sort_keys=True, indent=2))
            return 1
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0

    if args.entity_command == "set-tax-method":
        target = _resolve_target(args.path)
        try:
            from . import taxview
            if args.tax_view is not None:
                print(json.dumps({"status": "blocked", "errors": [{"code": "tax_view_removed", "message": taxview.TAX_VIEW_REMOVED}]},
                                 sort_keys=True, indent=2))
                return 1
            entity = load_entity(target)
            if args.apply:
                result = taxview.apply_set_tax_method(entity, args.method, args.plan_hash)
            else:
                result = taxview.preview_set_tax_method(entity, args.method)
        except (FileNotFoundError, ValueError) as exc:
            result = exc.as_dict() if hasattr(exc, "as_dict") else {"status": "blocked", "errors": [{"code": "invalid_operation", "message": str(exc)}]}
            print(json.dumps(result, sort_keys=True, indent=2))
            return 1
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0

    if args.entity_command == "set-tax-profile":
        target = _resolve_target(args.path)
        try:
            from . import taxview
            entity = load_entity(target)
            if args.apply:
                result = taxview.apply_set_tax_profile(entity, args.prior_return_method, args.entity_type, args.plan_hash)
            else:
                result = taxview.preview_set_tax_profile(entity, args.prior_return_method, args.entity_type)
        except (FileNotFoundError, ValueError) as exc:
            result = exc.as_dict() if hasattr(exc, "as_dict") else {"status": "blocked", "errors": [{"code": "invalid_operation", "message": str(exc)}]}
            print(json.dumps(result, sort_keys=True, indent=2))
            return 1
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0

    if args.entity_command == "set-reporting-goals":
        try:
            from . import reporting_goals as goals_mod
            entity = load_entity(_resolve_target(args.path))
            if args.apply:
                result = goals_mod.apply_set(entity, args.goal, args.line, args.clear, args.plan_hash)
            else:
                result = goals_mod.preview_set(entity, args.goal, args.line, args.clear)
        except (FileNotFoundError, ValueError) as exc:
            result = exc.as_dict() if hasattr(exc, "as_dict") else {"status": "blocked", "errors": [{"code": "invalid_operation", "message": str(exc)}]}
            print(json.dumps(result, sort_keys=True, indent=2))
            return 1
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0

    if args.entity_command == "set-currency":
        target = _resolve_target(args.path)
        try:
            from . import currency as currency_mod
            entity = load_entity(target)
            if args.apply:
                result = currency_mod.apply_set_currency(entity, args.currency, args.plan_hash)
            else:
                result = currency_mod.preview_set_currency(entity, args.currency)
        except (FileNotFoundError, ValueError) as exc:
            result = exc.as_dict() if hasattr(exc, "as_dict") else {"status": "blocked", "errors": [{"code": "invalid_operation", "message": str(exc)}]}
            if hasattr(exc, "as_dict") and "errors" not in result:
                result = {"status": "blocked", "errors": [result]}
            print(json.dumps(result, sort_keys=True, indent=2))
            return 1
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0

    if args.entity_command == "infer-currency":
        from . import currency as currency_mod
        print(json.dumps(currency_mod.infer_from_files(args.input), sort_keys=True, indent=2))
        return 0

    if args.entity_command == "bank-account-map":
        target = _resolve_target(args.path)
        try:
            report = map_bank_account(
                target, args.feed_account_id, args.account,
                provider=args.provider, institution_id=args.institution_id,
                institution_name=args.institution_name, last4=args.last4,
                account_type=args.account_type, currency=args.currency,
                cutover_date=args.cutover_date,
                clear=("cutover_date",) if args.clear_cutover_date else ())
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        verb = "Updated" if report["status"] == "updated" else "Mapped"
        print(f"{verb}: {report['feed_account_id']} → {report['account']}")
        return 0

    if args.entity_command == "source-coverage":
        target = _resolve_target(args.path)
        try:
            report = record_source_coverage(
                target, args.source_name, args.coverage_from, args.coverage_to, args.account
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        verb = "Updated" if report["status"] == "updated" else "Recorded"
        print(
            f"{verb} coverage for {report['source']}: "
            f"{report['coverage_from']} through {report['coverage_to']}"
        )
        print(
            "Coverage now recorded for this source (earlier ranges are kept): "
            + ", ".join(f"{r['from']} through {r['to']}" for r in report["ranges"])
        )
        if len(report["ranges"]) > 1:
            print("Warning: these ranges have a gap between them; a period that spans the gap stays blocked. "
                  f"Fill the gap with: books entity source-coverage {target} --source \"{report['source']}\" --from <gap start> --to <gap end>")
        return 0

    if args.entity_command == "onboarding-complete":
        try:
            result = record_onboarding_complete(_resolve_target(args.path), args.via, args.on)
        except (FileNotFoundError, ValueError) as exc:
            print(json.dumps({"status": "blocked", "errors": [{"code": "invalid_operation", "message": str(exc)}]},
                             sort_keys=True, indent=2))
            return 1
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0

    if args.entity_command == "related-entity":
        target = _resolve_target(args.path)
        if args.related_entity_command == "set":
            try:
                report = record_related_entity(
                    target,
                    args.name,
                    args.receivable_account,
                    args.payable_account,
                    args.inbound_policy,
                    args.outbound_policy,
                    args.inbound_income_account,
                )
            except (FileNotFoundError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            verb = "Updated" if report["status"] == "updated" else "Recorded"
            print(f"{verb} pending related entity {report['name']}: inbound={report['inbound_policy']}, outbound={report['outbound_policy']}")
            return 0
        if args.related_entity_command == "approve":
            try:
                report = approve_related_entity(target, args.name, args.approval_note)
            except (FileNotFoundError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            print(f"Approved related entity: {report['name']}")
            return 0
        if args.related_entity_command == "list":
            try:
                records = list_related_entities(target)
            except FileNotFoundError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            if not records:
                print("No related-entity policies configured.")
                return 0
            for record in records:
                print(
                    f"{record.get('name')}: inbound={record.get('inbound_policy')}, "
                    f"outbound={record.get('outbound_policy')}, "
                    f"authorization={'approved' if record.get('owner_authorized') is True else 'pending'}"
                )
            return 0

    print(f"Unknown entity command: {args.entity_command}", file=sys.stderr)
    return 2
