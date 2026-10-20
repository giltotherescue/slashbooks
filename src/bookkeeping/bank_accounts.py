"""Provider-neutral bank account identity: find the ledger account for a bank.

A bank data provider (BankSync, Plaid, Stripe Financial Connections, another)
gives each connected account its own ID. When a company switches provider, the
same bank account arrives with a new ID. This module compares the new account
with the accounts already mapped in ``entity.json`` and proposes the existing
ledger account when the institution, last four digits, account type and
currency all match.

A proposal is for a person to confirm. This module never changes a mapping.
The person confirms with ``books entity bank-account-map``; the proposal prints
that command.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .entity import BANK_IDENTITY_FIELDS, bank_mapping_account, bank_mapping_details, load_entity

# Account types that providers spell differently. Equal after this table means equal.
_TYPE_ALIASES = {
    "credit": "credit_card", "creditcard": "credit_card", "card": "credit_card",
    "checking": "checking", "depository_checking": "checking", "current": "checking",
    "savings": "savings", "saving": "savings", "depository_savings": "savings",
}


def _name_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _type_key(value: Any) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _TYPE_ALIASES.get(key, key)


def _last4_key(value: Any) -> str:
    return str(value or "").strip().upper()


def _currency_key(value: Any) -> str:
    return str(value or "").strip().upper()


def _institution_match(known: dict[str, Any], new: dict[str, Any]) -> str | None:
    """How the institution matched: ``institution_id``, ``institution_name`` or ``None``."""
    known_id, new_id = str(known.get("institution_id") or ""), str(new.get("institution_id") or "")
    if known_id and new_id and known_id == new_id:
        return "institution_id"
    known_name, new_name = _name_key(known.get("institution_name")), _name_key(new.get("institution_name"))
    if known_name and new_name and known_name == new_name:
        return "institution_name"
    return None


def propose_account_match(entity: Any, new_account: dict[str, Any]) -> dict[str, Any]:
    """Propose the existing ledger account for a provider account. Read-only.

    ``new_account`` carries the new account's identity: ``provider``,
    ``provider_account_id``, ``institution_id``, ``institution_name``,
    ``last4``, ``account_type`` and ``currency``. The result ``status`` is:

    ``already_mapped``
        The provider account ID is already in ``bank_account_mappings``.
    ``proposal``
        Exactly one ledger account matches on institution, last four digits,
        account type and currency. A person must confirm it.
    ``ambiguous``
        More than one ledger account matches. Nothing is proposed.
    ``no_match``
        No mapped account matches, or the new account lacks an identity field.
    """
    result: dict[str, Any] = {
        "schema_version": 1, "kind": "bank_account_match",
        "provider": new_account.get("provider"),
        "provider_account_id": new_account.get("provider_account_id"),
    }
    mappings = entity.entity_config.get("bank_account_mappings")
    mappings = mappings if isinstance(mappings, dict) else {}
    account_id = str(new_account.get("provider_account_id") or "")
    if account_id and account_id in mappings:
        result.update(status="already_mapped", ledger_account=bank_mapping_account(mappings[account_id]),
                      candidates=[], reason="This provider account ID is already mapped.")
        return result

    missing = [name for name, ok in (
        ("institution", bool(new_account.get("institution_id") or new_account.get("institution_name"))),
        ("last4", bool(_last4_key(new_account.get("last4")))),
        ("account_type", bool(_type_key(new_account.get("account_type")))),
        ("currency", bool(_currency_key(new_account.get("currency")))),
    ) if not ok]
    if missing:
        result.update(status="no_match", ledger_account=None, candidates=[],
                      reason="The new account lacks: " + ", ".join(missing) + ". A match needs all four.")
        return result

    by_ledger: dict[str, dict[str, Any]] = {}
    for key, value in mappings.items():
        details = bank_mapping_details(value)
        ledger = bank_mapping_account(value)
        if not ledger or not details:
            continue  # A flat mapping has no identity to compare.
        institution = _institution_match(details, new_account)
        if (institution is None
                or _last4_key(details.get("last4")) != _last4_key(new_account.get("last4"))
                or _type_key(details.get("account_type")) != _type_key(new_account.get("account_type"))
                or _currency_key(details.get("currency")) != _currency_key(new_account.get("currency"))):
            continue
        found = by_ledger.setdefault(ledger, {"ledger_account": ledger, "matched_on": [
            institution, "last4", "account_type", "currency"], "existing_provider_account_ids": []})
        found["existing_provider_account_ids"].append(str(key))
        if details.get("provider") and details["provider"] not in found.setdefault("existing_providers", []):
            found["existing_providers"].append(details["provider"])
    candidates = sorted(by_ledger.values(), key=lambda item: item["ledger_account"])
    for item in candidates:
        item["existing_provider_account_ids"].sort()
    result["candidates"] = candidates
    if len(candidates) == 1:
        ledger = candidates[0]["ledger_account"]
        result.update(status="proposal", ledger_account=ledger,
                      reason="Institution, last four digits, account type and currency match. "
                             "A person must confirm before the new account ID is mapped.",
                      confirm_command=_confirm_command(entity, new_account, ledger))
    elif candidates:
        result.update(status="ambiguous", ledger_account=None,
                      reason="More than one ledger account matches. Ask the person which one is right.")
    else:
        result.update(status="no_match", ledger_account=None,
                      reason="No mapped account matches this institution, last four digits, type and currency.")
    return result


def _confirm_command(entity: Any, new_account: dict[str, Any], ledger: str) -> str:
    parts = ["books entity bank-account-map", f'"{entity.path}"',
             f'--feed-account-id "{new_account.get("provider_account_id") or "<provider account id>"}"',
             f'--account "{ledger}"']
    flags = {"provider": "--provider", "institution_id": "--institution-id",
             "institution_name": "--institution-name", "last4": "--last4",
             "account_type": "--account-type", "currency": "--currency"}
    for name in BANK_IDENTITY_FIELDS:
        if name in flags and new_account.get(name):
            parts.append(f'{flags[name]} "{new_account[name]}"')
    return " ".join(parts)


def add_parser(subparsers: Any) -> None:
    bank = subparsers.add_parser("bank", help="Connected bank account identity")
    bank_sub = bank.add_subparsers(dest="bank_command", required=True)
    match = bank_sub.add_parser(
        "match-account",
        help="Propose the existing ledger account for a provider account (changes nothing)")
    match.add_argument("--entity", required=True, type=Path, help="Company books directory")
    match.add_argument("--provider", default=None, help="New provider, e.g. plaid")
    match.add_argument("--provider-account-id", default=None, help="The new provider's account ID")
    match.add_argument("--institution-id", default=None)
    match.add_argument("--institution-name", default=None)
    match.add_argument("--last4", default=None, help="Last four characters of the account number")
    match.add_argument("--account-type", default=None, help="checking, savings or credit_card")
    match.add_argument("--currency", default=None, help="Three-letter currency code")
    match.add_argument("--json", action="store_true", help="Print the result as JSON")


def run(args: argparse.Namespace) -> int:
    if args.bank_command != "match-account":  # pragma: no cover - argparse guards this
        return 2
    try:
        entity = load_entity(args.entity)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    new_account = {name: getattr(args, name) for name in (
        "provider", "provider_account_id", "institution_id", "institution_name",
        "last4", "account_type", "currency")}
    result = propose_account_match(entity, new_account)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    status = result["status"]
    if status == "proposal":
        print(f"Proposed ledger account: {result['ledger_account']}")
        print("A person must confirm. To map the new account, run:")
        print(f"  {result['confirm_command']}")
    elif status == "already_mapped":
        print(f"Already mapped to {result['ledger_account']}.")
    elif status == "ambiguous":
        print("More than one ledger account matches: "
              + ", ".join(item["ledger_account"] for item in result["candidates"]))
    else:
        print(f"No match. {result['reason']}")
    return 0
