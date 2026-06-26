"""Amex activity-CSV ingestion connector.

Parses the 12-column Amex activity-export CSV format into the same normalized
transaction contract produced by ``banksync.normalize_transaction``.

Sign convention
---------------
Amex CSV ``Amount`` is **positive for a charge** (expense on the card) and
**negative for a payment/credit to the card**.

The BankSync normalized contract follows the same bank-feed convention used by
BankSync for a credit-card liability account:

- A **charge** (positive CSV amount) increases the card liability.
  It is emitted with a **positive ``amount``** (same sign as CSV), a non-null
  ``debitAmount`` equal to that value, and ``creditAmount = None``.

- A **payment/credit** (negative CSV amount) decreases the card liability.
  It is emitted with a **negative ``amount``** (same sign as CSV), a non-null
  ``creditAmount`` equal to the absolute value, and ``debitAmount = None``.

This matches how BankSync returns posted Amex card transactions when the same
account is connected via bank feed (charges are debits/positive, payments are
credits/negative on the card-feed axis), ensuring U8's comparison can match
rows across the two sources without sign flips.

Boundary semantics
------------------
``boundary_date``  (YYYY-MM-DD, optional) combined with ``side`` restricts
which rows are imported:

- ``side="before"``  → include rows where ``date <= boundary_date``
- ``side="after"``   → include rows where ``date > boundary_date``

The boundary date itself belongs to the **CSV side** when ``side="before"``.
When ``boundary_date`` is None no filtering is applied.

Account mapping
---------------
Mapping lives in ``entity.json`` under ``csv_account_mappings``, keyed by the
CSV file's filename stem (e.g. ``"activity"`` for ``activity.csv``):

.. code-block:: json

    {
      "csv_account_mappings": {
        "activity": {
          "account_name": "Amex Card (CSV)",
          "ledger_account": "Liabilities:CreditCard:Amex-CSV",
          "boundary_date": "2026-03-31",
          "side": "before",
          "confirmed": true
        }
      }
    }

When the mapping key is absent the connector proposes a mapping (derived from
filename and header) and returns it marked ``proposed: true``.  The CLI's
``confirm-mapping`` subcommand writes the confirmed entry back into
``entity.json``.

Fingerprint recipe
------------------
``id = "csv:" + sha256[:16]``

SHA-256 input (pipe-delimited UTF-8 string):

    ``<mapping_account_id>|<iso_date>|<amount>|<norm_desc>|<stripped_reference>``

- ``mapping_account_id``: the ledger_account from the confirmed mapping
  (ensures fingerprints for the same raw row are stable per account assignment).
- ``iso_date``: YYYY-MM-DD string.
- ``amount``: canonical string of the Decimal value (e.g. ``"49.99"``).
- ``norm_desc``: ``normalize_description(description)`` per ``ledger/normalize.py``.
- ``stripped_reference``: Reference column with leading apostrophe stripped.

The five components are joined with ``|`` (pipe) and SHA-256'd.  The first 16
hex characters form the fingerprint suffix.  Deterministic across re-parses.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bookkeeping.ledger.normalize import normalize_description

JsonObject = dict[str, Any]

# ---------------------------------------------------------------------------
# Expected Amex CSV header columns (order-independent presence check)
# ---------------------------------------------------------------------------
_EXPECTED_COLUMNS = frozenset(
    [
        "Date",
        "Receipt",
        "Description",
        "Amount",
        "Extended Details",
        "Appears On Your Statement As",
        "Address",
        "City/State",
        "Zip Code",
        "Country",
        "Reference",
        "Category",
    ]
)

_MONEY_QUANT = Decimal("0.01")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_amex_date(date_str: str) -> str:
    """Convert MM/DD/YYYY to YYYY-MM-DD.  Raises ValueError on bad input."""
    date_str = date_str.strip()
    try:
        dt = datetime.strptime(date_str, "%m/%d/%Y")
    except ValueError as exc:
        raise ValueError(f"Cannot parse Amex date {date_str!r}: expected MM/DD/YYYY") from exc
    return dt.strftime("%Y-%m-%d")


def _strip_reference(ref: str) -> str:
    """Strip a leading apostrophe from the Reference field (Amex CSV artifact)."""
    ref = ref.strip()
    if ref.startswith("'"):
        ref = ref[1:]
    return ref


def _money_decimal(value: Any) -> Decimal:
    """Parse a money value to a quantized Decimal."""
    try:
        return Decimal(str(value)).quantize(_MONEY_QUANT)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid money value: {value!r}") from exc


def _fingerprint(
    account_id: str,
    iso_date: str,
    amount: Decimal,
    description: str,
    stripped_ref: str,
) -> str:
    """Compute the deterministic 16-hex-char fingerprint for a CSV row.

    Recipe: sha256(account_id|iso_date|amount_str|norm_desc|stripped_ref)[:16]
    """
    norm_desc = normalize_description(description)
    amount_str = format(amount, "f")
    payload = f"{account_id}|{iso_date}|{amount_str}|{norm_desc}|{stripped_ref}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"csv:{digest[:16]}"


def _validate_header(fieldnames: list[str], path: Path) -> None:
    """Raise a plain-English ValueError when required columns are missing."""
    present = set(fieldnames)
    missing = _EXPECTED_COLUMNS - present
    if missing:
        missing_sorted = sorted(missing)
        raise ValueError(
            f"Unrecognized CSV format in '{path.name}'. "
            f"Missing column(s): {', '.join(missing_sorted)}. "
            f"Expected all of: {', '.join(sorted(_EXPECTED_COLUMNS))}."
        )


# ---------------------------------------------------------------------------
# Public parse function
# ---------------------------------------------------------------------------

def parse_amex_csv(
    path: str | Path,
    *,
    account_id: str = "",
    account_name: str = "",
    ledger_account: str = "",
    bank_id: str = "amex-csv",
    currency: str = "USD",
    boundary_date: str | None = None,
    side: str = "before",
) -> tuple[list[JsonObject], int]:
    """Parse an Amex activity CSV into the normalized transaction contract.

    Returns ``(transactions, excluded_count)`` where ``excluded_count`` is the
    number of rows that were filtered out by the boundary rule.

    Sign convention: see module docstring.

    Parameters
    ----------
    path:
        Path to the CSV file.
    account_id:
        Value to use for ``accountId`` in each transaction (typically the
        confirmed mapping ``ledger_account``).
    account_name:
        Human-readable account name (e.g. ``"Amex Card (CSV)"``).
    ledger_account:
        Ledger account string used in fingerprint computation.  Falls back to
        ``account_id`` when empty.
    bank_id:
        Value for the ``bankId`` field; defaults to ``"amex-csv"``.
    currency:
        ISO currency code; defaults to ``"USD"``.
    boundary_date:
        Optional YYYY-MM-DD boundary for import filtering.
    side:
        ``"before"`` (include date <= boundary) or ``"after"`` (include date >
        boundary).  Ignored when ``boundary_date`` is None.
    """
    path = Path(path)
    fp_account = ledger_account or account_id

    transactions: list[JsonObject] = []
    excluded = 0

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"CSV file '{path.name}' appears to be empty.")
        _validate_header(list(reader.fieldnames), path)

        for row in reader:
            iso_date = _parse_amex_date(row["Date"])
            raw_amount_str = row["Amount"].strip()
            amount = _money_decimal(raw_amount_str)
            raw_ref = row.get("Reference") or ""
            stripped_ref = _strip_reference(raw_ref)
            description = (row.get("Description") or "").strip()
            original_description = (row.get("Appears On Your Statement As") or description).strip()
            category = (row.get("Category") or "").strip() or None

            # Boundary filtering
            if boundary_date is not None:
                if side == "before":
                    if iso_date > boundary_date:
                        excluded += 1
                        continue
                elif side == "after":
                    if iso_date <= boundary_date:
                        excluded += 1
                        continue

            # Sign convention — feed axis, verified against the live BankSync
            # Amex feed on 2026-06-12: bank feeds deliver card CHARGES as
            # NEGATIVE amounts (liability grows) and payments/credits as
            # POSITIVE. The Amex consumer CSV is the opposite (charges
            # positive), so the normalized `amount` is the NEGATED CSV value.
            # The importer posts `amount` directly to the liability account,
            # so a charge must arrive negative to grow the card balance.
            feed_amount = -amount
            amount_str = format(feed_amount, "f")
            if amount >= Decimal("0"):
                # Charge: debit on the card's expense axis.
                debit_amount = format(amount, "f")
                credit_amount = None
            else:
                # Payment/credit to the card.
                debit_amount = None
                credit_amount = format(abs(amount), "f")

            txn_id = _fingerprint(fp_account, iso_date, feed_amount, description, stripped_ref)

            txn: JsonObject = {
                "id": txn_id,
                "date": iso_date,
                "description": description,
                "originalDescription": original_description,
                "amount": amount_str,
                "creditAmount": credit_amount,
                "debitAmount": debit_amount,
                "currency": currency,
                "category": category,
                "type": "credit_card",
                "reference": stripped_ref or None,
                "pending": False,
                "pendingTransactionId": None,
                "accountId": account_id,
                "accountName": account_name,
                "accountNumberLast4": None,
                "bankId": bank_id,
                "bank": None,
            }
            transactions.append(txn)

    return transactions, excluded


# ---------------------------------------------------------------------------
# Account mapping helpers
# ---------------------------------------------------------------------------

_PROPOSED_ACCOUNT_NAME = "Amex Card (CSV)"
_PROPOSED_LEDGER_ACCOUNT = "Liabilities:CreditCard:Amex-CSV"


def _mapping_key(csv_path: Path) -> str:
    """Return the stable mapping key for a CSV file (its filename stem)."""
    return csv_path.stem


def _is_amex_header(csv_path: Path) -> bool:
    """Return True when the file looks like an Amex activity CSV by header sniff."""
    try:
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                return False
            present = set(reader.fieldnames)
            return _EXPECTED_COLUMNS.issubset(present)
    except OSError:
        return False


def _propose_mapping(csv_path: Path) -> JsonObject:
    """Build a proposal mapping from filename/header heuristics."""
    stem = csv_path.stem
    if "amex" in stem.lower() or _is_amex_header(csv_path):
        account_name = _PROPOSED_ACCOUNT_NAME
        ledger_account = _PROPOSED_LEDGER_ACCOUNT
    else:
        account_name = f"{stem.title()} Card (CSV)"
        ledger_account = f"Liabilities:CreditCard:{stem.title()}-CSV"
    return {
        "account_name": account_name,
        "ledger_account": ledger_account,
        "boundary_date": None,
        "side": "before",
        "confirmed": False,
        "proposed": True,
    }


def resolve_mapping(
    entity_config: dict[str, Any],
    csv_path: str | Path,
    *,
    propose_only: bool = False,
) -> JsonObject:
    """Return the account mapping for *csv_path* from entity config.

    When a confirmed mapping exists, returns it (``proposed`` key absent or
    False).  When absent, proposes a mapping marked ``proposed: True``.

    The caller (CLI) must refuse import when ``proposed: True`` is returned,
    printing the proposal and instructions to confirm.

    Parameters
    ----------
    entity_config:
        The parsed ``entity.json`` dict (``Entity.entity_config``).
    csv_path:
        Path to the CSV file.
    propose_only:
        When True, always return the auto-proposal regardless of entity config
        (useful for the ``propose-mapping`` CLI subcommand).
    """
    csv_path = Path(csv_path)
    key = _mapping_key(csv_path)

    mappings: dict[str, Any] = entity_config.get("csv_account_mappings", {})

    if not propose_only and key in mappings:
        mapping = dict(mappings[key])
        mapping.setdefault("confirmed", False)
        return mapping

    return _propose_mapping(csv_path)


def write_confirmed_mapping(
    entity_path: str | Path,
    csv_path: str | Path,
    *,
    account_name: str,
    ledger_account: str,
    boundary_date: str | None = None,
    side: str = "before",
) -> None:
    """Persist a confirmed mapping into entity.json (atomic write).

    Reads the existing ``entity.json``, inserts/updates the mapping for
    ``csv_path.stem``, and writes back via a sibling temp file + rename.
    """
    entity_path = Path(entity_path).resolve()
    csv_path = Path(csv_path)
    entity_json_path = entity_path / "entity.json"

    if not entity_json_path.exists():
        raise FileNotFoundError(
            f"No entity.json at '{entity_json_path}'. "
            "Run 'books entity init <path>' first."
        )

    entity_config: dict[str, Any] = json.loads(entity_json_path.read_text(encoding="utf-8"))

    if "csv_account_mappings" not in entity_config:
        entity_config["csv_account_mappings"] = {}

    key = _mapping_key(csv_path)
    entity_config["csv_account_mappings"][key] = {
        "account_name": account_name,
        "ledger_account": ledger_account,
        "boundary_date": boundary_date,
        "side": side,
        "confirmed": True,
    }

    tmp_path = entity_json_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(entity_config, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, entity_json_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# High-level import orchestration (used by CLI parse subcommand)
# ---------------------------------------------------------------------------

def import_csv(
    entity_config: dict[str, Any],
    csv_path: str | Path,
    *,
    currency: str = "USD",
) -> JsonObject:
    """Parse a CSV file using the confirmed entity mapping.

    Returns a dict with keys:
    - ``transactions``: list of normalized transaction dicts
    - ``excluded_count``: rows filtered by boundary rule
    - ``mapping``: the mapping used
    - ``proposed``: True when import was refused (no confirmed mapping)
    - ``proposal``: the proposed mapping (only present when ``proposed: True``)

    Callers must check ``result["proposed"]``; when True no transactions are
    returned and the caller should print the proposal and instructions.
    """
    csv_path = Path(csv_path)
    mapping = resolve_mapping(entity_config, csv_path)

    if mapping.get("proposed") or not mapping.get("confirmed"):
        return {
            "transactions": [],
            "excluded_count": 0,
            "mapping": None,
            "proposed": True,
            "proposal": mapping,
        }

    transactions, excluded = parse_amex_csv(
        csv_path,
        account_id=mapping["ledger_account"],
        account_name=mapping["account_name"],
        ledger_account=mapping["ledger_account"],
        currency=currency,
        boundary_date=mapping.get("boundary_date"),
        side=mapping.get("side", "before"),
    )

    return {
        "transactions": transactions,
        "excluded_count": excluded,
        "mapping": mapping,
        "proposed": False,
        "proposal": None,
    }


# ---------------------------------------------------------------------------
# CLI surface (wired into cli.py by orchestrator)
# ---------------------------------------------------------------------------

def add_parser(subparsers: Any) -> None:
    """Register the ``csv`` subcommand onto *subparsers*."""
    csv_parser = subparsers.add_parser("csv", help="Amex activity-CSV ingestion")
    csv_sub = csv_parser.add_subparsers(dest="csv_command", required=True)

    # --- inspect ---
    inspect_p = csv_sub.add_parser(
        "inspect",
        help="Inspect a CSV file: show header, row count, date range, amount totals",
    )
    inspect_p.add_argument("file", type=Path, help="Path to the CSV file")

    # --- propose-mapping ---
    propose_p = csv_sub.add_parser(
        "propose-mapping",
        help="Print an auto-proposed account mapping for a CSV file (no changes written)",
    )
    propose_p.add_argument("file", type=Path, help="Path to the CSV file")
    propose_p.add_argument(
        "--entity", type=Path, required=True, help="Path to the entity directory"
    )

    # --- confirm-mapping ---
    confirm_p = csv_sub.add_parser(
        "confirm-mapping",
        help="Confirm (write) an account mapping for a CSV file into entity.json",
    )
    confirm_p.add_argument("file", type=Path, help="Path to the CSV file")
    confirm_p.add_argument(
        "--entity", type=Path, required=True, help="Path to the entity directory"
    )
    confirm_p.add_argument(
        "--ledger-account",
        required=True,
        help="Ledger account to assign (e.g. Liabilities:CreditCard:Amex-CSV)",
    )
    confirm_p.add_argument(
        "--account-name",
        default="",
        help="Human-readable account name (defaults to auto-proposal)",
    )
    confirm_p.add_argument(
        "--boundary",
        default=None,
        help="Boundary date YYYY-MM-DD for overlap with BankSync history",
    )
    confirm_p.add_argument(
        "--side",
        choices=["before", "after"],
        default="before",
        help=(
            "Which side of the boundary this CSV owns. "
            "'before' → include dates <= boundary; 'after' → include dates > boundary"
        ),
    )

    # --- parse ---
    parse_p = csv_sub.add_parser(
        "parse",
        help="Parse a CSV file using the confirmed entity mapping; print normalized transactions",
    )
    parse_p.add_argument("file", type=Path, help="Path to the CSV file")
    parse_p.add_argument(
        "--entity", type=Path, required=True, help="Path to the entity directory"
    )
    parse_p.add_argument(
        "--output", type=Path, default=None, help="Write JSON output to this file"
    )


def run(args: Any) -> int:
    """Execute the csv subcommand described by *args*."""
    cmd = args.csv_command

    if cmd == "inspect":
        return _run_inspect(args)
    if cmd == "propose-mapping":
        return _run_propose_mapping(args)
    if cmd == "confirm-mapping":
        return _run_confirm_mapping(args)
    if cmd == "parse":
        return _run_parse(args)

    print(f"Unknown csv command: {cmd}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# CLI subcommand implementations
# ---------------------------------------------------------------------------

def _run_inspect(args: Any) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                print("Error: CSV file appears to be empty.", file=sys.stderr)
                return 1
            _validate_header(list(reader.fieldnames), path)
            rows = list(reader)

        amounts = []
        dates = []
        for row in rows:
            try:
                amounts.append(_money_decimal(row["Amount"]))
                dates.append(_parse_amex_date(row["Date"]))
            except (ValueError, KeyError):
                pass

        print(f"File:      {path}")
        print(f"Columns:   {', '.join(reader.fieldnames or [])}")  # type: ignore[union-attr]
        print(f"Rows:      {len(rows)}")
        print(f"Date range: {min(dates) if dates else 'n/a'} – {max(dates) if dates else 'n/a'}")
        if amounts:
            total = sum(amounts, Decimal("0"))
            charges = sum(a for a in amounts if a > 0)
            payments = sum(a for a in amounts if a < 0)
            print(f"Amount total (net):  {format(total, 'f')}")
            print(f"Charges (+):         {format(charges, 'f')}")
            print(f"Payments/Credits (-): {format(payments, 'f')}")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _run_propose_mapping(args: Any) -> int:
    from bookkeeping.entity import load_entity  # lazy import — entity may be absent

    path = Path(args.file)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1

    try:
        entity = load_entity(args.entity)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    proposal = resolve_mapping(entity.entity_config, path, propose_only=True)
    print("Proposed mapping (not yet confirmed):")
    print(json.dumps(proposal, indent=2))
    print()
    print(
        "To confirm, run:\n"
        f"  books csv confirm-mapping {path} "
        f"--entity {args.entity} "
        f"--ledger-account {proposal['ledger_account']!r}"
    )
    return 0


def _run_confirm_mapping(args: Any) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1

    # Derive account_name if not supplied
    account_name = args.account_name
    if not account_name:
        proposal = _propose_mapping(path)
        account_name = proposal["account_name"]

    try:
        write_confirmed_mapping(
            args.entity,
            path,
            account_name=account_name,
            ledger_account=args.ledger_account,
            boundary_date=args.boundary,
            side=args.side,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Confirmed mapping written for '{path.name}' → {args.entity}/entity.json")
    print(f"  account_name:   {account_name}")
    print(f"  ledger_account: {args.ledger_account}")
    if args.boundary:
        print(f"  boundary_date:  {args.boundary} (side={args.side})")
    return 0


def _run_parse(args: Any) -> int:
    from bookkeeping.entity import load_entity  # lazy import

    path = Path(args.file)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1

    try:
        entity = load_entity(args.entity)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        result = import_csv(entity.entity_config, path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if result["proposed"]:
        print("Import refused: no confirmed account mapping found for this file.")
        print()
        print("Proposed mapping:")
        print(json.dumps(result["proposal"], indent=2))
        print()
        print(
            "To confirm this mapping, run:\n"
            f"  books csv confirm-mapping {path} "
            f"--entity {args.entity} "
            f"--ledger-account {result['proposal']['ledger_account']!r}"
        )
        return 1

    txns = result["transactions"]
    excluded = result["excluded_count"]
    output = {
        "transactions": txns,
        "count": len(txns),
        "excluded_count": excluded,
        "mapping": result["mapping"],
    }

    json_str = json.dumps(output, indent=2)
    if args.output:
        Path(args.output).write_text(json_str + "\n", encoding="utf-8")
        print(f"Wrote {len(txns)} transaction(s) to {args.output}")
    else:
        print(json_str)

    if excluded:
        print(f"\nNote: {excluded} row(s) excluded by boundary rule.", file=sys.stderr)

    return 0
