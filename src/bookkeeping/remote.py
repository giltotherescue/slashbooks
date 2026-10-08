"""Transparent hosted execution, with local inputs and explicitly mapped outputs only."""

from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from typing import Any
from urllib.parse import urlencode
import uuid

from bookkeeping import hosted


CONFIG_NAME = ".slashbooks-remote.json"
MAX_TRANSFER_BYTES = 12 * 1024 * 1024
MAX_FILES = 1000


def _credential_file(path: Path) -> bool:
    return any(part == ".env" or part.startswith(".env.") or part.startswith(".slashbooks-")
               or part in {".ssh", ".aws", ".config", ".codex", ".claude", ".netrc",
                           "hosted.json", "credentials.json", "hosted-receipts"}
               or part.endswith((".pem", ".key")) for part in path.parts)


def _error(code: str, message: str) -> hosted.HostedError:
    return hosted.HostedError(code, message)


def _absolute(value: str | Path) -> Path:
    path = Path(os.path.abspath(Path(value).expanduser()))
    # macOS exposes these system directories through symlinks; retain checks on
    # every user-controlled path component below them.
    if sys.platform == "darwin":
        for alias in (Path("/tmp"), Path("/var")):
            if path.is_relative_to(alias):
                return alias.resolve() / path.relative_to(alias)
    return path


def _binding(directory: Path) -> Path | None:
    directory = _absolute(directory)
    for parent in (directory, *directory.parents):
        candidate = parent / CONFIG_NAME
        # A broken symlink or unreadable binding must not enable local fallback.
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        return candidate
    return None


def _discover(args: argparse.Namespace) -> Path | None:
    entity = getattr(args, "entity", None) or getattr(args, "entity_path", None)
    if args.command in {"entity", "demo"}:
        entity = getattr(args, "path", None)
    return _binding(Path(entity) if entity is not None else Path.cwd())


def _revision(result: dict[str, Any]) -> int:
    value = result.get("books_revision")
    if type(value) is not int or value < 0:
        raise _error("INVALID_REVISION", "Server must return a nonnegative integer books_revision.")
    return value


@contextmanager
def _revision_lock(config: Path):
    import fcntl

    config = _absolute(config)
    path = config.parent / ".slashbooks-remote-state.lock"
    _no_symlinks(path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags, 0o600), "r+") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise _error("INSECURE_FILE", "Remote revision state must be owner-only.")
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield config.parent / ".slashbooks-remote-state.json"
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _cached_revision(path: Path, client: hosted.HostedClient) -> int | None:
    if not os.path.lexists(path):
        return None
    value = hosted._read_json(path, private=True)
    if value.get("endpoint") != client.endpoint or value.get("company") != client.company:
        return None
    return _revision({"books_revision": value.get("last_seen_revision")})


def _last_revision(config: Path, client: hosted.HostedClient) -> int | None:
    with _revision_lock(config) as path:
        return _cached_revision(path, client)


def remember_revision(config: Path, client: hosted.HostedClient, revision: int) -> None:
    _revision({"books_revision": revision})
    with _revision_lock(config) as path:
        previous = _cached_revision(path, client)
        # Delayed responses and replayed receipts cannot regress an observed snapshot.
        if previous is None or revision > previous:
            hosted._write_private(path, {"endpoint": client.endpoint, "company": client.company,
                                        "last_seen_revision": revision})


def _expected_revision(config: Path, client: hosted.HostedClient, *, mutates: bool) -> int:
    revision = _last_revision(config, client) if mutates else None
    if revision is None:
        revision = _revision(client.request(""))
        if mutates:
            remember_revision(config, client, revision)
    return revision


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise _error("INVALID_PATH", "Expected a normalized relative file path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise _error("INVALID_PATH", "Expected a normalized relative file path without traversal.")
    return path.as_posix()


def _no_symlinks(path: Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise _error("UNSAFE_PATH", "Symbolic links are not supported for transferred files.")


def _read_bytes(path: Path) -> bytes:
    _no_symlinks(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise _error("INVALID_FILE", "Transfers require regular files.")
        data = handle.read(MAX_TRANSFER_BYTES + 1)
    if len(data) > MAX_TRANSFER_BYTES:
        raise _error("TRANSFER_LIMIT", "File exceeds the transfer size limit.")
    return data


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint(path: Path) -> str | None:
    _no_symlinks(path)
    return _digest(_read_bytes(path)) if path.exists() else None


def _protected(path: Path, root: Path) -> None:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return
    if parts and (parts[0].startswith(".slashbooks-") or parts[0] in {
        "entity.json", "ledger.sqlite", "ledger.sqlite-wal", "ledger.sqlite-shm",
        "context", "profile", "learned-context", ".env",
    }):
        raise _error("LOCAL_STATE_WRITE", "Choose an explicit download location outside local company state.")


def _output_map(wire: str, local: Path, *, directory: bool, root: Path) -> dict[str, Any]:
    _no_symlinks(local)
    _protected(local, root)
    existing: dict[str, str | None] = {}
    if directory:
        if local.exists() and not local.is_dir():
            raise _error("INVALID_OUTPUT", "Output directory is not a directory.")
        if local.exists():
            for file in sorted(local.rglob("*")):
                _no_symlinks(file)
                if file.is_file():
                    existing[file.relative_to(local).as_posix()] = _fingerprint(file)
                    if len(existing) > MAX_FILES:
                        raise _error("TRANSFER_LIMIT", "Output directory has too many files.")
    else:
        existing[""] = _fingerprint(local)
    return {"wire": wire, "local": str(local), "directory": directory, "existing": existing}


def _serialize(args: argparse.Namespace, spec: dict[str, Any], replacements: dict[str, Any]) -> list[str]:
    from bookkeeping.cli import build_parser

    parser = build_parser()
    result: list[str] = []
    route = list(spec["path"])
    while True:
        positional: list[str] = []
        sub = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                sub = action
                continue
            if action.dest == "help":
                continue
            value = replacements.get(action.dest, getattr(args, action.dest, None))
            if value is None:
                continue
            if not action.option_strings:
                positional.extend(str(item) for item in (value if isinstance(value, list) else [value]))
                continue
            option = action.option_strings[0]
            if isinstance(action, argparse._StoreTrueAction):
                if value:
                    result.append(option)
            elif isinstance(action, argparse._StoreFalseAction):
                if not value:
                    result.append(option)
            elif isinstance(action, argparse._AppendAction):
                result.extend(option + "=" + str(item) for item in value)
            elif isinstance(value, list):
                result.append(option)
                result.extend(str(item) for item in value)
            else:
                result.append(option + "=" + str(value))
        if sub is None:
            if positional:
                result.extend(["--", *positional])
            break
        if positional or not route or route[0] not in sub.choices:
            raise _error("INVALID_CONTRACT", "Shared command specification does not match the CLI parser.")
        name = route.pop(0)
        result.append(name)
        parser = sub.choices[name]
    if route:
        raise _error("INVALID_CONTRACT", "Shared command path has extra components.")
    return result


def _prepare(args: argparse.Namespace, spec: dict[str, Any], root: Path) -> tuple[list[str], list[dict], list[dict]]:
    replacements: dict[str, Any] = {}
    inputs: list[dict] = []
    outputs: list[dict] = []
    total = 0
    migration = tuple(spec["path"]) == ("ledger", "migrate")
    if migration:
        source = root / "books.beancount"
        _no_symlinks(source)
        try:
            source.lstat()
        except FileNotFoundError:
            pass
        else:
            data = _read_bytes(source)
            total += len(data)
            inputs.append({"path": "@entity/books.beancount", "data_base64": base64.b64encode(data).decode("ascii")})
    for field in spec["entity_fields"]:
        if getattr(args, field, None) is not None:
            replacements[field] = "@entity"
    for field in spec["input_fields"]:
        original = getattr(args, field, None)
        if original is None:
            continue
        values = original if isinstance(original, list) else [original]
        mapped = []
        for index, value in enumerate(values):
            local = _absolute(value)
            _no_symlinks(local)
            if field == "store" and local.is_relative_to(root):
                mapped.append("@entity/" + _relative(local.relative_to(root).as_posix()))
                continue
            if not local.exists():
                try:
                    relative = local.relative_to(root).as_posix()
                except ValueError:
                    raise _error("MISSING_INPUT", "Local input does not exist.") from None
                mapped.append("@entity/" + _relative(relative))
                continue
            # Explicit local inputs are uploads, never a mirror of company state.
            wire = "@input/" + field + "/" + str(index) + "/" + _relative(local.name)
            mapped.append(wire)
            paths = sorted(local.rglob("*")) if local.is_dir() else [local]
            for file in paths:
                if _credential_file(file.relative_to(local) if local.is_dir() else Path(file.name)):
                    if local.is_dir():
                        continue
                    raise _error("SECRET_INPUT", "Configuration and credential files cannot be uploaded as inputs.")
                _no_symlinks(file)
                if file.is_dir():
                    continue
                _protected(file, root)
                data = _read_bytes(file)
                total += len(data)
                if total > MAX_TRANSFER_BYTES or len(inputs) >= MAX_FILES:
                    raise _error("TRANSFER_LIMIT", "Inputs exceed the transfer size or file-count limit.")
                name = wire + "/" + _relative(file.relative_to(local).as_posix()) if local.is_dir() else wire
                inputs.append({"path": name, "data_base64": base64.b64encode(data).decode("ascii")})
        replacements[field] = mapped if isinstance(original, list) else mapped[0]
    for field in spec["output_fields"]:
        value = getattr(args, field, None)
        if value is None:
            continue
        local = _absolute(value)
        if spec["network"] and hasattr(args, "overwrite") and not args.overwrite and local.exists():
            raise _error("OUTPUT_EXISTS", "Output already exists. Use --overwrite to replace it.")
        try:
            relative = local.relative_to(root).as_posix()
            wire = "@entity" if relative == "." else "@entity/" + _relative(relative)
        except ValueError:
            wire = "@output/" + field + "/" + _relative(local.name)
        replacements[field] = wire
        if field == "store" and migration and args.dry_run:
            continue
        if field != "store" or not local.is_relative_to(root):
            outputs.append(_output_map(wire, local, directory=field == "output_dir", root=root))
    if args.command == "export" and getattr(args, "output_dir", None) is None:
        outputs.append(_output_map("@entity/reports/accountant-export", root / "reports/accountant-export", directory=True, root=root))
    if args.command == "quarterly-review":
        for suffix in ("json", "txt"):
            relative = f"reports/quarterly/{args.year}-{args.quarter}.{suffix}"
            outputs.append(_output_map("@entity/" + relative, root / relative, directory=False, root=root))
    return _serialize(args, spec, replacements), inputs, outputs


def _artifact(value: Any) -> bytes:
    if not isinstance(value, dict) or not isinstance(value.get("data_base64"), str):
        raise _error("INVALID_ARTIFACT", "Server returned an invalid artifact.")
    try:
        data = base64.b64decode(value["data_base64"], validate=True)
    except (ValueError, binascii.Error):
        raise _error("INVALID_ARTIFACT", "Artifact is not valid base64.") from None
    if len(data) > MAX_TRANSFER_BYTES or _digest(data) != value.get("sha256"):
        raise _error("INVALID_ARTIFACT", "Artifact size or checksum validation failed.")
    return data


def _publish(path: Path, data: bytes, expected: str | None) -> None:
    current = _fingerprint(path)
    if current == _digest(data):
        return
    if current != expected:
        raise _error("OUTPUT_CHANGED", "Local output changed since submission. Saved receipt can recover the artifact without rerunning the command.")
    path.parent.mkdir(parents=True, exist_ok=True)
    _no_symlinks(path)
    fd, temporary = tempfile.mkstemp(prefix=".remote-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if _fingerprint(path) != current:
            raise _error("OUTPUT_CHANGED", "Local output changed while preparing the download.")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _download(result: dict, record: dict) -> dict[str, str]:
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > MAX_FILES:
        raise _error("INVALID_RESPONSE", "Command result must contain a bounded artifact list.")
    pending = []
    destinations: set[Path] = set()
    seen: set[str] = set()
    total = 0
    for artifact in artifacts:
        name = artifact.get("path") if isinstance(artifact, dict) else None
        if not isinstance(name, str) or name in seen:
            raise _error("INVALID_ARTIFACT", "Artifact paths must be unique strings.")
        seen.add(name)
        _relative(name)
        for mapping in record["outputs"]:
            prefix = mapping["wire"]
            relative = ""
            if mapping["directory"]:
                if not name.startswith(prefix + "/"):
                    continue
                relative = _relative(name[len(prefix) + 1:])
            elif name != prefix:
                continue
            local = Path(mapping["local"]) / relative if relative else Path(mapping["local"])
            _protected(local, Path(record["root"]))
            _no_symlinks(local)
            if local in destinations:
                raise _error("INVALID_ARTIFACT", "Multiple artifacts target the same local file.")
            destinations.add(local)
            data = _artifact(artifact)
            total += len(data)
            if total > MAX_TRANSFER_BYTES:
                raise _error("TRANSFER_LIMIT", "Artifacts exceed the transfer size limit.")
            pending.append((local, data, mapping["existing"].get(relative), name, mapping))
            break
        # Unrequested server artifacts never become local files.
    if result.get("exit_code") == 0:
        for mapping in record["outputs"]:
            if not mapping["directory"] and mapping["wire"] not in seen:
                raise _error("MISSING_ARTIFACT", "Requested output is missing. Recover using the saved receipt.")
    downloaded = {}
    for local, data, expected, name, mapping in pending:
        _publish(local, data, expected)
        downloaded[name] = str(local)
        if mapping["directory"]:
            # Only ancestors of a downloaded file are known to exist locally.
            wire_parent, local_parent = PurePosixPath(name).parent, local.parent
            while True:
                downloaded[str(wire_parent)] = str(local_parent)
                if str(wire_parent) == mapping["wire"]:
                    break
                wire_parent, local_parent = wire_parent.parent, local_parent.parent
    return downloaded


_OUTPUT_PATH_FIELDS = frozenset({
    "output", "output_dir", "output_file", "output_path", "path", "paths",
    "file", "files", "file_path", "filename", "directory", "csv_files", "csv_dir",
    "xlsx_file", "json_path", "txt_path", "report_path", "report_file",
})
_ARTIFACT_MESSAGE = re.compile(
    r"([ \t]*(?:Snapshot written:|Written to|Wrote \d+ transaction\(s\) to|"
    r"Accountant export written to:|CSV exports: \d+ files in|XLSX workbook:|"
    r"Migrated:|Already current:|Checked:)[ \t]+)([^\r\n]+)(\r?\n)?"
)


def _localize_output(text: str, downloaded: dict[str, str]) -> str:
    if not downloaded:
        return text

    def translate(value: Any, field: str = "") -> Any:
        if isinstance(value, dict):
            return {key: translate(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [translate(item, field) for item in value]
        if isinstance(value, str) and field in _OUTPUT_PATH_FIELDS:
            return downloaded.get(value, value)
        return value

    try:
        document = hosted._json_loads(text)
        localized = translate(document)
    except (ValueError, RecursionError):
        # Match the core's artifact messages, not arbitrary descriptions or substrings.
        lines = []
        for line in text.splitlines(keepends=True):
            match = _ARTIFACT_MESSAGE.fullmatch(line)
            if match and match[2] in downloaded:
                line = match[1] + downloaded[match[2]] + (match[3] or "")
            lines.append(line)
        return "".join(lines)
    if localized == document:
        return text
    trailing = text[len(text.rstrip()):]
    return json.dumps(localized, ensure_ascii=False, allow_nan=False) + trailing


def _record_digest(record: dict) -> str:
    return hosted._body_digest({key: value for key, value in record.items() if key not in {
        "record_sha256", "status", "response", "response_sha256",
    }})


def _new_record(client: hosted.HostedClient, config: Path, body: dict, outputs: list,
                *, kind: str = "engine-command", key: str | None = None,
                file_local: Path | None = None) -> tuple[dict, Path]:
    if len(json.dumps(body, sort_keys=True, allow_nan=False).encode("utf-8")) > hosted.MAX_REQUEST_BYTES:
        raise _error("REQUEST_TOO_LARGE", "Encoded JSON request exceeds 16 MiB. Reduce the inputs; nothing was submitted.")
    key = hosted._key(key if key is not None else str(uuid.uuid4()))
    if hosted._redact(body, client.secrets) != body or hosted._redact(key, client.secrets) != key:
        raise _error("SECRET_IN_INPUT", "Do not include API credentials in command inputs or keys.")
    encoded = [item["data_base64"] for item in body.get("inputs", [])]
    if "data_base64" in body:
        encoded.append(body["data_base64"])
    for value in encoded:
        data = base64.b64decode(value, validate=True)
        if any(secret and secret.encode() in data for secret in client.secrets):
            raise _error("SECRET_IN_INPUT", "Input file contains an API credential.")
    record = {**hosted._identity(client), "kind": kind, "config_path": str(config),
              "root": str(config.parent), "idempotency_key": key,
              "route": "/engine/commands" if kind == "engine-command" else "/engine/files",
              "method": "POST" if kind == "engine-command" else "PUT",
              "body": body, "outputs": outputs, "body_sha256": hosted._body_digest(body), "status": "pending"}
    if file_local is not None:
        record["file_local"] = str(file_local)
    record["record_sha256"] = _record_digest(record)
    filename = hosted._body_digest({**hosted._identity(client), "key": key}) + ".json"
    path = config.parent / ".slashbooks-remote-receipts" / filename
    _no_symlinks(path)
    try:
        hosted._write_private(path, record, exclusive=True)
    except FileExistsError:
        existing = hosted._read_json(path, private=True)
        if existing.get("record_sha256") != record["record_sha256"]:
            raise _error("IDEMPOTENCY_CONFLICT", "Existing key has different input. Retry its original receipt.") from None
        record = existing
    return record, path


def _finish(client: hosted.HostedClient, record: dict, path: Path) -> int:
    recovery = {"idempotency_key": record["idempotency_key"], "local_receipt": str(path)}
    print(json.dumps(hosted._redact({"event": "remote_submission", **recovery}, client.secrets)), file=sys.stderr, flush=True)
    try:
        if record.get("status") == "received":
            response = record.get("response")
            if not isinstance(response, dict) or record.get("response_sha256") != hosted._body_digest(response):
                raise _error("INVALID_RECEIPT", "Saved response failed integrity validation.")
        else:
            response = client.request(record["route"], body=record["body"], key=record["idempotency_key"],
                                      method=record["method"], preserve_artifacts=True)
            record = {**record, "status": "received", "response": response, "response_sha256": hosted._body_digest(response)}
            hosted._write_private(path, record)
        _revision(response)
        if record["kind"] == "engine-file-put":
            remember_revision(Path(record["config_path"]), client, _revision(response))
            if record.get("file_local"):
                tracking = _file_tracking(Path(record["config_path"]), client, record["body"]["path"], Path(record["file_local"]))
                with _revision_lock(Path(record["config_path"])):
                    _no_symlinks(tracking)
                    previous = hosted._read_json(tracking, private=True) if tracking.exists() else None
                    # Replaying an old receipt must not roll back a newer download baseline.
                    if previous is None or _revision(previous) <= _revision(response):
                        hosted._write_private(tracking, {"books_revision": _revision(response),
                            "sha256": _digest(base64.b64decode(record["body"]["data_base64"], validate=True))})
            print(json.dumps(hosted._redact(response, client.secrets), sort_keys=True))
            return 0
        if (type(response.get("exit_code")) is not int or not 0 <= response["exit_code"] <= 255
                or not isinstance(response.get("stdout"), str) or not isinstance(response.get("stderr"), str)
                or type(response.get("state_committed")) is not bool
                or not isinstance(response.get("command_id"), str) or not response["command_id"]):
            raise _error("INVALID_RESPONSE", "Server returned an invalid engine result. Use the saved receipt for recovery.")
        remember_revision(Path(record["config_path"]), client, _revision(response))
        downloaded = _download(response, record)
        sys.stdout.write(hosted._redact(_localize_output(response["stdout"], downloaded), client.secrets))
        sys.stderr.write(hosted._redact(_localize_output(response["stderr"], downloaded), client.secrets))
        return response["exit_code"]
    except hosted.HostedError as exc:
        exc.payload["error"].update(recovery)
        raise
    except OSError as exc:
        raise hosted.HostedError("LOCAL_RECOVERY_REQUIRED", "Cannot save receipt or output. Retry the original receipt; do not submit a new command.", **recovery) from exc


def _report_error(exc: Exception, secrets: Any = None) -> int:
    payload = exc.payload if isinstance(exc, hosted.HostedError) else {
        "error": {"code": "REMOTE_LOCAL_ERROR", "message": "Cannot prepare remote request or recover local outputs. No local fallback was used."},
    }
    print(json.dumps(hosted._redact(payload, secrets)), file=sys.stderr)
    return 1


def maybe_run(argv: list[str], args: argparse.Namespace) -> int | None:
    secrets: Any = None
    try:
        config_path = _discover(args)
        if config_path is None:
            return None
        config = hosted._read_json(config_path, private=True)
        try:
            from bookkeeping.remote_contract import command_spec
        except ImportError:
            raise _error("REMOTE_CONTRACT_UNAVAILABLE", "Shared remote command contract is unavailable. No local fallback was used.") from None
        spec = command_spec(argv)
        if spec["network"] and args.command == "connector" and os.environ.get(args.api_key_env):
            return None
        client = hosted.HostedClient(config)
        secrets = client.secrets
        normalized, inputs, outputs = _prepare(args, spec, config_path.parent)
        body = {"argv": normalized, "inputs": inputs,
                "expected_books_revision": _expected_revision(config_path, client, mutates=spec["mutates"])}
        explanation = next((getattr(args, field, None) for field in ("reason", "reasoning", "explanation", "approval_note", "note")
                            if isinstance(getattr(args, field, None), str) and getattr(args, field).strip()), None)
        if explanation is not None:
            body["explanation"] = explanation
        record, receipt = _new_record(client, config_path, body, outputs)
        return _finish(client, record, receipt)
    except (hosted.HostedError, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return _report_error(exc, secrets)


def retry(args: argparse.Namespace) -> int:
    secrets: Any = None
    try:
        path = _absolute(args.receipt)
        record = hosted._read_json(path, private=True)
        config_path = Path(record["config_path"])
        default = Path.home() / ".config/slashbooks/hosted.json"
        if args.config != default:
            config_path = _absolute(args.config)
        client = hosted.HostedClient(hosted._read_json(config_path, private=True), timeout=args.timeout)
        secrets = client.secrets
        if any(record.get(key) != value for key, value in hosted._identity(client).items()):
            raise _error("RECEIPT_SCOPE_MISMATCH", "Receipt belongs to another endpoint, company or credential.")
        hosted._key(record["idempotency_key"])
        expected = ("/engine/commands", "POST") if record["kind"] == "engine-command" else ("/engine/files", "PUT")
        if ((record.get("route"), record.get("method")) != expected
                or record.get("record_sha256") != _record_digest(record)
                or record.get("body_sha256") != hosted._body_digest(record["body"])):
            raise _error("INVALID_RECEIPT", "Receipt changed. Restore the original immutable request.")
        return _finish(client, record, path)
    except (hosted.HostedError, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return _report_error(exc, secrets)


def _file_tracking(config: Path, client: hosted.HostedClient, relative: str, local: Path) -> Path:
    identity = {**hosted._identity(client), "path": relative, "local": str(local)}
    return config.parent / ".slashbooks-remote-downloads" / (hosted._body_digest(identity) + ".json")


def run_file(args: argparse.Namespace) -> int:
    secrets: Any = None
    try:
        config_path = _binding(args.entity or Path.cwd())
        if args.entity is not None and config_path is None:
            raise _error("NOT_REMOTE", "Entity directory has no remote binding.")
        if config_path is None:
            config_path = _absolute(args.config)
        client = hosted.HostedClient(hosted._read_json(config_path, private=config_path.name == CONFIG_NAME), timeout=args.timeout)
        secrets = client.secrets
        if args.file_command == "list":
            result = client.request("/engine/files")
            _revision(result)
            if not isinstance(result.get("files"), list):
                raise _error("INVALID_RESPONSE", "Server must return a files array.")
            remember_revision(config_path, client, _revision(result))
            print(json.dumps(result, sort_keys=True))
            return 0
        relative = _relative(args.path)
        if args.file_command == "get":
            local = _absolute(args.output)
            _protected(local, config_path.parent)
            expected = _fingerprint(local)
            result = client.request("/engine/files?" + urlencode({"path": relative}), preserve_artifacts=True)
            revision = _revision(result)
            if result.get("path") != relative:
                raise _error("INVALID_RESPONSE", "Server returned a different file path.")
            data = _artifact(result)
            tracking = _file_tracking(config_path, client, relative, local)
            with _revision_lock(config_path):
                _no_symlinks(tracking)
                _publish(local, data, expected)
                hosted._write_private(tracking, {"books_revision": revision, "sha256": result["sha256"]})
            remember_revision(config_path, client, revision)
            print(json.dumps(hosted._redact({"path": relative, "output": str(local), "sha256": result["sha256"], "books_revision": revision}, client.secrets), sort_keys=True))
            return 0
        local = _absolute(args.file)
        data = _read_bytes(local)
        revision = args.expected_books_revision
        tracking = _file_tracking(config_path, client, relative, local)
        if revision is None:
            if tracking.exists():
                revision = _revision(hosted._read_json(tracking, private=True))
            else:
                listing = client.request("/engine/files")
                revision = _revision(listing)
                if not isinstance(listing.get("files"), list):
                    raise _error("INVALID_RESPONSE", "Server must return a files array before file creation.")
                exists = any(isinstance(item, dict) and item.get("path") == relative for item in listing["files"])
                if exists and not args.overwrite:
                    raise _error("BASELINE_REQUIRED", "Existing remote file has no downloaded baseline. Use file get, edit and file put; or explicitly use --overwrite.")
        _revision({"books_revision": revision})
        body = {"path": relative, "data_base64": base64.b64encode(data).decode("ascii"), "expected_books_revision": revision}
        if args.explanation is not None:
            body["explanation"] = args.explanation
        record, receipt = _new_record(client, config_path, body, [], kind="engine-file-put", key=args.idempotency_key, file_local=local)
        return _finish(client, record, receipt)
    except (hosted.HostedError, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return _report_error(exc, secrets)
