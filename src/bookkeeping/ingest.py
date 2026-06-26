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
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .entity import load_entity
from . import queue as queue_module
from .ledger.importer import import_transactions


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
        "--session-id",
        default=None,
        help="Override the import session id (defaults to <source>-<timestamp>)",
    )


def load_transactions(path: Path) -> list[dict]:
    """Read and shape-check a normalized transactions file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        txns = data
    elif isinstance(data, dict) and isinstance(data.get("transactions"), list):
        txns = data["transactions"]
    else:
        raise ValueError(
            "Input must be a JSON array of transactions, or an object with a "
            "'transactions' array."
        )
    if not all(isinstance(item, dict) for item in txns):
        raise ValueError("Every transaction must be a JSON object.")
    return txns


def run(args: argparse.Namespace) -> int:
    entity = load_entity(args.entity)
    txns = load_transactions(args.input)

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
    )

    print(
        f"Ingested {len(txns)} transaction(s) from source '{args.source}' "
        f"(session {session_id}):"
    )
    print(f"  posted to ledger:    {result.new_entries}")
    print(f"  queued for review:   {result.pending_categorization}")
    print(f"  duplicates skipped:  {result.skipped_duplicate}")
    if result.late_arrivals:
        print(f"  late arrivals:        {result.late_arrivals}")
    if result.errors:
        print(f"  errors:               {len(result.errors)}")
        for err in result.errors[:20]:
            print(f"    - {err}")
        return 1
    return 0
