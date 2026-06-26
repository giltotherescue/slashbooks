"""Demo company scaffolding with deterministic fictional books."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .entity import Entity, init_entity, load_entity
from .ledger.importer import import_transactions
from .ledger.normalize import normalize_description
from .ledger.store import LedgerStore, default_store_path
from .queue import propose

DEMO_COMPANY_NAME = "Northstar Metrics LLC"


@dataclass(frozen=True)
class DemoPeriod:
    as_of: date
    start_year: int

    @property
    def current_year(self) -> int:
        return self.as_of.year

    @property
    def start_date(self) -> date:
        return date(self.start_year, 1, 1)

    @property
    def previous_year_end(self) -> date:
        return date(self.start_year, 12, 31)


@dataclass(frozen=True)
class DemoInitResult:
    target: Path
    created: list[str]
    existed: list[str]
    posted_entries: int
    queued_for_review: int
    period_start: date
    period_end: date
    onboarding_answers: tuple[tuple[str, str], ...]


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "demo",
        help="Create a fictional sample company for exploring Slashbooks",
    )
    demo_sub = parser.add_subparsers(dest="demo_command", required=True)

    init_parser = demo_sub.add_parser(
        "init",
        help="Initialise a populated fictional company directory",
    )
    init_parser.add_argument("path", type=Path, help="Path to create for the demo company")


def run(args: argparse.Namespace) -> int:
    if args.demo_command == "init":
        try:
            result = init_demo(args.path)
        except SystemExit:
            return 1

        print(f"Demo company: {result.target}")
        if result.created:
            print("  Created:")
            for item in result.created:
                print(f"    + {item}")
        if result.existed:
            print("  Already present or refreshed:")
            for item in result.existed:
                print(f"    = {item}")
        print(f"  Posted demo entries: {result.posted_entries}")
        print(f"  Queued for review:   {result.queued_for_review}")
        print(f"  Demo period:         {result.period_start} through {result.period_end}")
        print("  Demo onboarding answers:")
        for question, answer in result.onboarding_answers:
            print(f"    - {question}: {answer}")
        return 0

    print(f"Unknown demo command: {args.demo_command}", file=sys.stderr)
    return 2


def _demo_period(as_of: date | None = None) -> DemoPeriod:
    today = as_of or date.today()
    return DemoPeriod(as_of=today, start_year=today.year - 1)


def _demo_onboarding_answers(period: DemoPeriod) -> tuple[tuple[str, str], ...]:
    return (
        ("Legal business name", DEMO_COMPANY_NAME),
        ("Legal structure", "Single-member LLC"),
        ("Business type", "SaaS business with light consulting revenue"),
        (
            "What the company does",
            "Sells subscription analytics software to small agencies and does occasional implementation projects.",
        ),
        (
            "Main customers",
            "Monthly Stripe subscription customers plus occasional ACH consulting invoices from larger customers.",
        ),
        (
            "Main vendors and contractors",
            "AWS, GitHub, Figma, Notion, two recurring contractors, travel, meals, and office supplies.",
        ),
        (
            "Owner compensation pattern",
            "The owner takes periodic draws from operating checking.",
        ),
        (
            "Books start date",
            (
                f"{period.start_date.isoformat()}, with the full {period.start_year} calendar year "
                f"plus {period.current_year} year-to-date through {period.as_of.isoformat()}"
            ),
        ),
        ("Fiscal year", "January 1"),
        (
            "Bank accounts and cards",
            "Demo operating checking, demo business credit card, and demo Stripe payouts.",
        ),
        ("Country", "United States"),
        ("Tax jurisdiction", "US"),
        ("Operating currency", "USD"),
        (
            "Commingling rules",
            "No real commingling. Three fictional transactions are intentionally queued for review.",
        ),
    )


def init_demo(target: str | Path, *, as_of: date | None = None) -> DemoInitResult:
    """Create a deterministic fictional company directory at *target*."""
    target_path = Path(target).resolve()
    period = _demo_period(as_of)
    onboarding_answers = _demo_onboarding_answers(period)
    session_id = f"demo-seed-{period.start_year}-{period.current_year}-ytd"
    imported_at = f"{period.as_of.isoformat()}T12:00:00Z"
    demo_managed_paths = (
        "ingestion/demo-normalized-transactions.json",
        "learned-context/counterparties.json",
        "review-queue/demo-review-001.json",
        "review-queue/demo-review-002.json",
        "review-queue/demo-review-003.json",
        "ONBOARDING.md",
        "DEMO.md",
    )
    preexisting_demo_paths = {
        rel_path for rel_path in demo_managed_paths if (target_path / rel_path).exists()
    }

    entity_report = init_entity(
        target_path,
        name=DEMO_COMPANY_NAME,
        business_type="saas",
        legal_structure="Single-member LLC",
        cutover_date=period.start_date.isoformat(),
    )
    entity = load_entity(target_path)

    _write_demo_entity_config(entity.path, period)
    _write_demo_business_profile(entity.path, period)
    _write_demo_onboarding_answers(entity.path, onboarding_answers)
    _write_demo_learned_context(entity.path, period)
    _write_demo_notes(entity.path, period)

    transactions = _demo_transactions(period)
    normalized_path = entity.path / "ingestion" / "demo-normalized-transactions.json"
    normalized_path.write_text(
        json.dumps({"source": "demo", "transactions": transactions}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result = import_transactions(
        load_entity(entity.path),
        transactions,
        session_id=session_id,
        categorizer=_demo_categorizer,
        ts=imported_at,
        session_date=period.as_of,
    )
    _seed_demo_queue_items(load_entity(entity.path), imported_at)

    created = list(entity_report["created"])
    existed = list(entity_report["existed"])
    for rel_path in demo_managed_paths:
        destination = existed if rel_path in preexisting_demo_paths else created
        if rel_path not in destination:
            destination.append(rel_path)

    return DemoInitResult(
        target=target_path,
        created=created,
        existed=existed,
        posted_entries=result.new_entries,
        queued_for_review=result.pending_categorization,
        period_start=period.start_date,
        period_end=period.as_of,
        onboarding_answers=onboarding_answers,
    )


def _write_demo_entity_config(entity_path: Path, period: DemoPeriod) -> None:
    path = entity_path / "entity.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(
        {
            "bank_account_mappings": {
                "demo_card": "Liabilities:CreditCard:Business-Card",
                "demo_checking": "Assets:Bank:Operating-Checking",
            },
            "business_type": "saas",
            "country": "US",
            "cutover_date": period.start_date.isoformat(),
            "declared_sources": [
                "Demo operating checking",
                "Demo business credit card",
                "Demo Stripe payouts",
            ],
            "indirect_tax": {"registered": False, "type": None},
            "legal_structure": "Single-member LLC",
            "name": DEMO_COMPANY_NAME,
            "operating_currency": "USD",
            "payroll": {"enabled": False, "provider": None, "posting_mode": "not_applicable"},
            "provider_sources": ["stripe"],
            "tax_jurisdiction": "US",
        }
    )
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_demo_business_profile(entity_path: Path, period: DemoPeriod) -> None:
    profile = f"""# Business Profile

## Business Type

SaaS business with light consulting revenue.

## Legal Structure

Single-member LLC.

## What This Business Does

Northstar Metrics sells a subscription analytics product to small agencies and does occasional implementation projects for larger customers.

## Customer Patterns

Subscriptions arrive through Stripe in monthly payout batches. A few consulting projects pay larger invoices by ACH.

## Vendor Patterns

Recurring vendors include AWS for hosting, GitHub and Figma for software, Gusto for payroll-like contractor operations, and Benchling Cloud for analytics infrastructure.

## Owner Compensation Pattern

The owner takes periodic draws from operating checking. These are fictional and included so reviewers can see equity flows.

## Books Start Date

January 1, {period.start_year}. The demo includes the full {period.start_year} calendar year plus {period.current_year} year-to-date activity through {period.as_of.isoformat()}.

## Fiscal Year

January 1.

## Country, Tax, and Currency Context

Northstar is a fictional United States company. The demo books use USD as the operating currency and assume no indirect tax registration.

## Declared Data Sources

- Demo operating checking (BankSync account ID: demo_checking)
- Demo business credit card (BankSync account ID: demo_card)
- Demo Stripe payouts

## Commingling Rules

Three transactions are intentionally queued for review so users can try the review workflow without using real company data.
"""
    (entity_path / "business-profile.md").write_text(profile, encoding="utf-8")


def _write_demo_onboarding_answers(
    entity_path: Path,
    onboarding_answers: tuple[tuple[str, str], ...],
) -> None:
    lines = [
        f"# {DEMO_COMPANY_NAME} Onboarding Answers",
        "",
        "These are the fictional answers used to set up the demo company.",
        "They mirror the context Slashbooks asks for when onboarding a real entity.",
        "",
    ]
    for question, answer in onboarding_answers:
        lines.extend([f"## {question}", "", answer, ""])
    (entity_path / "ONBOARDING.md").write_text("\n".join(lines), encoding="utf-8")


def _write_demo_notes(entity_path: Path, period: DemoPeriod) -> None:
    notes = f"""# {DEMO_COMPANY_NAME} Demo Books

This is a fictional sample company. All transactions, counterparties, balances, and business context are synthetic.

The canonical ledger and account catalog are in `ledger.sqlite`. This demo does not seed `books.beancount` or `chart-of-accounts.beancount`; Beancount is only an import/export projection.

`ONBOARDING.md` shows the fictional answers used to set up this company, so you can see the kind of context Slashbooks collects during real onboarding.

Try:

```sh
books report pnl --entity . --from {period.start_date.isoformat()} --to {period.as_of.isoformat()}
books queue list --entity .
books export --entity . --from {period.start_date.isoformat()} --to {period.as_of.isoformat()}
```
"""
    (entity_path / "DEMO.md").write_text(notes, encoding="utf-8")


def _write_demo_learned_context(entity_path: Path, period: DemoPeriod) -> None:
    ctx: dict[str, dict[str, Any]] = {}
    for description, category in _CATEGORY_RULES.items():
        key = normalize_description(description)
        ctx[key] = {
            "canonical_category": category,
            "confirmed_count": 3,
            "last_confirmed_date": period.previous_year_end.isoformat(),
            "notes": "Seeded by the fictional demo company.",
            "reset": False,
        }
    path = entity_path / "learned-context" / "counterparties.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ctx, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _seed_demo_queue_items(entity: Entity, imported_at: str) -> None:
    proposals = [
        (
            "demo-review-001",
            "Expenses:Meals",
            "Could be a customer event or travel-related venue cost; owner should confirm.",
        ),
        (
            "demo-review-002",
            "Income:Consulting",
            "Incoming wire looks like project revenue, but the payer is new in the demo books.",
        ),
        (
            "demo-review-003",
            "Expenses:Software",
            "Unfamiliar AI tool subscription; owner should confirm whether it is software or a personal charge.",
        ),
    ]
    for source_id, category, reasoning in proposals:
        item = propose(entity, source_id, category, reasoning, context="Seeded fictional demo review item.")
        item["created_at"] = imported_at
        item["updated_at"] = imported_at
        (entity.path / "review-queue" / f"{source_id}.json").write_text(
            json.dumps(item, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


_CATEGORY_RULES: dict[str, str] = {
    "AWS": "Expenses:Hosting",
    "Blue Bottle Coffee": "Expenses:Meals",
    "Brightlane Studios": "Income:Consulting",
    "Coda": "Expenses:Software",
    "Contractor - Maya Chen": "Expenses:Subcontractors",
    "Contractor - Priya Shah": "Expenses:Subcontractors",
    "Delaware Franchise Tax": "Expenses:Taxes",
    "Delta Air Lines": "Expenses:Travel",
    "Figma": "Expenses:Software",
    "GitHub": "Expenses:Software",
    "Google Workspace": "Expenses:Software",
    "Mercury Bank Fee": "Expenses:Fees",
    "Notion": "Expenses:Software",
    "Office Depot": "Expenses:Office",
    "Owner Contribution": "Equity:Owner-Contributions",
    "Owner Draw": "Equity:Owner-Draws",
    "Stripe Fees": "Expenses:Payment-Fees",
    "Stripe Payout": "Income:Subscriptions",
    "United Airlines": "Expenses:Travel",
    "Zoom": "Expenses:Software",
}


def _demo_categorizer(txn: dict[str, Any]) -> tuple[str, str]:
    description = str(txn.get("description") or "")
    for prefix, account in _CATEGORY_RULES.items():
        if description.startswith(prefix):
            return account, "demo"
    return "", "queue"


def _demo_transactions(period: DemoPeriod) -> list[dict[str, Any]]:
    transactions: list[dict[str, Any]] = [
        _txn(
            f"demo-{period.start_year}-opening-contribution",
            period.start_date.isoformat(),
            "Owner Contribution",
            "25000.00",
        ),
    ]

    monthly = [
        (1, "Jan", Decimal("18420.00"), Decimal("4200.00"), Decimal("2240.00"), Decimal("0.00")),
        (2, "Feb", Decimal("19680.00"), Decimal("0.00"), Decimal("2480.00"), Decimal("1250.00")),
        (3, "Mar", Decimal("21350.00"), Decimal("6300.00"), Decimal("2750.00"), Decimal("1250.00")),
        (4, "Apr", Decimal("22910.00"), Decimal("0.00"), Decimal("3100.00"), Decimal("1800.00")),
        (5, "May", Decimal("24480.00"), Decimal("7200.00"), Decimal("3250.00"), Decimal("1800.00")),
        (6, "Jun", Decimal("26340.00"), Decimal("0.00"), Decimal("3490.00"), Decimal("1800.00")),
        (7, "Jul", Decimal("27920.00"), Decimal("8400.00"), Decimal("3725.00"), Decimal("2100.00")),
        (8, "Aug", Decimal("28650.00"), Decimal("0.00"), Decimal("3890.00"), Decimal("2100.00")),
        (9, "Sep", Decimal("30110.00"), Decimal("9600.00"), Decimal("4100.00"), Decimal("2100.00")),
        (10, "Oct", Decimal("31840.00"), Decimal("0.00"), Decimal("4350.00"), Decimal("2400.00")),
        (11, "Nov", Decimal("33570.00"), Decimal("11200.00"), Decimal("4625.00"), Decimal("2400.00")),
        (12, "Dec", Decimal("36120.00"), Decimal("0.00"), Decimal("4980.00"), Decimal("2400.00")),
    ]
    years = [period.start_year]
    if period.current_year != period.start_year:
        years.append(period.current_year)
    for year in years:
        for idx, (month, label, stripe, consulting, contractor_maya, contractor_priya) in enumerate(monthly, start=1):
            if year == period.current_year and month > period.as_of.month:
                continue
            ym = f"{year}-{month:02d}"
            maybe_transactions = [
                _txn(f"demo-{ym}-stripe-payout", f"{ym}-03", f"Stripe Payout {label}", stripe),
                _txn(f"demo-{ym}-stripe-fees", f"{ym}-03", f"Stripe Fees {label}", -stripe * Decimal("0.029")),
                _txn(f"demo-{ym}-aws", f"{ym}-05", "AWS", "-840.00"),
                _txn(f"demo-{ym}-google-workspace", f"{ym}-06", "Google Workspace", "-144.00", account_id="demo_card", account_type="credit_card"),
                _txn(f"demo-{ym}-github", f"{ym}-07", "GitHub", "-96.00", account_id="demo_card", account_type="credit_card"),
                _txn(f"demo-{ym}-figma", f"{ym}-09", "Figma", "-180.00", account_id="demo_card", account_type="credit_card"),
                _txn(f"demo-{ym}-notion", f"{ym}-10", "Notion", "-48.00", account_id="demo_card", account_type="credit_card"),
                _txn(f"demo-{ym}-zoom", f"{ym}-11", "Zoom", "-54.99", account_id="demo_card", account_type="credit_card"),
                _txn(f"demo-{ym}-contractor-maya", f"{ym}-14", "Contractor - Maya Chen", -contractor_maya),
                _txn(f"demo-{ym}-meals", f"{ym}-16", "Blue Bottle Coffee", "-42.80", account_id="demo_card", account_type="credit_card"),
                _txn(f"demo-{ym}-office", f"{ym}-18", "Office Depot", "-73.44", account_id="demo_card", account_type="credit_card"),
                _txn(f"demo-{ym}-coda", f"{ym}-21", "Coda", "-36.00", account_id="demo_card", account_type="credit_card"),
                _txn(f"demo-{ym}-bank-fee", f"{ym}-24", "Mercury Bank Fee", "-15.00"),
                _txn(f"demo-{ym}-owner-draw", f"{ym}-25", "Owner Draw", "-3500.00"),
            ]
            if contractor_priya:
                maybe_transactions.append(_txn(f"demo-{ym}-contractor-priya", f"{ym}-15", "Contractor - Priya Shah", -contractor_priya))
            if consulting:
                maybe_transactions.append(_txn(f"demo-{ym}-consulting", f"{ym}-20", "Brightlane Studios", consulting))
            if idx in {3, 6, 9, 12}:
                maybe_transactions.append(
                    _txn(
                        f"demo-{ym}-travel",
                        f"{ym}-22",
                        "Delta Air Lines" if idx in {3, 9} else "United Airlines",
                        "-612.40" if idx in {3, 9} else "-728.15",
                        account_id="demo_card",
                        account_type="credit_card",
                    )
                )
                maybe_transactions.append(
                    _txn(f"demo-{year}-q{idx // 3}-franchise-tax", f"{ym}-01", "Delaware Franchise Tax", "-300.00")
                )
            transactions.extend(
                txn for txn in maybe_transactions if date.fromisoformat(str(txn["date"])) <= period.as_of
            )

    transactions.extend(
        [
            _txn(
                "demo-review-001",
                date(period.start_year, 6, 27).isoformat(),
                "North Pier Event Space",
                "-1180.00",
                account_id="demo_card",
                account_type="credit_card",
            ),
            _txn("demo-review-002", date(period.start_year, 9, 28).isoformat(), "Wire Transfer - Aster Labs", "2800.00"),
            _txn(
                "demo-review-003",
                _third_review_date(period).isoformat(),
                "LumenForge AI",
                "-249.00",
                account_id="demo_card",
                account_type="credit_card",
            ),
        ]
    )
    return transactions


def _third_review_date(period: DemoPeriod) -> date:
    current_year_candidate = date(period.current_year, 3, 19)
    if current_year_candidate <= period.as_of:
        return current_year_candidate
    return date(period.start_year, 12, 19)


def _txn(
    txn_id: str,
    txn_date: str,
    description: str,
    amount: str | Decimal,
    *,
    account_id: str = "demo_checking",
    account_type: str = "checking",
) -> dict[str, Any]:
    dec = Decimal(str(amount)).quantize(Decimal("0.01"))
    account_name = "Business Card" if account_id == "demo_card" else "Operating Checking"
    return {
        "accountId": account_id,
        "accountName": account_name,
        "accountType": account_type,
        "amount": str(dec),
        "date": txn_date,
        "description": description,
        "id": txn_id,
        "pending": False,
    }


def demo_store_counts(target: str | Path) -> dict[str, int]:
    """Return basic canonical-store counts for tests and diagnostics."""
    store = LedgerStore(default_store_path(Path(target)))
    counts = store.counts()
    return {
        "accounts": counts.accounts,
        "audit_events": counts.audit_events,
        "entries": counts.entries,
        "postings": counts.postings,
    }
