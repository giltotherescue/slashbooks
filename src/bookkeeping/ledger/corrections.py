"""Public, audited correction commands for posted ledger activity."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from ..entity import load_entity
from .importer import _ensure_ledger_store, _find_live_entry, reverse_and_correct, reverse_entry
from .model import Entry, Posting


def add_parser(subparsers: Any) -> None:
    reverse = subparsers.add_parser("reverse", help="Reverse a posted entry while preserving its source lineage")
    reverse.add_argument("--entity", required=True)
    reverse.add_argument("--source-id", required=True)
    reverse.add_argument("--session", default="ledger-correction")

    correct = subparsers.add_parser("correct", help="Reverse and replace a posted entry with explicit balanced postings")
    correct.add_argument("--entity", required=True)
    correct.add_argument("--source-id", required=True)
    correct.add_argument("--new-source-id", required=True)
    correct.add_argument("--date", required=True)
    correct.add_argument("--description", required=True)
    correct.add_argument("--posting", action="append", required=True, help="ACCOUNT=SIGNED_AMOUNT; repeat for every posting")
    correct.add_argument("--session", default="ledger-correction")

    reclassify = subparsers.add_parser("reclassify-bank", help="Move a posted transaction between bank accounts via reversal and correction")
    reclassify.add_argument("--entity", required=True)
    reclassify.add_argument("--source-id", required=True)
    reclassify.add_argument("--new-source-id", required=True)
    reclassify.add_argument("--from-account", required=True)
    reclassify.add_argument("--to-account", required=True)
    reclassify.add_argument("--session", default="ledger-correction")

    split = subparsers.add_parser("split-equity", help="Split a posted partner distribution across equity accounts")
    split.add_argument("--entity", required=True)
    split.add_argument("--source-id", required=True)
    split.add_argument("--new-source-id", required=True)
    split.add_argument("--equity-posting", action="append", required=True, help="Equity:Account=SIGNED_AMOUNT; repeat for each allocation")
    split.add_argument("--session", default="ledger-correction")


def _postings(values: list[str], *, require_balanced: bool = True) -> tuple[Posting, ...]:
    postings: list[Posting] = []
    for value in values:
        if "=" not in value:
            raise ValueError("Posting must use ACCOUNT=SIGNED_AMOUNT.")
        account, raw_amount = value.rsplit("=", 1)
        try:
            amount = Decimal(raw_amount).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError(f"Invalid posting amount: {raw_amount!r}") from exc
        if not account or not amount:
            raise ValueError("Posting account and non-zero amount are required.")
        postings.append(Posting(account=account, amount=amount, currency="USD"))
    if require_balanced and sum((posting.amount for posting in postings), Decimal("0")) != Decimal("0"):
        raise ValueError("Correcting postings must balance to zero.")
    return tuple(postings)


def _correct(entity: Any, source_id: str, new_source_id: str, corrected: Entry, session: str) -> None:
    if not new_source_id or new_source_id == source_id:
        raise ValueError("--new-source-id must be a new, non-empty source ID.")
    if _ensure_ledger_store(entity).source_exists(new_source_id):
        raise ValueError(f"A posted entry already uses source ID '{new_source_id}'.")
    reverse_and_correct(entity, source_id, corrected, session)


def run(args: argparse.Namespace) -> int:
    entity = load_entity(args.entity)
    try:
        if args.ledger_command == "reverse":
            reverse_entry(entity, args.source_id, args.session)
            print(f"Reversed: {args.source_id}")
            return 0
        if args.ledger_command == "correct":
            corrected = Entry(
                date=date.fromisoformat(args.date), narration=args.description, flag="*",
                meta=(("source-id", args.new_source_id),), postings=_postings(args.posting),
            )
            _correct(entity, args.source_id, args.new_source_id, corrected, args.session)
            print(f"Corrected: {args.source_id} → {args.new_source_id}")
            return 0
        store = _ensure_ledger_store(entity)
        original = _find_live_entry(store, args.source_id)
        if args.ledger_command == "reclassify-bank":
            replacements = tuple(
                Posting(args.to_account, posting.amount, posting.currency, posting.meta)
                if posting.account == args.from_account else posting
                for posting in original.postings
            )
            if replacements == original.postings:
                raise ValueError(f"Source entry does not post to '{args.from_account}'.")
            corrected = Entry(
                date=original.date, narration=original.narration, payee=original.payee, flag=original.flag,
                meta=(("source-id", args.new_source_id),), postings=replacements,
            )
            _correct(entity, args.source_id, args.new_source_id, corrected, args.session)
            print(f"Reclassified: {args.source_id} → {args.to_account}")
            return 0
        if args.ledger_command == "split-equity":
            equity = _postings(args.equity_posting, require_balanced=False)
            if any(not posting.account.startswith("Equity:") for posting in equity):
                raise ValueError("Every --equity-posting account must start with Equity:.")
            retained = tuple(posting for posting in original.postings if not posting.account.startswith("Equity:"))
            corrected = Entry(
                date=original.date, narration=original.narration, payee=original.payee, flag=original.flag,
                meta=(("source-id", args.new_source_id),), postings=retained + equity,
            )
            if sum((posting.amount for posting in corrected.postings), Decimal("0")) != Decimal("0"):
                raise ValueError("Equity split must balance with the retained postings.")
            _correct(entity, args.source_id, args.new_source_id, corrected, args.session)
            print(f"Split equity distribution: {args.source_id}")
            return 0
    except (ValueError, InvalidOperation) as exc:
        print(f"Error: {exc}")
        return 1
    return 2
