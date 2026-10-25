"""Company-scoped hosted API client. Never reads or writes a local ledger."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any
from urllib import error, parse, request
import uuid

from . import __version__


OPERATIONS = {
    "run-start": "run.start",
    "run-checkpoint": "run.checkpoint",
    "run-finish": "run.finish",
    "propose": "queue.propose",
    "confirm": "queue.confirm",
    "withdraw": "queue.withdraw",
}
READS = {"status": "", **{name: "/" + name for name in (
    "context", "integrations", "sources", "proposals", "entries", "reports", "history", "runs", "questions", "evidence",
)}, "held-mail": "/agent/mail/held-summary"}
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024 * 1024
COLLECTIONS = {name: name for name in ("sources", "proposals", "entries", "runs", "questions", "evidence")}
COLLECTIONS["history"] = "events"
STATUS_READS = {"sources", "proposals", "runs", "questions"}
MAX_PAGE_LIMIT = 200
MAX_PAGES = 100
MAX_COLLECTION_BYTES = 64 * 1024 * 1024
DEFAULT_TIMEOUT = 180.0
TIMEOUT_ENV = "SLASHBOOKS_REMOTE_TIMEOUT_SECONDS"


class HostedError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.payload = {"error": {"code": code, "message": message, **details}}


# The official Slashbooks Cloud service. Self-hosted servers pass --endpoint.
CLOUD_ENDPOINT = "https://slashbooks.co"


def add_parser(subparsers: Any) -> None:
    # "hosted" stays an alias so existing connections, prompts and scripts keep working.
    parser = subparsers.add_parser("cloud", aliases=["hosted"], help="Connect to and use Slashbooks Cloud books")
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/slashbooks/hosted.json")
    parser.add_argument("--timeout", type=float, help=f"Request timeout in seconds (default {DEFAULT_TIMEOUT:g}, or {TIMEOUT_ENV}); no automatic retries")
    commands = parser.add_subparsers(dest="hosted_command", required=True)
    from . import hosted_questions
    hosted_questions.add_parser(commands)
    from . import hosted_stripe
    hosted_stripe.add_parser(commands)
    login = commands.add_parser("login", help="Connect online books with browser approval; no API key needed")
    login.add_argument("--endpoint", default=CLOUD_ENDPOINT, help=f"Slashbooks Cloud server (default {CLOUD_ENDPOINT})")
    login.add_argument("--company", help="Company ID; omit it to choose the company in the browser")
    login.add_argument("--entity", type=Path, help="Folder for the connection; default ~/Documents/Slashbooks/online-<id hash>")
    login.add_argument("--allow-localhost", action="store_true")
    login.add_argument("--reauthorize", action="store_true", help="Explicitly reconnect the same company after access expires or is revoked")
    login.add_argument("--scope", nargs="+", choices=["books:read", "books:write", "sources:write", "proposals:write",
                       "runs:write", "questions:write", "evidence:write", "adjustments:write"],
                       help="Explicit permissions to request with browser consent; include books:read. Adjustments are never granted by default")
    configure = commands.add_parser("configure", help="Save endpoint/company, without contacting the server")
    configure.add_argument("--endpoint", required=True, help="HTTPS origin, optionally ending in /api/v1")
    configure.add_argument("--company", required=True)
    configure.add_argument("--entity", type=Path, help="Bind this company directory for ordinary books commands")
    configure.add_argument("--tokenref", help="Credential environment reference, for example env:BOOKS_API_TOKEN")
    configure.add_argument("--allow-localhost", action="store_true", help="Explicitly permit HTTP on localhost, 127.0.0.1 or ::1")
    configure.add_argument("--store-token", action="store_true", help="Store BOOKS_API_TOKEN in owner-only config; otherwise use the environment")
    for name in READS:
        sub = commands.add_parser(name)
        if name in COLLECTIONS:
            sub.add_argument("--cursor", help="Opaque next_cursor from the previous page; never a URL to follow")
            sub.add_argument("--limit", type=int, help="Page size, 1-200")
            sub.add_argument("--all", dest="all_pages", action="store_true", help="Collect remaining pages; fail if completion cannot be verified")
            sub.add_argument("--max-pages", type=int, help="Maximum pages with --all, 1-100 (default 100)")
            if name in STATUS_READS:
                sub.add_argument("--status", dest="filter_status", help="Server-defined status filter; preserved on every page")
        if name == "reports":
            sub.add_argument("--from", dest="from_date")
            sub.add_argument("--to", dest="to_date")
    result = commands.add_parser("command-result", help="Fetch a server command receipt")
    result.add_argument("command_id")
    for name in ("command", *OPERATIONS, "import-source"):
        sub = commands.add_parser(name, help=(
            "Submit source JSON (deduplicated by source_id on server)" if name == "import-source" else
            "Submit command envelope JSON; confirm still requires server-authorized human staff"
        ))
        sub.add_argument("--file", type=Path, required=True, help="JSON object; commands use {payload, explanation?, expected_books_revision?, run_id?, operation?}")
        sub.add_argument("--idempotency-key", help="Reuse the SAME key and input after an uncertain outcome; otherwise generated and saved")
    evidence = commands.add_parser("evidence-upload", help=(
        "Upload one document (PDF, PNG, JPEG, plain text or CSV, up to 10 MB) as company evidence"))
    evidence.add_argument("--file", type=Path, required=True)
    retry = commands.add_parser("retry", help="Resubmit an immutable local receipt with its original key")
    retry.add_argument("--receipt", type=Path, required=True)
    migration = commands.add_parser("migration", help="Export or adopt a verified company snapshot")
    migrations = migration.add_subparsers(dest="migration_command", required=True)
    export = migrations.add_parser("export", help="Create an offline private migration bundle")
    export.add_argument("--entity-dir", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    adopt = migrations.add_parser("import", help="Adopt a snapshot into an empty hosted company")
    adopt.add_argument("--bundle", type=Path, required=True)
    adopt.add_argument("--explanation", required=True)
    adopt.add_argument("--idempotency-key")
    files = commands.add_parser("file", help="Read or explicitly update a shared company file")
    file_commands = files.add_subparsers(dest="file_command", required=True)
    get = file_commands.add_parser("get")
    put = file_commands.add_parser("put")
    listing = file_commands.add_parser("list")
    listing.add_argument("--entity", type=Path)
    for sub in (get, put):
        sub.add_argument("path", help="Company-relative file path")
        sub.add_argument("--entity", type=Path)
    get.add_argument("--output", type=Path, required=True, help="Download to this local file")
    put.add_argument("--file", type=Path, required=True)
    put.add_argument("--expected-books-revision", type=int, help="Override the revision recorded by file get")
    put.add_argument("--explanation")
    put.add_argument("--idempotency-key")
    put.add_argument("--overwrite", action="store_true", help="Explicitly replace an existing remote file without a downloaded baseline")


EVIDENCE_MAX_BYTES = 10 * 1024 * 1024
EVIDENCE_TYPES = {
    "pdf": ("application/pdf", b"%PDF-"),
    "png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    "jpg": ("image/jpeg", b"\xff\xd8\xff"),
    "jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    "txt": ("text/plain", b""),
    "csv": ("text/csv", b""),
}


def _evidence_upload(args: argparse.Namespace, client: "HostedClient") -> dict[str, Any]:
    """Upload one local file as company evidence, after the same checks the service makes."""
    path = args.file.expanduser()
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in EVIDENCE_TYPES:
        raise HostedError("INVALID_EVIDENCE", "Use a PDF, PNG, JPEG, plain text or CSV file.")
    mime, magic = EVIDENCE_TYPES[suffix]
    try:
        with path.open("rb") as handle:
            data = handle.read(EVIDENCE_MAX_BYTES + 1)
    except OSError:
        raise HostedError("INVALID_EVIDENCE", "Cannot read the file. Nothing was uploaded.") from None
    if not data or len(data) > EVIDENCE_MAX_BYTES:
        raise HostedError("INVALID_EVIDENCE", "The file must hold 1 byte to 10 MB. Nothing was uploaded.")
    if not data.startswith(magic):
        raise HostedError("INVALID_EVIDENCE", "The file content does not match its extension. Nothing was uploaded.")
    if not magic:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise HostedError("INVALID_EVIDENCE", "Text and CSV files must be UTF-8. Nothing was uploaded.") from None
    name = "".join(ch for ch in path.name if ch.isprintable() and ch not in '"\\')[:180] or f"evidence.{suffix}"
    boundary = "slashbooks-" + uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
    body = head + data + f"\r\n--{boundary}--\r\n".encode("ascii")
    result = client.request("/evidence", upload=(body, f"multipart/form-data; boundary={boundary}"))
    digest = hashlib.sha256(data).hexdigest()
    if result.get("sha256") != digest:
        raise HostedError("EVIDENCE_MISMATCH", "The service stored different bytes than the file. Do not rely on this upload.",
                          local_sha256=digest, evidence_id=result.get("id"))
    return result


def _json_loads(raw: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError("Non-finite JSON number")

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite JSON number")
        return number

    return json.loads(raw, parse_constant=reject_constant, parse_float=finite_float)


def _read_json(path: Path, *, private: bool = False) -> dict[str, Any]:
    from . import file_safety
    try:
        file_safety.reject_links(path)
        with os.fdopen(file_safety.open_read(path), encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise HostedError("INVALID_FILE", "Expected a regular JSON file.")
            value = _json_loads(handle.read())
            if not isinstance(value, dict):
                raise ValueError("Expected object")
            if private or "token" in value:
                if not file_safety.is_private(handle.fileno()):
                    raise HostedError("INSECURE_FILE", "Config with a token and local receipts must be accessible only to the current user.")
            return value
    except (OSError, ValueError, RecursionError) as exc:
        raise HostedError("INVALID_FILE", "Cannot read JSON file. Check its path, permissions and JSON object syntax.") from exc


def _write_private(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    """Publish only complete, fsynced, owner-only JSON; exclusive creation fences key reuse."""
    from . import file_safety
    file_safety.reject_links(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = file_safety.private_temp(path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        file_safety.replace(temporary, path, exclusive=exclusive)
        file_safety.sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _endpoint(value: Any, allow_localhost: bool) -> str:
    message = "Use an HTTPS origin (optional /api/v1). HTTP requires --allow-localhost and a loopback host."
    if not isinstance(value, str) or not value or re.search(r"[\s\\]", value):
        raise HostedError("INVALID_ENDPOINT", message)
    try:
        parts = parse.urlsplit(value)
        port = parts.port
        if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
            raise ValueError()
        if not parts.hostname or parts.path.rstrip("/") not in ("", "/api/v1"):
            raise ValueError()
        if port is not None and port == 0:
            raise ValueError()
        if parts.scheme != "https" and not (
            allow_localhost and parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError()
        return parse.urlunsplit((parts.scheme, parts.netloc, "/api/v1", "", ""))
    except ValueError as exc:
        raise HostedError("INVALID_ENDPOINT", message) from exc


def _segment(value: Any) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or re.search(r"[\x00-\x20\x7f/\\%]", value):
        raise HostedError("INVALID_ID", "Company and command IDs must be nonempty single path segments.")
    return parse.quote(value, safe="")


def _token(value: Any) -> str:
    if not isinstance(value, str) or not value or not re.fullmatch(r"[\x21-\x7e]+", value):
        raise HostedError("AUTH_REQUIRED", "Set BOOKS_API_TOKEN to a valid company API token, or configure --store-token.")
    return value


def _token_reference(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", value):
        raise HostedError("INVALID_TOKENREF", "Token reference must be env:ENVIRONMENT_VARIABLE.")
    return value[4:]


def _key(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", value):
        raise HostedError("INVALID_KEY", "Idempotency keys must be 1-200 ASCII letters, digits, dots, underscores, colons or hyphens.")
    return value


def _redact(value: Any, token: Any) -> Any:
    if isinstance(value, str):
        tokens = token if isinstance(token, (list, tuple)) else [token]
        for secret in tokens:
            if isinstance(secret, str) and secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_redact(item, token) for item in value]
    if isinstance(value, dict):
        return {
            str(_redact(key, token)): "[REDACTED]" if key.lower().replace("_", "").replace("-", "") in {
                "token", "authorization", "apitoken", "apikey", "password", "accesstoken",
                "refreshtoken", "clientsecret", "secret", "cookie", "setcookie", "proxyauthorization",
            }
            else _redact(item, token) for key, item in value.items()
        }
    return value


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class HostedClient:
    def __init__(self, config: dict[str, Any], *, timeout: float | None = None) -> None:
        if timeout is None:
            try:
                timeout = float(os.environ.get(TIMEOUT_ENV, DEFAULT_TIMEOUT))
            except ValueError:
                raise HostedError("INVALID_TIMEOUT", f"{TIMEOUT_ENV} must be a finite number greater than zero.") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise HostedError("INVALID_TIMEOUT", "Timeout must be a finite number greater than zero.")
        self.endpoint = _endpoint(config.get("endpoint"), config.get("allow_localhost") is True)
        self.company = config.get("company")
        self.base = self.endpoint + "/companies/" + _segment(self.company)
        if "credential_file" in config:
            from .agent_auth import credential_token
            self.token, refresh = credential_token(config, timeout)
            self.secrets = [self.token, refresh]
        else:
            self.token = _token(os.environ.get(_token_reference(config["tokenref"]))) if "tokenref" in config else _token(os.environ.get("BOOKS_API_TOKEN", config.get("token")))
            self.secrets = [self.token, config.get("token")]
        self.timeout = timeout
        # No environment proxy or redirect can forward the bearer token elsewhere.
        self.opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())

    def request(self, suffix: str, *, body: dict[str, Any] | None = None, key: str | None = None,
                method: str | None = None, preserve_artifacts: bool = False,
                upload: tuple[bytes, str] | None = None) -> Any:
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json",
                   "User-Agent": f"slashbooks/{__version__}"}
        data = None
        if upload is not None:
            data, headers["Content-Type"] = upload
            method = method or "POST"
        elif body is not None:
            data = json.dumps(body, sort_keys=True, allow_nan=False).encode("utf-8")
            if len(data) > MAX_REQUEST_BYTES:
                raise HostedError("REQUEST_TOO_LARGE", "Encoded JSON request exceeds 16 MiB. Reduce the inputs; nothing was submitted.")
            headers["Content-Type"] = "application/json"
            headers["Idempotency-Key"] = _key(key)
        req = request.Request(self.base + suffix, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise HostedError("HTTP_ERROR", "Unexpected HTTP response.", status=response.status)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise HostedError("INVALID_RESPONSE", "Response exceeded the size limit; outcome is uncertain. Retry commands with the same key.")
            result = _json_loads(raw.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("Expected object")
            if "error" in result:
                raise HostedError("API_ERROR", "Server returned an error; inspect server_error.", server_error=_redact(result["error"], self.secrets))
            if "next_cursor" in result and _redact(result["next_cursor"], self.secrets) != result["next_cursor"]:
                raise HostedError("INVALID_CURSOR", "Server cursor contains credential data; refusing to expose or forward it.")
            redacted = _redact(result, self.secrets)
            if preserve_artifacts:
                # Encoded file bytes are opaque; redaction would corrupt their checksums.
                if "data_base64" in result:
                    redacted["data_base64"] = result["data_base64"]
                if isinstance(result.get("artifacts"), list):
                    for original, cleaned in zip(result["artifacts"], redacted["artifacts"]):
                        if isinstance(original, dict) and "data_base64" in original:
                            cleaned["data_base64"] = original["data_base64"]
            return redacted
        except error.HTTPError as exc:
            with exc:
                if 300 <= exc.code < 400:
                    raise HostedError("REDIRECT_BLOCKED", "Redirect refused. Configure the final trusted HTTPS endpoint explicitly.", status=exc.code) from exc
                try:
                    server_error = _json_loads(exc.read(MAX_RESPONSE_BYTES).decode("utf-8"))
                except (ValueError, OSError, http.client.HTTPException, RecursionError):
                    server_error = None
            guidance = {
                401: "Token is missing, invalid or revoked; check BOOKS_API_TOKEN.",
                403: "Server denied permission. Check company access, credential scopes, and the specific server_error before choosing the next action.",
                404: "Resource not found or not accessible in the configured company.",
                409: "Conflict. Refresh and review shared company state before making a new decision. Do not rebase or change input under an existing idempotency key.",
                422: "Request rejected. Check the JSON fields against the hosted command contract.",
            }.get(exc.code, "Hosted API failed. Retry an uncertain command only with its original key and input.")
            raise HostedError("HTTP_ERROR", guidance, status=exc.code, server_error=_redact(server_error, self.secrets)) from exc
        except (error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise HostedError("TRANSPORT_ERROR", "Hosted request failed or timed out. Command outcome is uncertain; retry the saved receipt with the same key. No local fallback was used.") from exc
        except (ValueError, RecursionError) as exc:
            raise HostedError("INVALID_RESPONSE", "Server did not return a JSON object. Command outcome is uncertain; retry with the same key.") from exc


def _cursor(value: Any, client: HostedClient) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise HostedError("INVALID_CURSOR", "Cursor must be a nonempty opaque string of at most 8192 characters.")
    if _redact(value, client.secrets) != value:
        raise HostedError("INVALID_CURSOR", "Never put API credentials in a pagination cursor.")
    return value


def _read_collection(args: argparse.Namespace, client: HostedClient) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if args.limit is not None:
        if not 1 <= args.limit <= MAX_PAGE_LIMIT:
            raise HostedError("INVALID_LIMIT", "Page limit must be between 1 and 200.")
        query["limit"] = args.limit
    if args.max_pages is not None and (not args.all_pages or not 1 <= args.max_pages <= MAX_PAGES):
        raise HostedError("INVALID_PAGE_LIMIT", "--max-pages requires --all and a value between 1 and 100.")
    if args.cursor is not None:
        query["cursor"] = _cursor(args.cursor, client)
    status = getattr(args, "filter_status", None)
    if status is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", status) or _redact(status, client.secrets) != status:
            raise HostedError("INVALID_STATUS", "Status must be a server-defined name of 1-64 letters, digits, underscores or hyphens, never credentials.")
        query["status"] = status
    if args.all_pages:
        query.setdefault("limit", MAX_PAGE_LIMIT)
    collection = COLLECTIONS[args.hosted_command]
    rows: list[Any] = []
    size = 0
    seen = {args.cursor} if args.cursor is not None else set()
    for page_number in range(1, (args.max_pages or MAX_PAGES) + 1):
        suffix = READS[args.hosted_command]
        if query:
            suffix += "?" + parse.urlencode(query)
        page = client.request(suffix)
        if not args.all_pages:
            if page.get("next_cursor") is not None:
                _cursor(page["next_cursor"], client)
            return page
        if not isinstance(page.get(collection), list) or "next_cursor" not in page:
            raise HostedError("INVALID_PAGE", "--all requires a collection array and explicit next_cursor on every page. No partial collection was returned.")
        size += len(json.dumps(page, ensure_ascii=True).encode("utf-8"))
        if size > MAX_COLLECTION_BYTES:
            raise HostedError("PAGINATION_SIZE_LIMIT", "Collected pages exceeded 64 MiB. Read and process individual pages instead; no partial collection was returned.")
        rows.extend(page[collection])
        cursor = page["next_cursor"]
        if cursor is None:
            return {collection: rows, "next_cursor": None, "pages_read": page_number}
        cursor = _cursor(cursor, client)
        if cursor in seen:
            raise HostedError("PAGINATION_LOOP", "Server repeated a pagination cursor. Stop and investigate; no partial collection was returned.", pages_read=page_number)
        seen.add(cursor)
        query["cursor"] = cursor
    raise HostedError("PAGINATION_LIMIT", "Page limit reached before completion. Read and process individual pages from your original cursor; no partial collection was returned.", pages_read=page_number)


def _command_body(name: str, body: dict[str, Any]) -> dict[str, Any]:
    body = dict(body)
    if name in OPERATIONS:
        operation = OPERATIONS[name]
        if body.get("operation", operation) != operation:
            raise HostedError("INVALID_COMMAND", "File operation does not match the selected subcommand.")
        body["operation"] = operation
    if body.get("operation") not in OPERATIONS.values() or not isinstance(body.get("payload"), dict):
        raise HostedError("INVALID_COMMAND", "Command requires a supported operation and a payload object.")
    if set(body) - {"operation", "payload", "explanation", "run_id", "expected_books_revision"}:
        raise HostedError("INVALID_COMMAND", "Unknown command envelope fields; identity and authority come from the server.")
    if body["operation"].startswith("queue."):
        revision = body.get("expected_books_revision")
        if type(revision) is not int or revision < 0:
            raise HostedError("INVALID_COMMAND", "Financial commands require a nonnegative integer expected_books_revision; read hosted status first.")
    if body["operation"] == "queue.propose":
        explanation = body.get("explanation")
        if not isinstance(explanation, dict) or not isinstance(explanation.get("summary"), str) or not explanation["summary"].strip():
            raise HostedError("INVALID_COMMAND", "Proposals require explanation.summary; include evidence_ids and policy_versions when available.")
    return body


def _stable_question_id(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 200
            or any(ord(character) < 33 or ord(character) == 127 for character in value)):
        raise HostedError(
            "INVALID_QUESTION", f"{label} must be a nonempty stable identifier of at most 200 characters."
        )
    return value


def _stable_reply_id(value: Any) -> str:
    return _stable_question_id(value, "Reply ID")


def _question_body(route: str, body: dict[str, Any]) -> dict[str, Any]:
    """Validate the immutable public question request used for submit and retry."""
    if route == "/questions":
        if set(body) != {"subject", "body", "source_ids"}:
            raise HostedError(
                "INVALID_QUESTION", "Question create requires subject, body and source_ids."
            )
        if (not isinstance(body["subject"], str) or not body["subject"].strip()
                or len(body["subject"]) > 200):
            raise HostedError("INVALID_QUESTION", "Question subject must be 1-200 characters.")
        if (not isinstance(body["body"], str) or not body["body"].strip()
                or len(body["body"]) > 10000):
            raise HostedError("INVALID_QUESTION", "Question body must be 1-10000 characters.")
        source_ids = body.get("source_ids")
        if (not isinstance(source_ids, list) or len(source_ids) > 100
                or len(set(_stable_question_id(value, "Source ID") for value in source_ids))
                != len(source_ids)):
            raise HostedError(
                "INVALID_QUESTION", "Question create allows at most 100 unique stable source IDs."
            )
        return {**body, "source_ids": sorted(source_ids)}
    match = re.fullmatch(r"/questions/([^/]+)/resolve", route) if isinstance(route, str) else None
    if match is None:
        raise HostedError("INVALID_RECEIPT", "Receipt does not contain a supported question route.")
    encoded_id = match.group(1)
    try:
        if _segment(parse.unquote(encoded_id)) != encoded_id:
            raise ValueError
    except (HostedError, ValueError) as exc:
        raise HostedError("INVALID_RECEIPT", "Receipt question route is invalid.") from exc
    # The server needs a command_id only for a question that declares source IDs.
    if set(body) not in ({"reply_ids", "reply_watermark"}, {"reply_ids", "reply_watermark", "command_id"}):
        raise HostedError(
            "INVALID_QUESTION",
            "Question resolve requires reply_ids and reply_watermark, and command_id when the question declares sources.",
        )
    reply_ids = body.get("reply_ids")
    if (not isinstance(reply_ids, list) or not 1 <= len(reply_ids) <= 100
            or len(set(_stable_reply_id(value) for value in reply_ids)) != len(reply_ids)):
        raise HostedError("INVALID_QUESTION", "Question resolve requires 1-100 unique stable reply IDs.")
    if type(body.get("reply_watermark")) is not int or body["reply_watermark"] <= 0:
        raise HostedError("INVALID_QUESTION", "Reply watermark must be a positive integer.")
    if "command_id" in body:
        _segment(body["command_id"])
    return dict(body)


def _held_mail(client: HostedClient, summary: dict[str, Any]) -> dict[str, Any]:
    """Describe held email for a person: a count, the age of the oldest, and a link.

    The service returns no sender, subject or content for held mail, and the CLI
    has no command that reads or releases it. A person reviews it in the Inbox.
    """
    count, oldest, path = summary.get("held_count"), summary.get("oldest_held_at"), summary.get("review_path")
    if (type(count) is not int or count < 0 or not isinstance(path, str)
            or not re.fullmatch(r"/(?!/)[^\s\\\x00-\x1f\x7f]{0,500}", path)
            or not (oldest is None or isinstance(oldest, str)) or (count > 0 and not oldest)):
        raise HostedError("INVALID_RESPONSE", "The held-mail summary is incomplete. Do not guess the count.")
    parts = parse.urlsplit(client.endpoint)
    url = f"{parts.scheme}://{parts.netloc}{path}"
    if count == 0:
        message = "No emails are waiting for your review in Inbox."
    else:
        day = re.match(r"\d{4}-\d{2}-\d{2}", oldest)
        arrived = f" The oldest arrived on {day.group(0)}." if day else ""
        subject = "1 email is" if count == 1 else f"{count} emails are"
        message = f"{subject} waiting for your review in Inbox.{arrived}"
    return {"held_count": count, "oldest_held_at": oldest, "review_url": url, "message": message,
            "note": "Held email needs a person's review. Release and read it in Inbox only."}


def _identity(client: HostedClient) -> dict[str, Any]:
    return {"endpoint": client.endpoint, "company": client.company,
            "credential_sha256": hashlib.sha256(client.token.encode()).hexdigest()}


def _body_digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _submit(client: HostedClient, record: dict[str, Any], path: Path) -> dict[str, Any]:
    retry = {"idempotency_key": record["idempotency_key"], "local_receipt": str(path)}
    try:
        response = client.request(record["route"], body=record["body"], key=record["idempotency_key"])
        completed = {**record, "status": "received", "response": response}
        _write_private(path, completed)
        return {**retry, "response": response}
    except HostedError as exc:
        exc.payload["error"].update(retry)
        raise
    except OSError as exc:
        raise HostedError("RECEIPT_WRITE_FAILED", "Response received but could not save it. Retry the original receipt, never a new key.", **retry) from exc


def _mutate(args: argparse.Namespace, client: HostedClient) -> dict[str, Any]:
    if args.hosted_command == "retry":
        path = args.receipt.expanduser().absolute()
        record = _read_json(path, private=True)
        if any(record.get(key) != value for key, value in _identity(client).items()):
            raise HostedError("RECEIPT_SCOPE_MISMATCH", "Receipt belongs to a different endpoint, company or credential. Restore its original configuration before retrying.")
        _key(record.get("idempotency_key"))
        if not isinstance(record.get("body"), dict):
            raise HostedError("INVALID_RECEIPT", "Receipt must contain a supported company route and JSON body.")
        if record.get("body_sha256") != _body_digest(record["body"]):
            raise HostedError("INVALID_RECEIPT", "Receipt input changed. Restore the original receipt; never edit input under an existing key.")
        if record["route"] == "/commands":
            _command_body("command", record["body"])
        elif record["route"] == "/sources":
            if not isinstance(record["body"].get("source_id"), str) or not record["body"]["source_id"].strip():
                raise HostedError("INVALID_RECEIPT", "Source receipt requires a stable source_id.")
        else:
            _question_body(record.get("route"), record["body"])
    else:
        body = _read_json(args.file.expanduser())
        route = "/sources" if args.hosted_command == "import-source" else "/commands"
        if route == "/commands":
            body = _command_body(args.hosted_command, body)
        elif not isinstance(body.get("source_id"), str) or not body["source_id"].strip():
            raise HostedError("INVALID_SOURCE", "Source imports require a stable source_id for server deduplication.")
        if _redact(body, client.secrets) != body:
            raise HostedError("SECRET_IN_INPUT", "Input contains a credential or secret field. Remove it before submission.")
        key = _key(args.idempotency_key if args.idempotency_key is not None else str(uuid.uuid4()))
        if _redact(key, client.secrets) != key:
            raise HostedError("SECRET_IN_INPUT", "Do not use the API token as an idempotency key.")
        record = {**_identity(client), "idempotency_key": key, "route": route, "body": body,
                  "body_sha256": _body_digest(body), "status": "pending"}
        name = hashlib.sha256(json.dumps([record[k] for k in ("endpoint", "company", "credential_sha256", "idempotency_key")]).encode()).hexdigest()
        path = args.config.expanduser().absolute().parent / "hosted-receipts" / (name + ".json")
        try:
            _write_private(path, record, exclusive=True)
        except FileExistsError:
            existing = _read_json(path, private=True)
            if any(existing.get(k) != v for k, v in record.items() if k != "status"):
                raise HostedError("IDEMPOTENCY_CONFLICT", "Local receipt already uses this key with different input. Restore the original input for retry.")
    if _redact(record["body"], client.secrets) != record["body"]:
        raise HostedError("SECRET_IN_INPUT", "Receipt input contains a credential or secret field.")
    # Publish recovery coordinates before sending any bytes to the API.
    print(json.dumps(_redact({"event": "hosted_submission", "idempotency_key": record["idempotency_key"], "local_receipt": str(path)}, client.secrets)), file=sys.stderr, flush=True)
    return _submit(client, record, path)


def _mutate_question(args: argparse.Namespace, client: HostedClient) -> dict[str, Any]:
    if args.question_command == "create":
        route = "/questions"
        body = _question_body(route, {
            "subject": args.subject, "body": args.body, "source_ids": list(args.source_ids),
        })
    else:
        route = "/questions/" + _segment(args.question_id) + "/resolve"
        resolve = {"reply_ids": list(args.reply_ids or []), "reply_watermark": args.reply_watermark}
        if args.command_id is not None:
            resolve["command_id"] = args.command_id
        body = _question_body(route, resolve)
    if _redact(body, client.secrets) != body:
        raise HostedError("SECRET_IN_INPUT", "Question input contains an API credential. Remove it before submission.")
    key = _key(args.idempotency_key if args.idempotency_key is not None else str(uuid.uuid4()))
    if _redact(key, client.secrets) != key:
        raise HostedError("SECRET_IN_INPUT", "Do not use the API token as an idempotency key.")
    record = {
        **_identity(client),
        "kind": "question-" + args.question_command,
        "idempotency_key": key,
        "route": route,
        "body": body,
        "body_sha256": _body_digest(body),
        "status": "pending",
    }
    name = hashlib.sha256(json.dumps(
        [record[field] for field in ("endpoint", "company", "credential_sha256", "idempotency_key")]
    ).encode()).hexdigest()
    path = args.config.expanduser().absolute().parent / "hosted-receipts" / (name + ".json")
    try:
        _write_private(path, record, exclusive=True)
    except FileExistsError:
        existing = _read_json(path, private=True)
        if any(existing.get(field) != value for field, value in record.items() if field != "status"):
            raise HostedError(
                "IDEMPOTENCY_CONFLICT",
                "Local receipt already uses this key with different input. Restore the original input for retry.",
            )
        record = existing
    print(json.dumps(_redact({
        "event": "hosted_submission",
        "idempotency_key": record["idempotency_key"],
        "local_receipt": str(path),
    }, client.secrets)), file=sys.stderr, flush=True)
    return _submit(client, record, path)


def run(args: argparse.Namespace) -> int:
    if args.hosted_command == "login":
        from . import agent_auth
        try:
            return agent_auth.login(args)
        except HostedError as exc:
            print(json.dumps(exc.payload), file=sys.stderr)
            return 1
        except OSError:
            failure = HostedError("AUTH_STORAGE_FAILED",
                                  "Sign-in could not finish saving local files. Check folder access and free disk space. "
                                  "Browser approval may already exist; a working connection has not been confirmed.")
            print(json.dumps(failure.payload), file=sys.stderr)
            return 1
    if args.hosted_command == "migration":
        from . import hosted_adoption

        return hosted_adoption.run(args)
    token = os.environ.get("BOOKS_API_TOKEN", "")
    try:
        from bookkeeping import remote

        if args.hosted_command == "stripe":
            from . import hosted_stripe
            return hosted_stripe.run(args)
        if args.hosted_command == "question":
            scoped = args.question_command == "create" or (
                args.question_command == "resolve" and any(
                    getattr(args, field, None) is not None
                    for field in ("reply_ids", "reply_watermark", "command_id", "idempotency_key")
                )
            )
            if not scoped:
                from . import hosted_questions
                return hosted_questions.run(args)
            if getattr(args, "request_id", None) is not None:
                raise HostedError("INVALID_QUESTION", "Do not mix human question receipt flags with scoped answer application flags.")
            if getattr(args, "entity", None) is not None:
                args.config = args.entity.expanduser() / remote.CONFIG_NAME
        if args.hosted_command == "file":
            return remote.run_file(args)
        if args.hosted_command == "retry":
            receipt = _read_json(args.receipt.expanduser(), private=True)
            if receipt.get("kind") == "native-question":
                from . import hosted_questions
                return hosted_questions.run(args)
            if receipt.get("kind") == "engine-adoption":
                from . import hosted_adoption

                return hosted_adoption.retry(args)
            if receipt.get("kind") in {"engine-command", "engine-file-put"}:
                return remote.retry(args)
        config_path = args.config.expanduser()
        if args.hosted_command == "configure":
            config = {"endpoint": _endpoint(args.endpoint, args.allow_localhost), "company": args.company, "allow_localhost": args.allow_localhost}
            _segment(args.company)
            if args.tokenref is not None and args.store_token:
                raise HostedError("INVALID_CONFIG", "Use --tokenref or --store-token, not both.")
            if args.tokenref is not None:
                _token_reference(args.tokenref)
                config["tokenref"] = args.tokenref
            if args.entity is not None:
                config_path = args.entity.expanduser() / remote.CONFIG_NAME
                if args.tokenref is None and not args.store_token:
                    config["tokenref"] = "env:BOOKS_API_TOKEN"
            if args.store_token:
                config["token"] = _token(token)
            _write_private(config_path, config)
            result = {"configured": True, "endpoint": config["endpoint"], "company": args.company, "token_stored": args.store_token}
        else:
            config = _read_json(config_path)
            token = [os.environ.get("BOOKS_API_TOKEN"), config.get("token")]
            client = HostedClient(config, timeout=args.timeout)
            if args.hosted_command in COLLECTIONS:
                result = _read_collection(args, client)
            elif args.hosted_command == "question":
                result = _mutate_question(args, client)
            elif args.hosted_command in READS:
                suffix = READS[args.hosted_command]
                if args.hosted_command == "reports":
                    query = {key: value for key, value in (("from", args.from_date), ("to", args.to_date)) if value is not None}
                    if query:
                        suffix += "?" + parse.urlencode(query)
                result = client.request(suffix)
                if args.hosted_command == "held-mail":
                    result = _held_mail(client, result)
            elif args.hosted_command == "evidence-upload":
                result = _evidence_upload(args, client)
            elif args.hosted_command == "command-result":
                result = client.request("/commands/" + _segment(args.command_id))
            else:
                result = _mutate(args, client)
            if config_path.name == remote.CONFIG_NAME and "books_revision" in result:
                remote.remember_revision(config_path, client, remote._revision(result))
        print(json.dumps(_redact(result, token), sort_keys=True, allow_nan=False))
        return 0
    except HostedError as exc:
        print(json.dumps(_redact(exc.payload, token)), file=sys.stderr)
        return 1
    except (OSError, ValueError, RecursionError):
        # Do not print exception strings: they may contain URL credentials or request data.
        print(json.dumps({"error": {"code": "LOCAL_ERROR", "message": "Cannot prepare hosted request or persist receipt. Check configuration, input and local write permissions. No local fallback was used."}}), file=sys.stderr)
        return 1
