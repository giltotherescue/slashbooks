"""Source-neutral accrual opening balances.

One request describes the cutover date, balanced opening lines and optional
supporting rows (open invoices, open bills, remaining prepaid or deferred
balances, and external control-account support). The request goes through the
same tie-out and atomic apply path as the QuickBooks migration, which is only a
parser that builds the same kind of opening.
"""
from __future__ import annotations

from datetime import date, timedelta
import json
from decimal import Decimal
from typing import Any, Mapping

from ..entity import Entity, load_entity, require_accrual
from ..ledger.model import _validate_account_name
from ..ledger.store import LedgerStore, default_store_path
from .journals import JournalError
from .migration import _build_plan, _hash, _money

REQUEST_FIELDS = {"operation_id", "cutover", "lines", "items", "schedules", "external", "evidence_refs", "evidence_ids"}
LINE_FIELDS = {"account", "amount", "debit", "credit"}
ITEM_FIELDS = {"id", "kind", "account", "counterparty", "reference", "description", "due_date", "open_amount",
               "original_amount", "direction", "evidence_refs", "evidence_ids"}
SCHEDULE_FIELDS = {"id", "kind", "account", "recognition_account", "contra_account", "salvage_value", "amount", "recognized_amount", "remaining_amount",
                   "service_start", "service_end", "method", "counterparty", "reference", "description", "evidence_refs", "evidence_ids", "currency"}
EXTERNAL_FIELDS = {"id", "account", "balance", "counterparty", "reference", "description", "evidence_refs", "evidence_ids", "currency"}
BALANCE_SHEET_ROOTS = ("Assets:", "Liabilities:", "Equity:")
NEW_COMPANY_STEPS = (
    "books entity init NEW_DIR --name NAME --basis accrual --cutover-date CUTOVER",
    "books accrual opening draft --entity CASH_DIR --cutover CUTOVER > draft.json",
    "Edit draft.json: add open invoices, open bills and any prepaid or deferred balances. Keep the lines balanced.",
    "books accrual opening preview --entity NEW_DIR --input draft.json > plan.json",
    "books accrual opening apply --entity NEW_DIR --input plan.json",
)


def _err(code: str, message: str, action: str) -> JournalError:
    return JournalError(code, message, action)


def _unknown(row: Mapping[str, Any], allowed: set[str], where: str) -> None:
    extra = sorted(set(row) - allowed)
    if extra:
        raise _err("unknown_field", f"Unknown field {', '.join(repr(k) for k in extra)} in {where}.",
                   f"Remove {', '.join(extra)} from {where}. Allowed fields: {', '.join(sorted(allowed))}.")


def _rows(request: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    rows = request.get(name, [])
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise _err("invalid_request", f"{name} must be an array of objects.", f"Set {name} to an array of objects or omit it.")
    return rows


def _line_amount(line: Mapping[str, Any], where: str) -> Decimal:
    given = [k for k in ("amount", "debit", "credit") if k in line]
    if not given or ("amount" in given and len(given) > 1):
        raise _err("invalid_line", f"{where} needs signed amount, or debit and/or credit.",
                   f"Give {where} either amount (debit positive, credit negative) or debit/credit, not both styles.")
    try:
        if "amount" in line:
            return _money(line["amount"])
        debit, credit = _money(line.get("debit", "0.00")), _money(line.get("credit", "0.00"))
    except ValueError as exc:
        raise _err("invalid_amount", f"{where}: {exc}", f"Use an exact decimal string such as \"100.00\" in {where}.") from None
    if debit < 0 or credit < 0 or (debit and credit):
        raise _err("invalid_line", f"{where} debit and credit must be nonnegative with one nonzero side.",
                   f"Use one nonzero side in {where}.")
    return debit - credit


def normalize_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise _err("invalid_request", "Opening request must be an object.", "Pass one JSON object; see `books accrual schema` request opening.")
    _unknown(request, REQUEST_FIELDS, "the opening request")
    try:
        cutover = date.fromisoformat(request.get("cutover"))
    except (TypeError, ValueError):
        raise _err("invalid_date", "cutover must be a YYYY-MM-DD date.",
                   "Set cutover to the first day of accrual books. The opening entry is dated the day before.") from None
    lines = []
    seen = set()
    for index, line in enumerate(_rows(request, "lines")):
        where = f"lines[{index}]"
        _unknown(line, LINE_FIELDS, where)
        account = line.get("account")
        if not isinstance(account, str):
            raise _err("invalid_line", f"{where}.account is required.", f"Set {where}.account to a chart account name.")
        try:
            _validate_account_name(account)
        except ValueError as exc:
            raise _err("invalid_account", f"{where}.account: {exc}", f"Fix {where}.account.") from None
        if not account.startswith(BALANCE_SHEET_ROOTS):
            raise _err("opening_income_expense", f"{where}.account {account} is an income or expense account.",
                       "An opening carries balance sheet accounts only. Put prior-period earnings in an Equity account such as Equity:Retained-Earnings.")
        if account in seen:
            raise _err("duplicate_line", f"Account {account} appears twice in lines.", "Combine the amounts into one line per account.")
        seen.add(account)
        amount = _line_amount(line, where)
        if amount:
            lines.append({"account": account, "amount": format(amount, ".2f")})
    if not lines:
        raise _err("missing_lines", "lines is empty.", "Add opening balance lines (account and amount).")
    total = sum(Decimal(row["amount"]) for row in lines)
    if total:
        raise _err("unbalanced_opening", f"Opening lines do not balance; debits minus credits is {total:.2f} USD.",
                   "Adjust lines so debits equal credits. Use Equity:Retained-Earnings or Equity:Opening-Balances for the difference only if it is real.")
    default_refs = {k: request.get(k, []) for k in ("evidence_refs", "evidence_ids")}
    normalized: dict[str, Any] = {"operation_id": request.get("operation_id") or f"opening-{cutover.isoformat()}",
                                  "cutover": cutover.isoformat(), "lines": lines}
    for key, value in default_refs.items():
        if value:
            normalized[key] = value
    for name, allowed in (("items", ITEM_FIELDS), ("schedules", SCHEDULE_FIELDS), ("external", EXTERNAL_FIELDS)):
        out = []
        for index, row in enumerate(_rows(request, name)):
            where = f"{name}[{index}]"
            _unknown(row, allowed, where)
            row = dict(row)
            if not any(row.get(k) for k in ("evidence_refs", "evidence_ids")):
                if not any(default_refs.values()):
                    raise _err("missing_evidence", f"{where} has no supporting document.",
                               f"Set {where}.evidence_refs to a company-relative file path (or evidence_ids to a stored evidence ID), or set request-level evidence_refs.")
                row.update({k: v for k, v in default_refs.items() if v})
            row.setdefault("currency", "USD")
            if name == "items":
                if "open_amount" not in row:
                    raise _err("missing_open_amount", f"{where}.open_amount is required.", f"Set {where}.open_amount to the unpaid amount at cutover.")
                row["remaining_amount"] = row.pop("open_amount")
                row.setdefault("original_amount", row["remaining_amount"])
            elif name == "schedules" and "remaining_amount" not in row and "amount" in row and "recognized_amount" in row:
                row["remaining_amount"] = format(_money(row["amount"]) - _money(row.get("salvage_value", "0.00")) - _money(row["recognized_amount"]), ".2f")
            out.append(row)
        if out:
            normalized[name] = out
    return normalized


def preview_opening(entity: Entity, request: Any) -> dict[str, Any]:
    """Preview an opening without writing. Returns a plan that `apply` takes unchanged."""
    entity = load_entity(entity.path)
    require_accrual(entity)
    raw_request = json.loads(json.dumps(request))
    request = normalize_request(request)
    cutover = date.fromisoformat(request["cutover"])
    as_of = cutover - timedelta(days=1)
    financial = {row["account"]: Decimal(row["amount"]) for row in request["lines"]}
    store = LedgerStore(default_store_path(entity.path))
    existing = {opened.account: opened for opened in store.load_opens()}
    supporting = {name: request[name] for name in ("items", "schedules", "external") if name in request}
    used = set(financial)
    for rows in supporting.values():
        for row in rows:
            account = row.get("account")
            if isinstance(account, str):
                used.add(account)
    for row in supporting.get("schedules", []):
        if isinstance(row.get("contra_account"), str):
            used.add(row["contra_account"])
        recognition = row.get("recognition_account")
        if isinstance(recognition, str) and recognition not in existing:
            raise _err("unknown_recognition_account", f"schedules recognition_account {recognition} is not in the chart.",
                       f"Add it first: books entity account-add --entity PATH --account {recognition}")
    mapping = {name: name for name in sorted(used | set(existing))}
    chart = []
    for account in sorted(used):
        try:
            _validate_account_name(account)
        except ValueError as exc:
            raise _err("invalid_account", f"{account}: {exc}", "Use a valid chart account name.") from None
        opened = existing.get(account)
        if opened and opened.currencies and "USD" not in opened.currencies:
            raise _err("unsupported_currency", f"Account {account} does not support USD.", "Use a USD account.")
        # Opening the account as of the opening date lets the opening entry post.
        effective = min(as_of, opened.date) if opened else as_of
        chart.append({"source_account": account, "account": account, "account_type": account.split(":")[0],
                      "detail_type": "", "open_date": effective.isoformat(), "currency": "USD"})
    try:
        return _build_plan(
            entity, store, operation="accrual-opening", import_source="accrual-opening",
            narration="Accrual opening balances", financial=financial, mapping=mapping, chart=chart,
            supporting=supporting, cutover=cutover, fiscal_start=cutover, closed_position=True,
            source_id=request["operation_id"], coverage_extra={"source_hashes": {}, "mapping_hash": _hash(mapping)},
            request_extra={"opening": "accrual-opening"}, plan_extra={"request": raw_request, "folder": None})
    except ValueError as exc:
        if isinstance(exc, JournalError):
            raise
        raise _err("opening_invalid", str(exc), "Fix the named field in the opening request and preview again.") from None


def apply_opening(entity: Entity, plan: Mapping[str, Any]) -> dict[str, Any]:
    from . import migration
    if not isinstance(plan, Mapping) or plan.get("operation") != "accrual-opening":
        raise _err("invalid_plan", "Pass the unchanged output of `books accrual opening preview`.",
                   "Run `books accrual opening preview --entity PATH --input REQUEST` and pass its output to apply.")
    return migration.apply(entity, dict(plan))


def draft_from_cash_company(entity: Entity, cutover: str) -> dict[str, Any]:
    """Draft opening lines from an existing company's balances the day before cutover.

    Read-only. The agent must still add open receivables, payables and any
    prepaid or deferred balances, and adjust the equity line to keep it balanced.
    """
    entity = load_entity(entity.path)
    day = date.fromisoformat(cutover)
    as_of = day - timedelta(days=1)
    store = LedgerStore(default_store_path(entity.path))
    totals: dict[str, Decimal] = {}
    with store.connection() as conn:
        for account, amount in conn.execute(
                "SELECT p.account,p.amount FROM postings p JOIN entries e ON e.id=p.entry_id WHERE e.date<=?", (as_of.isoformat(),)):
            totals[account] = totals.get(account, Decimal(0)) + Decimal(amount)
    earnings = -sum((v for a, v in totals.items() if a.startswith(("Income:", "Expenses:"))), Decimal(0))
    lines = {a: v for a, v in totals.items() if a.startswith(BALANCE_SHEET_ROOTS) and v}
    if earnings:
        lines["Equity:Retained-Earnings"] = lines.get("Equity:Retained-Earnings", Decimal(0)) + earnings * -1
    lines = {a: v for a, v in lines.items() if v}
    request = {"operation_id": f"opening-{day.isoformat()}", "cutover": day.isoformat(),
               "lines": [{"account": a, "amount": format(v, ".2f")} for a, v in sorted(lines.items())]}
    return {"status": "draft", "source_company": entity.name, "balances_as_of": as_of.isoformat(), "request": request,
            "agent_must_add": [
                "items: each open customer invoice and supplier bill (id, kind, account, counterparty, reference, due_date, open_amount).",
                "lines: add the receivable and payable accounts and a balancing equity line for them (cash books carry no receivables or payables).",
                "schedules or external: remaining prepaid, deferred, fixed asset and other control balances.",
                "evidence_refs: a company-relative file for each supporting row."],
            "next_steps": list(NEW_COMPANY_STEPS)}
