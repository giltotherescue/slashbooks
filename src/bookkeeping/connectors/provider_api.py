"""Shared helpers for direct provider API downloads."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping
from urllib import error, parse, request


JsonObject = dict[str, Any]
Transport = Callable[[str, str, Mapping[str, str], float], tuple[int, str]]


class ProviderAPIError(RuntimeError):
    """Raised when a provider API returns an error or invalid response."""

    def __init__(self, provider: str, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(f"{provider} API error: {message}")
        self.provider = provider
        self.status = status
        self.body = body


@dataclass(frozen=True)
class APIClient:
    provider: str
    api_key: str
    base_url: str
    timeout: float = 30.0
    auth_scheme: str = "Bearer"
    transport: Transport | None = None
    extra_headers: Mapping[str, str] | None = None

    def get(self, path: str, *, query: Mapping[str, Any] | None = None) -> JsonObject:
        clean_query = {key: value for key, value in (query or {}).items() if value is not None}
        url = self.base_url.rstrip("/") + path
        if clean_query:
            url += "?" + parse.urlencode(clean_query, doseq=True)

        headers = {
            "Accept": "application/json",
            "Authorization": f"{self.auth_scheme} {self.api_key}",
            "User-Agent": "agent-books/0.2",
        }
        if self.extra_headers:
            headers.update(self.extra_headers)
        status, text = self._send("GET", url, headers)
        payload = _loads_json(self.provider, text, status=status)
        if status < 200 or status >= 300:
            raise ProviderAPIError(self.provider, _error_message(payload, status), status=status, body=payload)
        if isinstance(payload, list):
            return {"data": payload}
        if not isinstance(payload, dict):
            raise ProviderAPIError(self.provider, f"expected JSON object, got {type(payload).__name__}")
        return payload

    def _send(self, method: str, url: str, headers: Mapping[str, str]) -> tuple[int, str]:
        if self.transport is not None:
            return self.transport(method, url, headers, self.timeout)

        req = request.Request(url, headers=dict(headers), method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - caller chooses provider URL
                body = resp.read().decode("utf-8")
                return resp.status, body
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, body
        except error.URLError as exc:
            raise ProviderAPIError(self.provider, f"request failed: {exc.reason}") from exc


def extract_list(payload: JsonObject) -> list[JsonObject]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(payload.get("accounts"), list):
        return [item for item in payload["accounts"] if isinstance(item, dict)]
    if isinstance(payload.get("transactions"), list):
        return [item for item in payload["transactions"] if isinstance(item, dict)]
    return []


def next_cursor(payload: JsonObject, *, item_id: str | None = None) -> str | None:
    for key in ("nextCursor", "next_cursor", "cursor"):
        value = payload.get(key)
        if value:
            return str(value)
    for key in ("pagination", "meta"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            for nested_key in ("nextCursor", "next_cursor", "cursor"):
                value = nested.get(nested_key)
                if value:
                    return str(value)
    if payload.get("has_more") and item_id:
        return item_id
    return None


def _loads_json(provider: str, text: str, *, status: int) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderAPIError(provider, f"non-JSON response with HTTP {status}", status=status) from exc


def _error_message(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        error_obj = payload.get("error")
        if isinstance(error_obj, dict):
            message = error_obj.get("message") or error_obj.get("detail") or error_obj.get("code")
            if message:
                return f"HTTP {status}: {message}"
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return f"HTTP {status}: {value}"
    return f"HTTP {status}"
