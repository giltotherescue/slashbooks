"""Command-line entry point for books utilities."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys
from typing import Any

from bookkeeping.connectors.banksync import (
    BankSyncClient,
    BankSyncError,
    build_stats,
    collect_bank_data,
    normalize_account,
    normalize_bank,
    normalize_transaction,
    summarize_transactions,
)
from bookkeeping.connectors.mercury import (
    MercuryClient,
    collect_mercury_accounts,
    collect_mercury_data,
)
from bookkeeping.connectors.provider_api import ProviderAPIError
from bookkeeping.connectors.stripe import (
    StripeClient,
    collect_stripe_data,
    summarize_account as summarize_stripe_account,
)
from bookkeeping import compare as compare_module
from bookkeeping import entity as entity_module
from bookkeeping import ingest as ingest_module
from bookkeeping import queue as queue_module
from bookkeeping import quickbooks as quickbooks_module
from bookkeeping import reconcile as reconcile_module
from bookkeeping.connectors import csvsource as csvsource_module
from bookkeeping.reports import statements as statements_module
from bookkeeping.reports import workbook as workbook_module


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path.cwd() / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "connector":
            return run_connector(args)
        if args.command == "entity":
            return entity_module.run(args)
        if args.command == "ingest":
            return ingest_module.run(args)
        if args.command == "qb":
            return quickbooks_module.run(args)
        if args.command in {"report", "ask"}:
            return statements_module.run(args)
        if args.command in {"reconcile", "reconcile-resolve"}:
            return reconcile_module.run(args)
        if args.command in {"backtest", "compare"}:
            return compare_module.run(args)
        if args.command in {"queue", "quarterly-review"}:
            return queue_module.run(args)
        if args.command in {"sanity-check", "export"}:
            return workbook_module.run(args)
    except (BankSyncError, ProviderAPIError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    parser.error("unknown command")
    return 2


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE pairs from *path* without executing shell code.

    Existing environment variables win. Values may be unquoted, single-quoted,
    or double-quoted. This intentionally supports only the small .env subset
    needed for local credentials.
    """
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="books")
    subcommands = parser.add_subparsers(dest="command", required=True)

    connector = subcommands.add_parser(
        "connector", help="Pull data from a connector: bank feed, provider API, or CSV"
    )
    connector_subcommands = connector.add_subparsers(dest="connector_name", required=True)

    banksync = connector_subcommands.add_parser("banksync", help="Interact with the BankSync REST API")
    banksync.add_argument(
        "--api-key-env",
        default="BANKSYNC_API_KEY",
        help="Environment variable containing the BankSync API key",
    )
    banksync.add_argument("--base-url", default="https://api.banksync.io")
    banksync.add_argument("--timeout", type=float, default=30.0)
    banksync_subcommands = banksync.add_subparsers(dest="banksync_command", required=True)

    banks = banksync_subcommands.add_parser("banks", help="List connected BankSync bank connections")
    banks.add_argument("--scope", choices=["self", "family"], default="self")

    accounts = banksync_subcommands.add_parser("accounts", help="List accounts for a bank connection")
    accounts.add_argument("--bank-id", required=True)

    transactions = banksync_subcommands.add_parser("transactions", help="List normalized transactions")
    transactions.add_argument("--bank-id", required=True)
    transactions.add_argument("--account-id", required=True)
    transactions.add_argument("--from", dest="from_date", default=_default_from_date(), type=_date_arg)
    transactions.add_argument("--to", dest="to_date", default=_default_to_date(), type=_date_arg)

    stats = banksync_subcommands.add_parser("stats", help="Summarize transactions for matching banks")
    add_collection_args(stats)

    download = banksync_subcommands.add_parser("download", help="Write normalized BankSync data to JSON")
    add_collection_args(download)
    download.add_argument("--output", required=True, type=Path)
    download.add_argument("--overwrite", action="store_true")

    validate = banksync_subcommands.add_parser(
        "validate",
        help="Probe BankSync for ID stability, historical depth, pending/posted semantics, and page-boundary truncation",
    )
    add_collection_args(validate)
    validate.add_argument("--output", type=Path, default=None, help="Write JSON report to this file")

    stripe = connector_subcommands.add_parser("stripe", help="Download Stripe balance and payout data")
    stripe.add_argument(
        "--api-key-env",
        default="STRIPE_SECRET_KEY",
        help="Environment variable containing the Stripe secret key",
    )
    stripe.add_argument("--base-url", default="https://api.stripe.com")
    stripe.add_argument("--timeout", type=float, default=30.0)
    stripe.add_argument(
        "--stripe-account",
        default=None,
        help="Optional connected account ID for Stripe Connect platforms",
    )
    stripe_subcommands = stripe.add_subparsers(dest="stripe_command", required=True)
    stripe_subcommands.add_parser("account", help="Show the Stripe account used by this key")
    stripe_download = stripe_subcommands.add_parser(
        "download",
        aliases=["pull"],
        help="Write Stripe balance transactions and payouts to JSON",
    )
    add_date_range_args(stripe_download)
    stripe_download.add_argument("--output", required=True, type=Path)
    stripe_download.add_argument("--overwrite", action="store_true")

    mercury = connector_subcommands.add_parser("mercury", help="Download Mercury account and transaction data")
    mercury.add_argument(
        "--api-key-env",
        default="MERCURY_API_KEY",
        help="Environment variable containing the Mercury API key",
    )
    mercury.add_argument("--base-url", default="https://api.mercury.com")
    mercury.add_argument("--timeout", type=float, default=30.0)
    mercury_subcommands = mercury.add_subparsers(dest="mercury_command", required=True)
    mercury_subcommands.add_parser("accounts", help="List Mercury operating, credit, and treasury accounts available to this key")
    mercury_download = mercury_subcommands.add_parser(
        "download",
        aliases=["pull"],
        help="Write Mercury accounts and transactions to JSON",
    )
    add_date_range_args(mercury_download)
    mercury_download.add_argument(
        "--date-field",
        choices=["posted", "created"],
        default="posted",
        help="Use posted dates by default; choose created only when matching Mercury API createdAt filters",
    )
    mercury_scope = mercury_download.add_mutually_exclusive_group(required=True)
    mercury_scope.add_argument(
        "--account-id",
        action="append",
        default=[],
        help="Mercury account ID to include; repeat for multiple accounts",
    )
    mercury_scope.add_argument(
        "--all-accounts",
        action="store_true",
        help="Download transactions for every Mercury operating account returned by the API",
    )
    mercury_download.add_argument("--output", required=True, type=Path)
    mercury_download.add_argument("--overwrite", action="store_true")

    csvsource_module.add_parser(connector_subcommands)

    entity_module.add_parser(subcommands)
    ingest_module.add_parser(subcommands)
    quickbooks_module.add_parser(subcommands)
    statements_module.add_parser(subcommands)
    reconcile_module.add_parser(subcommands)
    compare_module.add_parser(subcommands)
    queue_module.add_parser(subcommands)
    workbook_module.add_parser(subcommands)

    return parser


def add_collection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bank-name", default="", help="Bank name filter; omit to include all connected banks")
    parser.add_argument("--scope", choices=["self", "family"], default="self")
    add_date_range_args(parser)


def add_date_range_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="from_date", default=_default_from_date(), type=_date_arg)
    parser.add_argument("--to", dest="to_date", default=_default_to_date(), type=_date_arg)


def run_banksync(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise BankSyncError(f"Missing BankSync API key. Set {args.api_key_env}.")

    client = BankSyncClient(api_key=api_key, base_url=args.base_url, timeout=args.timeout)

    if args.banksync_command == "banks":
        print_json([normalize_bank(bank) for bank in client.list_banks(scope=args.scope)])
        return 0

    if args.banksync_command == "accounts":
        print_json([normalize_account(account) for account in client.list_accounts(args.bank_id)])
        return 0

    if args.banksync_command == "transactions":
        print_json(
            [
                normalize_transaction(transaction)
                for transaction in client.list_transactions(
                    args.bank_id,
                    args.account_id,
                    from_date=args.from_date,
                    to_date=args.to_date,
                )
            ]
        )
        return 0

    if args.banksync_command in {"stats", "download"}:
        collected = collect_bank_data(
            client,
            bank_name=args.bank_name,
            from_date=args.from_date,
            to_date=args.to_date,
            scope=args.scope,
        )
        if args.banksync_command == "stats":
            print_json(build_stats(collected))
            return 0
        write_download(args.output, collected, overwrite=args.overwrite)
        print_json(
            {
                "output": str(args.output),
                "stats": build_stats(collected),
            }
        )
        return 0

    if args.banksync_command == "validate":
        report = run_validate(
            client,
            bank_name=args.bank_name,
            from_date=args.from_date,
            to_date=args.to_date,
            scope=args.scope,
        )
        print_json(report)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        return 0

    raise BankSyncError(f"Unknown BankSync command: {args.banksync_command}")


def run_connector(args: argparse.Namespace) -> int:
    if args.connector_name == "banksync":
        return run_banksync(args)
    if args.connector_name == "csv":
        return csvsource_module.run(args)
    if args.connector_name == "stripe":
        return run_stripe(args)
    if args.connector_name == "mercury":
        return run_mercury(args)
    raise ProviderAPIError("Connector", f"Unknown connector: {args.connector_name}")


def run_stripe(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise ProviderAPIError("Stripe", f"Missing Stripe API key. Set {args.api_key_env}.")

    client = StripeClient(
        api_key=api_key,
        base_url=args.base_url,
        timeout=args.timeout,
        stripe_account=args.stripe_account,
    )
    if args.stripe_command == "account":
        print_json(summarize_stripe_account(client.retrieve_account()))
        return 0

    if args.stripe_command in {"download", "pull"}:
        payload = collect_stripe_data(client, from_date=args.from_date, to_date=args.to_date)
        from bookkeeping.connectors.stripe import write_download as write_stripe_download

        write_stripe_download(args.output, payload, overwrite=args.overwrite)
        print_json({"output": str(args.output), "summary": payload["summary"]})
        return 0

    raise ProviderAPIError("Stripe", f"Unknown Stripe command: {args.stripe_command}")


def run_mercury(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise ProviderAPIError("Mercury", f"Missing Mercury API key. Set {args.api_key_env}.")

    client = MercuryClient(api_key=api_key, base_url=args.base_url, timeout=args.timeout)
    if args.mercury_command == "accounts":
        print_json(collect_mercury_accounts(client))
        return 0

    if args.mercury_command in {"download", "pull"}:
        payload = collect_mercury_data(
            client,
            from_date=args.from_date,
            to_date=args.to_date,
            date_field=args.date_field,
            account_ids=args.account_id,
            all_accounts=args.all_accounts,
        )
        from bookkeeping.connectors.mercury import write_download as write_mercury_download

        write_mercury_download(args.output, payload, overwrite=args.overwrite)
        print_json({"output": str(args.output), "summary": payload["summary"]})
        return 0

    raise ProviderAPIError("Mercury", f"Unknown Mercury command: {args.mercury_command}")


def run_validate(
    client: BankSyncClient,
    *,
    bank_name: str,
    from_date: str,
    to_date: str,
    scope: str = "self",
) -> dict[str, Any]:
    """Produce a JSON validation report for BankSync data quality.

    Performs:
      (a) ID stability — fetches the full window twice and diffs transaction-ID sets.
      (b) Historical depth — checks the earliest returned transaction date vs requested from-date.
      (c) Pending vs posted counts and which fields appear on pending records.
      (d) Per-account counts and suspected page-boundary truncation.
    """
    # --- Collect run 1 ---
    run1 = collect_bank_data(
        client,
        bank_name=bank_name,
        from_date=from_date,
        to_date=to_date,
        scope=scope,
    )
    # --- Collect run 2 (repeat fetch for stability comparison) ---
    run2 = collect_bank_data(
        client,
        bank_name=bank_name,
        from_date=from_date,
        to_date=to_date,
        scope=scope,
    )

    # Flatten all transactions from each run
    def _all_txns(collected: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            txn
            for bank in collected["banks"]
            for acct in bank["accounts"]
            for txn in acct["transactions"]
        ]

    txns1 = _all_txns(run1)
    txns2 = _all_txns(run2)

    ids1 = sorted(t["id"] for t in txns1 if t.get("id"))
    ids2 = sorted(t["id"] for t in txns2 if t.get("id"))

    set1 = set(ids1)
    set2 = set(ids2)
    only_in_run1 = sorted(set1 - set2)
    only_in_run2 = sorted(set2 - set1)
    stability = "stable" if not only_in_run1 and not only_in_run2 else "unstable"

    id_stability: dict[str, Any] = {
        "stability": stability,
        "run1_count": len(ids1),
        "run2_count": len(ids2),
        "ids_only_in_run1": only_in_run1,
        "ids_only_in_run2": only_in_run2,
    }

    # --- Historical depth (using run1) ---
    all_dates = sorted(
        str(t["date"])
        for t in txns1
        if t.get("date")
    )
    earliest = all_dates[0] if all_dates else None
    historical_depth: dict[str, Any] = {
        "requested_from": from_date,
        "requested_to": to_date,
        "earliest_returned_date": earliest,
        "latest_returned_date": all_dates[-1] if all_dates else None,
    }

    # --- Pending/posted analysis (using run1) ---
    pending_txns = [t for t in txns1 if t.get("pending")]
    posted_txns = [t for t in txns1 if not t.get("pending")]
    # Collect all fields present on pending records
    pending_fields: list[str] = sorted(
        {key for txn in pending_txns for key, val in txn.items() if val is not None}
    )
    pending_posted: dict[str, Any] = {
        "total_count": len(txns1),
        "posted_count": len(posted_txns),
        "pending_count": len(pending_txns),
        "pending_fields": pending_fields,
        "pending_transaction_id_present": any(
            t.get("pendingTransactionId") is not None for t in txns1
        ),
    }

    # --- Per-account analysis (using run1) ---
    per_account: list[dict[str, Any]] = []
    PAGE_BOUNDARY_THRESHOLD = 100  # common page sizes; flag if count is a round multiple
    for bank in run1["banks"]:
        for acct_entry in bank["accounts"]:
            acct = acct_entry["account"]
            acct_txns = acct_entry["transactions"]
            count = len(acct_txns)
            # Suspect truncation when count > 0 and is an exact multiple of a common page size,
            # suggesting the last page may equal exactly one page worth of results.
            suspected_truncation = count > 0 and count % PAGE_BOUNDARY_THRESHOLD == 0
            per_account.append(
                {
                    "account_id": acct.get("id"),
                    "account_name": acct.get("accountName"),
                    "bank_id": bank["bank"].get("id"),
                    "transaction_count": count,
                    "suspected_truncation": suspected_truncation,
                }
            )

    return {
        "bank_name_filter": bank_name,
        "scope": scope,
        "date_range": {"from": from_date, "to": to_date},
        "id_stability": id_stability,
        "historical_depth": historical_depth,
        "pending_posted": pending_posted,
        "per_account": per_account,
    }


def write_download(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise BankSyncError(f"Output already exists: {path}. Use --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _default_from_date() -> str:
    today = date.today()
    return f"{today.year}-01-01"


def _default_to_date() -> str:
    return date.today().isoformat()


def _date_arg(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


if __name__ == "__main__":
    raise SystemExit(main())
