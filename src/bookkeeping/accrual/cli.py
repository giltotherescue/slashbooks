"""Thin JSON command surface for the shared accrual domain modules."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..entity import load_entity
from . import CAPABILITY_VERSION, journals
from .intake import IntakeValidationError, schema


def add_parser(subparsers: Any) -> None:
    root = subparsers.add_parser("accrual", help="Inspect evidence schemas and maintain reviewable accrual records")
    commands = root.add_subparsers(dest="accrual_command", required=True)
    commands.add_parser("schema", help="Print the provider-neutral evidence and journal contracts")
    commands.add_parser("capabilities", help="Print available accrual operations")
    intake = commands.add_parser("intake", help="Retain immutable source evidence")
    intake.add_argument("--entity", required=True, type=Path)
    intake.add_argument("--input", required=True, type=Path)
    for domain, selector, verbs in (
        ("journal", "journal_command", ("preview", "apply", "correct-preview", "correct-apply", "reverse-preview", "reverse-apply")),
        ("schedule", "schedule_command", ("preview", "apply", "due-apply", "change-preview", "change-apply")),
        ("item", "item_command", ("preview", "apply", "settle-preview", "settle-apply", "adjust-preview", "adjust-apply", "reverse-preview", "reverse-apply")),
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
    report = commands.add_parser("report")
    report.add_argument("--entity", required=True, type=Path)
    report.add_argument("--kind", required=True, choices=("schedules", "outstanding", "aging", "adjustments", "support", "reconciliation"))
    report.add_argument("--as-of")
    report.add_argument("--from", dest="from_date")
    report.add_argument("--to", dest="to_date")
    report.add_argument("--format", choices=("json", "text"), default="json", help="JSON by default; text prints a readable accounting report")
    accounts = commands.add_parser("accounts", help="List the chart of accounts with open dates and roles (read-only)")
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
             **{name: "YYYY-MM-DD" for name in ("as_of", "through", "from_date", "to_date", "cutover", "fiscal_start")}}


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
        "reversal_date": day, "reversal_strategy": {"enum": ["direct", "reverse"]}, "obligation_ids": ids,
        "origin": record(["mode", "source_id"], {"mode": {"const": "existing"}, "source_id": text})},
        anyOf=[{"required": ["evidence_ids"], "properties": {"evidence_ids": {"minItems": 1}}},
               {"required": ["estimate_basis"]}],
        description="Exact balance, account opening/currency, evidence, locks and authority are checked by the engine.")
    origin = record(["mode", "source_id"], {"mode": {"enum": ["new", "existing", "opening"]},
        "source_id": text, "date": day, "funding_account": text, "reclass_account": text, "counter_account": text})
    schedule_origin = record(["mode", "source_id"], {key: value for key, value in origin["properties"].items() if key != "counter_account"})
    schedule_origin["properties"]["funding_account"] = {**text, "description": "Existing USD asset account; prepaid schedules also allow Liabilities:CreditCard or its colon-separated subaccounts. Deferred revenue remains asset-funded."}
    item_origin = record(["mode", "source_id"], {key: value for key, value in origin["properties"].items() if key in {"mode", "source_id", "counter_account"}})
    schedule = record(["operation_id", "id", "kind", "account", "recognition_account", "currency", "amount",
                       "start_date", "end_date", "method", "origin"], {
        "operation_id": text, "id": text, "kind": {"enum": ["prepaid", "deferred"]}, "account": text,
        "recognition_account": text, "currency": {"const": "USD"}, "amount": amount,
        "start_date": day, "end_date": {**day, "description": "Exclusive service-period end date."},
        "method": {"enum": ["actual_day", "equal_month"]}, "origin": schedule_origin, "evidence_ids": ids,
        "recognition_start": day, "recognized_amount": amount,
        "reference": {**text, "maxLength": 2000, "description": "Human reference or description, separate from the stable machine ID."}},
        examples=[{"operation_id": "synthetic-prepaid-create", "id": "synthetic-prepaid", "reference": "Annual software subscription #104", "kind": "prepaid",
            "account": "Assets:Other:Prepaid-Expenses", "recognition_account": "Expenses:Software", "currency": "USD",
            "amount": "1200.00", "start_date": "2026-01-01", "end_date": "2027-01-01", "method": "equal_month",
            "origin": {"mode": "new", "source_id": "synthetic-funding", "date": "2026-01-01", "funding_account": "Assets:Bank:Checking"},
            "evidence_ids": ["synthetic-stored-evidence-id"]}])
    item = record(["operation_id", "id", "kind", "account", "currency", "original_amount", "as_of", "origin", "settlement_strategy"], {
        "operation_id": text, "id": text, "kind": {"enum": ["receivable", "payable", "accrual", "credit"]},
        "account": text, "currency": {"const": "USD"}, "original_amount": amount, "as_of": day, "origin": item_origin,
        "settlement_strategy": {"enum": ["direct", "reverse"]}, "direction": {"enum": ["debit", "credit"]},
        "counterparty": text, "reference": text, "due_date": {**day, "description": "Explicit payment due date; distinct from accounting as_of. Omit when unknown."}, "evidence_ids": ids, "reversal_date": day, "obligation_ids": ids, "remaining_amount": amount},
        examples=[{"operation_id": "synthetic-payable-create", "id": "synthetic-payable", "kind": "payable",
            "account": "Liabilities:Payable:Accounts-Payable", "currency": "USD", "original_amount": "100.00", "as_of": "2026-01-31",
            "origin": {"mode": "new", "source_id": "synthetic-bill", "counter_account": "Expenses:Software"},
            "settlement_strategy": "direct", "evidence_ids": ["synthetic-stored-evidence-id"]}])
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
        "kind": {"enum": ["prepaid", "deferred"]}, "amount": amount, "recognized_amount": amount, "remaining_amount": amount,
        "service_start": {**day, "description": "First service day (inclusive)."},
        "service_end": {**day, "description": "Last service day (inclusive)."},
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
        "schedules": {"type": "array", "items": opening_schedule, "description": "Remaining prepaid or deferred balances."},
        "external": {"type": "array", "items": opening_external, "description": "Support for other control accounts (fixed assets, loans, accruals)."},
        **refs}, description="Source-neutral opening balances. Use `books accrual opening draft` to start from an existing company; apply the unchanged preview.",
        examples=[{"operation_id": "synthetic-opening", "cutover": "2026-01-01", "evidence_refs": ["evidence/opening-ar-list.pdf"],
            "lines": [{"account": "Assets:Bank:Checking", "amount": "30000.00"}, {"account": "Assets:Receivable:Accounts-Receivable", "amount": "300.00"},
                      {"account": "Liabilities:Payable:Accounts-Payable", "amount": "-100.00"}, {"account": "Equity:Opening-Balances", "amount": "-30200.00"}],
            "items": [{"id": "synthetic-inv-1", "kind": "receivable", "account": "Assets:Receivable:Accounts-Receivable", "counterparty": "Synthetic Customer",
                       "reference": "INV-1", "due_date": "2025-12-15", "open_amount": "300.00"},
                      {"id": "synthetic-bill-1", "kind": "payable", "account": "Liabilities:Payable:Accounts-Payable", "counterparty": "Synthetic Supplier",
                       "reference": "BILL-9", "due_date": "2026-01-10", "open_amount": "100.00"}]}])
    return {"opening": opening, "journal": journal,
        "evidence_link": record(["operation_id", "target_operation_id", "evidence_ids", "reason"], {
            "operation_id": text, "target_operation_id": text, "evidence_ids": ids, "reason": text},
            description="Explicitly identify another source as evidence of a committed effect; no matching heuristic or financial posting."),
        "correction": record(["id", "original_id", "date", "description", "replacement"],
            {"id": text, "original_id": text, "date": day, "description": text, "replacement": journal}),
        "reversal": record(["id", "original_id", "date", "description"],
            {"id": text, "original_id": text, "date": day, "description": text}),
        "schedule": schedule,
        "schedule_due": record(["operation_id", "through"], {"operation_id": text, "through": day}),
        "schedule_change": record(["operation_id", "id", "action", "date", "reason"], {
            "operation_id": text, "id": text, "action": {"enum": ["cancel", "supersede", "refund"]}, "date": day,
            "reason": text, "evidence_ids": ids, "amount": amount, "offset_account": text,
            "recognized_refund_amount": amount, "remaining_refund_amount": amount, "recognized_refund_account": text,
            "replacement": record(["id", "start_date", "end_date", "method"], {"id": text, "start_date": day,
                "end_date": day, "method": {"enum": ["actual_day", "equal_month"]}, "recognition_account": text})}),
        "item": item,
        "settlement": record(["operation_id", "date", "source", "allocations"], {
            "operation_id": text, "date": day, "evidence_ids": ids,
            "source": record(["mode", "source_id", "bank_account", "amount"], {"mode": {"enum": ["new", "existing"]},
                "source_id": text, "bank_account": text, "amount": amount}),
            "allocations": {"type": "array", "minItems": 1, "items": record(["item_id", "amount"], {"item_id": text, "amount": amount})}}),
        "item_adjustment": record(["operation_id", "item_id", "action", "amount", "date", "counter_account", "source_id", "reason"], {
            "operation_id": text, "item_id": text, "action": {"enum": ["credit", "writeoff"]}, "amount": amount,
            "date": day, "counter_account": text, "source_id": text, "reason": text, "evidence_ids": ids}),
        "item_reversal": record(["operation_id", "item_id", "date", "counter_account", "reason"], {
            "operation_id": text, "item_id": text, "date": day, "counter_account": text, "reason": text, "evidence_ids": ids},
            description="Reverse a carried or new reverse-strategy obligation with explicit income/expense offset; original opening journal stays intact."),
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


def _replayed(entity: Any, plan: Any) -> dict[str, Any] | None:
    """Exact repeat of a committed request: original receipt, status already_applied, no new effect."""
    from .store import ReplayConflict, committed_replay
    try:
        receipt = committed_replay(_read_only_store(entity), plan)
    except ReplayConflict as exc:
        raise journals.JournalError("identity_conflict", str(exc),
            f"Same operation_id with changed facts is a conflict. Inspect it with `books accrual receipt --id {exc.operation_id}`; "
            "use the committed facts, or make an explicit correction with a new operation_id.") from None
    return receipt


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
                      "verbs": verbs, "authority_action": "adjust", "posting_requires_opt_in": True,
                      "operator_only": ["period"], "request_schemas": sorted(request_schemas())}
        else:
            entity = load_entity(args.entity)
            verb_name = getattr(args, command.replace("-", "_") + "_command", None)
            replay = _replayed(entity, _input(args.input)) if isinstance(verb_name, str) and verb_name.endswith("apply") else None
            if replay is not None:
                result = replay
            elif command == "intake":
                from .store import check_intake_conflict
                payload = _input(args.input)
                check_intake_conflict(_read_only_store(entity), payload)
                result = journals.intake_evidence(entity, payload)
            elif command == "accounts":
                from . import accounts
                result = accounts.list_accounts(entity)
            elif command == "receipt":
                from .store import operation_receipt
                record = operation_receipt(_read_only_store(entity), args.id)
                if record is None:
                    raise journals.JournalError("receipt_not_found", f"No committed operation has ID {args.id}.",
                                                "Check operation_id, or `books accrual report --kind adjustments`. The operation was not applied.")
                result = {"status": record["status"], "id": args.id, "kind": record["kind"], "request_hash": record["request_hash"],
                          "plan_hash": record["plan_hash"], "receipt": record["receipt"]}
            elif command == "opening":
                from . import opening
                verb = args.accrual_opening_command
                if verb == "draft":
                    result = opening.draft_from_cash_company(entity, args.cutover)
                elif verb == "preview":
                    result = opening.preview_opening(entity, _input(args.input))
                else:
                    plan = _input(args.input)
                    replay = _replayed(entity, plan.get("accounting_plan"))
                    result = replay or opening.apply_opening(entity, plan)
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
                        "reverse-preview": items.preview_reversal, "reverse-apply": items.apply_reversal}[verb]
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
        elif command == "accounts" and args.format == "text":
            from .accounts import render_accounts
            print(render_accounts(result))
        else:
            print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
        return 1 if isinstance(result, dict) and result.get("status") == "blocked" else 0
    except (ValueError, PermissionError, OSError, ImportError) as exc:
        if hasattr(exc, "as_dict"):
            result = exc.as_dict()
        else:
            result = {"status": "blocked", "errors": [{"code": getattr(exc, "reason", "invalid_operation"), "message": str(exc)}]}
        print(json.dumps(result, sort_keys=True, indent=2))
        return 1
