"""The "Tax assumptions" section of the tax export, for a tax preparer to review.

The engine stores facts and does mechanical math. It makes no tax rule decision.
This module only restates, in plain words, what the tax export did and what it left alone.
Every value comes from the books and the company settings. Nothing here computes tax or
checks a rule, and no dollar limit appears in this code.

``build`` returns one JSON-ready object. It is stored as ``tax_view.assumptions`` in the tax
export and the tax map, and it fills the "Tax Assumptions" sheet and a short Read Me section.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping

NOT_RECORDED = "not recorded"
RECORDED = "Recorded by a person"
ASSUMED = "Assumed default"
NOT_ADJUSTED = "Not adjusted"
RECORDED_OR_ASSUMED = "Recorded by a person where set. Assumed default where not set"

# What the preparer decides. This list never changes with the books.
PREPARER_DECIDES = [
    "The tax method and the tax year on the filed return.",
    "Whether the company may use the cash method, and whether it is a tax shelter.",
    "The 12-month rule for prepaid items, and the election out. The election out is made on the return.",
    "Form 3115 and the section 481(a) adjustment, if the method changes.",
    "Depreciation, section 179, bonus depreciation and the de minimis safe harbor.",
    "Whether a deposit is income when received. Check the agreement. Do not decide it from an account name.",
    "Whether funds held for a client in trust or escrow are the company's income.",
    "The inventory method.",
    "Whether a card charge is financing.",
]

NOTES = [
    "Slashbooks does not give tax advice. It prepares book figures and lists what it did.",
    "Passing the 12-month test only removes the capitalization requirement. It does not prove that an item is deductible.",
    "A prepaid schedule override in Slashbooks is not the election out. The election out is made on the return.",
    "The Fixed Assets sheet shows the purchase date. The placed-in-service date is not recorded.",
    "Cost alone does not decide the de minimis safe harbor. The preparer checks the written procedures and the election.",
    "A person recorded the deposit kind. It is not a tax conclusion. The preparer confirms it against the agreement.",
]


def _prepaid_without_treatment(schedules: list[Mapping[str, Any]]) -> list[str]:
    return sorted(r["id"] for r in schedules if r.get("kind") == "prepaid" and "tax_treatment" not in r)


def _items(view: Any, fiscal_start: str, fiscal_recorded: bool) -> list[dict[str, str]]:
    """One row per item: what this export did, where that handling came from, and whether the preparer decides."""
    def row(item: str, handling: str, source: str, decides: str) -> dict[str, str]:
        return {"item": item, "handling": handling, "source": source, "preparer_decides": decides}

    tax_year = row("Tax year",
                   f"The fiscal year start is {fiscal_start} ({'set by a person' if fiscal_recorded else 'the default'}). "
                   "The export covers the period that was asked for. The federal tax year is not checked.",
                   RECORDED if fiscal_recorded else ASSUMED, "Yes")
    depreciation = row("Depreciation, section 179, bonus, de minimis",
                       "Not computed. Book depreciation is shown as recorded. The Fixed Assets sheet lists purchases with their cost. "
                       "Cost alone does not decide any of these.", NOT_ADJUSTED, "Yes")
    inventory = row("Inventory", "Not adjusted. Inventory accounts are used as the books record them.", NOT_ADJUSTED, "Yes")
    if view.source == "derived":
        return [
            row("Prepaid expenses",
                "A schedule with a recorded tax treatment uses it. Under expense_when_paid, the payment is an expense when paid. "
                "Under capitalize, the expense is taken as the item is used. A schedule with no recorded treatment stays capitalized, "
                "as the books record it.",
                RECORDED_OR_ASSUMED, "Yes"),
            row("Customer deposits",
                "A deposit recorded as advance_payment is income on receipt. A deposit recorded as refundable is a liability. "
                "It stays a liability until it is applied to an invoice or forfeited. A deposit with no recorded kind is assumed "
                "to be an advance_payment.",
                RECORDED_OR_ASSUMED, "Yes"),
            row("Deferred revenue", "Income on receipt (cash method). The deferral that the accrual books show is removed.", ASSUMED, "Yes"),
            row("Opening receivables and payables",
                "Counted on the date of the cash movement. Slashbooks does not check whether an earlier return reported them.", ASSUMED, "Yes"),
            row("Credit card charges",
                "The expense is on the charge date. The payment of the card balance is a transfer.", ASSUMED, "Yes"),
            row("Bad debts",
                "Not recorded as a deduction. Write-offs, credits and true-ups are left out. Receivables that were never received "
                "are never in income. The preparer decides if any deduction applies.", ASSUMED, "Yes"),
            depreciation,
            row("Accrued liabilities", "Excluded in the cash view until they are paid.", ASSUMED, "Yes"),
            inventory,
            tax_year,
        ]
    recorded = ("the recorded books are cash books, so income and expense follow cash movement"
                if view.tax_method == "cash" else "the recorded books are used as they are (see the basis of the recorded books)")
    generic = f"Not adjusted: {recorded}."
    return [
        row("Prepaid expenses", generic, NOT_ADJUSTED, "Yes"),
        row("Customer deposits", generic, NOT_ADJUSTED, "Yes"),
        row("Deferred revenue", generic, NOT_ADJUSTED, "Yes"),
        row("Opening receivables and payables", generic, NOT_ADJUSTED, "Yes"),
        row("Credit card charges", generic, NOT_ADJUSTED, "Yes"),
        row("Bad debts", generic, NOT_ADJUSTED, "Yes"),
        depreciation,
        row("Accrued liabilities", generic, NOT_ADJUSTED, "Yes"),
        inventory,
        tax_year,
    ]


def build(conn: Any, config: Mapping[str, Any], view: Any, from_date: date, to_date: date,
          flag_list: list[Mapping[str, Any]], data: Mapping[str, Any], profile: Mapping[str, str]) -> dict[str, Any]:
    """The assumptions object. ``flag_list`` and ``data`` are the flags, fixed assets and deposits of the same export."""
    from .currency import company_currency
    from .taxmap import book_basis
    from .taxview import _records
    # The start is recorded by a person only when it differs from the default. A start of 01-01 is the default either way.
    fiscal_recorded = view.fiscal_year_start != "01-01"
    settings = {
        "tax_method": view.tax_method,
        "tax_method_source": "recorded in entity.json by a person (books entity set-tax-method)",
        "fiscal_year_start": view.fiscal_year_start,
        "fiscal_year_start_source": "set by a person" if fiscal_recorded else "default",
        "prior_return_method": profile.get("prior_return_method", NOT_RECORDED),
        "entity_type": profile.get("entity_type", NOT_RECORDED),
        "currency": company_currency(config),
        "period": f"{from_date.isoformat()} to {to_date.isoformat()}",
        "books_basis": book_basis(conn, config, to_date)[0],
        "tax_view_used": view.view,
    }
    counts: dict[str, Any] = {
        "fixed_asset_rows_listed": len(data.get("fixed_assets", [])),
        "fixed_asset_rows_in_period": sum(1 for r in data.get("fixed_assets", []) if r.get("in_period")),
    }
    if view.source == "derived":
        # Only the cash view reads these two inputs.
        counts["prepaid_schedules_without_tax_treatment"] = len(_prepaid_without_treatment(_records(conn, "schedules")))
        counts["deposits_without_recorded_kind"] = sum(1 for r in data.get("deposits", []) if r.get("deposit_kind_source") == "assumed")
    open_flags: dict[str, int] = {}
    for flag in flag_list:
        if flag["code"] != "tax_method":
            open_flags[flag["code"]] = open_flags.get(flag["code"], 0) + 1
    counts["open_flags"] = dict(sorted(open_flags.items()))
    return {"settings": settings, "items": _items(view, view.fiscal_year_start, fiscal_recorded), "counts": counts,
            "preparer_decides": list(PREPARER_DECIDES), "notes": list(NOTES)}


SETTING_LABELS = [
    ("tax_method", "Tax method"), ("tax_method_source", "Tax method source"), ("fiscal_year_start", "Fiscal year start"),
    ("fiscal_year_start_source", "Fiscal year start source"), ("prior_return_method", "Prior return method"),
    ("entity_type", "Entity type"), ("currency", "Currency"), ("period", "Period covered"),
    ("books_basis", "Basis of the recorded books"), ("tax_view_used", "View used for tax"),
]
COUNT_LABELS = {
    "prepaid_schedules_without_tax_treatment": "Prepaid schedules with no recorded tax treatment",
    "deposits_without_recorded_kind": "Deposits with no recorded kind (assumed advance payment)",
    "fixed_asset_rows_listed": "Fixed-asset rows listed",
    "fixed_asset_rows_in_period": "Fixed-asset rows with a purchase date in the period",
}
FLAGS_LISTED = "Flags listed"


def sheet_rows(assumptions: Mapping[str, Any]) -> list[list[str]]:
    """The "Tax Assumptions" sheet. The first row is the header of the items table, so a CSV copy reads as one table."""
    rows = [["Item", "Handling in this export", "Source of the handling", "Preparer decides?"]]
    for item in assumptions["items"]:
        rows.append([item["item"], item["handling"], item["source"], item["preparer_decides"]])
    rows += [[], ["Settings in effect", "Value", "", ""]]
    settings = assumptions["settings"]
    for key, label in SETTING_LABELS:
        rows.append([label, str(settings[key]), "", ""])
    rows += [[], ["Input completeness", "Count", "", ""]]
    counts = assumptions["counts"]
    for key, label in COUNT_LABELS.items():
        if key in counts:
            rows.append([label, str(counts[key]), "", ""])
    if counts["open_flags"]:
        for code, number in counts["open_flags"].items():
            rows.append([f"Flag: {code}", str(number), "", ""])
    else:
        rows.append([FLAGS_LISTED, "0", "", ""])
    rows += [[], ["Preparer decides", "", "", ""]]
    rows += [[text, "", "", ""] for text in assumptions["preparer_decides"]]
    rows += [[], ["Notes", "", "", ""]]
    rows += [[text, "", "", ""] for text in assumptions["notes"]]
    return rows


def readme_rows(assumptions: Mapping[str, Any]) -> list[list[str]]:
    """A short Read Me section. The Tax Assumptions sheet and tax_view.assumptions in manifest.json hold the full text."""
    counts = assumptions["counts"]
    text = (f"The Tax Assumptions sheet states how each item is handled and what the preparer decides. "
            f"{FLAGS_LISTED}: {sum(counts['open_flags'].values())}.")
    if "prepaid_schedules_without_tax_treatment" in counts:
        text += f" Prepaid schedules with no recorded tax treatment: {counts['prepaid_schedules_without_tax_treatment']}."
    return [
        ["Tax assumptions", text],
        ["Preparer decides", "The tax method and tax year, a method change, cash-method eligibility, the 12-month rule and the election out, "
                             "depreciation and expensing, deposit and trust or escrow classification, the inventory method and card financing. "
                             "The Tax Assumptions sheet has the full list."],
    ]
