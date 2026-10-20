"""`books ingest`: post a connector's normalized transactions into the ledger.

This is the stable contract any connector targets, whether it ships with
Slashbooks (BankSync, Stripe, Mercury) or is a company's own custom connector
under ``ingestion/custom/``. The transactions go through the same deterministic
importer the rest of the system uses, so behavior is identical regardless of
where the data came from:

- Trusted counterparties auto-post; unknown ones go to the review queue.
- Re-ingesting the same data is idempotent (duplicates are skipped by ``id``).
- Every change is recorded in the audit log.

Normalized transaction shape (one JSON object per transaction)::

    {
      "id": "stable-unique-id",     # required: dedup / supersession key
      "date": "2026-01-15",         # required: ISO-8601 date
      "description": "ACME CORP",   # counterparty / memo text
      "amount": "-42.50",           # signed string; or creditAmount/debitAmount
      "accountId": "acct_checking", # mapped via entity bank_account_mappings
      "accountName": "Checking",    # optional fallback when accountId is unmapped
      "accountType": "checking",    # optional fallback: checking|savings|credit_card
      "pending": false              # optional
    }

The input file is either a JSON array of these objects, or an object with a
``"transactions"`` array.

Feed windows for source coverage
--------------------------------
After a successful local import, ``books ingest`` records ``feed_sync``
evidence for ``books coverage check`` (see ``coverage.py``):

- A BankSync download (``books connector banksync download``) gives one
  window per mapped account with posted transactions. The window starts on
  the later of the requested start and the first posted transaction, because
  a feed can return less history than requested. It ends on the earlier of the
  requested end and the import date, and before the first pending
  transaction, because a pending item can post on a later date.
- Any input object can carry ``"coverage_windows"``: a list of
  ``{"account": "Assets:...", "from": "YYYY-MM-DD", "to": "YYYY-MM-DD",
  "source": "label"}``. ``accountId`` may replace ``account`` when it is in
  ``bank_account_mappings``. A connector adds a window only for dates that the
  provider returned in full.

A hosted import (a prepared snapshot) records no windows here; the host writes
the ``feed_sync`` records itself. A host that reads a provider in chunks passes
``--no-feed-window`` (``record_feed_window=False``) so that a chunk that stops
early claims no days. BankSync marks an incomplete read with ``meta.partial``;
the download then carries ``"partial": true`` for that account, and the engine
writes no window for it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .entity import flat_bank_account_mappings, load_entity
from . import queue as queue_module
from .ledger.importer import import_transactions
from .source_ids import CONNECTED_FEED_PROVIDERS, namespaced_feed_id


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "ingest",
        help="Post a connector's normalized transactions (JSON) into the ledger",
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Path to the normalized transactions JSON file",
    )
    parser.add_argument(
        "--entity",
        required=True,
        type=Path,
        help="Company books directory",
    )
    parser.add_argument(
        "--source",
        default="ingest",
        help="Short label for this connector/import session (e.g. mercury-custom)",
    )
    parser.add_argument(
        "--intake-only", action="store_true",
        help="Stage transactions for explicit review without learned auto-posting",
    )
    parser.add_argument(
        "--no-feed-window", dest="record_feed_window", action="store_false",
        help="Write no feed_sync coverage windows for this ingest. A host that reads a provider "
             "in chunks uses this and writes its own windows",
    )
    parser.add_argument(
        "--feed-provider", default=None, metavar="NAME",
        help="Name the bank data provider of a connected feed that is not a built-in --source "
             "(for example plaid). New source IDs become NAME:<id>",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Override the import session id (defaults to <source>-<timestamp>)",
    )


def load_transactions(path: Path) -> list[dict]:
    """Read and shape-check a normalized transactions file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return _transactions_from_data(data)


def _transactions_from_data(data: Any) -> list[dict]:
    """Shape an already verified JSON value into normalized transactions."""
    if isinstance(data, list):
        txns = data
    elif isinstance(data, dict) and isinstance(data.get("transactions"), list):
        txns = data["transactions"]
    elif isinstance(data, dict) and data.get("source") == "banksync" and isinstance(data.get("banks"), list):
        txns = []
        for bank in data["banks"]:
            if not isinstance(bank, dict):
                continue
            for account_entry in bank.get("accounts", []):
                if not isinstance(account_entry, dict):
                    continue
                account = account_entry.get("account")
                if not isinstance(account, dict):
                    continue
                for txn in account_entry.get("transactions", []):
                    if not isinstance(txn, dict):
                        continue
                    # Old download files do not always repeat account identity on
                    # each transaction.  Keep the stable provider ID from the
                    # account envelope so mapping and deduplication remain safe.
                    txns.append({
                        "accountId": account.get("id"),
                        "accountName": account.get("accountName"),
                        "accountType": account.get("accountType"),
                        "bankId": account.get("bankId"),
                        **txn,
                    })
    else:
        raise ValueError(
            "Input must be a JSON array of transactions, or an object with a "
            "'transactions' array."
        )
    if not all(isinstance(item, dict) for item in txns):
        raise ValueError("Every transaction must be a JSON object.")
    return txns


def _require_banksync_mappings(entity: Any, txns: list[dict], source: str) -> None:
    """Refuse a BankSync close before every feed account has an ID mapping.

    Display-name fallback is useful for ad-hoc CSV imports, but unsafe for a
    connected feed: it silently opens a second cash account when a name changes
    or an opening-balance account already exists.
    """
    if source != "banksync":
        return
    mappings = flat_bank_account_mappings(entity.entity_config)
    missing = sorted({str(txn.get("accountId") or "") for txn in txns if txn.get("accountId") and not mappings.get(str(txn.get("accountId")))})
    if missing:
        joined = ", ".join(missing)
        raise ValueError(
            "BankSync account mapping required before ingesting connected activity "
            f"for: {joined}. Record each stable feed ID with 'books entity bank-account-map'."
        )


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def coverage_windows(data: Any, entity: Any, *, today: date,
                     record_feed_window: bool = True) -> list[dict[str, Any]]:
    """Feed windows (``feed_sync`` evidence) that an input file supports.

    Raises ``ValueError`` for an explicit window that is not valid, so the
    import stops before it changes the ledger. With ``record_feed_window=False``
    the engine makes no window at all (``books ingest --no-feed-window``), and
    it does not read ``coverage_windows`` from the input.
    """
    if not isinstance(data, dict) or not record_feed_window:
        return []
    mappings = flat_bank_account_mappings(entity.entity_config)
    windows: list[dict[str, Any]] = []
    if data.get("source") == "banksync" and isinstance(data.get("banks"), list):
        requested = data.get("dateRange") if isinstance(data.get("dateRange"), dict) else {}
        first, last = _parse_day(requested.get("from")), _parse_day(requested.get("to"))
        if first and last:
            for bank in data["banks"]:
                for entry in (bank.get("accounts") or []) if isinstance(bank, dict) else []:
                    account = entry.get("account") if isinstance(entry, dict) else None
                    if not isinstance(account, dict):
                        continue
                    if entry.get("partial") is True:
                        # BankSync marked this read incomplete: any window would claim
                        # days that the provider did not return in full.
                        continue
                    ledger_account = mappings.get(str(account.get("id") or ""))
                    if not isinstance(ledger_account, str):
                        continue
                    txns = [t for t in entry.get("transactions") or [] if isinstance(t, dict)]
                    posted = sorted(d for t in txns if not t.get("pending") and (d := _parse_day(t.get("date"))))
                    pending = sorted(d for t in txns if t.get("pending") and (d := _parse_day(t.get("date"))))
                    if not posted:
                        continue
                    start = max(first, posted[0])
                    end = min(last, today)
                    if pending:
                        end = min(end, pending[0] - timedelta(days=1))
                    if start <= end:
                        name = str(account.get("accountName") or "account")
                        last4 = account.get("accountNumberLast4")
                        windows.append({"account": ledger_account, "from": start, "to": end,
                                        "source": f"BankSync {name}" + (f" ending {last4}" if last4 else "")})
    for index, item in enumerate(data.get("coverage_windows") or []):
        if not isinstance(item, dict):
            raise ValueError(f"coverage_windows[{index}] must be an object.")
        account = item.get("account") or mappings.get(str(item.get("accountId") or ""))
        start, end = _parse_day(item.get("from")), _parse_day(item.get("to"))
        if not isinstance(account, str) or not account.startswith(("Assets:", "Liabilities:")):
            raise ValueError(f"coverage_windows[{index}] needs an Assets: or Liabilities: account "
                             "or a mapped accountId.")
        if start is None or end is None or start > end:
            raise ValueError(f"coverage_windows[{index}] needs from and to dates (YYYY-MM-DD), in order.")
        windows.append({"account": account, "from": start, "to": end, "source": str(item.get("source") or "")})
    return windows


def _check_window_accounts(entity: Any, windows: list[dict[str, Any]]) -> None:
    from .ledger.store import LedgerStore, default_store_path

    store_path = default_store_path(entity.path)
    if not windows or not store_path.is_file():
        return
    names = LedgerStore(store_path).load_account_names()
    missing = sorted({w["account"] for w in windows if w["account"] not in names})
    if missing:
        raise ValueError(f"Coverage window account(s) not in the account catalog: {', '.join(missing)}")


def _record_windows(entity: Any, windows: list[dict[str, Any]], input_path: Path) -> int:
    from . import coverage

    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    try:
        ref = input_path.resolve().relative_to(entity.path.resolve()).as_posix()
    except ValueError:
        ref = input_path.name
    for window in windows:
        coverage.record_feed_window(
            entity.path, account=window["account"], start=window["from"], end=window["to"],
            source=window["source"], evidence_ref=ref, evidence_sha256=digest,
            extra={"writer": "books ingest"})
    return len(windows)


def _is_banksync_download(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("source") == "banksync" and isinstance(data.get("banks"), list)


_PROVIDER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def feed_provider(source: str, explicit: str | None, is_banksync: bool) -> str | None:
    """The connected-feed provider of this ingest, or ``None`` for a generic one.

    ``--feed-provider`` names it. A BankSync download, ``--source banksync`` and
    the other built-in provider names (``CONNECTED_FEED_PROVIDERS``) imply it.
    """
    if explicit:
        name = explicit.strip().lower()
        if not _PROVIDER_NAME.fullmatch(name):
            raise ValueError("--feed-provider must be lowercase letters, digits, '-' or '_'.")
        return name
    if is_banksync:
        return "banksync"
    return source if source in CONNECTED_FEED_PROVIDERS else None


def namespace_feed_ids(txns: list[dict], provider: str) -> list[dict]:
    """Copies of *txns* with ``<provider>:<id>`` source IDs.

    A pending link (``pendingTransactionId``) names another feed row, so it gets
    the same prefix. The input rows are not changed.
    """
    shaped: list[dict] = []
    for txn in txns:
        row = dict(txn)
        for key in ("id", "pendingTransactionId"):
            if row.get(key):
                row[key] = namespaced_feed_id(provider, row[key])
        shaped.append(row)
    return shaped


def run(args: argparse.Namespace) -> int:
    entity = load_entity(args.entity)
    try:
        from .preparation_runtime import prepared_ingest_transactions
        prepared = prepared_ingest_transactions(args.input)
        windows: list[dict[str, Any]] = []
        if prepared is None:
            txns = load_transactions(args.input)
            is_banksync = args.source == "banksync" or _is_banksync_download(args.input)
            windows = coverage_windows(json.loads(args.input.read_text(encoding="utf-8")), entity,
                                       today=datetime.now(tz=timezone.utc).date(),
                                       record_feed_window=getattr(args, "record_feed_window", True))
            _check_window_accounts(entity, windows)
        else:
            txns, verified_banksync = prepared
            is_banksync = args.source == "banksync" or verified_banksync
        _require_banksync_mappings(
            entity, txns, "banksync" if is_banksync else args.source
        )
        provider = feed_provider(args.source, getattr(args, "feed_provider", None), is_banksync)
        # A host-prepared snapshot is verified row by row against the host's own
        # fingerprints, so the engine must not change its IDs. The host writes
        # ``<provider>:<id>`` into the snapshot itself.
        if provider and prepared is None:
            txns = namespace_feed_ids(txns, provider)

        session_id = args.session_id or (
            f"{args.source}-{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        )
        categorizer = queue_module.make_categorizer(entity)
        result = import_transactions(
            entity,
            txns,
            session_id=session_id,
            categorizer=categorizer,
            ts=None,
            legacy_duplicate_guard=is_banksync,
            intake_only=getattr(args, "intake_only", False),
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Ingested {len(txns)} transaction(s) from source '{args.source}' "
        f"(session {session_id}):"
    )
    print(f"  posted to ledger:    {result.new_entries}")
    print(f"  queued for review:   {result.pending_categorization}")
    print(f"  duplicates skipped:  {result.skipped_duplicate}")
    if result.duplicate_candidates:
        print(f"  duplicate candidates:{result.duplicate_candidates}")
    if result.late_arrivals:
        print(f"  late arrivals:        {result.late_arrivals}")
    if result.errors:
        print(f"  errors:               {len(result.errors)}")
        for err in result.errors[:20]:
            print(f"    - {err}")
        return 1
    if windows:
        print(f"  feed windows:         {_record_windows(entity, windows, args.input)} recorded for coverage")
    return 0
