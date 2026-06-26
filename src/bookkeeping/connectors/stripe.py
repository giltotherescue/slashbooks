"""Stripe API download helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
from typing import Any

from .provider_api import APIClient, JsonObject, ProviderAPIError, Transport, extract_list, next_cursor


@dataclass(frozen=True)
class StripeClient:
    api_key: str
    base_url: str = "https://api.stripe.com"
    timeout: float = 30.0
    transport: Transport | None = None
    stripe_account: str | None = None

    def retrieve_account(self) -> JsonObject:
        return self._client().get("/v1/account")

    def list_balance_transactions(self, *, from_date: str, to_date: str) -> list[JsonObject]:
        return self._list(
            "/v1/balance_transactions",
            {
                "created[gte]": _date_start_timestamp(from_date),
                "created[lte]": _date_end_timestamp(to_date),
                "limit": 100,
            },
        )

    def list_payouts(self, *, from_date: str, to_date: str) -> list[JsonObject]:
        return self._list(
            "/v1/payouts",
            {
                "created[gte]": _date_start_timestamp(from_date),
                "created[lte]": _date_end_timestamp(to_date),
                "limit": 100,
            },
        )

    def _list(self, path: str, query: dict[str, Any]) -> list[JsonObject]:
        client = self._client()
        rows: list[JsonObject] = []
        cursor: str | None = None
        while True:
            page_query = dict(query)
            if cursor:
                page_query["starting_after"] = cursor
            payload = client.get(path, query=page_query)
            page_rows = extract_list(payload)
            rows.extend(page_rows)
            last_id = str(page_rows[-1]["id"]) if page_rows and page_rows[-1].get("id") else None
            cursor = next_cursor(payload, item_id=last_id)
            if not cursor:
                break
        return rows

    def _client(self) -> APIClient:
        headers = {"Stripe-Account": self.stripe_account} if self.stripe_account else None
        return APIClient(
            provider="Stripe",
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            auth_scheme="Bearer",
            transport=self.transport,
            extra_headers=headers,
        )


def collect_stripe_data(client: StripeClient, *, from_date: str, to_date: str) -> JsonObject:
    account = client.retrieve_account()
    balance_transactions = client.list_balance_transactions(from_date=from_date, to_date=to_date)
    payouts = client.list_payouts(from_date=from_date, to_date=to_date)
    return {
        "source": "stripe",
        "account": account,
        "dateRange": {"from": from_date, "to": to_date, "field": "created"},
        "balanceTransactions": balance_transactions,
        "payouts": payouts,
        "summary": {
            "accountId": account.get("id"),
            "balanceTransactionCount": len(balance_transactions),
            "payoutCount": len(payouts),
        },
    }


def summarize_account(account: JsonObject) -> JsonObject:
    business_profile = account.get("business_profile")
    return {
        "id": account.get("id"),
        "country": account.get("country"),
        "defaultCurrency": account.get("default_currency"),
        "chargesEnabled": account.get("charges_enabled"),
        "payoutsEnabled": account.get("payouts_enabled"),
        "businessName": business_profile.get("name") if isinstance(business_profile, dict) else None,
    }


def write_download(path: Path, payload: JsonObject, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise ProviderAPIError("Stripe", f"Output already exists: {path}. Use --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _date_start_timestamp(value: str) -> int:
    parsed = date.fromisoformat(value)
    return int(datetime.combine(parsed, time.min, tzinfo=timezone.utc).timestamp())


def _date_end_timestamp(value: str) -> int:
    parsed = date.fromisoformat(value)
    return int(datetime.combine(parsed, time.max, tzinfo=timezone.utc).timestamp())
