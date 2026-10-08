"""Local migration capture and read-only assessment; CLI remains assessment-only."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import tempfile
import time

from .ledger.projections import render_store_ledger
from .ledger import importer
from .ledger.store import LedgerStore, SCHEMA_VERSION, _DDL
from .ledger.validator import validate
from .remote_contract import ContractError, relative_path

# Conservative native runtime.py workspace limits, not an upload contract.
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_ITEMS = 4096
DATA_SUFFIXES = {".json", ".jsonl", ".ndjson", ".md", ".txt", ".csv", ".xlsx", ".pdf",
                 ".png", ".jpg", ".jpeg", ".webp", ".beancount"}
CONTEXT = {"entity.json", "trust-policy.json", "business-profile.md", "ONBOARDING.md", "DEMO.md"}
ROOTS = {"learned-context": "context", "memory": "context", "context": "context",
         "review-queue": "queues", "staging": "queues", "audit": "audit",
         "audit-log": "audit", "ingestion": "references", "intake": "references",
         "reconciliation": "reports", "reports": "reports", "reconciliations": "reports",
         "outputs": "reports", "qb-exports": "references"}
EXPORT_ROOTS = {**ROOTS, "documents": "references", "evidence": "references"}
DOCUMENT_ROOTS = {"documents", "evidence", "intake", "ingestion", "reconciliation", "reports",
                  "reconciliations", "outputs", "qb-exports"}
ATTACHMENT_SUFFIXES = {".csv", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg", ".webp"}
PATH_KEYS = {"file", "path", "original_path", "source_file", "source_path", "folder",
             "output_dir", "entity_path", "ledger_path", "qb_folder", "evidence_path",
             "attachment_path", "document_path"}
SECRET = re.compile(r"(?:^|[_-])(secret|password|token|oauth|credentials?|api[_-]?key|private[_-]?key|authorization)(?:$|[_-])", re.I)
SECRET_TEXT = re.compile(
    rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|\b(?:bsk_|sk_live_|sk_test_|ghp_)[A-Za-z0-9_-]{16,}"
    rb"|(?i:authorization\s*[:=]\s*bearer\s+\S+)")


class Rejected(Exception):
    """Only fixed diagnostic codes may cross the output boundary."""


def is_document_path(name: str) -> bool:
    """Classify evidence candidates only; callers must validate paths and bytes."""
    return (name == "audit-log.jsonl"
            or ("/" in name and name.split("/", 1)[0] in DOCUMENT_ROOTS)
            or ("/" not in name and Path(name).suffix.lower() in ATTACHMENT_SUFFIXES))


def require(condition, code):
    if not condition:
        raise Rejected(code)


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _excluded(name):
    parts = PurePosixPath(name).parts
    if name == ".books.lock":
        return "runtime_state"
    if name == ".gitignore" or parts[0] == ".git":
        return "local_vcs"
    if any(p.lower().startswith((".env", ".slashbooks", ".books")) or
           p.lower() in {".ssh", ".secrets", ".aws", ".config", ".netrc", ".npmrc", ".pypirc",
                         ".gnupg", "id_rsa", "id_ed25519", "hosted.json"} or
           SECRET.search(p) or SECRET.search(Path(p).stem) for p in parts) or Path(name).suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
        return "credentials"
    if any(p in {".git", "__pycache__", "node_modules", ".venv", "venv"} for p in parts):
        return "executable_state"
    if Path(name).suffix.lower() in {".py", ".pyc", ".sh", ".js", ".mjs", ".ts", ".so", ".dylib", ".exe"}:
        return "custom_code"
    if name in {"ledger.sqlite-shm", "reports/cache.sqlite"} or name.endswith("ledger-cache.sqlite"):
        return "derived_state"
    if name.endswith((".tmp", "-journal")):
        return "unfinished_write"
    if parts[-1] == ".DS_Store":
        return "platform_metadata"
    return None


@contextmanager
def _directory(path):
    raw = Path(path)
    require(".." not in raw.parts, "unsafe_entity_path")
    absolute = Path(os.path.abspath(raw))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, absolute
    finally:
        os.close(fd)


def _inventory(fd, prefix="", found=None):
    found = {} if found is None else found
    for name in sorted(os.listdir(fd)):
        require(len(found) < MAX_ITEMS, "inventory_limit")
        path = prefix + name
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        found[path] = _signature(info)
        if stat.S_ISDIR(info.st_mode) and not _excluded(path):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                require(_signature(os.fstat(child)) == found[path], "source_changed")
                _inventory(child, path + "/", found)
            finally:
                os.close(child)
    return found


def _book_inventory(inventory):
    # Git can update independently of the company writer. Its declared root
    # exclusions are never traversed, read, or used as book-state anchors.
    return {name: signature for name, signature in inventory.items() if _excluded(name) != "local_vcs"}


def _read(fd, name, expected):
    """Resolve each component relative to pinned directories, never through links."""
    parent = os.dup(fd)
    try:
        parts = name.split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(file_fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe_file")
            require(_signature(info) == expected, "source_changed")
            require(info.st_size <= MAX_FILE, "file_size_limit")
            data = handle.read(MAX_FILE + 1)
            require(len(data) == info.st_size and _signature(os.fstat(handle.fileno())) == expected, "source_changed")
            return data
    finally:
        os.close(parent)


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "invalid_json")
            result[key] = value
        return result

    def constant(_):
        raise Rejected("invalid_json")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def _normalized_key(key):
    return re.sub(r"([a-z])([A-Z])", r"\1_\2", key).lower().replace("-", "_")


def _inspection_items(name, data, names):
    """Inspection views only; the captured historical bytes are never rewritten."""
    quarantine = PurePosixPath(name).parent.as_posix() == "review-queue/quarantine"
    if quarantine:
        require(not SECRET_TEXT.search(data), "embedded_credentials")
        if name.endswith(".error.json") and name[:-11] + ".json" in names:
            value = _json(data)
            require(isinstance(value, dict) and set(value) == {"original_path", "error", "quarantined_at"}
                    and all(isinstance(item, str) for item in value.values()), "invalid_quarantine")
            require(not SECRET_TEXT.search(value["original_path"].encode("utf-8")), "embedded_credentials")
            # original_path identifies the pre-quarantine location, not live input.
            return [("error", value["error"]), ("quarantined_at", value["quarantined_at"])]
        require(name[:-5] + ".error.json" in names, "invalid_quarantine")
        try:
            return [("", _json(data))]
        except (Rejected, ValueError, UnicodeError):
            # Invalid JSON is the reason quarantine exists. Scan decoded JSON
            # tokens even when no complete object can be parsed (including keys
            # with escapes and nested secret fields in a truncated object).
            text = data.decode("utf-8")
            items = []
            for token in re.finditer(r'("(?:[^"\\]|\\.)*")\s*(:?)', text):
                value = json.loads(token[1])
                items.append((value, None) if token[2] else ("", value))
            for token in re.finditer(r'(?:^|[{,])\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:', text):
                items.append((token[1], None))
            return items
    value = _json(data)
    if name in {"staging/split-templates.json", "learned-context/counterparties.json"}:
        require(isinstance(value, dict), "unsupported_state_shape")
        for item in value.values():
            require(isinstance(item, dict) if name.startswith("learned-context/") else
                    isinstance(item, list) and all(isinstance(posting, dict) for posting in item),
                    "unsupported_state_shape")
        return [("", item) for item in value.values()]
    return [("", value)]


def _related_policies(entity, accounts):
    from .entity import _INBOUND_RELATED_POLICIES, _OUTBOUND_RELATED_POLICIES

    records = entity.get("related_entities", [])
    require(isinstance(records, list), "invalid_related_entity_policy")
    fields = {"name", "receivable_account", "payable_account", "inbound_policy",
              "outbound_policy", "inbound_income_account", "owner_authorized"}
    seen = set()
    for record in records:
        require(isinstance(record, dict) and fields <= set(record)
                and set(record) <= fields | {"approval_note", "approved_at"}, "invalid_related_entity_policy")
        require(all(isinstance(record[key], str) for key in fields - {"owner_authorized"})
                and type(record["owner_authorized"]) is bool, "invalid_related_entity_policy")
        name = record["name"].strip().casefold()
        require(name and name not in seen, "invalid_related_entity_policy")
        seen.add(name)
        require(record["inbound_policy"] in _INBOUND_RELATED_POLICIES
                and record["outbound_policy"] in _OUTBOUND_RELATED_POLICIES, "invalid_related_entity_policy")
        required = {record["receivable_account"], record["payable_account"]}
        if record["inbound_policy"] == "income":
            required.add(record["inbound_income_account"])
        else:
            require(record["inbound_income_account"] == "", "invalid_related_entity_policy")
        require(required <= accounts, "invalid_related_entity_policy")
        if record["owner_authorized"]:
            require(isinstance(record.get("approval_note"), str) and record["approval_note"].strip()
                    and isinstance(record.get("approved_at"), str), "invalid_related_entity_policy")
            try:
                require(datetime.fromisoformat(record["approved_at"]).utcoffset() is not None,
                        "invalid_related_entity_policy")
            except ValueError:
                raise Rejected("invalid_related_entity_policy") from None
        else:
            require(not (set(record) & {"approval_note", "approved_at"}), "invalid_related_entity_policy")


def _inspect(value, references, blockers, key="", depth=0):
    require(depth < 80, "structured_data_limit")
    if SECRET.search(_normalized_key(key)):
        blockers.add("embedded_credentials")
    if key in {"command", "executable", "base_url"}:
        blockers.add("custom_configuration")
    if key in PATH_KEYS and isinstance(value, str) and value:
        references.add(value)
    if isinstance(value, dict):
        for child, item in value.items():
            _inspect(item, references, blockers, child, depth + 1)
    elif isinstance(value, list):
        for item in value:
            # Ledger metadata is stored as pairs rather than JSON objects.
            if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str):
                _inspect(item[1], references, blockers, item[0], depth + 1)
            else:
                _inspect(item, references, blockers, key, depth + 1)


class _ReadOnlyStore(LedgerStore):
    def connect(self):
        sql = sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)
        sql.row_factory = sqlite3.Row
        sql.execute("PRAGMA query_only=ON")
        sql.execute("PRAGMA trusted_schema=OFF")
        deadline = time.monotonic() + 10
        sql.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        return sql


def _schema(sql):
    with closing(sqlite3.connect(":memory:")) as expected:
        expected.executescript(_DDL)
        tables = {row[0] for row in expected.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require({row[0] for row in sql.execute("SELECT name FROM sqlite_master WHERE type='table'")} == tables,
                "unsupported_schema")
        require(not sql.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger')").fetchone(), "unsupported_schema")
        for table in sorted(tables):
            require([tuple(row) for row in sql.execute(f'PRAGMA table_info("{table}")')] ==
                    list(expected.execute(f'PRAGMA table_info("{table}")')), "unsupported_schema")


def _ledger(files, report, references, blockers):
    # SQLite never opens the source path. WAL recovery/index writes occur only
    # on private copies, after the whole source capture passed stability checks.
    with tempfile.TemporaryDirectory(prefix="slashbooks-migration-") as directory:
        root = Path(directory).resolve()
        replica, snapshot = root / "replica.sqlite", root / "snapshot.sqlite"
        replica.write_bytes(files["ledger.sqlite"])
        if "ledger.sqlite-wal" in files:
            (root / "replica.sqlite-wal").write_bytes(files["ledger.sqlite-wal"])
        deadline = time.monotonic() + 10

        def progress(*_):
            require(time.monotonic() < deadline, "sqlite_timeout")

        source = sqlite3.connect(replica.as_uri() + "?mode=ro", uri=True, timeout=1)
        destination = sqlite3.connect(snapshot)
        try:
            source.backup(destination, pages=128, progress=progress, sleep=0.01)
        finally:
            destination.close()
            source.close()
        store = _ReadOnlyStore(snapshot)
        with store.connection() as sql:
            _schema(sql)
            require([row[0] for row in sql.execute("PRAGMA integrity_check")] == ["ok"], "sqlite_integrity")
            require(sql.execute("PRAGMA foreign_key_check").fetchone() is None, "sqlite_foreign_keys")
            report["checks"]["sqlite_integrity"] = True
            require(store.get_meta("schema_version") == str(SCHEMA_VERSION), "unsupported_schema")
            require(store.get_meta("canonical") == "true", "noncanonical_ledger")
            require(store.get_meta("account_catalog") == "sqlite", "external_catalog_requires_review")
            report["checks"]["canonical_schema"] = True
            require(not store.verify_audit_chain(), "audit_chain_invalid")
            require(not sql.execute("SELECT 1 FROM entries LIMIT 1").fetchone() or
                    sql.execute("SELECT 1 FROM audit_events LIMIT 1").fetchone(), "audit_history_missing")
            report["checks"]["audit_chain"] = True
            integrity = importer.check_store_integrity(store)
            require(integrity.status != "incomplete-write", "ledger_write_incomplete")
            require(integrity.status == "ok", "ledger_seal_invalid")
            for table in ("accounts", "postings", "balance_assertions"):
                require(not sql.execute(f"SELECT 1 FROM {table} WHERE currency <> 'USD' LIMIT 1").fetchone(), "unsupported_currency")
            require(not validate(render_store_ledger(snapshot, conn=sql)), "core_ledger_invalid")
            report["checks"]["core_ledger"] = True
            if "entity.json" in files:
                _related_policies(_json(files["entity.json"]), {row[0] for row in sql.execute("SELECT name FROM accounts")})
            local = set()
            for table, column in (("entries", "metadata_json"), ("postings", "metadata_json"),
                                  ("source_transactions", "payload_json"), ("import_sessions", "metadata_json"),
                                  ("audit_events", "payload_json")):
                for row in sql.execute(f"SELECT {column} FROM {table}"):
                    _inspect(_json(row[0]), references, local)
            blockers.update(local)
            if local & {"embedded_credentials", "custom_configuration"}:
                for item in report["inventory"]:
                    if item["category"] == "ledger":
                        item.update(disposition="excluded", reason="sensitive_or_executable_configuration")
                        item.pop("sha256", None)
                return
            report["ledger"] = {"snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                                "content_sha256": store.content_digest(sql),
                                "wal_included": "ledger.sqlite-wal" in files,
                                "chart": "canonical_sqlite", "audit": "canonical_sqlite"}
        return snapshot.read_bytes()


def _references(values, root, inventory, files, blockers):
    checked = 0
    for value in sorted(values):
        name = value
        if value.startswith(str(root) + "/"):
            name = value[len(str(root)) + 1:]
            blockers.add("absolute_reference_requires_rewrite")
        elif value.startswith("@entity/"):
            name = value[8:]
        try:
            relative_path(name)
        except ContractError:
            blockers.add("unsafe_or_external_reference")
            continue
        if name not in inventory:
            blockers.add("referenced_bytes_missing")
        elif name not in files:
            # Directory references require a nonempty, wholly assessed subtree.
            descendants = [p for p in inventory if p.startswith(name + "/") and not stat.S_ISDIR(inventory[p][2])]
            if stat.S_ISDIR(inventory[name][2]) and descendants and all(p in files for p in descendants):
                checked += 1
            else:
                blockers.add("referenced_bytes_excluded")
        else:
            checked += 1
    return {"recognized": len(values), "resolved": checked, "exhaustive": False}


def _known_credentials(name):
    """Canonical non-book configuration only, not secret-looking evidence names."""
    first = PurePosixPath(name).parts[0]
    return (first == ".env" or re.fullmatch(
        r"\.env\.(?:local|development|production|staging|test|testing|preview|example|sample)(?:\.local)?", first
    ) is not None or first in {
        ".ssh", ".secrets", ".aws", ".config", ".gnupg", ".netrc", ".npmrc", ".pypirc",
        ".slashbooks-remote.json", "hosted.json",
        "id_rsa", "id_ed25519",
    })


def _capture(entity_dir, *, for_export=False):
    """Shared pinned capture. Private bytes never appear in the public assessment."""
    report = {"format": 1, "status": "blocked", "migration_not_implemented": True,
              "import_ready": False, "checks": {}, "inventory": [],
              "references": {"recognized": 0, "resolved": 0, "exhaustive": False},
              "limits": {"max_file_bytes": MAX_FILE, "max_total_bytes": MAX_TOTAL, "max_items": MAX_ITEMS}}
    blockers, references, files = {"migration_not_implemented"}, set(), {}
    snapshot, before = None, {}
    roots = EXPORT_ROOTS if for_export else ROOTS
    try:
        with _directory(entity_dir) as (fd, root):
            before = _inventory(fd)
            report["checks"]["inventory_complete"] = True
            for name, signature in before.items():
                mode, links = signature[2:4]
                if stat.S_ISDIR(mode) and not _excluded(name):
                    continue
                category = roots.get(name.split("/")[0], "other")
                if name in CONTEXT:
                    category = "entity" if name == "entity.json" else "context"
                elif name in {"ledger.sqlite", "ledger.sqlite-wal"}:
                    category = "ledger"
                elif name in {"books.beancount", "chart-of-accounts.beancount"}:
                    category = "chart_or_legacy_ledger"
                elif "/" not in name and is_document_path(name):
                    category = "audit" if name == "audit-log.jsonl" else "references"
                item = {"item": len(report["inventory"]) + 1, "category": category, "disposition": "excluded"}
                report["inventory"].append(item)
                reason = _excluded(name)
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)) or (stat.S_ISREG(mode) and links != 1):
                    reason = "unsafe_file"
                elif reason in {"runtime_state", "platform_metadata"} and not stat.S_ISREG(mode):
                    reason = "unsafe_file"
                elif not reason and mode & 0o111:
                    reason = "custom_code"
                if not reason:
                    try:
                        relative_path(name)
                    except ContractError:
                        reason = "unsafe_file"
                if not reason and (any(p.startswith(".") for p in name.split("/")) or category == "other" or
                                   (category != "ledger" and Path(name).suffix.lower() not in DATA_SUFFIXES)):
                    reason = "unsupported_file"
                if reason:
                    item["reason"] = reason
                    if reason not in {"derived_state", "runtime_state", "local_vcs", "platform_metadata"} and not (
                        for_export and reason == "credentials" and _known_credentials(name)
                    ):
                        blockers.add(reason)
                    continue
                try:
                    data = _read(fd, name, signature)
                    require(sum(len(v) for v in files.values()) + len(data) <= MAX_TOTAL, "workspace_size_limit")
                    local = set()
                    if Path(name).suffix.lower() == ".json":
                        for key, value in _inspection_items(name, data, before):
                            _inspect(value, references, local, key)
                        if name in {"entity.json", "trust-policy.json"}:
                            value = _json(data)
                            require(isinstance(value, dict), "invalid_context")
                        if name == "entity.json":
                            require(value.get("basis", value.get("accounting_basis", "cash")) == "cash", "unsupported_basis")
                            require(value.get("currency", "USD") == "USD", "unsupported_currency")
                            for key in ("bank_account_mappings", "csv_account_mappings", "source_coverage"):
                                require(key not in value or isinstance(value[key], dict), "invalid_context")
                            for key in ("declared_sources", "provider_sources", "related_entities", "category_rules"):
                                require(key not in value or isinstance(value[key], list), "invalid_context")
                            report["checks"]["entity_config"] = True
                    elif Path(name).suffix.lower() in {".jsonl", ".ndjson"}:
                        for line in data.splitlines():
                            if line.strip():
                                _inspect(_json(line), references, local)
                    blockers.update(local)
                    if local & {"embedded_credentials", "custom_configuration"}:
                        item["reason"] = "sensitive_or_executable_configuration"
                        continue
                    files[name] = data
                    item.update(disposition="review_candidate", bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                except Rejected as exc:
                    item["reason"] = str(exc)
                    blockers.add(str(exc))
                except (ValueError, UnicodeError, RecursionError):
                    item["reason"] = "invalid_structured_data"
                    blockers.add("invalid_structured_data")
            require(_book_inventory(before) == _book_inventory(_inventory(fd)), "source_changed")
            if "entity.json" not in files:
                blockers.add("entity_config_missing_or_excluded")
            if "ledger.sqlite" not in files:
                blockers.add("canonical_ledger_missing_or_excluded")
            elif "ledger.sqlite-wal" in before and "ledger.sqlite-wal" not in files:
                blockers.add("wal_not_captured")
            elif "ledger.sqlite-journal" not in before:
                try:
                    snapshot = _ledger(files, report, references, blockers)
                except Rejected as exc:
                    blockers.add(str(exc))
                except (sqlite3.Error, ValueError, ArithmeticError, TypeError, KeyError, RecursionError):
                    blockers.add("ledger_validation_failed")
            report["references"] = _references(references, root, before, files, blockers)
            require(_book_inventory(before) == _book_inventory(_inventory(fd)), "source_changed")
            # Detect replacement of the selected directory as well as its contents.
            with _directory(entity_dir) as (current, _):
                require(_signature(os.fstat(current)) == _signature(os.fstat(fd)), "source_changed")
            report["checks"]["source_stable"] = True
    except Rejected as exc:
        blockers.add(str(exc))
    except (OSError, ValueError, RecursionError):
        blockers.add("source_unavailable_or_unsafe")
    report["blockers"] = sorted(blockers)
    report["local_checks_passed"] = blockers == {"migration_not_implemented"}
    report["state_inventory"] = {
        category: sum(item["category"] == category and item["disposition"] == "review_candidate"
                      for item in report["inventory"])
        for category in ("entity", "ledger", "chart_or_legacy_ledger", "audit", "queues", "context", "references", "reports")
    }
    return report, files, snapshot, before


def assess(entity_dir):
    """Return sanitized observations; even a clean assessment is not import-ready."""
    return _capture(entity_dir)[0]


def build_bundle(entity_dir, destination):
    """Export a private portable snapshot; this does not authorize adoption."""
    from .migration_bundle import build_bundle as build
    return build(entity_dir, destination)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '{"error":"invalid_arguments"}\n')


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("--entity-dir", required=True)
    args = parser.parse_args(argv)
    result = assess(args.entity_dir)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 1  # No result authorizes migration, including a clean local assessment.


if __name__ == "__main__":
    raise SystemExit(main())
