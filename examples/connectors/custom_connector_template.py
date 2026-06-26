#!/usr/bin/env python3
"""Template for a custom Slashbooks connector.

Slashbooks ships connectors for BankSync, Stripe, and Mercury. When you need a
source it doesn't cover yet, your agent can build a small connector like this
one. Copy it into your company's books folder under ``ingestion/custom/``, adapt
the fetch step to your provider's API, then run:

    python ingestion/custom/my_connector.py out.json
    books ingest out.json --entity . --source my-connector

A connector only *fetches* data and writes it in Slashbooks' normalized format.
The deterministic importer does the accounting: trusted counterparties auto-post,
unknown ones go to the review queue, and re-running is idempotent (keyed on
``id``). Connectors are read-only; nothing is ever sent back to the provider.

Normalized transaction shape (one object per transaction)::

    {
      "id":          "stable-unique-id",   # required: dedup key
      "date":        "2026-01-15",         # required: ISO-8601 date
      "description": "ACME CORP",          # counterparty / memo
      "amount":      "-42.50",             # signed string; negative = money out
      "accountId":   "acct_checking",      # mapped via entity bank_account_mappings
      "accountName": "Checking",           # optional fallback if accountId unmapped
      "accountType": "checking",           # optional: checking|savings|credit_card
      "pending":     false                 # optional
    }
"""

from __future__ import annotations

import json
import sys


def fetch_rows() -> list[dict]:
    """Replace this with a call to your provider's API.

    Read credentials from the company ``.env`` via the environment, for example
    ``api_key = os.environ["MYPROVIDER_API_KEY"]``, and return the raw rows the
    API gives you. The sample rows below let the template run as-is; delete them
    once your fetch works.
    """
    return [
        {"ref": "100", "when": "2026-01-04", "who": "Acme Hosting", "cents": -4200},
        {"ref": "101", "when": "2026-01-09", "who": "Client Payment", "cents": 250000},
    ]


def to_normalized(row: dict) -> dict:
    """Map one raw provider row to the normalized transaction shape."""
    return {
        "id": str(row["ref"]),
        "date": row["when"],
        "description": row["who"],
        "amount": f"{row['cents'] / 100:.2f}",
        "accountId": "acct_custom",
        "accountName": "Custom Account",
        "accountType": "checking",
        "pending": False,
    }


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "custom-connector.json"
    txns = [to_normalized(row) for row in fetch_rows()]
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({"source": "custom", "transactions": txns}, handle, indent=2)
    print(f"wrote {len(txns)} normalized transactions to {out}")
    print(f"next: books ingest {out} --entity . --source custom")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
