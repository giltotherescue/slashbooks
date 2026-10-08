"""Private portable snapshots; no adoption, provider calls or ZIP extraction.

load_bundle returns {manifest, files}, where files maps company-relative paths to
verified bytes for the existing native entity/* transport. ZIP is offline only.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import zipfile

from . import hosted_migration as capture
from .remote_contract import ContractError, relative_path
from .reports import statements

FORMAT = "slashbooks-company-snapshot"
VERSION = 1
MAX_MANIFEST = 2 * 1024 * 1024
MAX_FRAME_HEADER = 1024 * 1024
TABLES = ("meta", "accounts", "entries", "postings", "balance_assertions",
          "source_transactions", "import_sessions", "audit_events", "sqlite_sequence")
SAFE_EXCLUSIONS = {"credentials", "derived_state", "runtime_state", "local_vcs", "platform_metadata"}
PATH_KEYS = capture.PATH_KEYS | {"files", "file_path", "directory", "csv_files", "csv_dir",
    "xlsx_file", "json_path", "txt_path", "report_path", "report_file", "artifacts",
    "evidence", "attachments", "documents"}
SECRET_TEXT = capture.SECRET_TEXT


class BundleError(ValueError):
    """A fixed diagnostic code, never a path, provider value or financial text."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise BundleError(code)


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _path(name):
    try:
        return relative_path(name)
    except (ContractError, TypeError):
        raise BundleError("unsafe_bundle_path") from None


def _limits(files):
    # Adoption adds .engine/bridge.json; its actual bytes are bounded by compute.
    _require(len(files) < capture.MAX_ITEMS, "inventory_limit")
    _require(all(isinstance(data, bytes) and len(data) <= capture.MAX_FILE for data in files.values()), "file_size_limit")
    _require(sum(map(len, files.values())) <= capture.MAX_TOTAL, "workspace_size_limit")


def _native_header_sizes(files):
    """Compact compute-v1 descriptors, with bounded sizes for future job/output.

    Hash contents do not affect encoded length. Reserve the native projection-v2
    descriptors and bridge too: a request that fits must have a return path.
    """
    def descriptor(path, size):
        return {"path": path, "size": size, "sha256": "0" * 64}

    entity = [descriptor("entity/" + name, len(data)) for name, data in sorted(files.items())]
    request = [descriptor("meta/job.json", MAX_FRAME_HEADER),
               descriptor("input/manifest.json", MAX_MANIFEST), *entity]
    views = {"accounts": ("all",), "sources": ("all", "staged", "posted"),
             "proposals": ("all", "open", "posted", "withdrawn"), "entries": ("all",)}
    response = [descriptor("meta/result.json", 15 * 1024 * 1024),
                descriptor("meta/projection-summary.json", 64 * 1024),
                descriptor("entity/.engine/bridge.json", capture.MAX_FILE), *entity]
    response.extend(descriptor(f"projection/{collection}/{view}.{suffix}", 2 ** 53 - 1)
                    for collection, group in views.items() for view in group for suffix in ("data", "index"))
    return tuple(len(_json_bytes({"version": 1, "files": items})) for items in (request, response))


def _supported(name):
    _path(name)
    _path("entity/" + name)
    _require(len(("entity/" + name).encode("utf-8")) <= 512, "framed_path_limit")
    _require(not capture._excluded(name), "excluded_bundle_member")
    _require(not any(part.startswith(".") for part in name.split("/")), "unsupported_file")
    _require(name == "ledger.sqlite" or
             ((name in capture.CONTEXT or name in {"books.beancount", "chart-of-accounts.beancount"}
               or name.split("/")[0] in capture.EXPORT_ROOTS or capture.is_document_path(name))
              and Path(name).suffix.lower() in capture.DATA_SUFFIXES), "unsupported_file")
    _require(name != "ledger.sqlite-wal", "noncanonical_bundle_ledger")


def _inspect(value, references, key="", depth=0):
    _require(depth < 80, "structured_data_limit")
    normalized = capture._normalized_key(key)
    _require(not capture.SECRET.search(normalized), "embedded_credentials")
    _require(normalized not in {"command", "executable", "base_url"}, "custom_configuration")
    if isinstance(value, str):
        _require(not SECRET_TEXT.search(value.encode("utf-8")), "embedded_credentials")
        path_field = normalized in PATH_KEYS or normalized.endswith(("_path", "_paths", "_file", "_files", "_folder", "_directory"))
        if value and (path_field or value.startswith(("/", "~/", "file:", "@entity/", "@input/", "@output/"))
                      or re.match(r"^[A-Za-z]:[\\/]", value)
                      or value.split("/")[0] in capture.EXPORT_ROOTS and "/" in value):
            references.add(value)
    elif isinstance(value, dict):
        for child, item in value.items():
            _inspect(item, references, child, depth + 1)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str):
                _inspect(item[1], references, item[0], depth + 1)
            else:
                _inspect(item, references, key, depth + 1)


def _references(files, references, inventory=None):
    for value in references:
        _require(not value.startswith(("/", "~/", "file:")) and not re.match(r"^[A-Za-z]:[\\/]", value),
                 "absolute_reference_requires_rewrite")
        name = _path(value[8:] if value.startswith("@entity/") else value)
        _require(not capture._excluded(name), "referenced_bytes_excluded")
        _require(name in files or any(path.startswith(name + "/") for path in files), "referenced_bytes_missing")
        if inventory is not None and name not in files:
            _require(not any(path.startswith(name + "/") and path not in files and
                             (not stat.S_ISDIR(signature[2]) or capture._excluded(path))
                             for path, signature in inventory.items()), "referenced_bytes_excluded")


def _file_checks(files, references):
    _limits(files)
    folded = set()
    for name, data in files.items():
        _supported(name)
        _require(name.casefold() not in folded, "ambiguous_bundle_path")
        folded.add(name.casefold())
        _require(not any(parent.as_posix() in files for parent in Path(name).parents if parent != Path(".")), "ambiguous_bundle_path")
        _require(not SECRET_TEXT.search(data), "embedded_credentials")
        if name == "ledger.sqlite":
            continue
        suffix = Path(name).suffix.lower()
        if suffix == ".json":
            for key, value in capture._inspection_items(name, data, files):
                _inspect(value, references, key)
            if Path(name).parent.as_posix() == "review-queue/quarantine":
                continue
            value = capture._json(data)
            if name in {"entity.json", "trust-policy.json"} or name.startswith(("review-queue/", "learned-context/")):
                _require(isinstance(value, dict), "unsupported_state_shape")
            if name in {"staging/pending.json", "staging/pending-categorization.json", "staging/seen-ids.json"}:
                _require(isinstance(value, list), "unsupported_state_shape")
        elif suffix in {".jsonl", ".ndjson"}:
            for line in data.splitlines():
                if line.strip():
                    _inspect(capture._json(line), references)
        elif suffix in {".md", ".txt", ".beancount"}:
            text = data.decode("utf-8")
            for target in re.findall(r"\]\(([^\s)]+)(?:\s+[^)]*)?\)", text):
                if not target.startswith(("https://", "http://", "mailto:", "#")):
                    references.add(target.strip("<>"))
            for target in re.findall(r'^\s*include\s+"([^"]+)"', text, re.M):
                references.add(target)
    _require(max(_native_header_sizes(files)) <= MAX_FRAME_HEADER, "framed_header_limit")


def _anchors(files, inventory=None):
    """Compute receiver-comparable anchors from private copied bytes only."""
    references = set()
    _file_checks(files, references)
    _require("entity.json" in files and "ledger.sqlite" in files, "required_state_missing")
    entity = capture._json(files["entity.json"])
    _require(entity.get("basis", entity.get("accounting_basis", "cash")) == "cash", "unsupported_basis")
    _require(entity.get("currency", "USD") == "USD", "unsupported_currency")
    identity = {"entity_json_sha256": _sha(files["entity.json"]), "basis": "cash", "currency": "USD"}
    for field in ("id", "name", "legal_name", "business_type", "legal_structure", "cutover_date"):
        if field in entity:
            _require((field == "cutover_date" and entity[field] is None) or
                     (isinstance(entity[field], str) and len(entity[field]) <= 1024), "invalid_entity_identity")
            identity[field] = entity[field]
    report, blockers = {"checks": {}, "inventory": []}, set()
    try:
        capture._ledger(files, report, references, blockers)
    except capture.Rejected as exc:
        raise BundleError(str(exc)) from None
    _require(not blockers, sorted(blockers)[0] if blockers else "ledger_validation_failed")
    _require("ledger" in report, "ledger_validation_failed")
    with tempfile.TemporaryDirectory(prefix="slashbooks-bundle-check-") as directory:
        path = Path(directory).resolve() / "ledger.sqlite"
        path.write_bytes(files["ledger.sqlite"])
        store = capture._ReadOnlyStore(path)
        with store.connection() as sql:
            tables = {}
            for table in TABLES:
                order = "key" if table == "meta" else "name" if table == "sqlite_sequence" else "id"
                rows = [dict(row) for row in sql.execute(f'SELECT * FROM "{table}" ORDER BY "{order}"')]
                tables[table] = {"rows": len(rows), "sha256": _sha(_json_bytes(rows))}
            for row in sql.execute("SELECT key,value FROM meta"):
                _inspect(row[1], references, row[0])
            for table, column in (("entries", "metadata_json"), ("entries", "links_json"), ("postings", "metadata_json"),
                                  ("source_transactions", "payload_json"), ("import_sessions", "metadata_json"), ("audit_events", "payload_json")):
                for row in sql.execute(f'SELECT "{column}" FROM "{table}"'):
                    _inspect(capture._json(row[0]), references)
            dates = [row[0] for row in sql.execute("SELECT date FROM entries UNION SELECT date FROM balance_assertions UNION SELECT open_date FROM accounts")]
            start, end = (min(dates), max(dates)) if dates else ("1970-01-01", "1970-01-01")
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            reports = [statements._compute_pnl(sql, first, last), statements._compute_balance_sheet(sql, last),
                       statements._compute_trial_balance(sql, last), statements._compute_general_ledger(sql, first, last)]
            fingerprints = {item.kind: _sha(_json_bytes(capture._json(item.to_json()))) for item in reports}
            ledger = {"schema_version": capture.SCHEMA_VERSION, "content_sha256": store.content_digest(sql), "tables": tables}
    _references(files, references, inventory)
    return identity, ledger, {"from": start, "to": end, "fingerprints": fingerprints}


def _manifest(files, excluded, inventory=None):
    identity, ledger, reports = _anchors(files, inventory)
    return {"format": FORMAT, "version": VERSION,
            "source_entity": identity, "ledger": ledger, "reports": reports, "excluded": excluded,
            "files": [{"path": name, "size": len(data), "sha256": _sha(data)} for name, data in sorted(files.items())]}


def _summary(manifest):
    return {"format": VERSION, "status": "validated", "import_ready": False,
            "file_count": len(manifest["files"]), "total_bytes": sum(item["size"] for item in manifest["files"]),
            "manifest_sha256": _sha(_json_bytes(manifest)), "ledger_content_sha256": manifest["ledger"]["content_sha256"],
            "report_fingerprints": manifest["reports"]["fingerprints"], "blockers": []}


def validate_files(manifest, files):
    """Validate private manifest + relative bytes for an engine consumer, no ZIP required.

    Raises BundleError containing a fixed code. The returned summary is safe to log;
    the input manifest/files contain company data and must remain private.
    """
    try:
        _require(isinstance(manifest, dict) and isinstance(files, dict), "invalid_bundle")
        _limits(files)
        _require(set(manifest) == {"format", "version", "source_entity", "ledger", "reports", "excluded", "files"}, "invalid_manifest")
        _require(manifest["format"] == FORMAT and type(manifest["version"]) is int and manifest["version"] == VERSION,
                 "unsupported_bundle_version")
        excluded = manifest["excluded"]
        _require(isinstance(excluded, dict) and all(key in SAFE_EXCLUSIONS and type(count) is int and 0 < count <= capture.MAX_ITEMS
                                                  for key, count in excluded.items()), "invalid_manifest")
        _require(len(_json_bytes(manifest)) <= MAX_MANIFEST, "manifest_size_limit")
        entries = manifest["files"]
        _require(isinstance(entries, list) and len(entries) < capture.MAX_ITEMS, "invalid_manifest")
        names = []
        for entry in entries:
            _require(isinstance(entry, dict) and set(entry) == {"path", "size", "sha256"}, "invalid_manifest")
            name = _path(entry["path"])
            _require(name not in names, "duplicate_bundle_member")
            names.append(name)
            _require(type(entry["size"]) is int and 0 <= entry["size"] <= capture.MAX_FILE and
                     isinstance(entry["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), "invalid_manifest")
            _require(name in files and isinstance(files[name], bytes) and len(files[name]) == entry["size"] and _sha(files[name]) == entry["sha256"],
                     "bundle_hash_mismatch")
        _require(names == sorted(files), "bundle_members_mismatch")
        expected = _manifest(files, excluded)
        _require(_json_bytes(manifest) == _json_bytes(expected), "bundle_anchors_mismatch")
        return _summary(manifest)
    except BundleError:
        raise
    except capture.Rejected as exc:
        raise BundleError(str(exc)) from None
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError, RecursionError, sqlite3.Error):
        raise BundleError("bundle_validation_failed") from None


def _load_bytes(raw):
    _require(len(raw) <= capture.MAX_TOTAL + MAX_MANIFEST + capture.MAX_ITEMS * 4096, "bundle_size_limit")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        _require(len(members) <= capture.MAX_ITEMS, "inventory_limit")
        names, total = set(), 0
        for info in members:
            _require(info.filename == info.orig_filename, "unsafe_bundle_path")
            _path(info.filename)
            _require(info.filename not in names, "duplicate_bundle_member")
            names.add(info.filename)
            mode = info.external_attr >> 16
            _require(not info.is_dir() and stat.S_IFMT(mode) in {0, stat.S_IFREG} and not mode & 0o111,
                     "unsafe_bundle_member")
            _require(info.compress_type == zipfile.ZIP_STORED and not info.flag_bits & 1, "unsupported_zip_encoding")
            _require(not info.extra and not info.comment, "unsupported_zip_metadata")
            maximum = MAX_MANIFEST if info.filename == "manifest.json" else capture.MAX_FILE
            _require(info.file_size <= maximum and info.compress_size == info.file_size, "file_size_limit")
            if info.filename != "manifest.json":
                _require(info.filename.startswith("entity/"), "unexpected_bundle_member")
                _supported(info.filename[7:])
                total += info.file_size
        _require(not archive.comment, "unsupported_zip_metadata")
        _require(total <= capture.MAX_TOTAL and "manifest.json" in names, "invalid_bundle")
        manifest = capture._json(archive.read("manifest.json"))
        files = {info.filename[7:]: archive.read(info) for info in members if info.filename != "manifest.json"}
    validate_files(manifest, files)
    return {"manifest": manifest, "files": files}


def load_bundle(path):
    """Return verified private manifest/files. Never extracts paths or opens source SQLite."""
    try:
        source = Path(path)
        with capture._directory(source.parent) as (fd, _):
            info = os.stat(source.name, dir_fd=fd, follow_symlinks=False)
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe_bundle_file")
            maximum = capture.MAX_TOTAL + MAX_MANIFEST + capture.MAX_ITEMS * 4096
            _require(info.st_size <= maximum, "bundle_size_limit")
            handle = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(handle, "rb") as stream:
                signature = capture._signature(info)
                _require(capture._signature(os.fstat(stream.fileno())) == signature, "bundle_changed")
                raw = stream.read(maximum + 1)
                _require(len(raw) == info.st_size and capture._signature(os.fstat(stream.fileno())) == signature, "bundle_changed")
        return _load_bytes(raw)
    except BundleError:
        raise
    except capture.Rejected as exc:
        raise BundleError(str(exc)) from None
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError, RecursionError, sqlite3.Error, zipfile.BadZipFile, NotImplementedError):
        raise BundleError("bundle_validation_failed") from None


def validate_bundle(path):
    """Sanitized validation result; successful validation is never import readiness."""
    try:
        return _summary(load_bundle(path)["manifest"])
    except BundleError as exc:
        return {"format": VERSION, "status": "blocked", "import_ready": False, "blockers": [exc.code]}


def _write_exclusive(destination, raw):
    target = Path(destination)
    with capture._directory(target.parent) as (fd, _):
        handle = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        signature = os.fstat(handle)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(fd)
        except BaseException:
            try:
                current = os.stat(target.name, dir_fd=fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (signature.st_dev, signature.st_ino):
                    os.unlink(target.name, dir_fd=fd)
            except FileNotFoundError:
                pass
            raise


def build_bundle(entity_dir, destination):
    """Freeze supported company state into a private exclusive ZIP; no source writes.

    Stop company writers before export. Any observed source change blocks export.
    No function in this module adopts a company or returns import_ready=True.
    """
    try:
        source, target = Path(os.path.abspath(entity_dir)), Path(os.path.abspath(destination))
        _require(target != source and source not in target.parents, "destination_inside_source")
        # Pin/validate output ancestors before doing any potentially expensive capture.
        with capture._directory(Path(destination).parent):
            pass
        report, files, snapshot, inventory = capture._capture(entity_dir, for_export=True)
        blockers = sorted(set(report["blockers"]) - {"migration_not_implemented"})
        if blockers:
            return {"format": VERSION, "status": "blocked", "bundle_created": False, "import_ready": False, "blockers": blockers}
        _require(report["checks"].get("source_stable") and snapshot is not None, "capture_incomplete")
        files = {name: data for name, data in files.items() if name != "ledger.sqlite-wal"}
        files["ledger.sqlite"] = snapshot
        excluded = dict(sorted(Counter(item["reason"] for item in report["inventory"]
                                       if item["disposition"] == "excluded" and item.get("reason") in SAFE_EXCLUSIONS).items()))
        manifest = _manifest(files, excluded, inventory)
        raw_manifest = _json_bytes(manifest)
        _require(len(raw_manifest) <= MAX_MANIFEST, "manifest_size_limit")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
            for name, data in [("manifest.json", raw_manifest), *(("entity/" + name, data) for name, data in sorted(files.items()))]:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, data)
        raw = stream.getvalue()
        _write_exclusive(destination, raw)
        return {**_summary(manifest), "status": "exported", "bundle_created": True, "bundle_sha256": _sha(raw)}
    except FileExistsError:
        code = "destination_exists"
    except (BundleError, capture.Rejected) as exc:
        code = str(exc)
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError, RecursionError, sqlite3.Error, zipfile.BadZipFile):
        code = "bundle_export_failed"
    return {"format": VERSION, "status": "blocked", "bundle_created": False, "import_ready": False, "blockers": [code]}
