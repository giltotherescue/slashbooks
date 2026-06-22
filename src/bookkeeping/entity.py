"""Entity directory scaffolding, configuration, and loader.

Provides:
  - ``entity init <path>`` CLI subcommand
  - ``load_entity(path)`` for reading an initialised entity directory
  - ``add_parser(subparsers)`` / ``run(args)`` — wired into cli.py by the
    orchestrator; this module is self-contained.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Template resolution
# ---------------------------------------------------------------------------

_ENV_TEMPLATES_DIR = "BOOKKEEPING_TEMPLATES_DIR"

# Account for the case where the package lives at various depths:
#   repo/src/bookkeeping/entity.py  ->  repo root is 2 parents up
_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parents[1]  # src/books -> src -> repo root
_BUILTIN_TEMPLATES = _REPO_ROOT / "skills" / "books-onboard" / "templates"


def _templates_dir() -> Path:
    """Return the templates directory, honouring the env-var override."""
    override = os.environ.get(_ENV_TEMPLATES_DIR)
    if override:
        return Path(override)
    return _BUILTIN_TEMPLATES


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

_COA_TEMPLATES: dict[str, str] = {
    "consulting": "chart-of-accounts-consulting.beancount",
    "saas": "chart-of-accounts-saas.beancount",
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def init_entity(
    target: Path,
    name: str = "",
    business_type: str = "consulting",
    legal_structure: str = "",
    cutover_date: str = "",
) -> dict[str, list[str]]:
    """Initialise (or safely re-init) an entity directory at *target*.

    Returns a report dict with keys ``"created"`` and ``"existed"``, each a
    list of relative path strings.
    """
    _refuse_if_inside_package_repo(target)
    normalized_cutover_date = _normalize_optional_date(cutover_date)

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
    if is_reinit and store_path.exists():
        existed.append("ledger.sqlite")
    else:
        if not store_path.exists():
            from .ledger.store import LedgerStore

            store = LedgerStore(store_path)
            store.initialize()
            store.set_meta("canonical", "true")
            if name:
                store.set_meta("title", name)
            created.append("ledger.sqlite")
        else:
            existed.append("ledger.sqlite")

    # --- chart-of-accounts.beancount -----------------------------------------
    coa_template_name = _COA_TEMPLATES.get(business_type, _COA_TEMPLATES["consulting"])
    coa_src = templates / coa_template_name
    coa_dst = target / "chart-of-accounts.beancount"
    if is_reinit:
        # Re-init: never overwrite chart-of-accounts
        existed.append("chart-of-accounts.beancount")
    else:
        _track(coa_dst, _copy_if_absent(coa_src, coa_dst))

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
    def name(self) -> str:
        return str(self.entity_config.get("name", ""))

    @property
    def business_type(self) -> str:
        return str(self.entity_config.get("business_type", "consulting"))

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
    init_parser.add_argument("--name", default="", help="Business name (stored in entity.json)")
    init_parser.add_argument(
        "--legal-structure",
        default="",
        help="Legal structure context, such as sole proprietorship, LLC, S corporation, or partnership",
    )
    init_parser.add_argument(
        "--business-type",
        choices=list(_COA_TEMPLATES.keys()),
        default="consulting",
        help="Business type; selects the chart-of-accounts template",
    )
    init_parser.add_argument(
        "--cutover-date",
        default="",
        help="Books start/cutover date in YYYY-MM-DD format",
    )


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

    print(f"Unknown entity command: {args.entity_command}", file=sys.stderr)
    return 2
