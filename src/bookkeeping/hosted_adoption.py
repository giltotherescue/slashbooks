"""Offline export and explicitly retried, immutable company adoption requests.

The local ZIP is a private recovery artifact, never the network encoding. Only
validated canonical manifest/entity bytes enter the public binary frame; the
server supplies all actor, company, job and command authority.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
from typing import Any, Iterator
from urllib import error, request
import uuid
import zipfile

from . import __version__, hosted, migration_bundle as bundle, remote
from . import hosted_migration as capture

CONTENT_TYPE = "application/vnd.slashbooks.adoption-v1"
ROUTE = "/engine/adopt"
MAX_HEADER = 1024 * 1024
MAX_INTENT = 64 * 1024
CHUNK = 64 * 1024
MAX_MAPPING_ITEMS = 10000


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise hosted.HostedError(code, message)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path, maximum: int, *, private: bool = False) -> bytes:
    with capture._directory(path.parent) as (directory, _):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path.name, flags, dir_fd=directory), "rb") as stream:
            info = os.fstat(stream.fileno())
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                     "INVALID_FILE", "A regular snapshot or receipt file with one hard link is required.")
            if private:
                _require(not info.st_mode & 0o077 and info.st_uid == os.getuid(),
                         "INSECURE_FILE", "Adoption receipts and snapshots must be owner-only; use chmod 600.")
            _require(info.st_size <= maximum, "ADOPTION_LIMIT", "Adoption file exceeds its size limit.")
            raw = stream.read(maximum + 1)
            signature = capture._signature(info)
            _require(len(raw) == info.st_size and signature == capture._signature(os.fstat(stream.fileno()))
                     and signature == capture._signature(os.stat(path.name, dir_fd=directory, follow_symlinks=False)),
                     "INPUT_CHANGED", "Adoption input changed while being captured. Nothing new was submitted.")
    return raw


def _load_snapshot(path: Path, *, private: bool = False) -> tuple[bytes, dict]:
    maximum = capture.MAX_TOTAL + bundle.MAX_MANIFEST + capture.MAX_ITEMS * 4096
    raw = _read_bytes(path, maximum, private=private)
    try:
        loaded = bundle._load_bytes(raw)
    except (bundle.BundleError, capture.Rejected, OSError, ValueError, KeyError, TypeError,
            RecursionError, ArithmeticError, zipfile.BadZipFile, NotImplementedError):
        raise hosted.HostedError("INVALID_BUNDLE", "Snapshot validation failed. Export and validate the source again.") from None
    return raw, loaded


def _frame(loaded: dict, body: dict) -> tuple[bytes, ...]:
    manifest = bundle._json_bytes(loaded["manifest"])
    files = loaded["files"]
    _require(len(manifest) <= bundle.MAX_MANIFEST and len(files) < capture.MAX_ITEMS
             and sum(map(len, files.values())) <= capture.MAX_TOTAL
             and all(len(data) <= capture.MAX_FILE for data in files.values()),
             "ADOPTION_LIMIT", "Snapshot exceeds the adoption transport limits.")
    _require(_sha(manifest) == body["manifest_sha256"], "INVALID_BUNDLE", "Canonical manifest hash does not match adoption intent.")
    job = bundle._json_bytes({"version": 1, "operation": "workspace.adopt", "body": body})
    documents = [("meta/job.json", job), ("input/manifest.json", manifest)]
    documents.extend(("entity/" + name, data) for name, data in sorted(files.items()))
    header = bundle._json_bytes({"version": 1, "files": [
        {"path": name, "size": len(data), "sha256": _sha(data)} for name, data in documents
    ]})
    _require(len(header) <= MAX_HEADER and len(job) <= MAX_INTENT,
             "ADOPTION_LIMIT", "Adoption frame metadata exceeds its size limit.")
    return (struct.pack(">I", len(header)), header, *(data for _, data in documents))


def _chunks(parts: tuple[bytes, ...]) -> Iterator[bytes]:
    for part in parts:
        for offset in range(0, len(part), CHUNK):
            yield part[offset:offset + CHUNK]


def _payload_digest(parts: tuple[bytes, ...]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.hexdigest()


def _reference(config: dict) -> str:
    if "tokenref" in config:
        hosted._token_reference(config["tokenref"])
        return config["tokenref"]
    return "env:BOOKS_API_TOKEN" if "BOOKS_API_TOKEN" in os.environ else "config:token"


def _no_secrets(value: Any, client: hosted.HostedClient) -> None:
    _require(hosted._redact(value, client.secrets) == value,
             "SECRET_IN_INPUT", "Do not include credentials in adoption input, keys or receipt metadata.")


def _read_record(path: Path) -> dict:
    raw = _read_bytes(path, hosted.MAX_RESPONSE_BYTES + MAX_HEADER, private=True)
    record = hosted._json_loads(raw.decode("utf-8"))
    _require(isinstance(record, dict) and record.get("kind") == "engine-adoption"
             and record.get("route") == ROUTE and record.get("method") == "POST"
             and record.get("status") in {"pending", "received"}
             and record.get("record_sha256") == remote._record_digest(record),
             "INVALID_RECEIPT", "Adoption receipt changed. Restore the original immutable receipt.")
    hosted._key(record.get("idempotency_key"))
    body = record.get("body")
    _require(isinstance(body, dict) and set(body) == {
        "expected_books_revision", "source_entity_sha256", "manifest_sha256", "explanation"}
        and type(body.get("expected_books_revision")) is int and body["expected_books_revision"] == 0
        and isinstance(body.get("explanation"), str) and 1 <= len(body["explanation"].strip()) <= 4000
        and len(body["explanation"]) <= 4000 and record.get("body_sha256") == hosted._body_digest(body),
        "INVALID_RECEIPT", "Adoption receipt body is invalid or changed.")
    _require(record.get("snapshot") == path.stem + ".bundle",
             "INVALID_RECEIPT", "Snapshot must be the receipt's adjacent private artifact.")
    return record


def _snapshot_intent(raw: bytes, loaded: dict, body: dict) -> tuple[dict, tuple[bytes, ...]]:
    parts = _frame(loaded, body)
    return {"bundle_sha256": _sha(raw), "bundle_size": len(raw),
            "manifest_sha256": _sha(bundle._json_bytes(loaded["manifest"])),
            "source_entity_sha256": loaded["manifest"]["source_entity"]["entity_json_sha256"],
            "payload_sha256": _payload_digest(parts), "payload_size": sum(map(len, parts))}, parts


def _validate_snapshot(client: hosted.HostedClient, record: dict, path: Path) -> tuple[dict, tuple[bytes, ...]]:
    raw, loaded = _load_snapshot(path.parent / record["snapshot"], private=True)
    intent, parts = _snapshot_intent(raw, loaded, record["body"])
    _require(all(record.get(key) == value for key, value in intent.items())
             and record["body"]["source_entity_sha256"] == intent["source_entity_sha256"],
             "INVALID_RECEIPT", "Frozen snapshot no longer matches the original adoption intent.")
    _no_secrets(record, client)
    _require(not any(secret and secret.encode() in raw for secret in client.secrets),
             "SECRET_IN_INPUT", "Snapshot contains an API credential; nothing was submitted.")
    return loaded, parts


def _new_record(args: argparse.Namespace, config: Path, config_value: dict, client: hosted.HostedClient) -> tuple[dict, Path]:
    explanation = args.explanation
    _require(isinstance(explanation, str) and 1 <= len(explanation.strip()) <= 4000 and len(explanation) <= 4000,
             "INVALID_EXPLANATION", "Provide an explanation of 1 to 4000 characters.")
    key = hosted._key(args.idempotency_key if args.idempotency_key is not None else str(uuid.uuid4()))
    raw, loaded = _load_snapshot(args.bundle.expanduser().absolute())
    manifest = loaded["manifest"]
    body = {"expected_books_revision": 0, "source_entity_sha256": manifest["source_entity"]["entity_json_sha256"],
            "manifest_sha256": _sha(bundle._json_bytes(manifest)), "explanation": explanation}
    intent, _ = _snapshot_intent(raw, loaded, body)
    name = hosted._body_digest({**hosted._identity(client), "key": key})
    path = config.parent / ".slashbooks-remote-receipts" / (name + ".json")
    record = {**hosted._identity(client), **intent, "kind": "engine-adoption", "config_path": str(config),
              "credential_reference": _reference(config_value), "idempotency_key": key,
              "route": ROUTE, "method": "POST", "body": body, "body_sha256": hosted._body_digest(body),
              "snapshot": name + ".bundle", "status": "pending"}
    record["record_sha256"] = remote._record_digest(record)
    _no_secrets(record, client)
    _require(not any(secret and secret.encode() in raw for secret in client.secrets),
             "SECRET_IN_INPUT", "Snapshot contains an API credential; nothing was submitted.")
    remote._no_symlinks(path)
    if path.exists():
        existing = _read_record(path)
        _require(existing["record_sha256"] == record["record_sha256"],
                 "IDEMPOTENCY_CONFLICT", "Existing key has different adoption input. Retry its original receipt.")
        return existing, path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot = path.parent / record["snapshot"]
    try:
        bundle._write_exclusive(snapshot, raw)
    except FileExistsError:
        existing_raw = _read_bytes(snapshot, len(raw), private=True)
        _require(existing_raw == raw, "IDEMPOTENCY_CONFLICT", "Existing key has a different frozen snapshot. Do not overwrite it.")
    try:
        hosted._write_private(path, record, exclusive=True)
    except FileExistsError:
        existing = _read_record(path)
        _require(existing["record_sha256"] == record["record_sha256"],
                 "IDEMPOTENCY_CONFLICT", "Existing key has different adoption input. Retry its original receipt.")
        return existing, path
    return record, path


def _send(client: hosted.HostedClient, record: dict, parts: tuple[bytes, ...]) -> dict:
    headers = {"Authorization": "Bearer " + client.token, "Accept": "application/json",
               "User-Agent": f"slashbooks/{__version__}", "Content-Type": CONTENT_TYPE,
               "Content-Length": str(record["payload_size"]), "Idempotency-Key": record["idempotency_key"]}
    req = request.Request(client.base + ROUTE, data=_chunks(parts), headers=headers, method="POST")
    try:
        with client.opener.open(req, timeout=client.timeout) as response:
            _require(200 <= response.status < 300, "HTTP_ERROR", "Unexpected adoption response; keep the original receipt.")
            raw = response.read(hosted.MAX_RESPONSE_BYTES + 1)
        _require(len(raw) <= hosted.MAX_RESPONSE_BYTES, "INVALID_RESPONSE", "Adoption response exceeds its size limit; outcome is uncertain.")
        result = hosted._json_loads(raw.decode("utf-8"))
        _require(isinstance(result, dict), "INVALID_RESPONSE", "Adoption response must be an object; outcome is uncertain.")
        if "error" in result:
            raise hosted.HostedError("API_ERROR", "Adoption was not confirmed; retain the original receipt.",
                                     server_error=hosted._redact(result["error"], client.secrets))
        return hosted._redact(result, client.secrets)
    except error.HTTPError as exc:
        with exc:
            if 300 <= exc.code < 400:
                raise hosted.HostedError("REDIRECT_BLOCKED", "Redirect refused. Configure the trusted final endpoint explicitly.", status=exc.code) from None
            try:
                raw = exc.read(hosted.MAX_RESPONSE_BYTES + 1)
                server_error = hosted._json_loads(raw.decode("utf-8")) if len(raw) <= hosted.MAX_RESPONSE_BYTES else None
            except (ValueError, OSError, http.client.HTTPException, RecursionError):
                server_error = None
        raise hosted.HostedError("HTTP_ERROR", "Adoption was not confirmed. Retry only the original receipt and key; never change its input.",
                                 status=exc.code, server_error=hosted._redact(server_error, client.secrets)) from None
    except (error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise hosted.HostedError("TRANSPORT_ERROR", "Adoption outcome is uncertain. Retry the saved receipt with the same key. No automatic retry or local fallback was used.") from None
    except (ValueError, RecursionError):
        raise hosted.HostedError("INVALID_RESPONSE", "Adoption response could not be read; outcome is uncertain. Retain the original receipt.") from None


def _validate_response(response: dict, record: dict, loaded: dict) -> None:
    invalid = "Adoption result does not match the saved intent. Retain the original receipt; success is not confirmed."
    manifest = loaded["manifest"]
    command = response.get("command_id")
    _require(isinstance(command, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", command)
             and len(bundle._json_bytes(response)) <= hosted.MAX_RESPONSE_BYTES,
             "INVALID_RESPONSE", invalid)
    try:
        hosted._segment(command)
    except hosted.HostedError:
        raise hosted.HostedError("INVALID_RESPONSE", invalid) from None
    _require(type(response.get("books_revision")) is int and response["books_revision"] == 1
             and response.get("state_committed") is True
             and response.get("source_entity_sha256") == record["source_entity_sha256"]
             and response.get("manifest_sha256") == record["manifest_sha256"]
             and bundle._json_bytes(response.get("validation_anchors")) ==
             bundle._json_bytes({key: manifest[key] for key in ("ledger", "reports")}),
             "INVALID_RESPONSE", invalid)
    mapping = response.get("id_mapping")
    _require(isinstance(mapping, dict) and set(mapping) == {"entries", "proposals", "sources"}
             and all(isinstance(rows, list) for rows in mapping.values())
             and sum(map(len, mapping.values())) <= MAX_MAPPING_ITEMS
             and len(mapping["entries"]) == manifest["ledger"]["tables"]["entries"]["rows"],
             "INVALID_RESPONSE", invalid)
    for collection, rows in mapping.items():
        sources, targets = set(), set()
        for row in rows:
            _require(isinstance(row, dict) and all(isinstance(row.get(key), str) and 1 <= len(row[key]) <= 1024
                     for key in ("source_id", "projected_id")), "INVALID_RESPONSE", invalid)
            _require(row["source_id"] not in sources and row["projected_id"] not in targets,
                     "INVALID_RESPONSE", invalid)
            if collection == "entries":
                _require(type(row.get("sqlite_entry_id")) is int and row["sqlite_entry_id"] > 0,
                         "INVALID_RESPONSE", invalid)
            sources.add(row["source_id"])
            targets.add(row["projected_id"])
    expected_documents = {item["path"]: item for item in manifest["files"] if capture.is_document_path(item["path"])}
    documents = response.get("documents")
    _require(isinstance(documents, list) and len(documents) == len(expected_documents), "INVALID_RESPONSE", invalid)
    seen, ids = set(), set()
    for document in documents:
        _require(isinstance(document, dict) and set(document) == {"id", "path", "size", "sha256"}
                 and isinstance(document.get("id"), str)
                 and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", document["id"])
                 and document["id"] not in ids and isinstance(document.get("path"), str)
                 and document["path"] in expected_documents and document["path"] not in seen
                 and type(document.get("size")) is int, "INVALID_RESPONSE", invalid)
        # Public receipts register evidence IDs without changing source descriptors.
        _require({key: document.get(key) for key in ("path", "size", "sha256")} == expected_documents[document["path"]],
                 "INVALID_RESPONSE", invalid)
        seen.add(document["path"])
        ids.add(document["id"])
    _require(bundle._json_bytes(response.get("validation_summary")) == bundle._json_bytes(bundle._summary(manifest)),
             "INVALID_RESPONSE", invalid)


def _finish(client: hosted.HostedClient, record: dict, path: Path) -> dict:
    recovery = {"idempotency_key": record["idempotency_key"], "local_receipt": str(path)}
    try:
        loaded, parts = _validate_snapshot(client, record, path)
        if record["status"] == "received":
            response = record.get("response")
            _require(isinstance(response, dict) and record.get("response_sha256") == hosted._body_digest(response),
                     "INVALID_RECEIPT", "Saved adoption response failed integrity validation.")
        else:
            print(json.dumps(hosted._redact({"event": "hosted_submission", **recovery}, client.secrets)), file=sys.stderr, flush=True)
            response = _send(client, record, parts)
        _validate_response(response, record, loaded)
        if record["status"] != "received":
            hosted._write_private(path, {**record, "status": "received", "response": response,
                                         "response_sha256": hosted._body_digest(response)})
        return {**recovery, "response": response}
    except hosted.HostedError as exc:
        exc.payload["error"].update(recovery)
        raise
    except OSError:
        raise hosted.HostedError("LOCAL_RECOVERY_REQUIRED", "Cannot read the frozen snapshot or persist the response. Keep the original receipt and key.", **recovery) from None


def _run(args: argparse.Namespace, *, retrying: bool = False) -> int:
    secrets: Any = None
    try:
        if not retrying and args.migration_command == "export":
            result = bundle.build_bundle(args.entity_dir.expanduser(), args.output.expanduser())
            print(json.dumps(result, sort_keys=True, allow_nan=False))
            return 0 if result.get("bundle_created") is True else 1
        config_path = args.config.expanduser().absolute()
        if retrying:
            path = args.receipt.expanduser().absolute()
            record = _read_record(path)
            if args.config == Path.home() / ".config/slashbooks/hosted.json":
                config_path = Path(record["config_path"])
        config = hosted._read_json(config_path, private=True)
        client = hosted.HostedClient(config, timeout=args.timeout)
        secrets = client.secrets
        if retrying:
            _require(all(record.get(key) == value for key, value in hosted._identity(client).items())
                     and record.get("credential_reference") == _reference(config),
                     "RECEIPT_SCOPE_MISMATCH", "Receipt belongs to another endpoint, company, credential or token reference.")
        else:
            record, path = _new_record(args, config_path, config, client)
        result = _finish(client, record, path)
        print(json.dumps(hosted._redact(result, secrets), sort_keys=True, allow_nan=False))
        return 0
    except (hosted.HostedError, capture.Rejected, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        payload = exc.payload if isinstance(exc, hosted.HostedError) else {
            "error": {"code": "ADOPTION_LOCAL_ERROR", "message": "Cannot prepare adoption or recover its private receipt. No local fallback was used."}}
        print(json.dumps(hosted._redact(payload, secrets)), file=sys.stderr)
        return 1


def run(args: argparse.Namespace) -> int:
    return _run(args)


def retry(args: argparse.Namespace) -> int:
    return _run(args, retrying=True)
