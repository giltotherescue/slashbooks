"""Browser-approved company login. Credentials are never returned to the agent."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import http.client
import json
import math
import os
import re
from pathlib import Path
import stat
import sys
import time
from urllib import error, parse, request
import uuid

from . import __version__, hosted

CLIENT = "slashbooks-cli"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"
MAX_BYTES = 16 * 1024


def _post(endpoint: str, suffix: str, body: dict, timeout: float = 30) -> dict:
    opener = request.build_opener(request.ProxyHandler({}), hosted._NoRedirect())
    req = request.Request(endpoint + suffix, data=parse.urlencode(body).encode(),
                          headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
                                   "User-Agent": f"slashbooks/{__version__}"})
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError()
        value = hosted._json_loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or "error" in value:
            raise ValueError()
        return value
    except error.HTTPError as exc:
        with exc:
            raw = exc.read(MAX_BYTES + 1)
        try:
            value = hosted._json_loads(raw.decode("utf-8")) if len(raw) <= MAX_BYTES else {}
            code = value.get("error") if isinstance(value, dict) else None
        except (ValueError, UnicodeError):
            code = None
        # Never forward server error bodies, which can contain secrets.
        allowed = {"authorization_pending", "slow_down", "expired_token", "access_denied", "invalid_grant", "invalid_client", "invalid_scope"}
        return {"error": code if code in allowed else "authorization_failed"}
    except (error.URLError, OSError, TimeoutError, http.client.HTTPException) as exc:
        raise hosted.HostedError("AUTH_CONNECTION_FAILED", "Agent sign-in could not be confirmed. Start sign-in again; no bookkeeping was submitted.") from exc
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise hosted.HostedError("AUTH_RESPONSE_INVALID", "Agent sign-in returned an invalid response. Start again.") from exc


def _tokens(value: dict, company: str) -> dict:
    if value.get("error") or value.get("token_type") != "Bearer" or value.get("company_id") != company:
        raise hosted.HostedError("AUTH_REQUIRED", "Company access was declined, expired or revoked. Start browser sign-in again.")
    expires = value.get("expires_in")
    if type(expires) is not int or not 60 <= expires <= 3600:
        raise hosted.HostedError("AUTH_RESPONSE_INVALID", "Invalid agent credential expiry.")
    token = hosted._token(value.get("access_token"))
    refresh = hosted._token(value.get("refresh_token"))
    if len(token) > 512 or len(refresh) > 512 or len(refresh) < 32:
        raise hosted.HostedError("AUTH_RESPONSE_INVALID", "Invalid agent credentials.")
    return {"token": token, "refresh_token": refresh, "expires_at": time.time() + expires}


@contextmanager
def _lock(path: Path):
    import fcntl
    from .remote import _no_symlinks

    _no_symlinks(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path.with_suffix(".lock"), flags, 0o600), "r+") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise hosted.HostedError("INSECURE_FILE", "Agent credential storage must be owner-only.")
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def credential_token(config: dict, timeout: float = 30) -> tuple[str, str]:
    value = config.get("credential_file")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise hosted.HostedError("INVALID_CONFIG", "Agent credential storage is invalid.")
    path = Path(value)
    with _lock(path):
        stored = hosted._read_json(path, private=True)
        if stored.get("endpoint") != config.get("endpoint") or stored.get("company") != config.get("company"):
            raise hosted.HostedError("INVALID_CONFIG", "Stored sign-in belongs to a different company or server.")
        expiry = stored.get("expires_at")
        if not isinstance(expiry, (float, int)) or not math.isfinite(expiry):
            raise hosted.HostedError("INVALID_CONFIG", "Stored agent sign-in expiry is invalid.")
        if expiry <= time.time() + 120:
            result = _post(stored["endpoint"], "/oauth/token", {"client_id": CLIENT, "grant_type": "refresh_token",
                           "refresh_token": hosted._token(stored.get("refresh_token"))}, timeout)
            stored.update(_tokens(result, config["company"]))
            hosted._write_private(path, stored)
        return hosted._token(stored.get("token")), hosted._token(stored.get("refresh_token"))


def login(args) -> int:
    from . import remote

    endpoint = hosted._endpoint(args.endpoint, args.allow_localhost)
    hosted._segment(args.company)
    directory = remote._absolute(args.entity)
    config_path = directory / remote.CONFIG_NAME
    remote._no_symlinks(config_path)
    if remote._binding(directory) is not None:
        existing_path = remote._binding(directory)
        existing = hosted._read_json(existing_path, private=True)
        if not args.reauthorize or existing_path != config_path or existing.get("endpoint") != endpoint or existing.get("company") != args.company:
            raise hosted.HostedError("ALREADY_CONFIGURED", "This folder already has a company connection. Use a separate folder, or explicitly reauthorize the same company.")
    if any((directory / name).exists() for name in ("ledger.sqlite", "entity.json")):
        raise hosted.HostedError("LOCAL_BOOKS_PRESENT", "Use a separate folder for online books. Existing local books were not changed.")
    started = _post(endpoint, "/oauth/device/authorize", {"client_id": CLIENT, "company_id": args.company})
    if started.get("error"):
        raise hosted.HostedError("AUTH_UNAVAILABLE", "Browser sign-in is unavailable for this company. Check the company connection details.")
    device = hosted._token(started.get("device_code"))
    link = started.get("verification_uri_complete")
    code = started.get("user_code")
    origin = parse.urlsplit(endpoint)
    if not isinstance(link, str) or not isinstance(code, str):
        raise hosted.HostedError("AUTH_RESPONSE_INVALID", "Sign-in instructions are unavailable.")
    parsed = parse.urlsplit(link)
    if parsed.scheme != origin.scheme or parsed.netloc != origin.netloc or parsed.path != "/connect-agent" or parsed.fragment or parsed.username:
        raise hosted.HostedError("AUTH_RESPONSE_INVALID", "Sign-in link is not on the configured Slashbooks server.")
    if parse.parse_qs(parsed.query) != {"user_code": [code]} or not re.fullmatch(r"[A-Z2-9]{5}-[A-Z2-9]{5}", code):
        raise hosted.HostedError("AUTH_RESPONSE_INVALID", "Invalid sign-in code.")
    interval = started.get("interval")
    duration = started.get("expires_in")
    if type(interval) is not int or not 5 <= interval <= 60 or type(duration) is not int or not 1 <= duration <= 600:
        raise hosted.HostedError("AUTH_RESPONSE_INVALID", "Invalid sign-in lifetime.")
    print(json.dumps({"event": "agent_sign_in", "verification_url": link, "confirmation_code": code,
                      "message": "Open this link yourself, confirm the code and company, and allow access. No key is needed."}), flush=True)
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        time.sleep(interval)
        result = _post(endpoint, "/oauth/token", {"client_id": CLIENT, "grant_type": GRANT, "device_code": device})
        if result.get("error") == "authorization_pending":
            continue
        if result.get("error") == "slow_down":
            interval = min(interval + 5, 60)
            continue
        credentials = _tokens(result, args.company)
        stored = {"endpoint": endpoint, "company": args.company, **credentials}
        # A separate owner-only credential file keeps secrets outside company documents.
        identity = hashlib.sha256((endpoint + "\n" + args.company).encode()).hexdigest()[:24]
        credential_path = Path.home() / ".config/slashbooks/agents" / identity / (uuid.uuid4().hex + ".json")
        with _lock(credential_path):
            hosted._write_private(credential_path, stored, exclusive=True)
        config = {"endpoint": endpoint, "company": args.company, "allow_localhost": args.allow_localhost,
                  "credential_file": str(credential_path)}
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with remote._revision_lock(config_path):
            if any((directory / name).exists() for name in ("ledger.sqlite", "entity.json")):
                raise hosted.HostedError("LOCAL_BOOKS_PRESENT", "Local books appeared in this folder. No connection was saved; choose a separate folder.")
            if config_path.exists() and not args.reauthorize:
                raise hosted.HostedError("ALREADY_CONFIGURED", "Another session connected this folder. Use a separate folder.")
            if config_path.exists():
                current = hosted._read_json(config_path, private=True)
                if current.get("endpoint") != endpoint or current.get("company") != args.company:
                    raise hosted.HostedError("ALREADY_CONFIGURED", "The folder connection changed. No binding was replaced.")
            # Prove authenticated identity before publishing the company binding.
            status = hosted.HostedClient(config).request("")
            if status.get("id") != args.company:
                raise hosted.HostedError("AUTH_RESPONSE_INVALID", "Could not confirm the selected company.")
            hosted._write_private(config_path, config)
        print(json.dumps({"connected": True, "company_name": status.get("name"), "message": "Online books connected. Existing local books were not changed."}), flush=True)
        return 0
    raise hosted.HostedError("AUTH_EXPIRED", "Agent sign-in expired. Ask the agent to start again.")
