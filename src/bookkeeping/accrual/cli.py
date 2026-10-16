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
    report.add_argument("--kind", required=True, choices=("schedules", "outstanding", "adjustments", "support", "reconciliation"))
    report.add_argument("--as-of")
    report.add_argument("--from", dest="from_date")
    report.add_argument("--to", dest="to_date")
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


def contracts() -> dict[str, Any]:
    return {"schema_version": 1, "capability_version": CAPABILITY_VERSION,
            "requests": request_schemas(),
            "commands": ["accrual schema", "accrual capabilities", "accrual intake --entity PATH --input FILE",
                "accrual journal preview|apply|correct-preview|correct-apply|reverse-preview|reverse-apply --entity PATH --input FILE",
                "accrual journal due-reversals --entity PATH --as-of YYYY-MM-DD [--apply]",
                "accrual schedule preview|apply|change-preview|change-apply|due-apply --entity PATH --input FILE",
                "accrual schedule due-preview --entity PATH --through YYYY-MM-DD --operation-id ID",
                "accrual schedule list --entity PATH", "accrual item list --entity PATH",
                "accrual item preview|apply|settle-preview|settle-apply|adjust-preview|adjust-apply|reverse-preview|reverse-apply --entity PATH --input FILE",
                "accrual support preview|apply --entity PATH --input FILE",
                "accrual evidence-link preview|apply --entity PATH --input FILE",
                "accrual period preview|apply --entity PATH --input FILE (local operator only; adjust authority does not cover close/reopen)",
                "accrual report --entity PATH --kind schedules|outstanding|adjustments|support|reconciliation [--as-of DATE --from DATE --to DATE]",
                "accrual migration preview --entity PATH --folder REPORTS --cutover YYYY-MM-DD --fiscal-start YYYY-MM-DD --supporting FILE [--closed-position]",
                "accrual migration apply --entity PATH --input FILE"],
            "evidence": schema(), "journal": {
                "required": ["id", "date", "description", "postings"],
                "optional": ["evidence_ids", "estimate_basis", "reversal_date", "reversal_strategy", "origin", "obligation_ids"],
                "posting": {"account": "mapped chart account", "amount": "exact USD decimal string or {minor_units:integer}", "currency": "USD"},
                "evidence_rule": "Stored evidence or an explicitly disclosed estimate basis is required.",
                "authority": "Local operator or independently authenticated adjust context; JSON claims grant no permission.",
                "correction": {"required": ["id", "original_id", "date", "description", "replacement"],
                               "replacement": "journal request with a fresh id"},
                "reversal": {"required": ["id", "original_id", "date", "description"]},
                "example": {"id": "synthetic-adjustment", "date": "2026-01-31", "description": "Synthetic estimate",
                    "estimate_basis": "Synthetic documented estimate", "postings": [
                        {"account": "Expenses:Software", "amount": "100.00"},
                        {"account": "Liabilities:AccountsPayable", "amount": "-100.00"}]}}}


def request_schemas() -> dict[str, Any]:
    """Discoverable input shapes; domain validation enforces financial invariants."""
    text = {"type": "string", "minLength": 1}
    day = {"type": "string", "format": "date", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
    amount = schema()["properties"]["amounts"]["additionalProperties"]
    ids = {"type": "array", "uniqueItems": True, "maxItems": 1000, "items": text}

    def record(required: list[str], properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                "required": required, "properties": properties, "additionalProperties": False, **extra}

    posting = record(["account", "amount"], {"account": text, "amount": amount, "currency": {"const": "USD"}})
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
    item_origin = record(["mode", "source_id"], {key: value for key, value in origin["properties"].items() if key in {"mode", "source_id", "counter_account"}})
    schedule = record(["operation_id", "id", "kind", "account", "recognition_account", "currency", "amount",
                       "start_date", "end_date", "method", "origin"], {
        "operation_id": text, "id": text, "kind": {"enum": ["prepaid", "deferred"]}, "account": text,
        "recognition_account": text, "currency": {"const": "USD"}, "amount": amount,
        "start_date": day, "end_date": {**day, "description": "Exclusive service-period end date."},
        "method": {"enum": ["actual_day", "equal_month"]}, "origin": schedule_origin, "evidence_ids": ids,
        "recognition_start": day, "recognized_amount": amount},
        examples=[{"operation_id": "synthetic-prepaid-create", "id": "synthetic-prepaid", "kind": "prepaid",
            "account": "Assets:PrepaidExpenses", "recognition_account": "Expenses:Software", "currency": "USD",
            "amount": "1200.00", "start_date": "2026-01-01", "end_date": "2027-01-01", "method": "equal_month",
            "origin": {"mode": "new", "source_id": "synthetic-funding", "date": "2026-01-01", "funding_account": "Assets:Bank:Checking"},
            "evidence_ids": ["synthetic-stored-evidence-id"]}])
    item = record(["operation_id", "id", "kind", "account", "currency", "original_amount", "as_of", "origin", "settlement_strategy"], {
        "operation_id": text, "id": text, "kind": {"enum": ["receivable", "payable", "accrual", "credit"]},
        "account": text, "currency": {"const": "USD"}, "original_amount": amount, "as_of": day, "origin": item_origin,
        "settlement_strategy": {"enum": ["direct", "reverse"]}, "direction": {"enum": ["debit", "credit"]},
        "counterparty": text, "reference": text, "evidence_ids": ids, "reversal_date": day, "obligation_ids": ids, "remaining_amount": amount},
        examples=[{"operation_id": "synthetic-payable-create", "id": "synthetic-payable", "kind": "payable",
            "account": "Liabilities:AccountsPayable", "currency": "USD", "original_amount": "100.00", "as_of": "2026-01-31",
            "origin": {"mode": "new", "source_id": "synthetic-bill", "counter_account": "Expenses:Software"},
            "settlement_strategy": "direct", "evidence_ids": ["synthetic-stored-evidence-id"]}])
    support_row = {"type": "object", "required": ["id", "account", "currency"],
        "properties": {"id": text, "account": text, "currency": {"const": "USD"},
            "evidence_refs": ids, "evidence_ids": ids},
        "description": "Opening item/schedule/external support; collection-specific facts and exact control tie-out are validated by migration."}
    def opening_support(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
        return {**support_row, "required": [*support_row["required"], *required],
                "properties": {**support_row["properties"], **properties},
                "anyOf": [{"required": ["evidence_refs"], "properties": {"evidence_refs": {"minItems": 1}}},
                          {"required": ["evidence_ids"], "properties": {"evidence_ids": {"minItems": 1}}}]}
    opening_item = opening_support(["kind", "original_amount", "remaining_amount"], {
        "kind": {"enum": ["receivable", "payable", "accrual", "credit"]}, "original_amount": amount,
        "remaining_amount": amount, "settlement_strategy": {"enum": ["direct", "reverse"]},
        "direction": {"enum": ["debit", "credit"]}})
    opening_schedule = opening_support(["kind", "amount", "recognized_amount", "remaining_amount", "start_date", "end_date", "method", "recognition_account"], {
        "kind": {"enum": ["prepaid", "deferred"]}, "amount": amount, "recognized_amount": amount, "remaining_amount": amount,
        "start_date": day, "end_date": {**day, "description": "Exclusive service-period end."},
        "method": {"enum": ["actual_day", "equal_month"]}, "recognition_account": text})
    opening_external = opening_support(["balance"], {"balance": amount})
    return {"journal": journal,
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
            result = {"capability_version": CAPABILITY_VERSION, "currency": "USD", "basis": "accrual",
                      "operations": ["intake", "evidence_link", "journal", "correction", "reversal", "schedule", "item", "item_reversal", "migration", "support", "report"],
                      "authority_action": "adjust", "posting_requires_opt_in": True}
        else:
            entity = load_entity(args.entity)
            if command == "intake":
                result = journals.intake_evidence(entity, _input(args.input))
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
        print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
        return 1 if isinstance(result, dict) and result.get("status") == "blocked" else 0
    except (ValueError, PermissionError, OSError, ImportError) as exc:
        if isinstance(exc, (IntakeValidationError, journals.JournalError)):
            result = exc.as_dict()
        else:
            result = {"status": "blocked", "errors": [{"code": getattr(exc, "reason", "invalid_operation"), "message": str(exc)}]}
        print(json.dumps(result, sort_keys=True, indent=2))
        return 1
