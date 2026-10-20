"""Account-to-tax-line mapping and the tax-preparer trial balance export.

This module maps engine-computed balances to federal return lines and writes
files that a tax preparer can import or key in. It never computes tax: no
deduction limits, no depreciation, no book-to-tax adjustments and no totals
that the return itself computes. Every nonzero account appears in the export,
mapped or not, so nothing is dropped.

Supported returns (federal, tax year 2025 line numbers)
-------------------------------------------------------
- ``schc``: Schedule C (Form 1040), sole proprietor or single-member LLC.
- ``1065``: U.S. Return of Partnership Income.
- ``1120s``: U.S. Income Tax Return for an S Corporation.
- ``1120``: U.S. Corporation Income Tax Return.

Not supported: Schedules E and F, Forms 1041 and 990, state returns, K-1
allocation, Form 1125-A and Form 4562 calculations, Schedule M-1/M-2/M-3
calculations and accrual conversion.

Mapping precedence for each account: an explicit override stored in the
company's ``entity.json`` under ``tax_handoff``; else a name rule; else an
account-type default for a few safe cases. Anything left is unmapped and is
reported. Name rules and type defaults are marked for preparer review.

File formats
------------
- Generic tax trial balance (CSV and XLSX): every column below, debit/credit
  and signed balance, form line and statement detail.
- Tax-line summary (CSV and XLSX): book amounts per form line for manual entry
  (Drake Tax template, ProConnect Tax and any product without a documented
  file import).
- Lacerte Trial Balance Utility (CSV): account number, account name, account
  type (A, L, R, E) and a signed balance; mapping happens in Lacerte SmartMap.
- UltraTax CS Trial Balance Excel Import (CSV): account number, description,
  unit, tax code, amount, in that order. Tax codes come from per-account
  UltraTax codes that the firm records with ``books tax set``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

from .entity import _locked_entity_operation, load_entity
from .safe_cells import neutralize_rows
from .accrual.scope import DISCIPLINE_ALL, DISCIPLINE_PER_ACCOUNT, discipline, tracking_start


FORMS: dict[str, str] = {
    "schc": "Schedule C (Form 1040)",
    "1065": "Form 1065",
    "1120s": "Form 1120-S",
    "1120": "Form 1120",
}
FORM_ALIASES = {
    "schc": "schc", "schedule-c": "schc", "schedulec": "schc", "sch-c": "schc", "1040": "schc", "c": "schc",
    "1065": "1065", "partnership": "1065",
    "1120s": "1120s", "1120-s": "1120s", "s-corp": "1120s", "scorp": "1120s",
    "1120": "1120", "c-corp": "1120", "ccorp": "1120",
}
TAX_YEAR_BASIS = 2025
PRIOR_EARNINGS_ACCOUNT = "Equity:Prior-Periods-Net-Income"
_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# Line catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaxLine:
    code: str
    form: str
    schedule: str
    line: str
    label: str
    normal: str  # "debit" or "credit": the side that shows as a positive book amount
    on_return: bool = True
    note: str = ""

    @property
    def measure(self) -> str:
        """``activity`` for period-flow lines (Schedule M-2), else ``balance``."""
        return "activity" if self.schedule == "M-2" else "balance"

    def to_document(self) -> dict[str, Any]:
        return {
            "code": self.code, "form": self.form, "form_name": FORMS[self.form], "measure": self.measure,
            "schedule": self.schedule, "line": self.line, "label": self.label,
            "normal": self.normal, "on_return": self.on_return, "note": self.note,
        }


def _lines(form: str, schedule: str, normal: str, rows: Iterable[tuple[str, str]], *,
           on_return: bool = True, notes: Mapping[str, str] | None = None) -> list[TaxLine]:
    notes = notes or {}
    return [
        TaxLine(f"{form}/{schedule}/{line}", form, schedule, line, label, normal, on_return, notes.get(line, ""))
        for line, label in rows
    ]


_BOOK_COGS = "Book purchases; the preparer completes the cost of goods sold calculation."
_MEALS = "Full book amount; the preparer applies the meals deduction limit."
_DISTRIBUTIONS = "Change in the equity account during the period; books are not closed."
_RETAINED = ("Book balance before closing the current period's net income and distributions; "
             "the preparer rolls equity forward.")
_NONDEDUCTIBLE = "Book expense that is not deductible; the preparer reports it on the book-to-tax schedules."


def _catalog() -> dict[str, TaxLine]:
    lines: list[TaxLine] = []
    # Schedule C (Form 1040), Part I and Part II.
    lines += _lines("schc", "I", "credit", [("1", "Gross receipts or sales"), ("6", "Other income")])
    lines += _lines("schc", "I", "debit", [("2", "Returns and allowances"), ("4", "Cost of goods sold")],
                    notes={"4": _BOOK_COGS})
    lines += _lines("schc", "II", "debit", [
        ("8", "Advertising"), ("9", "Car and truck expenses"), ("10", "Commissions and fees"),
        ("11", "Contract labor"), ("12", "Depletion"), ("13", "Depreciation and section 179 expense deduction"),
        ("14", "Employee benefit programs"), ("15", "Insurance (other than health)"),
        ("16a", "Interest: mortgage (paid to banks, etc.)"), ("16b", "Interest: other"),
        ("17", "Legal and professional services"), ("18", "Office expense"),
        ("19", "Pension and profit-sharing plans"), ("20a", "Rent or lease: vehicles, machinery, and equipment"),
        ("20b", "Rent or lease: other business property"), ("21", "Repairs and maintenance"),
        ("22", "Supplies"), ("23", "Taxes and licenses"), ("24a", "Travel"), ("24b", "Deductible meals"),
        ("25", "Utilities"), ("26", "Wages"), ("27b", "Other expenses (Part V, line 48)"),
        ("30", "Expenses for business use of your home"),
    ], notes={"24b": _MEALS, "13": "Book depreciation; the preparer computes tax depreciation on Form 4562."})
    lines += _lines("schc", "none", "debit", [
        ("balance_sheet", "Balance sheet account (Schedule C has no balance sheet)"),
        ("owner_equity", "Owner contributions and draws (not on Schedule C)"),
        ("personal", "Owner's personal return item, not Schedule C"),
        ("nondeductible", "Not deductible on Schedule C"),
    ], on_return=False)

    # Form 1065.
    lines += _lines("1065", "p1", "credit", [("1a", "Gross receipts or sales"), ("7", "Other income (loss)")])
    lines += _lines("1065", "p1", "debit", [
        ("1b", "Returns and allowances"), ("2", "Cost of goods sold"),
        ("9", "Salaries and wages (other than to partners)"), ("10", "Guaranteed payments to partners"),
        ("11", "Repairs and maintenance"), ("12", "Bad debts"), ("13", "Rent"), ("14", "Taxes and licenses"),
        ("15", "Interest"), ("16a", "Depreciation"), ("17", "Depletion"), ("18", "Retirement plans, etc."),
        ("19", "Employee benefit programs"), ("21", "Other deductions"),
    ], notes={"2": _BOOK_COGS, "16a": "Book depreciation; the preparer computes tax depreciation."})
    lines += _lines("1065", "K", "credit", [("5", "Interest income"), ("6a", "Ordinary dividends")])
    lines += _lines("1065", "K", "debit", [("13a", "Cash contributions (charitable)")])
    lines += _lines("1065", "M-2", "debit", [("6a", "Distributions: cash")], notes={"6a": _DISTRIBUTIONS})
    lines += _lines("1065", "M-1", "debit", [("nondeductible", "Nondeductible book expense")],
                    notes={"nondeductible": _NONDEDUCTIBLE})
    lines += _lines("1065", "L", "debit", [
        ("1", "Cash"), ("2a", "Trade notes and accounts receivable"), ("3", "Inventories"),
        ("6", "Other current assets"), ("7a", "Loans to partners (or persons related to partners)"),
        ("9a", "Buildings and other depreciable assets"), ("11", "Land (net of any amortization)"),
        ("12a", "Intangible assets (amortizable only)"), ("13", "Other assets"),
    ])
    lines += _lines("1065", "L", "credit", [
        ("9b", "Less accumulated depreciation"), ("12b", "Less accumulated amortization"),
        ("15", "Accounts payable"), ("16", "Mortgages, notes, bonds payable in less than 1 year"),
        ("17", "Other current liabilities"), ("19a", "Loans from partners (or persons related to partners)"),
        ("19b", "Mortgages, notes, bonds payable in 1 year or more"), ("20", "Other liabilities"),
        ("21", "Partners' capital accounts"),
    ], notes={"21": _RETAINED})

    # Form 1120-S.
    lines += _lines("1120s", "p1", "credit", [("1a", "Gross receipts or sales"), ("5", "Other income (loss)")])
    lines += _lines("1120s", "p1", "debit", [
        ("1b", "Returns and allowances"), ("2", "Cost of goods sold"), ("7", "Compensation of officers"),
        ("8", "Salaries and wages"), ("9", "Repairs and maintenance"), ("10", "Bad debts"), ("11", "Rents"),
        ("12", "Taxes and licenses"), ("13", "Interest"), ("14", "Depreciation"), ("15", "Depletion"),
        ("16", "Advertising"), ("17", "Pension, profit-sharing, etc., plans"), ("18", "Employee benefit programs"),
        ("20", "Other deductions"),
    ], notes={"2": _BOOK_COGS, "14": "Book depreciation; the preparer computes tax depreciation."})
    lines += _lines("1120s", "K", "credit", [("4", "Interest income"), ("5a", "Ordinary dividends")])
    lines += _lines("1120s", "K", "debit", [("12a", "Cash charitable contributions")])
    lines += _lines("1120s", "M-2", "debit", [("7", "Distributions")], notes={"7": _DISTRIBUTIONS})
    lines += _lines("1120s", "M-1", "debit", [("nondeductible", "Nondeductible book expense")],
                    notes={"nondeductible": _NONDEDUCTIBLE})
    lines += _lines("1120s", "L", "debit", [
        ("1", "Cash"), ("2a", "Trade notes and accounts receivable"), ("3", "Inventories"),
        ("6", "Other current assets"), ("7", "Loans to shareholders"),
        ("10a", "Buildings and other depreciable assets"), ("12", "Land (net of any amortization)"),
        ("13a", "Intangible assets (amortizable only)"), ("14", "Other assets"),
    ])
    lines += _lines("1120s", "L", "credit", [
        ("10b", "Less accumulated depreciation"), ("13b", "Less accumulated amortization"),
        ("16", "Accounts payable"), ("17", "Mortgages, notes, bonds payable in less than 1 year"),
        ("18", "Other current liabilities"), ("19", "Loans from shareholders"),
        ("20", "Mortgages, notes, bonds payable in 1 year or more"), ("21", "Other liabilities"),
        ("22", "Capital stock"), ("23", "Additional paid-in capital"), ("24", "Retained earnings"),
    ], notes={"24": _RETAINED})

    # Form 1120.
    lines += _lines("1120", "p1", "credit", [
        ("1a", "Gross receipts or sales"), ("4", "Dividends and inclusions"), ("5", "Interest"),
        ("10", "Other income"),
    ])
    lines += _lines("1120", "p1", "debit", [
        ("1b", "Returns and allowances"), ("2", "Cost of goods sold"), ("12", "Compensation of officers"),
        ("13", "Salaries and wages"), ("14", "Repairs and maintenance"), ("15", "Bad debts"), ("16", "Rents"),
        ("17", "Taxes and licenses"), ("18", "Interest"), ("19", "Charitable contributions"),
        ("20", "Depreciation"), ("21", "Depletion"), ("22", "Advertising"),
        ("23", "Pension, profit-sharing, etc., plans"), ("24", "Employee benefit programs"),
        ("26", "Other deductions"),
    ], notes={"2": _BOOK_COGS, "19": "Book amount; the preparer applies the contribution limit.",
              "20": "Book depreciation; the preparer computes tax depreciation."})
    lines += _lines("1120", "M-2", "debit", [("5a", "Distributions: cash")], notes={"5a": _DISTRIBUTIONS})
    lines += _lines("1120", "M-1", "debit", [("nondeductible", "Nondeductible book expense")],
                    notes={"nondeductible": _NONDEDUCTIBLE})
    lines += _lines("1120", "L", "debit", [
        ("1", "Cash"), ("2a", "Trade notes and accounts receivable"), ("3", "Inventories"),
        ("6", "Other current assets"), ("7", "Loans to shareholders"),
        ("10a", "Buildings and other depreciable assets"), ("12", "Land (net of any amortization)"),
        ("13a", "Intangible assets (amortizable only)"), ("14", "Other assets"),
    ])
    lines += _lines("1120", "L", "credit", [
        ("10b", "Less accumulated depreciation"), ("13b", "Less accumulated amortization"),
        ("16", "Accounts payable"), ("17", "Mortgages, notes, bonds payable in less than 1 year"),
        ("18", "Other current liabilities"), ("19", "Loans from shareholders"),
        ("20", "Mortgages, notes, bonds payable in 1 year or more"), ("21", "Other liabilities"),
        ("22b", "Capital stock: common stock"), ("23", "Additional paid-in capital"),
        ("25", "Retained earnings, unappropriated"),
    ], notes={"25": _RETAINED})
    return {line.code: line for line in lines}


CATALOG: dict[str, TaxLine] = _catalog()


def lines_for(form: str) -> list[TaxLine]:
    form = normalize_form(form)
    return [line for line in CATALOG.values() if line.form == form]


# Concept -> line per form. A missing concept on a form falls back to the
# form's "other deductions" line with the concept label as statement detail.
_OTHER_DEDUCTIONS = {"schc": "schc/II/27b", "1065": "1065/p1/21", "1120s": "1120s/p1/20", "1120": "1120/p1/26"}
_CONCEPT_LINES: dict[str, dict[str, str | None]] = {
    "gross_receipts": {"schc": "schc/I/1", "1065": "1065/p1/1a", "1120s": "1120s/p1/1a", "1120": "1120/p1/1a"},
    "returns_allowances": {"schc": "schc/I/2", "1065": "1065/p1/1b", "1120s": "1120s/p1/1b", "1120": "1120/p1/1b"},
    "cogs": {"schc": "schc/I/4", "1065": "1065/p1/2", "1120s": "1120s/p1/2", "1120": "1120/p1/2"},
    "other_income": {"schc": "schc/I/6", "1065": "1065/p1/7", "1120s": "1120s/p1/5", "1120": "1120/p1/10"},
    "interest_income": {"schc": "schc/none/personal", "1065": "1065/K/5", "1120s": "1120s/K/4", "1120": "1120/p1/5"},
    "dividend_income": {"schc": "schc/none/personal", "1065": "1065/K/6a", "1120s": "1120s/K/5a", "1120": "1120/p1/4"},
    "advertising": {"schc": "schc/II/8", "1120s": "1120s/p1/16", "1120": "1120/p1/22"},
    "car_truck": {"schc": "schc/II/9"},
    "commissions": {"schc": "schc/II/10"},
    "contract_labor": {"schc": "schc/II/11"},
    "depletion": {"schc": "schc/II/12", "1065": "1065/p1/17", "1120s": "1120s/p1/15", "1120": "1120/p1/21"},
    "depreciation": {"schc": "schc/II/13", "1065": "1065/p1/16a", "1120s": "1120s/p1/14", "1120": "1120/p1/20"},
    "employee_benefits": {"schc": "schc/II/14", "1065": "1065/p1/19", "1120s": "1120s/p1/18", "1120": "1120/p1/24"},
    "insurance": {"schc": "schc/II/15"},
    "interest_mortgage": {"schc": "schc/II/16a", "1065": "1065/p1/15", "1120s": "1120s/p1/13", "1120": "1120/p1/18"},
    "interest_expense": {"schc": "schc/II/16b", "1065": "1065/p1/15", "1120s": "1120s/p1/13", "1120": "1120/p1/18"},
    "legal_professional": {"schc": "schc/II/17"},
    "office": {"schc": "schc/II/18"},
    "retirement": {"schc": "schc/II/19", "1065": "1065/p1/18", "1120s": "1120s/p1/17", "1120": "1120/p1/23"},
    "rent_equipment": {"schc": "schc/II/20a", "1065": "1065/p1/13", "1120s": "1120s/p1/11", "1120": "1120/p1/16"},
    "rent": {"schc": "schc/II/20b", "1065": "1065/p1/13", "1120s": "1120s/p1/11", "1120": "1120/p1/16"},
    "repairs": {"schc": "schc/II/21", "1065": "1065/p1/11", "1120s": "1120s/p1/9", "1120": "1120/p1/14"},
    "supplies": {"schc": "schc/II/22"},
    "taxes_licenses": {"schc": "schc/II/23", "1065": "1065/p1/14", "1120s": "1120s/p1/12", "1120": "1120/p1/17"},
    "travel": {"schc": "schc/II/24a"},
    "meals": {"schc": "schc/II/24b"},
    "utilities": {"schc": "schc/II/25"},
    "wages": {"schc": "schc/II/26", "1065": "1065/p1/9", "1120s": "1120s/p1/8", "1120": "1120/p1/13"},
    "officer_compensation": {"schc": None, "1065": None, "1120s": "1120s/p1/7", "1120": "1120/p1/12"},
    "guaranteed_payments": {"schc": None, "1065": "1065/p1/10", "1120s": None, "1120": None},
    "bad_debts": {"1065": "1065/p1/12", "1120s": "1120s/p1/10", "1120": "1120/p1/15"},
    "charitable": {"schc": "schc/none/personal", "1065": "1065/K/13a", "1120s": "1120s/K/12a", "1120": "1120/p1/19"},
    "home_office": {"schc": "schc/II/30"},
    "nondeductible": {"schc": "schc/none/nondeductible", "1065": "1065/M-1/nondeductible",
                      "1120s": "1120s/M-1/nondeductible", "1120": "1120/M-1/nondeductible"},
    "other_deductions": {},
    # Balance sheet.
    "cash": {"1065": "1065/L/1", "1120s": "1120s/L/1", "1120": "1120/L/1"},
    "accounts_receivable": {"1065": "1065/L/2a", "1120s": "1120s/L/2a", "1120": "1120/L/2a"},
    "inventory": {"1065": "1065/L/3", "1120s": "1120s/L/3", "1120": "1120/L/3"},
    "other_current_assets": {"1065": "1065/L/6", "1120s": "1120s/L/6", "1120": "1120/L/6"},
    "loans_to_owners": {"1065": "1065/L/7a", "1120s": "1120s/L/7", "1120": "1120/L/7"},
    "fixed_assets": {"1065": "1065/L/9a", "1120s": "1120s/L/10a", "1120": "1120/L/10a"},
    "accumulated_depreciation": {"1065": "1065/L/9b", "1120s": "1120s/L/10b", "1120": "1120/L/10b"},
    "land": {"1065": "1065/L/11", "1120s": "1120s/L/12", "1120": "1120/L/12"},
    "intangibles": {"1065": "1065/L/12a", "1120s": "1120s/L/13a", "1120": "1120/L/13a"},
    "accumulated_amortization": {"1065": "1065/L/12b", "1120s": "1120s/L/13b", "1120": "1120/L/13b"},
    "other_assets": {"1065": "1065/L/13", "1120s": "1120s/L/14", "1120": "1120/L/14"},
    "accounts_payable": {"1065": "1065/L/15", "1120s": "1120s/L/16", "1120": "1120/L/16"},
    "short_term_debt": {"1065": "1065/L/16", "1120s": "1120s/L/17", "1120": "1120/L/17"},
    "other_current_liabilities": {"1065": "1065/L/17", "1120s": "1120s/L/18", "1120": "1120/L/18"},
    "loans_from_owners": {"1065": "1065/L/19a", "1120s": "1120s/L/19", "1120": "1120/L/19"},
    "long_term_debt": {"1065": "1065/L/19b", "1120s": "1120s/L/20", "1120": "1120/L/20"},
    "other_liabilities": {"1065": "1065/L/20", "1120s": "1120s/L/21", "1120": "1120/L/21"},
    "capital_stock": {"schc": "schc/none/owner_equity", "1065": "1065/L/21", "1120s": "1120s/L/22", "1120": "1120/L/22b"},
    "contributions": {"schc": "schc/none/owner_equity", "1065": "1065/L/21", "1120s": "1120s/L/23", "1120": "1120/L/23"},
    "retained_earnings": {"schc": "schc/none/owner_equity", "1065": "1065/L/21", "1120s": "1120s/L/24", "1120": "1120/L/25"},
    "distributions": {"schc": "schc/none/owner_equity", "1065": "1065/M-2/6a", "1120s": "1120s/M-2/7", "1120": "1120/M-2/5a"},
}
_BALANCE_SHEET_CONCEPTS = {
    "cash", "accounts_receivable", "inventory", "other_current_assets", "loans_to_owners", "fixed_assets",
    "accumulated_depreciation", "land", "intangibles", "accumulated_amortization", "other_assets",
    "accounts_payable", "short_term_debt", "other_current_liabilities", "loans_from_owners",
    "long_term_debt", "other_liabilities",
}
_CONCEPT_LABELS = {
    "advertising": "Advertising", "car_truck": "Car and truck expenses", "commissions": "Commissions and fees",
    "contract_labor": "Contract labor", "insurance": "Insurance", "legal_professional": "Legal and professional services",
    "office": "Office expense", "supplies": "Supplies", "travel": "Travel", "meals": "Meals (full book amount)",
    "utilities": "Utilities", "bad_debts": "Bad debts", "home_office": "Business use of home",
}


# Name rules: (root, concept, any-of token sets). The first match wins. A rule
# token set matches when all of its tokens appear in the account's tokens.
_RULES: list[tuple[str, str, tuple[frozenset[str], ...]]] = []


def _rule(root: str, concept: str, *patterns: str) -> None:
    _RULES.append((root, concept, tuple(frozenset(p.split()) for p in patterns)))


for _p in ("returns", "refunds", "refund", "allowances", "chargebacks", "discounts"):
    _rule("Income", "returns_allowances", _p)
_rule("Income", "interest_income", "interest")
_rule("Income", "dividend_income", "dividend", "dividends")
_rule("Income", "other_income", "other", "misc", "miscellaneous", "gain", "gains", "rent", "rental")

_rule("Expenses", "nondeductible", "income tax", "income taxes", "penalties", "penalty", "fines",
      "entertainment", "nondeductible", "political")
_rule("Expenses", "cogs", "cogs", "cost goods", "cost sales", "merchandise", "inventory", "materials")
_rule("Expenses", "officer_compensation", "officer", "officers")
_rule("Expenses", "guaranteed_payments", "guaranteed")
_rule("Expenses", "taxes_licenses", "tax", "taxes", "licenses", "license", "permits")
_rule("Expenses", "retirement", "retirement", "401k", "pension", "profit sharing")
_rule("Expenses", "employee_benefits", "benefits", "health")
_rule("Expenses", "wages", "wages", "salaries", "salary", "payroll")
_rule("Expenses", "insurance", "insurance")
_rule("Expenses", "interest_mortgage", "mortgage interest", "mortgage")
_rule("Expenses", "interest_expense", "interest")
_rule("Expenses", "depreciation", "depreciation")
_rule("Expenses", "depletion", "depletion")
_rule("Expenses", "bad_debts", "bad debt", "bad debts")
_rule("Expenses", "charitable", "charitable", "charity", "donations", "donation", "contributions")
_rule("Expenses", "home_office", "home office", "business use home")
_rule("Expenses", "advertising", "advertising", "marketing", "ads", "promotion")
_rule("Expenses", "car_truck", "vehicle", "vehicles", "auto", "car", "truck", "mileage", "fuel")
_rule("Expenses", "contract_labor", "subcontractors", "subcontractor", "contractors", "contractor",
      "contract labor", "freelancers", "freelance")
_rule("Expenses", "commissions", "commission", "commissions")
_rule("Expenses", "legal_professional", "legal", "professional", "accounting", "bookkeeping", "attorney", "cpa")
_rule("Expenses", "rent_equipment", "equipment rent", "equipment lease", "equipment rental",
      "vehicle lease", "machinery")
_rule("Expenses", "rent", "rent", "rental", "lease", "coworking")
_rule("Expenses", "repairs", "repairs", "repair", "maintenance")
_rule("Expenses", "supplies", "supplies")
_rule("Expenses", "office", "office", "postage")
_rule("Expenses", "travel", "travel", "airfare", "lodging", "hotel", "hotels")
_rule("Expenses", "meals", "meals", "meal")
_rule("Expenses", "utilities", "utilities", "utility", "electric", "internet", "phone", "telephone")
_rule("Expenses", "other_deductions", "software", "hosting", "subscriptions", "saas", "cloud", "fees", "fee",
      "bank", "merchant", "dues", "training", "education", "shipping", "amortization")

_rule("Assets", "accumulated_amortization", "accumulated amortization")
_rule("Assets", "accumulated_depreciation", "accumulated depreciation", "accumulated")
_rule("Assets", "loans_to_owners", "shareholder", "shareholders", "partner", "partners", "owner",
      "officer", "member")
_rule("Assets", "accounts_receivable", "receivable", "receivables", "ar")
_rule("Assets", "inventory", "inventory")
_rule("Assets", "land", "land")
_rule("Assets", "intangibles", "intangible", "intangibles", "goodwill")
_rule("Assets", "fixed_assets", "equipment", "furniture", "vehicles", "vehicle", "computers", "computer",
      "machinery", "buildings", "building", "fixed", "improvements")
_rule("Assets", "other_current_assets", "prepaid", "clearing", "undeposited", "transit")
_rule("Assets", "other_assets", "deposit", "deposits", "investments", "investment")
_rule("Assets", "cash", "bank", "checking", "savings", "cash", "money market", "petty", "treasury")

_rule("Liabilities", "loans_from_owners", "shareholder", "shareholders", "partner", "partners", "owner",
      "officer", "member")
_rule("Liabilities", "short_term_debt", "line credit", "loc")
_rule("Liabilities", "long_term_debt", "loan", "loans", "note", "notes", "mortgage", "sba")
_rule("Liabilities", "accounts_payable", "payable", "payables", "ap")
_rule("Liabilities", "other_current_liabilities", "credit card", "creditcard", "card", "cards", "amex",
      "visa", "mastercard", "payroll", "sales tax", "accrued", "deferred", "withholding", "taxes", "tax")

_rule("Equity", "distributions", "draws", "draw", "distributions", "distribution", "dividends")
_rule("Equity", "capital_stock", "stock")
_rule("Equity", "contributions", "contributions", "contribution", "paid in", "capital", "investment")
_rule("Equity", "retained_earnings", "retained", "opening", "prior periods net income")

# Account-type defaults: only cases where the root alone is a safe guess.
_TYPE_DEFAULTS = {"Income": "gross_receipts"}


def _tokens(account: str) -> set[str]:
    parts = account.split(":")[1:]
    tokens: set[str] = set()
    for part in parts:
        for token in re.split(r"[-_\s]+", part.lower()):
            if token:
                tokens.add(token)
    return tokens


def _detail_label(account: str) -> str:
    leaf = account.split(":")[-1]
    return re.sub(r"[-_]+", " ", leaf).strip()


def normalize_form(value: str) -> str:
    key = str(value or "").strip().lower().replace(" ", "-")
    if key not in FORM_ALIASES:
        raise ValueError(f"Unsupported return form '{value}'. Use one of: schc, 1065, 1120s, 1120.")
    return FORM_ALIASES[key]


def infer_form(legal_structure: str) -> str | None:
    """Infer the return form from the free-text legal structure, or None if unclear."""
    text = f" {str(legal_structure or '').lower()} "
    text = re.sub(r"[^a-z0-9]+", " ", text)
    if re.search(r" (s corp|s corporation|scorp|1120 s|1120s|s election) ", text):
        return "1120s"
    if re.search(r" (partnership|multi member|multimember|lp|llp|1065) ", text):
        return "1065"
    if re.search(r" (sole|single member|smllc|schedule c|disregarded|1040) ", text):
        return "schc"
    if re.search(r" (c corp|c corporation|ccorp|corporation|inc|1120) ", text):
        return "1120"
    return None


# ---------------------------------------------------------------------------
# Workspace settings
# ---------------------------------------------------------------------------


def _settings(config: Mapping[str, Any]) -> dict[str, Any]:
    raw = config.get("tax_handoff")
    return dict(raw) if isinstance(raw, Mapping) else {}


def resolve_form(config: Mapping[str, Any], requested: str | None = None) -> tuple[str, str]:
    """Return (form, basis) where basis is argument, workspace or legal_structure."""
    if requested:
        return normalize_form(requested), "argument"
    stored = _settings(config).get("return_form")
    if stored:
        return normalize_form(str(stored)), "workspace"
    inferred = infer_form(str(config.get("legal_structure") or ""))
    if inferred:
        return inferred, "legal_structure"
    raise ValueError(
        "The return form is unknown. Pass --form or record it with "
        "'books tax set --return-form' (schc, 1065, 1120s or 1120)."
    )


@_locked_entity_operation
def update_settings(
    entity_path: Path | str,
    *,
    return_form: str | None = None,
    form: str | None = None,
    account: str | None = None,
    line: str | None = None,
    clear_line: bool = False,
    account_number: str | None = None,
    ultratax_code: str | None = None,
) -> dict[str, Any]:
    """Record the return form and per-account overrides in ``entity.json``."""
    entity = load_entity(entity_path)
    settings = _settings(entity.entity_config)
    changes: dict[str, Any] = {}
    if return_form:
        settings["return_form"] = normalize_form(return_form)
        changes["return_form"] = settings["return_form"]
    per_account = line is not None or clear_line or account_number is not None or ultratax_code is not None
    if per_account:
        if not account:
            raise ValueError("--account is required for a line, account number or UltraTax code.")
        known = _catalog_accounts(entity.path)
        if known and account not in known:
            raise ValueError(f"Account '{account}' is not in the account catalog.")
    if line is not None or clear_line:
        target_form = normalize_form(form) if form else resolve_form(entity.entity_config)[0]
        overrides = dict(settings.get("line_overrides") or {})
        form_overrides = dict(overrides.get(target_form) or {})
        if clear_line:
            form_overrides.pop(account, None)
            changes["line"] = None
        else:
            if line not in CATALOG or CATALOG[line].form != target_form:
                raise ValueError(f"Line '{line}' is not a {FORMS[target_form]} line. See 'books tax lines --form {target_form}'.")
            form_overrides[account] = line
            changes["line"] = line
        overrides[target_form] = dict(sorted(form_overrides.items()))
        settings["line_overrides"] = overrides
        changes["form"] = target_form
    if account_number is not None:
        value = account_number.strip()
        if value and not re.fullmatch(r"[A-Za-z0-9.\-]{1,20}", value):
            raise ValueError("Account numbers must be 1-20 letters, digits, dots or hyphens.")
        numbers = dict(settings.get("account_numbers") or {})
        if value:
            numbers[account] = value
        else:
            numbers.pop(account, None)
        settings["account_numbers"] = dict(sorted(numbers.items()))
        changes["account_number"] = value or None
    if ultratax_code is not None:
        value = ultratax_code.strip()
        if value and not re.fullmatch(r"[A-Za-z0-9.\-]{1,20}", value):
            raise ValueError("UltraTax codes must be 1-20 letters, digits, dots or hyphens.")
        codes = dict(settings.get("ultratax_codes") or {})
        if value:
            codes[account] = value
        else:
            codes.pop(account, None)
        settings["ultratax_codes"] = dict(sorted(codes.items()))
        changes["ultratax_code"] = value or None
    if not changes:
        raise ValueError("Nothing to change. Pass --return-form, --line, --clear-line, --account-number or --ultratax-code.")
    entity.entity_config["tax_handoff"] = settings
    destination = entity.path / "entity.json"
    tmp = destination.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entity.entity_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, destination)
    return {"account": account, **changes}


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountMapping:
    account: str
    line: TaxLine | None
    basis: str  # override | name | account_type | unmapped
    detail: str
    review: bool
    reason: str

    def to_document(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "line": self.line.code if self.line else None,
            "form_line": self.line.line if self.line else None,
            "schedule": self.line.schedule if self.line else None,
            "label": self.line.label if self.line else None,
            "on_return": self.line.on_return if self.line else None,
            "detail": self.detail or None,
            "basis": self.basis,
            "review": self.review,
            "reason": self.reason,
        }


def _concept_for(account: str) -> tuple[str | None, str]:
    root = account.split(":")[0]
    tokens = _tokens(account)
    for rule_root, concept, patterns in _RULES:
        if rule_root == root and any(pattern <= tokens for pattern in patterns):
            return concept, "name"
    if root in _TYPE_DEFAULTS:
        return _TYPE_DEFAULTS[root], "account_type"
    return None, "unmapped"


def map_account(account: str, form: str, overrides: Mapping[str, str] | None = None) -> AccountMapping:
    form = normalize_form(form)
    override = (overrides or {}).get(account)
    if override:
        line = CATALOG.get(override)
        if line is None or line.form != form:
            return AccountMapping(account, None, "unmapped", "", True,
                            f"The stored override '{override}' is not a {FORMS[form]} line.")
        detail = _detail_label(account) if line.code == _OTHER_DEDUCTIONS.get(form) else ""
        return AccountMapping(account, line, "override", detail, False, "Set by the user.")
    if account == PRIOR_EARNINGS_ACCOUNT:
        concept, basis = "retained_earnings", "account_type"
    else:
        concept, basis = _concept_for(account)
    if concept is None:
        return AccountMapping(account, None, "unmapped", "", True, "No name rule or safe default matches this account.")
    if form == "schc" and concept in _BALANCE_SHEET_CONCEPTS:
        return AccountMapping(account, CATALOG["schc/none/balance_sheet"], basis, "", False,
                        "Schedule C has no balance sheet.")
    table = _CONCEPT_LINES[concept]
    if form in table:
        code = table[form]
        if code is None:
            return AccountMapping(account, None, "unmapped", "", True,
                            f"A {concept.replace('_', ' ')} account has no line on {FORMS[form]}.")
        detail = ""
    elif account.startswith("Expenses:"):
        code = _OTHER_DEDUCTIONS[form]
        detail = _CONCEPT_LABELS.get(concept) or _detail_label(account)
    else:
        return AccountMapping(account, None, "unmapped", "", True, f"No {FORMS[form]} line for this account type.")
    line = CATALOG[code]
    if code == _OTHER_DEDUCTIONS[form] and not detail:
        detail = _detail_label(account)
    reason = "Matched by account name." if basis == "name" else "Default for the account type."
    return AccountMapping(account, line, basis, detail, True, reason)


def build_mapping(config: Mapping[str, Any], accounts: Iterable[str], form: str) -> list[AccountMapping]:
    overrides = (_settings(config).get("line_overrides") or {}).get(form) or {}
    return [map_account(account, form, overrides) for account in sorted(set(accounts))]


# ---------------------------------------------------------------------------
# Balances (engine-computed; no tax math)
# ---------------------------------------------------------------------------


def _catalog_accounts(entity_path: Path) -> set[str]:
    from .ledger.store import LedgerStore, default_store_path

    path = default_store_path(entity_path)
    return LedgerStore(path).load_account_names() if path.is_file() else set()


def _coverage_metadata(conn: Any, from_date: date, to_date: date) -> dict[str, Any]:
    """Coverage facts for a handoff. The journal before the tracking start is the cash books, so the cash view is
    checked: a period that crosses the start is never refused here. Other coverage checks still apply."""
    from .accrual.reports import financial_metadata
    return financial_metadata(conn, "tax_handoff", from_date, to_date, view="cash")


def _accounting_metadata(conn: Any, config: Mapping[str, Any], from_date: date, to_date: date) -> dict[str, Any]:
    """Coverage facts plus the basis of the recorded books, whatever the default report view is.

    Every entry is an accrual entry when accrual rules apply to all transactions. A company that turned accrual on later
    records accrual entries only from its tracking start, and only once it holds accrual records.
    """
    from .reports.statements import has_accrual_records
    result = _coverage_metadata(conn, from_date, to_date)
    kind = discipline(config)
    started = tracking_start(config)
    mixed = (kind == DISCIPLINE_PER_ACCOUNT and has_accrual_records(conn)
             and (not started or to_date.isoformat() >= started))
    result["basis"] = "accrual" if kind == DISCIPLINE_ALL or mixed else "cash"
    return result


def period_balances(entity_path: Path | str, from_date: date, to_date: date) -> list[dict[str, Any]]:
    """Fiscal-period trial balance rows.

    Balance sheet accounts carry their balance at the start and end of the
    period. Income and expense accounts carry activity inside the period.
    Income and expense activity before the period appears as one computed
    ``Equity:Prior-Periods-Net-Income`` row, because the books are not closed
    into retained earnings. Balances are debit-positive.
    """
    from .reports.cache import iter_postings, list_accounts, open_cache

    entity_path = Path(entity_path)
    opening: dict[str, Decimal] = {}
    ending: dict[str, Decimal] = {}
    activity: dict[str, Decimal] = {}
    prior_earnings = _ZERO
    conn = open_cache(entity_path)
    try:
        _coverage_metadata(conn, from_date, to_date)
        accounts = {item["name"] for item in list_accounts(conn)}
        for row in iter_postings(conn, to_date=to_date):
            day, account, amount = str(row[0])[:10], row[3], row[4]
            accounts.add(account)
            before = day < from_date.isoformat()
            root = account.split(":")[0]
            if root in {"Income", "Expenses"}:
                if before:
                    prior_earnings += amount
                else:
                    activity[account] = activity.get(account, _ZERO) + amount
            else:
                ending[account] = ending.get(account, _ZERO) + amount
                if before:
                    opening[account] = opening.get(account, _ZERO) + amount
    finally:
        conn.close()
    rows = []
    for account in sorted(accounts):
        root = account.split(":")[0]
        if root in {"Income", "Expenses"}:
            begin, balance = None, activity.get(account, _ZERO)
        else:
            begin, balance = opening.get(account, _ZERO), ending.get(account, _ZERO)
        rows.append({"account": account, "type": root, "beginning": begin, "balance": balance})
    if prior_earnings != _ZERO:
        rows.append({"account": PRIOR_EARNINGS_ACCOUNT, "type": "Equity",
                     "beginning": prior_earnings, "balance": prior_earnings, "computed": True})
    return rows


# ---------------------------------------------------------------------------
# Reports and export
# ---------------------------------------------------------------------------


def _money(value: Decimal | None) -> str:
    return "" if value is None else str(value.quantize(_CENT))


def _account_numbers(rows: Sequence[Mapping[str, Any]], stored: Mapping[str, str]) -> dict[str, str]:
    """Stored numbers win; others get stable numbers by type and name order."""
    bases = {"Assets": 1000, "Liabilities": 2000, "Equity": 3000, "Income": 4000, "Expenses": 6000}
    used = set(stored.values())
    numbers: dict[str, str] = {}
    counters: dict[str, int] = {}
    for row in sorted(rows, key=lambda r: r["account"]):
        account = row["account"]
        if account in stored:
            numbers[account] = stored[account]
            continue
        root = row["type"]
        counters[root] = counters.get(root, 0) + 10
        candidate = bases.get(root, 9000) + counters[root]
        while str(candidate) in used:
            candidate += 1
        used.add(str(candidate))
        numbers[account] = str(candidate)
    return numbers


def mapping_report(entity_path: Path | str, *, form: str | None = None,
                   from_date: date | None = None, to_date: date | None = None) -> dict[str, Any]:
    """Mapping for every account; balances when a period is given."""
    entity = load_entity(entity_path)
    resolved, form_basis = resolve_form(entity.entity_config, form)
    rows = period_balances(entity.path, from_date, to_date) if from_date and to_date else [
        {"account": account, "type": account.split(":")[0], "beginning": None, "balance": None}
        for account in sorted(_catalog_accounts(entity.path))
    ]
    mapping = {m.account: m for m in build_mapping(entity.entity_config, (r["account"] for r in rows), resolved)}
    settings = _settings(entity.entity_config)
    numbers = settings.get("account_numbers") or {}
    ultratax = settings.get("ultratax_codes") or {}
    accounts = []
    for row in rows:
        item = mapping[row["account"]].to_document()
        # Recorded values only; an export may still assign a generated number.
        item["account_number"] = numbers.get(row["account"]) or None
        item["return_type"] = resolved
        item["ultratax_code"] = ultratax.get(row["account"]) or None
        if row["balance"] is not None:
            item["balance"] = _money(row["balance"])
        accounts.append(item)
    unmapped = [item for item in accounts if item["line"] is None]
    return {
        "schema_version": 1,
        "kind": "tax_line_mapping",
        "disclaimer": DISCLAIMER,
        "form": resolved,
        "form_name": FORMS[resolved],
        "form_basis": form_basis,
        "line_numbers": f"Tax year {TAX_YEAR_BASIS} federal forms",
        "from": from_date.isoformat() if from_date else None,
        "to": to_date.isoformat() if to_date else None,
        "accounts": accounts,
        "unmapped": [item["account"] for item in unmapped],
        "needs_review": [item["account"] for item in accounts if item["review"] and item["line"]],
    }


def _signed_amount(balance: Decimal, line: TaxLine | None) -> Decimal:
    """Book amount on the line's normal side (debit-normal keeps the sign)."""
    if line is None:
        return balance
    return balance if line.normal == "debit" else -balance


def build_handoff(entity_path: Path | str, from_date: date, to_date: date, *, form: str | None = None) -> dict[str, Any]:
    """Assemble every row of the handoff package without writing files."""
    if from_date > to_date:
        raise ValueError("The period start must not be after its end.")
    entity = load_entity(entity_path)
    resolved, form_basis = resolve_form(entity.entity_config, form)
    from .reports.cache import open_cache
    from .reports.statements import has_accrual_records
    conn = open_cache(entity.path)
    try:
        accounting = _accounting_metadata(conn, entity.entity_config, from_date, to_date)
        # A company that turned accrual on later and now holds accrual records keeps cash entries before its tracking
        # start and accrual records from it, so one label for the whole period would be wrong.
        records = discipline(entity.entity_config) == DISCIPLINE_PER_ACCOUNT and has_accrual_records(conn)
    finally:
        conn.close()
    settings = _settings(entity.entity_config)
    basis_label = None
    # Only once accrual records exist. Turning tracking on changes no export.
    if records:
        started = tracking_start(entity.entity_config)
        if started:
            basis_label = (f"Cash basis, accrual tracking for selected accounts from {started} "
                           f"(cash entries before {started}, accrual records from {started}). "
                           "Book balances computed by the Slashbooks engine, not a tax cash-basis view; the preparer adjusts")
        else:
            basis_label = ("Cash basis, accrual tracking for selected accounts from a start date that is not recorded; "
                           "book balances computed by the Slashbooks engine")
    rows = [r for r in period_balances(entity.path, from_date, to_date)
            if r["balance"] != _ZERO or (r["beginning"] not in (None, _ZERO))]
    mapping = {m.account: m for m in build_mapping(entity.entity_config, (r["account"] for r in rows), resolved)}
    numbers = _account_numbers(rows, settings.get("account_numbers") or {})
    ultratax = settings.get("ultratax_codes") or {}

    tb_rows = []
    total_debit = total_credit = _ZERO
    for row in rows:
        m = mapping[row["account"]]
        balance = row["balance"]
        debit = balance if balance > 0 else _ZERO
        credit = -balance if balance < 0 else _ZERO
        total_debit += debit
        total_credit += credit
        tb_rows.append({
            "account_number": numbers[row["account"]],
            "account": row["account"],
            "account_name": row["account"].replace(":", " › "),
            "account_type": row["type"],
            "computed": bool(row.get("computed")),
            "beginning_balance": row["beginning"],
            "debit": debit,
            "credit": credit,
            "balance": balance,
            "mapping": m,
            "ultratax_code": ultratax.get(row["account"], ""),
        })

    summary: dict[str, dict[str, Any]] = {}
    for row in tb_rows:
        m = row["mapping"]
        if m.line is None:
            continue
        key = (m.line.code, m.detail)
        item = summary.setdefault(key, {"line": m.line, "detail": m.detail, "amount": _ZERO,
                                        "beginning": None, "accounts": []})
        item["accounts"].append(row["account"])
        if m.line.measure == "activity" and row["beginning_balance"] is not None:
            item["amount"] += _signed_amount(row["balance"] - row["beginning_balance"], m.line)
            continue
        item["amount"] += _signed_amount(row["balance"], m.line)
        if row["beginning_balance"] is not None:
            item["beginning"] = (item["beginning"] or _ZERO) + _signed_amount(row["beginning_balance"], m.line)
    order = {code: index for index, code in enumerate(CATALOG)}
    summary_rows = sorted(summary.values(), key=lambda s: (order[s["line"].code], s["detail"]))
    unmapped = [row for row in tb_rows if row["mapping"].line is None]
    return {
        "entity_name": entity.name,
        "accounting_basis": accounting["basis"],
        "basis_label": basis_label,
        "accounting": accounting,
        "form": resolved,
        "form_basis": form_basis,
        "from": from_date,
        "to": to_date,
        "trial_balance": tb_rows,
        "summary": summary_rows,
        "unmapped": unmapped,
        "totals": {"debit": total_debit, "credit": total_credit, "difference": total_debit - total_credit},
    }


DISCLAIMER = (
    "Draft for review. Prepared from the data in these books only. It is not tax advice and does "
    "not replace preparation or review by a qualified tax professional."
)
# Vendor import layouts (Lacerte, UltraTax) must start with their header row,
# so they carry no note row; README.txt and the manifest carry the disclaimer.
_NOTE_TABLES = frozenset({"Trial Balance", "Tax Line Summary", "Unmapped Accounts"})


def _readme_text(handoff: Mapping[str, Any], files: Mapping[str, str]) -> str:
    lines = [DISCLAIMER, ""]
    lines += [f"{item}: {value}" for item, value in _readme_table(handoff, {})[2:]]
    lines += ["", "Files:"]
    lines += [f"  {name} ({sheet})" for sheet, name in files.items()]
    lines += ["", "The Lacerte and UltraTax files start with the header row that those products "
              "import, so they do not repeat this note."]
    return "\n".join(lines) + "\n"


_TB_HEADER = [
    "Account Number", "Account", "Account Type", "Beginning Balance", "Debit", "Credit", "Balance",
    "Tax Form", "Schedule", "Form Line", "Tax Line Code", "Tax Line", "Statement Detail",
    "On Return", "Mapping Basis", "Needs Review", "Note",
]


def _tb_table(handoff: Mapping[str, Any]) -> list[list[str]]:
    rows = [list(_TB_HEADER)]
    for row in handoff["trial_balance"]:
        m: AccountMapping = row["mapping"]
        line = m.line
        note = line.note if line else m.reason
        if row["computed"]:
            note = ("Computed: income and expenses before the period, not yet closed to equity. " + note).strip()
        rows.append([
            row["account_number"], row["account_name"], row["account_type"], _money(row["beginning_balance"]),
            _money(row["debit"]), _money(row["credit"]), _money(row["balance"]),
            FORMS[handoff["form"]], line.schedule if line else "", line.line if line else "",
            line.code if line else "UNMAPPED", line.label if line else "", m.detail,
            ("yes" if line.on_return else "no") if line else "", m.basis, "yes" if m.review else "no", note,
        ])
    totals = handoff["totals"]
    rows.append(["", "Total", "", "", _money(totals["debit"]), _money(totals["credit"]),
                 _money(totals["difference"]), "", "", "", "", "", "", "", "", "", ""])
    return rows


def _summary_table(handoff: Mapping[str, Any]) -> list[list[str]]:
    rows = [["Tax Form", "Schedule", "Form Line", "Tax Line Code", "Tax Line", "Statement Detail",
             "Beginning Book Amount", "Book Amount", "Normal Side", "On Return", "Accounts", "Note"]]
    for item in handoff["summary"]:
        line: TaxLine = item["line"]
        rows.append([
            FORMS[line.form], line.schedule, line.line, line.code, line.label, item["detail"],
            _money(item["beginning"]), _money(item["amount"]), line.normal, "yes" if line.on_return else "no",
            "; ".join(item["accounts"]), line.note,
        ])
    return rows


def _unmapped_table(handoff: Mapping[str, Any]) -> list[list[str]]:
    rows = [["Account Number", "Account", "Account Type", "Balance", "Reason", "How to fix"]]
    for row in handoff["unmapped"]:
        rows.append([
            row["account_number"], row["account_name"], row["account_type"], _money(row["balance"]),
            row["mapping"].reason,
            f"books tax set --entity <company> --form {handoff['form']} --account {row['account']} --line <code>",
        ])
    return rows


_LACERTE_TYPES = {"Assets": "A", "Liabilities": "L", "Equity": "L", "Income": "R", "Expenses": "E"}


def _lacerte_table(handoff: Mapping[str, Any]) -> list[list[str]]:
    rows = [["Account Number", "Account Name", "Account Type", "Balance", "Slashbooks Tax Line"]]
    for row in handoff["trial_balance"]:
        m: AccountMapping = row["mapping"]
        rows.append([
            row["account_number"], row["account_name"], _LACERTE_TYPES.get(row["account_type"], ""),
            _money(row["balance"]), f"{m.line.code} {m.line.label}" if m.line else "UNMAPPED",
        ])
    return rows


def _ultratax_table(handoff: Mapping[str, Any]) -> list[list[str]]:
    rows = [["Account Number", "Account Description", "Unit", "Tax Code", "Amount"]]
    for row in handoff["trial_balance"]:
        rows.append([row["account_number"], row["account_name"][:60], "", row["ultratax_code"],
                     _money(row["balance"])])
    return rows


def _coverage_summary(accounting: Mapping[str, Any]) -> str:
    """A short Read Me line. The full accounting block stays in manifest.json."""
    coverage = accounting.get("coverage")
    if not coverage:
        return ""
    parts = [f"{accounting.get('basis', 'cash')} basis", f"cutover {coverage.get('cutover_date') or 'not set'}"]
    if coverage.get("summary_from") and coverage.get("summary_to"):
        parts.append(f"summary period {coverage['summary_from']} to {coverage['summary_to']}"
                     + (" (included)" if accounting.get("summary_included") else " (not included)"))
    detail = accounting.get("historical_detail_available", True)
    parts.append("historical detail " + ("available" if detail else "not available before the cutover"))
    return "; ".join(parts) + ". Full details are in manifest.json."


def _readme_table(handoff: Mapping[str, Any], files: Mapping[str, str]) -> list[list[str]]:
    totals = handoff["totals"]
    rows = [
        ["Item", "Value"],
        ["Disclaimer", DISCLAIMER],
        ["Company", handoff["entity_name"]],
        ["Return", f"{FORMS[handoff['form']]} ({handoff['form']}, chosen by {handoff['form_basis']})"],
        ["Line numbers", f"Tax year {TAX_YEAR_BASIS} federal forms; confirm against the current year's form"],
        ["Period", f"{handoff['from'].isoformat()} to {handoff['to'].isoformat()}"],
        ["Basis", handoff.get("basis_label") or handoff["accounting_basis"].title() + " basis book balances computed by the Slashbooks engine"],
        ["Total debits", _money(totals["debit"])],
        ["Total credits", _money(totals["credit"])],
        ["Unmapped accounts", str(len(handoff["unmapped"]))],
        ["Sign convention", "Balance columns are debit-positive and credit-negative"],
        ["Tax computation", "None. Book amounts only; the preparer makes all tax adjustments"],
    ]
    summary = _coverage_summary(handoff.get("accounting") or {})
    if summary:
        rows.append(["Accounting coverage", summary])
    for name, path in files.items():
        rows.append([name, path])
    return rows


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(neutralize_rows(rows))


def export_handoff(
    entity_path: Path | str,
    from_date: date,
    to_date: date,
    *,
    form: str | None = None,
    output_dir: Path | None = None,
    formats: Sequence[str] = ("csv", "xlsx"),
    require_mapped: bool = False,
) -> dict[str, Any]:
    """Write the handoff files and return a manifest."""
    handoff = build_handoff(entity_path, from_date, to_date, form=form)
    if require_mapped and handoff["unmapped"]:
        names = ", ".join(row["account"] for row in handoff["unmapped"])
        raise ValueError(f"{len(handoff['unmapped'])} account(s) have no tax line: {names}")
    entity = load_entity(entity_path)
    period = f"{from_date.isoformat()}_{to_date.isoformat()}"
    output_dir = Path(output_dir) if output_dir else entity.reports_dir / "tax-handoff" / f"{handoff['form']}_{period}"
    stem = f"{handoff['form']}-{period}"
    tables = {
        "Trial Balance": (f"tax-trial-balance-{stem}.csv", _tb_table(handoff)),
        "Tax Line Summary": (f"tax-line-summary-{stem}.csv", _summary_table(handoff)),
        "Unmapped Accounts": (f"unmapped-accounts-{stem}.csv", _unmapped_table(handoff)),
        "Lacerte TBU": (f"lacerte-tbu-{stem}.csv", _lacerte_table(handoff)),
        "UltraTax CS Import": (f"ultratax-tb-import-{stem}.csv", _ultratax_table(handoff)),
    }
    written: dict[str, str] = {}
    unknown = set(formats) - {"csv", "xlsx"}
    if unknown:
        raise ValueError(f"Unknown export format(s): {', '.join(sorted(unknown))}. Use csv and/or xlsx.")
    if "csv" in formats:
        for sheet, (name, rows) in tables.items():
            _write_csv(output_dir / name, ([[DISCLAIMER]] if sheet in _NOTE_TABLES else []) + rows)
            written[sheet] = name
    xlsx_note = None
    if "xlsx" in formats:
        from .reports.workbook import XLSX_EXTRA_MESSAGE, XlsxSheet, _write_xlsx, _xlsxwriter_available

        if _xlsxwriter_available():
            name = f"tax-handoff-{stem}.xlsx"
            numeric = {
                "Trial Balance": frozenset({3, 4, 5, 6}),
                "Tax Line Summary": frozenset({6, 7}),
                "Unmapped Accounts": frozenset({3}),
                "Lacerte TBU": frozenset({3}),
                "UltraTax CS Import": frozenset({4}),
            }
            sheets = [XlsxSheet(name="Read Me", rows=_readme_table(handoff, {}))]
            sheets += [XlsxSheet(name=sheet, rows=rows, numeric_columns=numeric[sheet],
                                 total_label_column=1 if sheet == "Trial Balance" else None, total_label="Total")
                       for sheet, (_, rows) in tables.items()]
            _write_xlsx(output_dir / name, sheets)
            written["Workbook"] = name
        else:
            xlsx_note = XLSX_EXTRA_MESSAGE
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "README.txt").write_text(_readme_text(handoff, written), encoding="utf-8")
    written["Read Me"] = "README.txt"
    totals = handoff["totals"]
    files = {}
    for sheet, name in written.items():
        data = (output_dir / name).read_bytes()
        files[name] = {"sheet": sheet, "sha256": hashlib.sha256(data).hexdigest()}
    manifest = {
        "schema_version": 1,
        "kind": "tax_handoff",
        "disclaimer": DISCLAIMER,
        "form": handoff["form"],
        "form_name": FORMS[handoff["form"]],
        "form_basis": handoff["form_basis"],
        "accounting_basis": handoff["accounting_basis"],
        "accounting": handoff["accounting"],
        "line_numbers": f"Tax year {TAX_YEAR_BASIS} federal forms",
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "output_dir": str(output_dir),
        "files": files,
        "totals": {key: _money(value) for key, value in totals.items()},
        "accounts": len(handoff["trial_balance"]),
        "unmapped": [row["account"] for row in handoff["unmapped"]],
        "needs_review": [row["account"] for row in handoff["trial_balance"]
                         if row["mapping"].review and row["mapping"].line],
        "xlsx_note": xlsx_note,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _period(args: Any) -> tuple[date, date]:
    if getattr(args, "year", None):
        return date(args.year, 1, 1), date(args.year, 12, 31)
    if args.from_date and args.to_date:
        return date.fromisoformat(args.from_date), date.fromisoformat(args.to_date)
    raise ValueError("Pass --year or both --from and --to.")


def _add_period(parser: Any) -> None:
    parser.add_argument("--year", type=int, default=None, help="Calendar tax year, such as 2025")
    parser.add_argument("--from", dest="from_date", default=None, help="Period start (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", default=None, help="Period end (YYYY-MM-DD)")


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("tax", help="Map accounts to tax-return lines and export for a tax preparer")
    sub = parser.add_subparsers(dest="tax_command", required=True)

    lines = sub.add_parser("lines", help="List the supported tax lines for a return form")
    lines.add_argument("--form", required=True, help="schc, 1065, 1120s or 1120")
    lines.add_argument("--format", choices=["text", "json"], default="text", dest="fmt")

    mapping = sub.add_parser("map", help="Show each account's tax line, unmapped accounts and review flags")
    mapping.add_argument("--entity", required=True, type=Path)
    mapping.add_argument("--form", default=None, help="Override the company's return form")
    _add_period(mapping)
    mapping.add_argument("--format", choices=["text", "json"], default="text", dest="fmt")

    settings = sub.add_parser("set", help="Record the return form or a per-account tax line override")
    settings.add_argument("--entity", required=True, type=Path)
    settings.add_argument("--return-form", default=None, help="Company return form: schc, 1065, 1120s or 1120")
    settings.add_argument("--form", default=None, help="Form the --line override applies to; default the company form")
    settings.add_argument("--account", default=None)
    group = settings.add_mutually_exclusive_group()
    group.add_argument("--line", default=None, help="Tax line code from 'books tax lines'")
    group.add_argument("--clear-line", action="store_true", help="Remove the override and use the default")
    settings.add_argument("--account-number", default=None, help="Account number for exports; empty to clear")
    settings.add_argument("--ultratax-code", default=None, help="UltraTax CS tax code for this account; empty to clear")

    export = sub.add_parser("export", help="Write the tax trial balance, line summary and vendor import files")
    export.add_argument("--entity", required=True, type=Path)
    export.add_argument("--form", default=None, help="Override the company's return form")
    _add_period(export)
    export.add_argument("--output-dir", type=Path, default=None,
                        help="Default: <company>/reports/tax-handoff/<form>_<period>/")
    export.add_argument("--formats", default="csv,xlsx", help="Comma-separated: csv, xlsx")
    export.add_argument("--require-mapped", action="store_true", help="Fail if any nonzero account has no tax line")


def _render_mapping_text(report: Mapping[str, Any]) -> str:
    lines = [report["disclaimer"], "",
             f"Tax line mapping for {report['form_name']} (form from {report['form_basis']})"]
    for item in report["accounts"]:
        target = (f"{item['line']}  {item['label']}" + (f" — {item['detail']}" if item["detail"] else "")
                  if item["line"] else "UNMAPPED")
        flag = " [review]" if item["review"] and item["line"] else ""
        balance = f"  {item['balance']}" if "balance" in item else ""
        lines.append(f"  {item['account']}{balance} -> {target} ({item['basis']}){flag}")
    lines.append("")
    lines.append(f"Unmapped: {len(report['unmapped'])}" + (": " + ", ".join(report["unmapped"]) if report["unmapped"] else ""))
    lines.append(f"Needs review: {len(report['needs_review'])}")
    return "\n".join(lines) + "\n"


def run(args: Any) -> int:
    command = args.tax_command
    try:
        if command == "lines":
            catalog = [line.to_document() for line in lines_for(args.form)]
            if args.fmt == "json":
                print(json.dumps(catalog, indent=2, sort_keys=True))
            else:
                for line in catalog:
                    print(f"{line['code']:<26} {line['label']}" + ("" if line["on_return"] else " (not on return)"))
            return 0
        if command == "map":
            from_date = to_date = None
            if args.year or args.from_date or args.to_date:
                from_date, to_date = _period(args)
            report = mapping_report(args.entity, form=args.form, from_date=from_date, to_date=to_date)
            print(json.dumps(report, indent=2, sort_keys=True) if args.fmt == "json" else _render_mapping_text(report),
                  end="\n" if args.fmt == "json" else "")
            return 0
        if command == "set":
            result = update_settings(
                args.entity, return_form=args.return_form, form=args.form, account=args.account,
                line=args.line, clear_line=args.clear_line, account_number=args.account_number,
                ultratax_code=args.ultratax_code,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if command == "export":
            from_date, to_date = _period(args)
            formats = [item.strip() for item in args.formats.split(",") if item.strip()]
            manifest = export_handoff(args.entity, from_date, to_date, form=args.form, output_dir=args.output_dir,
                                      formats=formats, require_mapped=args.require_mapped)
            print(manifest["disclaimer"])
            print(f"Tax handoff for {manifest['form_name']} written to: {manifest['output_dir']}")
            for name in sorted(manifest["files"]):
                print(f"  {name}")
            print(f"  Debits {manifest['totals']['debit']}, credits {manifest['totals']['credit']}")
            if manifest["unmapped"]:
                print(f"  {len(manifest['unmapped'])} unmapped account(s): {', '.join(manifest['unmapped'])}")
            if manifest["xlsx_note"]:
                print(f"  {manifest['xlsx_note']}")
            return 0
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Unknown tax command: {command}", file=sys.stderr)
    return 2
