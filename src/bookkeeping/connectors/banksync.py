"""BankSync REST API client and normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import time
from typing import Any, Callable, Mapping
from urllib import error, parse, request


JsonObject = dict[str, Any]
Transport = Callable[[str, str, Mapping[str, str], float], tuple[int, str]]

MONEY_QUANT = Decimal("0.01")


class BankSyncError(RuntimeError):
    """Raised when BankSync returns an error or an invalid response."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class BankSyncClient:
    api_key: str
    base_url: str = "https://api.banksync.io"
    timeout: float = 30.0
    transport: Transport | None = None

    def list_banks(self, *, scope: str | None = None) -> list[JsonObject]:
        query = {"scope": scope} if scope else None
        return self._get("/v1/banks", query=query)

    def list_accounts(self, bank_id: str) -> list[JsonObject]:
        return self._get(f"/v1/banks/{parse.quote(bank_id, safe='')}/accounts")

    def list_transactions(
        self,
        bank_id: str,
        account_id: str,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[JsonObject]:
        """Fetch all transactions for the given account, following pagination cursors to exhaustion.

        The BankSync API cursor field name is not confirmed against the live API; we therefore
        check the response envelope for the following shapes (first non-empty/non-null wins):
          - top-level "nextCursor"
          - top-level "next_cursor"
          - top-level "cursor"
          - nested "pagination" -> "nextCursor"
          - nested "meta" -> "nextCursor"
        An unchanged cursor (same value as the one just sent) is treated as exhausted to guard
        against an infinite loop from a misbehaving server.
        """
        path = (
            f"/v1/banks/{parse.quote(bank_id, safe='')}"
            f"/accounts/{parse.quote(account_id, safe='')}/transactions"
        )
        all_transactions: list[JsonObject] = []
        current_cursor: str | None = None

        while True:
            query: dict[str, Any] = {
                "from": from_date,
                "to": to_date,
            }
            if current_cursor is not None:
                query["cursor"] = current_cursor

            envelope = self._get_envelope(path, query=query)
            page_data = envelope.get("data", [])
            if isinstance(page_data, list):
                all_transactions.extend(page_data)

            next_cursor = _extract_next_cursor(envelope)

            # Treat an unchanged cursor as exhausted to avoid an infinite loop.
            if next_cursor is None or next_cursor == current_cursor:
                break

            current_cursor = next_cursor

        return all_transactions

    def get_balance(self, bank_id: str, account_id: str) -> JsonObject:
        path = (
            f"/v1/banks/{parse.quote(bank_id, safe='')}"
            f"/accounts/{parse.quote(account_id, safe='')}/balances"
        )
        return self._get(path)

    def _get_envelope(self, path: str, *, query: Mapping[str, Any] | None = None) -> JsonObject:
        """Like _get but returns the full parsed response envelope (dict) instead of unwrapping data."""
        clean_query = {key: value for key, value in (query or {}).items() if value is not None}
        url = self.base_url.rstrip("/") + path
        if clean_query:
            url += "?" + parse.urlencode(clean_query)

        headers = {
            "Accept": "application/json",
            "X-API-Key": self.api_key,
            "User-Agent": "agent-books/0.1",
        }
        status, text = self._send("GET", url, headers)
        payload = _loads_json(text, status=status)
        if status < 200 or status >= 300:
            raise BankSyncError(_error_message(payload, status), status=status, body=payload)
        if isinstance(payload, dict) and payload.get("success") is False:
            raise BankSyncError(_error_message(payload, status), status=status, body=payload)
        if isinstance(payload, dict):
            return payload
        # Non-dict response — wrap it so callers always get a dict envelope.
        return {"data": payload}

    def _get(self, path: str, *, query: Mapping[str, Any] | None = None) -> Any:
        clean_query = {key: value for key, value in (query or {}).items() if value is not None}
        url = self.base_url.rstrip("/") + path
        if clean_query:
            url += "?" + parse.urlencode(clean_query)

        headers = {
            "Accept": "application/json",
            "X-API-Key": self.api_key,
            "User-Agent": "agent-books/0.1",
        }
        status, text = self._send("GET", url, headers)
        payload = _loads_json(text, status=status)
        if status < 200 or status >= 300:
            raise BankSyncError(_error_message(payload, status), status=status, body=payload)
        if isinstance(payload, dict) and payload.get("success") is False:
            raise BankSyncError(_error_message(payload, status), status=status, body=payload)
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def _send(self, method: str, url: str, headers: Mapping[str, str]) -> tuple[int, str]:
        if self.transport is not None:
            return self.transport(method, url, headers, self.timeout)

        req = request.Request(url, headers=dict(headers), method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - caller chooses API URL
                body = resp.read().decode("utf-8")
                return resp.status, body
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, body
        except error.URLError as exc:
            raise BankSyncError(f"BankSync request failed: {exc.reason}") from exc


# Aggregator-backed endpoints (observed against the live API on 2026-06-12) can
# transiently return an empty accounts list or HTTP 5xx for a *connected* bank
# when called in rapid succession; a backoff retry recovers the real data.
# Silent emptiness would corrupt ingestion, so retries are on by default here.
_EMPTY_ACCOUNTS_DELAYS = (5.0, 20.0, 45.0)
_TRANSIENT_DELAYS = (5.0, 15.0)


def _bank_connected(bank: JsonObject) -> bool:
    status = bank.get("connectionStatus")
    if isinstance(status, dict):
        return bool(status.get("connected", True))
    return True


def _retry_transient(fn: Any, *, sleeper: Any) -> Any:
    """Run fn(), retrying once per delay on HTTP 5xx BankSyncError."""
    last_error: BankSyncError | None = None
    for delay in (0.0, *_TRANSIENT_DELAYS):
        if delay:
            sleeper(delay)
        try:
            return fn()
        except BankSyncError as exc:
            if exc.status is None or exc.status < 500:
                raise
            last_error = exc
    raise last_error  # type: ignore[misc]


def collect_bank_data(
    client: BankSyncClient,
    *,
    bank_name: str,
    from_date: str,
    to_date: str,
    scope: str = "self",
    sleeper: Any = time.sleep,
) -> JsonObject:
    """Fetch banks, accounts, and normalized transactions for matching bank names."""

    banks = client.list_banks(scope=scope)
    matched_banks = [bank for bank in banks if bank_matches(bank, bank_name)]
    collected: list[JsonObject] = []

    for bank in matched_banks:
        accounts = _retry_transient(
            lambda: client.list_accounts(str(bank["id"])), sleeper=sleeper
        )
        if not accounts and _bank_connected(bank):
            for delay in _EMPTY_ACCOUNTS_DELAYS:
                sleeper(delay)
                accounts = _retry_transient(
                    lambda: client.list_accounts(str(bank["id"])), sleeper=sleeper
                )
                if accounts:
                    break
        collected_accounts: list[JsonObject] = []
        for account in accounts:
            transactions = _retry_transient(
                lambda: client.list_transactions(
                    str(bank["id"]),
                    str(account["id"]),
                    from_date=from_date,
                    to_date=to_date,
                ),
                sleeper=sleeper,
            )
            normalized_transactions = [
                normalize_transaction(
                    {
                        "accountName": account.get("accountName"),
                        "accountType": account.get("accountType"),
                        **txn,
                    }
                )
                for txn in transactions
            ]
            collected_accounts.append(
                {
                    "account": normalize_account(account),
                    "transactions": normalized_transactions,
                    "summary": summarize_transactions(normalized_transactions),
                }
            )
        collected.append(
            {
                "bank": normalize_bank(bank),
                "accountCount": len(collected_accounts),
                "accounts": collected_accounts,
            }
        )

    return {
        "source": "banksync",
        "bankNameFilter": bank_name,
        "scope": scope,
        "dateRange": {"from": from_date, "to": to_date},
        "connectedBankCount": len(banks),
        "connectedBanks": [bank_brief(bank) for bank in banks],
        "matchedBankCount": len(matched_banks),
        "banks": collected,
    }


def build_stats(collected: JsonObject) -> JsonObject:
    account_entries = [
        account
        for bank in collected["banks"]
        for account in bank["accounts"]
    ]
    transaction_entries = [
        txn
        for account in account_entries
        for txn in account["transactions"]
    ]
    summary = summarize_transactions(transaction_entries)
    return {
        "source": collected["source"],
        "bankNameFilter": collected["bankNameFilter"],
        "scope": collected["scope"],
        "dateRange": collected["dateRange"],
        "connectedBankCount": collected["connectedBankCount"],
        "connectedBanks": collected["connectedBanks"],
        "matchedBankCount": collected["matchedBankCount"],
        "matchedAccountCount": len(account_entries),
        "transactionSummary": summary,
        "banks": [
            {
                "bank": bank["bank"],
                "accountCount": bank["accountCount"],
                "accounts": [
                    {
                        "account": account["account"],
                        "summary": account["summary"],
                    }
                    for account in bank["accounts"]
                ],
            }
            for bank in collected["banks"]
        ],
    }


def bank_matches(bank: Mapping[str, Any], bank_name: str) -> bool:
    needle = bank_name.casefold()
    values = [
        bank.get("name"),
        bank.get("institutionId"),
        bank.get("type"),
        bank.get("source"),
    ]
    return any(needle in str(value).casefold() for value in values if value is not None)


def bank_brief(bank: Mapping[str, Any]) -> JsonObject:
    return {
        "id": bank.get("id"),
        "name": bank.get("name"),
        "source": bank.get("source"),
        "type": bank.get("type"),
        "status": (bank.get("connectionStatus") or {}).get("status"),
    }


def normalize_bank(bank: Mapping[str, Any]) -> JsonObject:
    normalized = bank_brief(bank)
    normalized.update(
        {
            "institutionId": bank.get("institutionId"),
            "createdAt": bank.get("createdAt"),
            "updatedAt": bank.get("updatedAt"),
            "workspaceId": bank.get("workspaceId"),
            "portalName": (bank.get("portal") or {}).get("name"),
        }
    )
    return normalized


def normalize_account(account: Mapping[str, Any]) -> JsonObject:
    return {
        "id": account.get("id"),
        "bankId": account.get("bankId"),
        "accountName": account.get("accountName"),
        "accountType": account.get("accountType"),
        "currency": account.get("currency"),
        "balance": money_string(account.get("balance")),
        "availableBalance": money_string(account.get("availableBalance")),
        "accountNumberLast4": last4(account.get("accountNumber")),
        "createdAt": account.get("createdAt"),
        "updatedAt": account.get("updatedAt"),
    }


def normalize_transaction(transaction: Mapping[str, Any]) -> JsonObject:
    return {
        "id": transaction.get("id"),
        "date": transaction.get("date"),
        "authorizedDate": transaction.get("authorizedDate"),
        "description": transaction.get("description"),
        "originalDescription": transaction.get("originalDescription"),
        "amount": money_string(transaction.get("amount")),
        "creditAmount": money_string(transaction.get("creditAmount")),
        "debitAmount": money_string(transaction.get("debitAmount")),
        "currency": transaction.get("currency"),
        "category": transaction.get("category"),
        "type": transaction.get("type"),
        "reference": transaction.get("reference"),
        "pending": bool(transaction.get("pending", False)),
        "pendingTransactionId": transaction.get("pendingTransactionId"),
        "accountId": transaction.get("accountId"),
        "accountName": transaction.get("accountName"),
        "accountType": transaction.get("accountType"),
        "accountNumberLast4": last4(transaction.get("accountNumber")),
        "bankId": transaction.get("bankId"),
        "bank": transaction.get("bank"),
    }


def summarize_transactions(transactions: list[Mapping[str, Any]]) -> JsonObject:
    dates = sorted(str(txn["date"]) for txn in transactions if txn.get("date"))
    pending = sum(1 for txn in transactions if txn.get("pending"))
    credit_total = sum((_money_decimal(txn.get("creditAmount")) for txn in transactions), Decimal("0"))
    debit_total = sum((_money_decimal(txn.get("debitAmount")) for txn in transactions), Decimal("0"))
    net_total = sum((_money_decimal(txn.get("amount")) for txn in transactions), Decimal("0"))
    currencies = sorted({str(txn["currency"]) for txn in transactions if txn.get("currency")})
    types: dict[str, int] = {}
    for txn in transactions:
        kind = str(txn.get("type") or "unknown")
        types[kind] = types.get(kind, 0) + 1

    return {
        "count": len(transactions),
        "posted": len(transactions) - pending,
        "pending": pending,
        "firstDate": dates[0] if dates else None,
        "lastDate": dates[-1] if dates else None,
        "creditAmountTotal": money_string(credit_total),
        "debitAmountTotal": money_string(debit_total),
        "netAmountTotal": money_string(net_total),
        "currencies": currencies,
        "types": dict(sorted(types.items())),
        "uniqueTransactionIds": len({txn.get("id") for txn in transactions if txn.get("id")}),
    }


def last4(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[-4:] if text else None


def money_string(value: Any) -> str | None:
    if value is None:
        return None
    return format(_money_decimal(value), "f")


def _money_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        amount = value
    else:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise BankSyncError(f"Invalid money value from BankSync: {value!r}") from exc
    return amount.quantize(MONEY_QUANT)


def _extract_next_cursor(envelope: JsonObject) -> str | None:
    """Extract the next-page cursor from a response envelope.

    Checks the following shapes in priority order (first non-empty string wins):
      1. envelope["nextCursor"]
      2. envelope["next_cursor"]
      3. envelope["cursor"]
      4. envelope["pagination"]["nextCursor"]
      5. envelope["meta"]["nextCursor"]
    Returns None when no valid cursor is found.
    """
    for key in ("nextCursor", "next_cursor", "cursor"):
        value = envelope.get(key)
        if value and isinstance(value, str):
            return value
    for nested_key in ("pagination", "meta"):
        nested = envelope.get(nested_key)
        if isinstance(nested, dict):
            value = nested.get("nextCursor")
            if value and isinstance(value, str):
                return value
    return None


def _loads_json(text: str, *, status: int) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BankSyncError(f"BankSync returned non-JSON response with HTTP {status}", status=status) from exc


def _error_message(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        detail = payload.get("error") or payload.get("message")
        if detail:
            return f"BankSync API error HTTP {status}: {detail}"
    return f"BankSync API error HTTP {status}"
