"""Mercury API download helpers."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .provider_api import APIClient, JsonObject, ProviderAPIError, Transport, extract_list, next_cursor


@dataclass(frozen=True)
class MercuryClient:
    api_key: str
    base_url: str = "https://api.mercury.com"
    timeout: float = 30.0
    transport: Transport | None = None

    def list_accounts(self) -> list[JsonObject]:
        return self._list("/api/v1/accounts", {"limit": 1000, "order": "asc"})

    def list_credit_accounts(self) -> list[JsonObject]:
        return self._list("/api/v1/credit", {})

    def list_treasury_accounts(self) -> list[JsonObject]:
        return self._list("/api/v1/treasury", {"limit": 1000, "order": "asc"})

    def list_transactions(self, *, from_date: str, to_date: str, date_field: str = "posted") -> list[JsonObject]:
        if date_field == "created":
            query = {"start": from_date, "end": to_date, "limit": 1000, "order": "asc"}
        else:
            query = {"postedStart": from_date, "postedEnd": to_date, "limit": 1000, "order": "asc"}
        return self._list("/api/v1/transactions", query)

    def list_account_transactions(
        self,
        account_id: str,
        *,
        from_date: str,
        to_date: str,
        date_field: str = "posted",
    ) -> list[JsonObject]:
        if date_field == "created":
            query = {"start": from_date, "end": to_date, "limit": 1000, "order": "asc"}
        else:
            query = {"postedStart": from_date, "postedEnd": to_date, "limit": 1000, "order": "asc"}
        return self._list(f"/api/v1/account/{account_id}/transactions", query)

    def _list(self, path: str, query: dict[str, Any]) -> list[JsonObject]:
        client = APIClient(
            provider="Mercury",
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            auth_scheme="Bearer",
            transport=self.transport,
        )
        rows: list[JsonObject] = []
        cursor: str | None = None
        while True:
            page_query = dict(query)
            if cursor:
                page_query["start_after"] = cursor
            payload = client.get(path, query=page_query)
            page_rows = extract_list(payload)
            rows.extend(page_rows)
            last_id = _last_id(page_rows)
            cursor = next_cursor(payload, item_id=last_id)
            if not cursor:
                break
        return rows


def collect_mercury_data(
    client: MercuryClient,
    *,
    from_date: str,
    to_date: str,
    date_field: str = "posted",
    account_ids: list[str] | None = None,
    all_accounts: bool = False,
) -> JsonObject:
    accounts = client.list_accounts()
    credit_accounts = client.list_credit_accounts()
    treasury_accounts = client.list_treasury_accounts()
    selected_ids = list(account_ids or [])
    if all_accounts:
        selected_ids = [str(account["id"]) for account in accounts if account.get("id")]

    if selected_ids:
        account_transactions = [
            {
                "accountId": account_id,
                "transactions": client.list_account_transactions(
                    account_id,
                    from_date=from_date,
                    to_date=to_date,
                    date_field=date_field,
                ),
            }
            for account_id in selected_ids
        ]
        transactions = [
            transaction
            for account_entry in account_transactions
            for transaction in account_entry["transactions"]
        ]
        selection = {"mode": "selected_accounts", "accountIds": selected_ids}
    else:
        account_transactions = []
        transactions = client.list_transactions(from_date=from_date, to_date=to_date, date_field=date_field)
        selection = {"mode": "organization"}

    return {
        "source": "mercury",
        "dateRange": {"from": from_date, "to": to_date, "field": "createdAt" if date_field == "created" else "postedAt"},
        "selection": selection,
        "accounts": accounts,
        "creditAccounts": credit_accounts,
        "treasuryAccounts": treasury_accounts,
        "accountTransactions": account_transactions,
        "transactions": transactions,
        "summary": {
            "accountCount": len(accounts),
            "creditAccountCount": len(credit_accounts),
            "treasuryAccountCount": len(treasury_accounts),
            "selectedAccountCount": len(selected_ids),
            "transactionCount": len(transactions),
        },
    }


def collect_mercury_accounts(client: MercuryClient) -> JsonObject:
    accounts = client.list_accounts()
    credit_accounts = client.list_credit_accounts()
    treasury_accounts = client.list_treasury_accounts()
    return {
        "source": "mercury",
        "accounts": [summarize_account(account, source_type="operating") for account in accounts],
        "creditAccounts": [
            summarize_account(account, source_type="credit") for account in credit_accounts
        ],
        "treasuryAccounts": [
            summarize_account(account, source_type="treasury") for account in treasury_accounts
        ],
        "summary": {
            "accountCount": len(accounts),
            "creditAccountCount": len(credit_accounts),
            "treasuryAccountCount": len(treasury_accounts),
        },
    }


def summarize_account(account: JsonObject, *, source_type: str = "operating") -> JsonObject:
    return {
        "id": account.get("id"),
        "sourceType": source_type,
        "name": account.get("name") or account.get("nickname"),
        "type": account.get("type") or account.get("kind"),
        "status": account.get("status"),
        "currency": account.get("currency"),
        "accountNumberLast4": _last4(account.get("accountNumber")),
        "routingNumberLast4": _last4(account.get("routingNumber")),
    }


def write_download(path: Path, payload: JsonObject, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise ProviderAPIError("Mercury", f"Output already exists: {path}. Use --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _last_id(rows: list[JsonObject]) -> str | None:
    if not rows:
        return None
    for key in ("id", "uuid"):
        value = rows[-1].get(key)
        if value:
            return str(value)
    return None


def _last4(value: Any) -> str | None:
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[-4:] if digits else None
