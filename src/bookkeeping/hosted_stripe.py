"""Use a company's OAuth-backed Stripe feed without exposing provider credentials."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
from typing import Any

from bookkeeping import hosted, remote


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("stripe", help="Read and process the connected Stripe feed under approved company rules")
    actions = parser.add_subparsers(dest="stripe_command", required=True)
    for action in ("status", "policy", "receipt", "preview", "sync"):
        sub = actions.add_parser(action)
        sub.add_argument("--entity", type=Path)
        if action not in {"status", "policy"}:
            sub.add_argument("--request-id", required=True, help="Stable ID; reuse the same ID and dates after interruption")
        if action in {"preview", "sync"}:
            sub.add_argument("--from", dest="date_from", required=True)
            sub.add_argument("--to", dest="date_to", required=True)


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise hosted.HostedError("INVALID_STRIPE_REQUEST", "Use a stable alphanumeric request ID with dashes or underscores.")
    return value


def process(client: hosted.HostedClient, action: str, request_id: str | None = None,
            date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
    if action == "status":
        return client.request("/stripe")
    if action == "policy":
        return client.request("/stripe/policy")
    root = identifier(request_id)
    if action == "receipt":
        return client.request("/stripe/sync/" + root)
    if action not in {"preview", "sync"}:
        raise hosted.HostedError("INVALID_STRIPE_REQUEST", "Only status, receipt, preview and sync are supported.")
    try:
        if date.fromisoformat(date_from).isoformat() != date_from or date.fromisoformat(date_to).isoformat() != date_to or date_from > date_to:
            raise ValueError()
    except (ValueError, TypeError):
        raise hosted.HostedError("INVALID_STRIPE_REQUEST", "Use an ordered YYYY-MM-DD date range.") from None
    body = {"request_id": root, "from": date_from, "to": date_to}
    seen: set[str] = set()
    for _ in range(100):
        current = body["request_id"]
        if current in seen:
            raise hosted.HostedError("INVALID_RESPONSE", "Stripe continuation repeated a request ID.")
        seen.add(current)
        # Replaying the root and server-provided child IDs recovers saved evidence;
        # no client-side cursor guessing, authority changes or financial math.
        result = client.request("/stripe/" + action, body=body, key=current)
        if (result.get("request_id") != current or result.get("state") != "completed"
                or result.get("kind") != action or result.get("from") != date_from
                or result.get("to") != date_to or result.get("chain_id") != root
                or type(result.get("range_complete")) is not bool):
            raise hosted.HostedError("INVALID_RESPONSE", "Stripe did not confirm the exact saved request. Resume with the same request ID and dates.")
        next_request = result.get("next_request")
        if result["range_complete"]:
            if next_request is not None:
                raise hosted.HostedError("INVALID_RESPONSE", "Completed Stripe range includes an unexpected continuation.")
            receipt = client.request("/stripe/sync/" + root)
            chain = receipt.get("chain", {})
            if chain.get("id") != root or chain.get("state") != "complete":
                raise hosted.HostedError("INVALID_RESPONSE", "The complete Stripe chain could not be confirmed. Read its receipt again.")
            return receipt
        if (not isinstance(next_request, dict) or set(next_request) != {"request_id", "from", "to", "provider_cursor"}
                or next_request.get("from") != date_from or next_request.get("to") != date_to
                or next_request.get("provider_cursor") != result.get("next_provider_cursor")):
            raise hosted.HostedError("INVALID_RESPONSE", "Invalid Stripe continuation; keep the original request ID.")
        identifier(next_request.get("request_id"))
        identifier(next_request.get("provider_cursor"))
        body = next_request
    raise hosted.HostedError("STRIPE_LIMIT", "Stripe import exceeded 100 pages. Inspect the saved receipt before choosing a narrower period.")


def run(args: Any) -> int:
    config_path = remote._binding(args.entity or Path.cwd())
    if args.entity is not None and config_path is None:
        raise hosted.HostedError("NOT_REMOTE", "This folder has no cloud company binding.")
    config_path = config_path or remote._absolute(args.config)
    config = hosted._read_json(config_path, private=config_path.name == remote.CONFIG_NAME)
    client = hosted.HostedClient(config, timeout=args.timeout)
    result = process(client, args.stripe_command, getattr(args, "request_id", None),
                     getattr(args, "date_from", None), getattr(args, "date_to", None))
    print(json.dumps(result, ensure_ascii=False))
    return 0
