"""Thin JSON command surface for the shared accrual domain modules."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..entity import load_entity
from . import CAPABILITY_VERSION, journals
from .accounts import SUBTYPE_HELP
from .intake import IntakeValidationError, schema


def add_parser(subparsers: Any) -> None:
    root = subparsers.add_parser("accrual", help="Inspect supporting document schemas and maintain reviewable accrual records")
    commands = root.add_subparsers(dest="accrual_command", required=True)
    commands.add_parser("schema", help="Print the provider-neutral supporting document and journal contracts")
    commands.add_parser("capabilities", help="Print available accrual operations")
    intake = commands.add_parser("intake", help="Retain immutable source supporting documents")
    intake.add_argument("--entity", required=True, type=Path)
    intake.add_argument("--input", required=True, type=Path)
    for domain, selector, verbs in (
        ("journal", "journal_command", ("preview", "apply", "correct-preview", "correct-apply", "reverse-preview", "reverse-apply")),
        ("schedule", "schedule_command", ("preview", "apply", "due-apply", "change-preview", "change-apply")),
        ("item", "item_command", ("preview", "apply", "settle-preview", "settle-apply", "adjust-preview", "adjust-apply", "offset-preview", "offset-apply")),
        ("support", "support_command", ("preview", "apply")),
        ("evidence-link", "evidence_link_command", ("preview", "apply")),
        ("period", "period_command", ("preview", "apply")),
    ):
        group = commands.add_parser(domain)
        children = group.add_subparsers(dest=selector, required=True)
        for verb in verbs:
            operation = children.add_parser(verb)
            operation.add_argument("--entity", required=True, type=Path)
            operation.add_argument("--input", required=True, type=Path)
        if domain in {"schedule", "item"}:
            listing = children.add_parser("list")
            listing.add_argument("--entity", required=True, type=Path)
        if domain == "journal":
            due = children.add_parser("due-reversals")
            due.add_argument("--entity", required=True, type=Path)
            due.add_argument("--as-of", required=True)
            due.add_argument("--apply", action="store_true")
        if domain == "schedule":
            due = children.add_parser("due-preview")
            due.add_argument("--entity", required=True, type=Path)
            due.add_argument("--through", required=True)
            due.add_argument("--operation-id", required=True)
    rules = commands.add_parser("rules", help="Saved accrual rules: what to propose for a supplier, customer or kind of cost. A rule never records anything.")
    children = rules.add_subparsers(dest="rules_command", required=True)
    for verb in ("set-preview", "set-apply", "clear-preview", "clear-apply", "match"):
        child = children.add_parser(verb)
        child.add_argument("--entity", required=True, type=Path)
        child.add_argument("--input", required=True, type=Path)
    listing = children.add_parser("list", help="List saved accrual rules (read-only)")
    listing.add_argument("--entity", required=True, type=Path)
    listing.add_argument("--format", choices=("json", "text"), default="json")
    report = commands.add_parser("report")
    report.add_argument("--entity", required=True, type=Path)
    report.add_argument("--kind", required=True, choices=("schedules", "outstanding", "aging", "adjustments", "support", "reconciliation"))
    report.add_argument("--as-of")
    report.add_argument("--from", dest="from_date")
    report.add_argument("--to", dest="to_date")
    report.add_argument("--format", choices=("json", "text"), default="json", help="JSON by default; text prints a readable accounting report")
    accounts = commands.add_parser("accounts", help="List the chart of accounts with open dates, roles and subtypes (read-only). " + SUBTYPE_HELP)
    accounts.add_argument("--entity", required=True, type=Path)
    accounts.add_argument("--format", choices=("json", "text"), default="json")
    receipt = commands.add_parser("receipt", help="Show the committed receipt for an operation ID (read-only)")
    receipt.add_argument("--entity", required=True, type=Path)
    receipt.add_argument("--id", required=True, help="The operation_id used in the request")
    opening = commands.add_parser("opening", help="Source-neutral opening balances for a new accrual company")
    children = opening.add_subparsers(dest="accrual_opening_command", required=True)
    for verb in ("preview", "apply"):
        child = children.add_parser(verb)
        child.add_argument("--entity", required=True, type=Path)
        child.add_argument("--input", required=True, type=Path)
    draft = children.add_parser("draft", help="Draft an opening request from an existing company's balances (read-only)")
    draft.add_argument("--entity", required=True, type=Path, help="Existing (for example cash-basis) company")
    draft.add_argument("--cutover", required=True, help="First day of the new accrual books; balances are taken the day before")
    enable = commands.add_parser("enable", help="Turn accrual on inside an existing company at a tracking start date (person-only; preview first)")
    children = enable.add_subparsers(dest="accrual_enable_command", required=True)
    preview = children.add_parser("preview", help="Show what turning accrual on changes. Writes nothing.")
    preview.add_argument("--entity", required=True, type=Path)
    preview.add_argument("--tracking-start", required=True, help="First day accrual records are kept; accrual figures start here")
    preview.add_argument("--operation-id", help="Stable ID; defaults to accrual-enable-<date>")
    apply = children.add_parser("apply", help="Apply the unchanged preview. An exact repeat returns already_applied.")
    apply.add_argument("--entity", required=True, type=Path)
    apply.add_argument("--input", required=True, type=Path)
    migration = commands.add_parser("migration")
    children = migration.add_subparsers(dest="accrual_migration_command", required=True)
    preview = children.add_parser("preview")
    preview.add_argument("--entity", required=True, type=Path)
    preview.add_argument("--folder", required=True, type=Path)
    preview.add_argument("--cutover", required=True)
    preview.add_argument("--fiscal-start", required=True)
    preview.add_argument("--supporting", required=True, type=Path)
    preview.add_argument("--closed-position", action="store_true")
    apply = children.add_parser("apply")
    apply.add_argument("--entity", required=True, type=Path)
    apply.add_argument("--input", required=True, type=Path)


_METAVARS = {"entity": "PATH", "input": "FILE", "supporting": "FILE", "folder": "REPORTS", "operation_id": "ID",
             **{name: "YYYY-MM-DD" for name in ("as_of", "through", "from_date", "to_date", "cutover", "fiscal_start", "tracking_start")}}


def _command_lines() -> tuple[list[str], dict[str, list[str]]]:
    """Describe every command from the argparse tree so the listing cannot drift."""
    root = argparse.ArgumentParser(prog="books")
    add_parser(root.add_subparsers(dest="top"))
    top = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction)).choices["accrual"]
    nodes = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction)).choices

    def usage(parser: argparse.ArgumentParser) -> str:
        parts = []
        for action in parser._actions:
            if not action.option_strings or isinstance(action, argparse._HelpAction):
                continue
            flag = action.option_strings[0]
            if action.nargs == 0:
                text = flag
            else:
                value = "|".join(map(str, action.choices)) if action.choices else (action.metavar or _METAVARS.get(action.dest, action.dest.upper()))
                text = f"{flag} {value}"
            parts.append(text if action.required else f"[{text}]")
        return " ".join(parts)

    lines: list[str] = []
    verbs: dict[str, list[str]] = {}
    for name, node in nodes.items():
        sub = next((a for a in node._actions if isinstance(a, argparse._SubParsersAction)), None)
        if sub is None:
            lines.append(f"accrual {name} {usage(node)}".rstrip())
            continue
        groups: dict[str, list[str]] = {}
        for verb, child in sub.choices.items():
            groups.setdefault(usage(child), []).append(verb)
        verbs[name] = list(sub.choices)
        for signature, names in groups.items():
            lines.append(f"accrual {name} {'|'.join(names)} {signature}".rstrip())
    return lines, verbs


def contracts() -> dict[str, Any]:
    lines, _ = _command_lines()
    return {"schema_version": 1, "capability_version": CAPABILITY_VERSION,
            "requests": request_schemas(), "commands": lines, "evidence": schema()}


def request_schemas() -> dict[str, Any]:
    """Discoverable input shapes; domain validation enforces financial invariants."""
    text = {"type": "string", "minLength": 1}
    day = {"type": "string", "format": "date", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
    amount = schema()["properties"]["amounts"]["additionalProperties"]
    ids = {"type": "array", "uniqueItems": True, "maxItems": 1000, "items": text}

    def record(required: list[str], properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                "required": required, "properties": properties, "additionalProperties": False, **extra}

    posting = record(["account"], {"account": text, "amount": amount, "debit": amount, "credit": amount, "currency": {"const": "USD"}},
        oneOf=[{"required": ["amount"], "not": {"anyOf": [{"required": ["debit"]}, {"required": ["credit"]}]}},
               {"not": {"required": ["amount"]}, "anyOf": [{"required": ["debit"]}, {"required": ["credit"]}]}],
        description="Use signed amount or debit/credit. Sides must be nonnegative with exactly one nonzero side; the engine validates exact balance.")
    journal = record(["id", "date", "description", "postings"], {
        "id": text, "date": day, "description": text, "evidence_ids": ids, "estimate_basis": text,
        "postings": {"type": "array", "minItems": 2, "maxItems": 1000, "items": posting},
        "estimate_basis": {**text, "description": "Plain-text basis of an estimate. Replaces stored supporting documents."},
        "reversal_date": {**day, "description": "Posts the exact reversal of this journal on this later date when `books accrual journal due-reversals --as-of DATE --apply` runs. This is the one path for an accrued estimate that reverses when the real bill arrives."},
        "origin": record(["mode", "source_id"], {"mode": {"const": "existing"}, "source_id": text})},
        anyOf=[{"required": ["evidence_ids"], "properties": {"evidence_ids": {"minItems": 1}}},
               {"required": ["estimate_basis"]}],
        description="Exact balance, account opening/currency, supporting documents, locks and authority are checked by the engine.")
    origin = record(["mode", "source_id"], {"mode": {"enum": ["new", "existing", "opening"]},
        "source_id": text, "date": day, "funding_account": text, "reclass_account": text, "counter_account": text})
    schedule_origin = record(["mode", "source_id"], {key: value for key, value in origin["properties"].items() if key != "counter_account"})
    schedule_origin["properties"]["funding_account"] = {**text, "description": "Existing USD asset account; prepaid schedules also allow Liabilities:CreditCard or its colon-separated subaccounts. Deferred revenue remains asset-funded."}
    item_origin = record(["mode", "source_id"], {key: value for key, value in origin["properties"].items() if key in {"mode", "source_id", "counter_account"}})
    item_origin["properties"]["counter_account"] = {**text, "description": "Income or expense account for a new origin. Use origin.lines instead to split one bill across several accounts."}
    item_origin["properties"]["lines"] = {"type": "array", "minItems": 1, "maxItems": 200, "description": "Split a new origin across several income or expense accounts. Amounts must add up exactly to original_amount. One control-account balance is posted. Do not combine with counter_account.",
        "items": record(["account", "amount"], {"account": text, "amount": amount})}
    installments_schema = {"type": "array", "minItems": 1, "maxItems": 1200, "items": record(["date", "amount"], {"date": day, "amount": amount}),
        "description": "Only for method custom: dated amounts to recognize, in strictly ascending date order, each inside service_start..service_end, "
                       "each positive, adding up exactly to the amount to recognize (amount minus salvage_value). Use for usage-based, milestone or uneven patterns."}
    schedule = record(["operation_id", "id", "kind", "account", "recognition_account", "currency", "amount",
                       "service_start", "service_end", "method", "origin"], {
        "operation_id": text, "id": text,
        "kind": {"enum": ["prepaid", "deferred", "depreciation"], "description":
            "prepaid: asset (account) released to expense (recognition_account). deferred: liability (account) released to income. "
            "depreciation: fixed asset cost (account) expensed to recognition_account with the credit going to contra_account."},
        "account": {**text, "description": "Control account: Assets: for prepaid and depreciation (the asset cost), Liabilities: for deferred."},
        "recognition_account": {**text, "description": "Expenses: for prepaid and depreciation, Income: for deferred."},
        "contra_account": {**text, "description": "Required for kind depreciation only: accumulated depreciation account, for example Assets:Fixed:Accumulated-Depreciation. Recognition debits the expense and credits this account; the asset cost stays unchanged."},
        "currency": {"const": "USD"},
        "amount": {**amount, "description": "Total amount funded: the full cost for depreciation."},
        "salvage_value": {**amount, "description": "Depreciation only: residual value that is not depreciated. The depreciable base is amount minus salvage_value."},
        "service_start": {**day, "description": "First day of service, inclusive. Same convention as a supporting document's service_period.start."},
        "service_end": {**day, "description": "Last day of service, inclusive: a one-year service from 2026-01-15 ends 2027-01-14. Same convention as service_period.end. Must lie inside any linked supporting document's service period."},
        "method": {"enum": ["actual_day", "equal_month", "custom"], "description":
            "actual_day: each month gets floor(amount x days in month / total days) cents. equal_month: each whole calendar month gets floor(amount / months) cents; "
            "needs service_start on the 1st and service_end on a month end (otherwise use actual_day or custom). custom: use installments. "
            "For the two computed methods the final installment receives the residual cents so the total is exact."},
        "installments": installments_schema, "origin": schedule_origin, "evidence_ids": ids,
        "recognition_start": {**day, "description": "Opening-balance carry-over only: first day not yet recognized."},
        "recognized_amount": {**amount, "description": "Opening-balance carry-over only: amount already recognized (accumulated depreciation for depreciation)."},
        "reference": {**text, "maxLength": 2000, "description": "Human reference or description, separate from the stable machine ID."}},
        examples=[{"operation_id": "synthetic-prepaid-create", "id": "synthetic-prepaid", "reference": "Annual software subscription #104", "kind": "prepaid",
            "account": "Assets:Other:Prepaid-Expenses", "recognition_account": "Expenses:Software", "currency": "USD",
            "amount": "1200.00", "service_start": "2026-01-01", "service_end": "2026-12-31", "method": "equal_month",
            "origin": {"mode": "new", "source_id": "synthetic-funding", "date": "2026-01-01", "funding_account": "Assets:Bank:Checking"},
            "evidence_ids": ["synthetic-stored-evidence-id"]},
            {"operation_id": "synthetic-deferred-create", "id": "synthetic-deferred", "kind": "deferred", "account": "Liabilities:DeferredRevenue",
            "recognition_account": "Income:Services", "currency": "USD", "amount": "12000.00", "service_start": "2026-01-15", "service_end": "2027-01-14",
            "method": "actual_day", "origin": {"mode": "new", "source_id": "synthetic-receipt", "date": "2026-01-15", "funding_account": "Assets:Bank:Checking"},
            "evidence_ids": ["synthetic-stored-evidence-id"]},
            {"operation_id": "synthetic-laptop-create", "id": "synthetic-laptop", "kind": "depreciation", "account": "Assets:Fixed:Equipment",
            "contra_account": "Assets:Fixed:Accumulated-Depreciation", "recognition_account": "Expenses:Depreciation", "currency": "USD",
            "amount": "2400.00", "salvage_value": "0.00", "service_start": "2026-01-01", "service_end": "2027-12-31", "method": "equal_month",
            "origin": {"mode": "new", "source_id": "synthetic-laptop-purchase", "date": "2026-01-05", "funding_account": "Assets:Bank:Checking"},
            "evidence_ids": ["synthetic-stored-evidence-id"]},
            {"operation_id": "synthetic-usage-create", "id": "synthetic-usage", "kind": "prepaid", "account": "Assets:PrepaidExpenses",
            "recognition_account": "Expenses:Software", "currency": "USD", "amount": "300.00", "service_start": "2026-01-01", "service_end": "2026-03-31",
            "method": "custom", "installments": [{"date": "2026-01-31", "amount": "50.00"}, {"date": "2026-02-28", "amount": "100.00"}, {"date": "2026-03-31", "amount": "150.00"}],
            "origin": {"mode": "new", "source_id": "synthetic-usage-payment", "date": "2026-01-01", "funding_account": "Assets:Bank:Checking"},
            "evidence_ids": ["synthetic-stored-evidence-id"]}])
    item = record(["operation_id", "id", "kind", "account", "currency", "original_amount", "as_of", "origin"], {
        "operation_id": text, "id": text, "kind": {"enum": ["receivable", "payable", "accrual", "credit"]},
        "account": text, "currency": {"const": "USD"}, "original_amount": amount, "as_of": day, "origin": item_origin,
        "direction": {"enum": ["debit", "credit"], "description": "Only for kind credit. credit = the counterparty holds a credit with you (customer deposit, credit memo, overpayment). debit = you hold a prepayment or credit with the counterparty (vendor prepayment)."},
        "counterparty": {**text, "description": "Name of the customer or supplier. A credit and the invoice or bill it is applied to must share one exact counterparty."},
        "reference": text, "due_date": {**day, "description": "Explicit payment due date; distinct from accounting as_of. Omit when unknown."}, "evidence_ids": ids,
        "estimate_basis": {**text, "description": "Plain-text basis of an estimate (for kind accrual). A new origin then needs no stored supporting document."}, "remaining_amount": amount},
        description="kind receivable/payable = invoice or bill; accrual = estimated liability to settle or true up later; credit = deposit, credit memo or prepayment that `item offset-preview` applies to an invoice or bill.",
        examples=[{"operation_id": "synthetic-payable-create", "id": "synthetic-payable", "kind": "payable",
            "account": "Liabilities:Payable:Accounts-Payable", "currency": "USD", "original_amount": "100.00", "as_of": "2026-01-31",
            "origin": {"mode": "new", "source_id": "synthetic-bill", "counter_account": "Expenses:Software"},
            "evidence_ids": ["synthetic-stored-evidence-id"]},
            {"operation_id": "synthetic-split-bill", "id": "synthetic-split", "kind": "payable", "counterparty": "Synthetic Vendor",
             "account": "Liabilities:AccountsPayable", "currency": "USD", "original_amount": "100.00", "as_of": "2026-01-31",
             "origin": {"mode": "new", "source_id": "synthetic-split-bill-doc", "lines": [
                 {"account": "Expenses:Software", "amount": "60.00"}, {"account": "Expenses:Utilities", "amount": "40.00"}]},
             "evidence_ids": ["synthetic-stored-evidence-id"]},
            {"operation_id": "synthetic-deposit", "id": "synthetic-deposit-credit", "kind": "credit", "direction": "credit", "counterparty": "Synthetic Customer",
             "account": "Liabilities:CustomerDeposits", "currency": "USD", "original_amount": "500.00", "as_of": "2026-02-01",
             "origin": {"mode": "existing", "source_id": "synthetic-deposit-journal"}, "evidence_ids": ["synthetic-stored-evidence-id"]},
            {"operation_id": "synthetic-bonus-estimate", "id": "synthetic-bonus", "kind": "accrual", "account": "Liabilities:AccruedBonus",
             "currency": "USD", "original_amount": "1000.00", "as_of": "2026-01-31", "estimate_basis": "Synthetic target bonus pool",
             "origin": {"mode": "new", "source_id": "synthetic-bonus-estimate-src", "counter_account": "Expenses:Bonuses"}}])
    support_row = {"type": "object", "required": ["id", "account", "currency"],
        "properties": {"id": text, "account": text, "currency": {"const": "USD"},
            "evidence_refs": ids, "evidence_ids": ids, "counterparty": text, "reference": text, "description": text},
        "additionalProperties": False,
        "description": "Opening item/schedule/external support; collection-specific facts and exact control tie-out are validated by migration."}
    def opening_support(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
        return {**support_row, "required": [*support_row["required"], *required],
                "properties": {**support_row["properties"], **properties},
                "anyOf": [{"required": ["evidence_refs"], "properties": {"evidence_refs": {"minItems": 1}}},
                          {"required": ["evidence_ids"], "properties": {"evidence_ids": {"minItems": 1}}}]}
    opening_item = opening_support(["kind", "original_amount", "remaining_amount"], {
        "kind": {"enum": ["receivable", "payable", "accrual", "credit"]}, "original_amount": amount,
        "remaining_amount": amount,
        "direction": {"enum": ["debit", "credit"]}, "due_date": {**day, "description": "Original explicit due date; may precede cutover."}})
    opening_schedule = opening_support(["kind", "amount", "recognized_amount", "remaining_amount", "service_start", "service_end", "method", "recognition_account"], {
        "kind": {"enum": ["prepaid", "deferred", "depreciation"]},
        "contra_account": {**text, "description": "depreciation only: accumulated depreciation account (Assets:). Carry its balance in lines; recognized_amount is the accumulated depreciation."},
        "salvage_value": {**amount, "description": "depreciation only: residual value that is not depreciated. remaining_amount is amount minus salvage_value minus recognized_amount."}, "amount": amount, "recognized_amount": amount, "remaining_amount": amount,
        "service_start": {**day, "description": "First day of service, inclusive."},
        "service_end": {**day, "description": "Last day of service, inclusive."},
        "method": {"enum": ["actual_day", "equal_month"]}, "recognition_account": text})
    opening_external = opening_support(["balance"], {"balance": amount})
    line = record(["account"], {"account": {**text, "description": "Balance sheet account (Assets, Liabilities or Equity). Opened as of the opening date if not yet open."},
        "amount": {**amount, "description": "Signed exact USD amount: debit positive, credit negative. Or use debit/credit instead."},
        "debit": amount, "credit": amount})
    refs = {"evidence_refs": {**ids, "description": "Company-relative supporting document paths."}, "evidence_ids": ids}
    open_item = record(["id", "kind", "account", "open_amount"], {
        "id": text, "kind": {"enum": ["receivable", "payable", "accrual", "credit"]},
        "account": {**text, "description": "Control account that carries the balance, for example Assets:Receivable:Accounts-Receivable."},
        "counterparty": {**text, "description": "Customer or supplier name."}, "reference": {**text, "description": "Invoice or bill number."},
        "due_date": {**day, "description": "Original due date; may precede the cutover."},
        "open_amount": {**amount, "description": "Unpaid amount at the cutover."},
        "original_amount": {**amount, "description": "Original invoice or bill amount. Defaults to open_amount."},
        "direction": {"enum": ["debit", "credit"]}, "description": text, **refs})
    opening = record(["cutover", "lines"], {
        "operation_id": {**text, "description": "Stable ID. Defaults to opening-<cutover>."},
        "cutover": {**day, "description": "First day of the accrual books. The opening entry is dated the day before; accounts it uses are opened as of that date."},
        "lines": {"type": "array", "minItems": 1, "items": line, "description": "Opening balance lines. Debits must equal credits; balance sheet accounts only."},
        "items": {"type": "array", "items": open_item, "description": "Open customer invoices and supplier bills. Their open amounts must equal the control account balance."},
        "schedules": {"type": "array", "items": opening_schedule, "description": "Remaining prepaid, deferred or depreciation (fixed asset) balances."},
        "external": {"type": "array", "items": opening_external, "description": "Support for other control accounts (fixed assets, loans, accruals)."},
        **refs}, description="Source-neutral opening balances. Use `books accrual opening draft` to start from an existing company; apply the unchanged preview.",
        examples=[{"operation_id": "synthetic-opening", "cutover": "2026-01-01", "evidence_refs": ["evidence/opening-ar-list.pdf"],
            "lines": [{"account": "Assets:Bank:Checking", "amount": "30000.00"}, {"account": "Assets:Receivable:Accounts-Receivable", "amount": "300.00"},
                      {"account": "Liabilities:Payable:Accounts-Payable", "amount": "-100.00"}, {"account": "Equity:Opening-Balances", "amount": "-30200.00"}],
            "items": [{"id": "synthetic-inv-1", "kind": "receivable", "account": "Assets:Receivable:Accounts-Receivable", "counterparty": "Synthetic Customer",
                       "reference": "INV-1", "due_date": "2025-12-15", "open_amount": "300.00"},
                      {"id": "synthetic-bill-1", "kind": "payable", "account": "Liabilities:Payable:Accounts-Payable", "counterparty": "Synthetic Supplier",
                       "reference": "BILL-9", "due_date": "2026-01-10", "open_amount": "100.00"}]}])
    enable = record(["tracking_start"], {
        "tracking_start": {**day, "description": "First day accrual records are kept. Accrual figures start on this date; earlier periods stay cash books."},
        "operation_id": {**text, "description": "Stable ID. Defaults to accrual-enable-<tracking_start>."}},
        description="Turn accrual on inside an existing company. `books accrual enable preview --tracking-start DATE`, then `enable apply --input` with the unchanged preview. "
                    "Nothing already recorded changes, and the default view stays as it is. Person-only. Afterward `books accrual opening` lists what is open on that date.")
    tracking_opening = record([], {key: value for key, value in opening["properties"].items() if key != "lines"} | {
        "cutover": {**day, "description": "Optional. Must equal the tracking start date. The one opening entry is dated that day."}},
        description="Opening items for a company that turned accrual on later (`books accrual enable`). List only what is still open on the tracking start date. "
                    "Lines are worked out from the support rows; the balancing amount goes to Equity:Opening-Balance-Adjustments. Same preview and apply commands as `opening`.",
        examples=[{"operation_id": "synthetic-tracking-opening", "evidence_refs": ["evidence/open-invoices.pdf"],
                   "items": [{"id": "synthetic-inv-7", "kind": "receivable", "account": "Assets:Receivable:Accounts-Receivable", "counterparty": "Synthetic Customer",
                              "reference": "INV-7", "due_date": "2026-04-20", "open_amount": "900.00"}]}])
    return {"enable": enable, "opening": opening, "tracking_opening": tracking_opening, "journal": journal,
        "evidence_link": record(["operation_id", "target_operation_id", "evidence_ids", "reason"], {
            "operation_id": text, "target_operation_id": text, "evidence_ids": ids, "reason": text},
            description="Explicitly identify another source as a supporting document of a committed effect; no matching heuristic or financial posting."),
        "correction": record(["id", "original_id", "date", "description", "replacement"],
            {"id": text, "original_id": text, "date": day, "description": text, "replacement": journal}),
        "reversal": record(["id", "original_id", "date", "description"],
            {"id": text, "original_id": text, "date": day, "description": text}),
        "schedule": schedule,
        "schedule_due": record(["operation_id", "through"], {"operation_id": text, "through": day}),
        "schedule_change": record(["operation_id", "id", "action", "date", "reason"], {
            "operation_id": text, "id": text,
            "action": {"enum": ["cancel", "supersede", "refund", "terminate"], "description":
                "cancel: release the whole remaining balance to offset_account on date. refund: return part of the balance (see amount and the refund split fields). "
                "supersede: replace the remaining balance with a new schedule. terminate: recognize service through effective_date (daily proration of a partial month "
                "for any method), then release the unearned balance to offset_account as a refund, credit or write-off; for depreciation it only stops depreciation."},
            "date": {**day, "description": "Posting date of the change entry. For terminate it must be on or after effective_date. It cannot precede recognition already posted."},
            "effective_date": {**day, "description": "terminate only: last day of service, inclusive. Service through this day is recognized; the rest is released."},
            "reason": text, "evidence_ids": ids,
            "amount": {**amount, "description": "refund only: total amount returned, equal to remaining_refund_amount plus recognized_refund_amount when both are given."},
            "offset_account": {**text, "description": "Account that receives or gives the released value: the bank for a cash refund, a receivable/payable for a credit, or an income/expense account for a write-off."},
            "recognized_refund_amount": {**amount, "description": "refund only: part of the refund that reverses service already recognized (posted to recognized_refund_account). Give together with remaining_refund_amount."},
            "remaining_refund_amount": {**amount, "description": "refund only: part of the refund that comes out of the unrecognized balance still held in the schedule account."},
            "recognized_refund_account": {**text, "description": "Expense (prepaid) or income (deferred) account credited or debited for recognized_refund_amount. Required when it is nonzero."},
            "replacement": record(["id", "service_start", "service_end", "method"], {"id": text,
                "service_start": {**day, "description": "First day of the replacement service, inclusive; on or after the change date."},
                "service_end": {**day, "description": "Last day of the replacement service, inclusive."},
                "method": {"enum": ["actual_day", "equal_month", "custom"]}, "installments": installments_schema, "recognition_account": text})},
            examples=[{"operation_id": "synthetic-terminate", "id": "synthetic-prepaid", "action": "terminate", "date": "2026-05-10",
                       "effective_date": "2026-04-30", "reason": "Subscription cancelled; vendor refunded the unused months",
                       "offset_account": "Assets:Bank:Checking", "evidence_ids": ["synthetic-stored-evidence-id"]}]),
        "item": item,
        "settlement": record(["operation_id", "date", "source", "allocations"], {
            "operation_id": text, "date": day, "evidence_ids": ids,
            "source": record(["mode", "source_id", "bank_account", "amount"], {"mode": {"enum": ["new", "existing"]},
                "source_id": text, "bank_account": text, "amount": amount}),
            "allocations": {"type": "array", "minItems": 0, "items": record(["item_id", "amount"], {"item_id": text, "amount": amount}),
                "description": "Items this payment settles. Empty is allowed only when excess records the whole payment, for example a customer deposit."},
            "excess": record(["id", "account"], {"id": text, "account": {**text, "description": "Control account that holds the credit: a receivable, payable, customer deposit or vendor prepayment account (never cash)."},
                "counterparty": {**text, "description": "Customer or supplier name. Defaults to the shared counterparty of the allocated items."}, "reference": text},
                description="Records the part of the payment not allocated to items (overpayment or unapplied cash) as a new open credit item. Apply it later with `item offset-preview`.")},
            description="Settle open invoices or bills with one bank movement. A receipt carries excess as a customer credit; a payment carries excess as a supplier credit.",
            examples=[{"operation_id": "synthetic-overpayment", "date": "2026-02-10", "evidence_ids": ["synthetic-stored-evidence-id"],
                "source": {"mode": "new", "source_id": "synthetic-bank-receipt", "bank_account": "Assets:Bank:Checking", "amount": "1200.00"},
                "allocations": [{"item_id": "synthetic-invoice", "amount": "1000.00"}],
                "excess": {"id": "synthetic-overpayment-credit", "account": "Liabilities:CustomerDeposits", "counterparty": "Synthetic Customer"}}]),
        "item_adjustment": record(["operation_id", "item_id", "action", "date", "counter_account", "reason"], {
            "operation_id": text, "item_id": text,
            "action": {"enum": ["credit", "writeoff", "true_up"], "description": "credit = reduce the item and the income or expense (discount, credit memo). writeoff = remove an uncollectable or forgiven balance. true_up = settle an accrued estimate against the actual item and record the variance; needs actual_item_id."},
            "amount": {**amount, "description": "Amount to remove from the item. Required for credit and writeoff. For true_up it defaults to the whole remaining estimate."},
            "date": day, "counter_account": {**text, "description": "Income or expense account that absorbs the adjustment."},
            "source_id": {**text, "description": "Unique ID of this adjustment source. It defaults to operation_id for credit, writeoff and true_up."},
            "reason": text, "evidence_ids": {**ids, "description": "Stored supporting documents. Required for credit and writeoff; optional for true_up, because the actual item already carries its own."},
            "actual_item_id": {**text, "description": "Only for true_up: the receivable or payable item that holds the real amount. Variance = actual amount minus the estimate removed; it is stored on the adjustment record."}},
            description="Adjust one open item. A true-up posts Dr accrued liability, Cr expense for the estimate; the actual bill keeps its own expense, so net expense equals the actual.",
            examples=[{"operation_id": "synthetic-true-up", "item_id": "synthetic-bonus", "action": "true_up", "actual_item_id": "synthetic-bonus-actual",
                "date": "2026-03-31", "counter_account": "Expenses:Bonuses", "reason": "Synthetic bonus paid at actual"}]),
        "item_offset": record(["operation_id", "date", "credit_item_id", "target_item_id", "amount", "reason"], {
            "operation_id": text, "date": day, "amount": {**amount, "description": "At most the open balance of both items."},
            "credit_item_id": {**text, "description": "Open item of kind credit: customer deposit, credit memo, overpayment, or vendor prepayment."},
            "target_item_id": {**text, "description": "Open receivable (for a customer credit) or payable (for a vendor prepayment) with the same counterparty."},
            "reason": text, "evidence_ids": ids},
            description="Apply a credit to an invoice or bill. If the two items sit in different accounts, posts Dr customer deposits (or Dr payable), Cr receivable (or Cr prepayment). If both sit in one account, links only. Aging shows the lower balances.",
            examples=[{"operation_id": "synthetic-apply-deposit", "date": "2026-03-01", "credit_item_id": "synthetic-deposit-credit",
                "target_item_id": "synthetic-invoice", "amount": "500.00", "reason": "Apply customer deposit to invoice"}]),
        "rule_set": record(["operation_id", "treatment", "note", "set_by", "source"], {
            "operation_id": text,
            "counterparty": {**text, "description": "Supplier or customer name or key. Makes a supplier or customer rule. Give this or match, not both."},
            "id": {**text, "description": "Company rule only: a stable id of letters, digits, dots, dashes and underscores. Defaults to a hash of match and threshold, so the same match is one rule. A rule with this id is replaced."},
            "match": record([], {"accounts": {"type": "array", "minItems": 1, "maxItems": 20, "items": text,
                                              "description": "Expense, income or non-cash asset accounts. An account also covers the accounts under it; a deeper account is more specific."},
                                 "keywords": {"type": "array", "minItems": 1, "maxItems": 10, "items": text,
                                              "description": "Words that must appear in the description or counterparty. Any one is enough. Not case sensitive."}},
                            description="Company rule only. Needs accounts or keywords."),
            "treatment": {"enum": ["prepaid", "deferred", "depreciation", "bill", "invoice", "estimate"]},
            "term_months": {"type": "integer", "minimum": 1, "maximum": 600, "description": "prepaid, deferred and depreciation only. Required for depreciation. Slashbooks does not choose a useful life."},
            "method": {"enum": ["actual_day", "equal_month"], "description": "prepaid, deferred and depreciation only. Use custom installments in the request itself, not in a rule."},
            "accounts": record(["account", "recognition_account"], {"account": text, "recognition_account": text, "contra_account": {**text, "description": "depreciation only, and required there."}},
                               description="The balance sheet account and the income or expense account. Checked against the chart, account roots and subtypes. Leave out to let the proposal choose from the chart."),
            "threshold": {**amount, "description": "Apply the rule only to amounts at or above this exact decimal string. Use 1000.01 for \"over 1,000\"."},
            "note": {"type": "string", "minLength": 1, "maxLength": 500, "description": "Why, in plain words. Shown with every proposal that uses the rule."},
            "set_by": {**text, "description": "Who decided: the person's name or id, or the agent's name."},
            "set_at": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", "description": "UTC time. Defaults to the preview time."},
            "source": {"enum": ["person", "agent"], "description": "person when a person decided. agent when an agent proposes: tell the person. An agent cannot replace or clear a rule a person set."}},
            description="Save what to PROPOSE. A rule never records anything: a person still approves. Preview with `rules set-preview`, then apply the unchanged preview with `rules set-apply`.",
            examples=[{"operation_id": "synthetic-rule-supplier", "counterparty": "Synthetic Hosting", "treatment": "prepaid", "term_months": 12,
                       "method": "equal_month", "note": "Annual plan, paid up front", "set_by": "synthetic-person", "source": "person"},
                      {"operation_id": "synthetic-rule-software", "id": "software-prepaid", "match": {"accounts": ["Expenses:Software"]},
                       "treatment": "prepaid", "threshold": "1000.00", "note": "Software at 1,000 or more is prepaid over its service period",
                       "set_by": "synthetic-person", "source": "person"}]),
        "rule_clear": record(["operation_id", "cleared_by", "source"], {
            "operation_id": text, "counterparty": text, "id": {**text, "description": "A company rule id."},
            "cleared_by": text, "cleared_at": {"type": "string"}, "reason": text, "source": {"enum": ["person", "agent"]}},
            description="Remove a saved rule. Give counterparty or id. Nothing in the books changes. Preview with `rules clear-preview`, then `rules clear-apply`.",
            examples=[{"operation_id": "synthetic-rule-clear", "counterparty": "Synthetic Hosting", "cleared_by": "synthetic-person", "source": "person"}]),
        "rule_match": record(["amount"], {
            "amount": {**amount, "description": "Size of the bill, invoice or payment. The sign is ignored."},
            "counterparty": text, "account": {**text, "description": "The expense or income account the item is, or would be, categorized to. `category` is accepted as the same fact."},
            "category": text, "description": text, "date": day, "service_start": day, "service_end": day,
            "source_id": {**text, "description": "The document's or bank transaction's source id; used to name the draft and its origin."},
            "funding_account": text, "evidence_ids": ids, "reference": text, "due_date": day, "estimate_basis": text},
            description="Facts of a bill, invoice, payment or supporting document. `rules match` reads the saved rules and returns the matching rule, why, and a DRAFT request for the ordinary preview. Needs a counterparty, description or account.",
            examples=[{"counterparty": "Synthetic Hosting", "amount": "1200.00", "account": "Expenses:Software", "date": "2026-02-01",
                       "service_start": "2026-02-01", "source_id": "synthetic-bank-payment", "funding_account": "Assets:Bank:Checking"}]),
        "support": record(["operation_id", "account", "as_of", "currency", "details", "evidence_ids"], {
            "operation_id": text, "account": text, "as_of": day, "currency": {"const": "USD"}, "evidence_ids": ids,
            "mode": {"enum": ["full", "residual"], "default": "full"}, "managed_support_ids": ids,
            "supersedes": text, "details": {"type": "array", "items": record(["id", "balance"], {
                "id": text, "balance": amount, "description": text})}}),
        "period": record(["operation_id", "period", "action", "as_of", "reason"], {
            "operation_id": text, "period": {"type": "string", "pattern": "^[0-9]{4}-(0[1-9]|1[0-2])$"},
            "action": {"enum": ["close", "reopen"]}, "as_of": day, "reason": text, "readiness_package_id": text},
            description="Local operator only; close additionally requires exact ready package ID. Standing adjust authority cannot close or reopen."),
        "migration_supporting": record([], {name: {"type": "array", "items": row} for name, row in
            (("items", opening_item), ("schedules", opening_schedule), ("external", opening_external))}),
        "migration_preview": record(["folder", "cutover", "fiscal_start", "supporting"], {
            "folder": text, "cutover": day, "fiscal_start": day, "supporting": {"type": "object"},
            "closed_position": {"type": "boolean", "default": False}},
            description="Python API keyword inputs; CLI takes --folder/--cutover/--fiscal-start/--supporting FILE and --closed-position."),
        "apply": {"type": "object", "required": ["schema_version", "id", "kind", "request", "request_hash", "plan_hash"],
            "description": "Pass the complete unchanged engine preview; do not construct or edit an apply payload."}}


def _read_only_store(entity: Any) -> Any:
    from ..ledger.store import LedgerStore
    return LedgerStore(entity.path / "ledger.sqlite")


def _input(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise journals.JournalError("invalid_input", "Input file must contain one JSON object.")
    return value


def run(args: argparse.Namespace) -> int:
    try:
        command = args.accrual_command
        if command == "schema":
            result = contracts()
        elif command == "capabilities":
            _, verbs = _command_lines()
            result = {"capability_version": CAPABILITY_VERSION, "currency": "USD", "basis": "accrual",
                      "operations": ["intake", *verbs, "report", "accounts", "receipt"],
                      "rules_effect": "proposals_only",
                      "verbs": verbs, "authority_action": "adjust", "posting_requires_opt_in": True,
                      "operator_only": ["period"], "request_schemas": sorted(request_schemas())}
        else:
            entity = load_entity(args.entity)
            if command == "intake":
                result = journals.intake_evidence(entity, _input(args.input))
            elif command == "accounts":
                from . import accounts
                result = accounts.list_accounts(entity)
            elif command == "receipt":
                from .store import operation_receipt
                record = operation_receipt(_read_only_store(entity), args.id)
                if record is None:
                    from .rules import stored_receipt
                    record = stored_receipt(entity, args.id)
                if record is None:
                    raise journals.JournalError("receipt_not_found", f"No committed operation has ID {args.id}.",
                                                "Check operation_id, or `books accrual report --kind adjustments`. The operation was not applied.")
                result = {"status": record["status"], "id": args.id, "kind": record["kind"], "request_hash": record["request_hash"],
                          "plan_hash": record["plan_hash"], "receipt": record["receipt"]}
            elif command == "enable":
                from . import enable
                if args.accrual_enable_command == "preview":
                    result = enable.preview_enable(entity, args.tracking_start, args.operation_id)
                else:
                    result = enable.apply_enable(entity, _input(args.input))
            elif command == "opening":
                from . import opening
                verb = args.accrual_opening_command
                if verb == "draft":
                    result = opening.draft_from_cash_company(entity, args.cutover)
                elif verb == "preview":
                    payload = _input(args.input)
                    if isinstance(payload, dict) and payload.get("status") == "draft" and isinstance(payload.get("request"), dict):
                        payload = payload["request"]  # accept the `opening draft` output unchanged
                    result = opening.preview_opening(entity, payload)
                else:
                    plan = _input(args.input)
                    result = opening.apply_opening(entity, plan)
            elif command == "rules":
                from . import rules
                verb = args.rules_command
                if verb == "list":
                    result = rules.list_rules(entity)
                else:
                    function = {"set-preview": rules.set_preview, "set-apply": rules.set_apply, "clear-preview": rules.clear_preview,
                                "clear-apply": rules.clear_apply, "match": rules.match}[verb]
                    result = function(entity, _input(args.input))
            elif command == "evidence-link":
                function = journals.preview_evidence_link if args.evidence_link_command == "preview" else journals.apply_evidence_link
                result = function(entity, _input(args.input))
            elif command == "journal":
                verb = args.journal_command
                if verb == "due-reversals":
                    result = journals.due_reversals(entity, args.as_of, apply=args.apply)
                else:
                    payload = _input(args.input)
                    if verb == "preview":
                        result = journals.preview_journal(entity, payload)
                    elif verb == "apply":
                        result = journals.apply_journal(entity, payload)
                    elif verb.endswith("preview"):
                        result = journals.preview_correction(entity, payload, reverse_only=verb == "reverse-preview")
                    else:
                        result = journals.apply_correction(entity, payload)
            elif command == "schedule":
                from . import schedules
                verb = args.schedule_command
                if verb == "list":
                    result = schedules.list_schedules(entity)
                elif verb == "due-preview":
                    result = schedules.preview_due(entity, args.through, operation_id=args.operation_id)
                else:
                    function = {"preview": schedules.preview_schedule, "apply": schedules.apply_schedule,
                                "due-apply": schedules.apply_due, "change-preview": schedules.preview_change,
                                "change-apply": schedules.apply_change}[verb]
                    result = function(entity, _input(args.input))
            elif command == "item":
                from . import items
                verb = args.item_command
                if verb == "list":
                    result = items.list_items(entity)
                else:
                    function = {"preview": items.preview_item, "apply": items.apply_item,
                        "settle-preview": items.preview_settlement, "settle-apply": items.apply_settlement,
                        "adjust-preview": items.preview_adjustment, "adjust-apply": items.apply_adjustment,
                        "offset-preview": items.preview_offset, "offset-apply": items.apply_offset}[verb]
                    result = function(entity, _input(args.input))
            elif command == "migration":
                from . import migration
                result = (migration.apply(entity, _input(args.input)) if args.accrual_migration_command == "apply"
                          else migration.preview(entity, args.folder, args.cutover, fiscal_start=args.fiscal_start,
                                                 supporting=_input(args.supporting), closed_position=args.closed_position))
            elif command == "support":
                from . import reports
                function = reports.preview_support if args.support_command == "preview" else reports.apply_support
                result = function(entity, _input(args.input))
            elif command == "period":
                from . import reports
                function = reports.preview_period if args.period_command == "preview" else reports.apply_period
                result = function(entity, _input(args.input))
            elif command == "report":
                from . import reports
                result = reports.report(entity, args.kind, as_of=args.as_of, from_date=args.from_date, to_date=args.to_date)
            else:
                raise journals.JournalError("unsupported_operation", "Unsupported accrual operation.")
        if command == "report" and args.format == "text":
            from .render import render_report
            print(render_report(result))
        elif command == "rules" and args.rules_command == "list" and args.format == "text":
            from .rules import render_rules
            print(render_rules(result))
        elif command == "accounts" and args.format == "text":
            from .accounts import render_accounts
            print(render_accounts(result))
        else:
            print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
        return 1 if isinstance(result, dict) and result.get("status") == "blocked" else 0
    except (ValueError, PermissionError, OSError, ImportError) as exc:
        if hasattr(exc, "as_dict"):
            result = exc.as_dict()
            if getattr(exc, "code", None) == "closed_period":
                from .reports import closed_period_action
                result["errors"][0]["action"] = closed_period_action(getattr(args, "entity", None), str(exc))
        else:
            result = {"status": "blocked", "errors": [{"code": getattr(exc, "reason", "invalid_operation"), "message": str(exc)}]}
        print(json.dumps(result, sort_keys=True, indent=2))
        return 1
