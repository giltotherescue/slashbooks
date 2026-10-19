"""Same-basis QuickBooks opening cutover with explicit summary coverage.

Only a reconciled full trial balance can activate an empty accrual target.
Supporting rows describe carried balances and never create a second origin.
Full historical transaction reconstruction is intentionally separate.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from .. import quickbooks as qb
from ..entity import load_entity
from ..ledger.importer import _entity_write_lock, check_store_integrity
from ..ledger.model import Entry, Open, Posting, _validate_account_name
from ..ledger.store import ACCRUAL_SCHEMA_VERSION, LedgerStore, default_store_path
from .store import SupportingMutation, TABLES, list_records, load_record
from .intake import canonical_hash, evidence_hash, facts_hash, normalize_evidence, source_identity

VERSION = 1
TRACKING_COVERAGE_ID = "tracking"


def require_capability(entity):
    """An opening needs the accrual capability (version 1). The default report view does not matter."""
    from .scope import capability
    if capability(entity) != 1:
        raise ValueError("Accrual writes require capability version 1. Run `books accrual enable` first, or create the company with --basis accrual.")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _money(value):
    if isinstance(value, (float, bool)):
        raise ValueError("Amounts require exact decimal strings, never floats.")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
            raise ValueError("Amounts require finite USD minor-unit precision.")
        return amount
    except (InvalidOperation, TypeError):
        raise ValueError("Invalid exact monetary amount.") from None


def _day(value):
    return value if isinstance(value, date) else date.fromisoformat(value)


def _source_folder(entity, folder):
    root = entity.path.resolve()
    candidate = Path(folder)
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise ValueError("Stage opening source reports inside the company under ingestion/quickbooks or qb-exports before preview.") from None
    if (not relative.parts or relative.parts[0] not in {"ingestion", "qb-exports", "intake", "evidence", "documents"}
            or ".." in relative.parts or any(root.joinpath(*relative.parts[:i]).is_symlink()
                                             for i in range(1, len(relative.parts) + 1))
            or not candidate.is_dir()):
        raise ValueError("Opening source folder must be a durable company-relative directory without symlinks or escapes.")
    return candidate, relative.as_posix()


def _source_amount(value):
    text = value.strip().strip('"').replace("$", "").replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    return _money(text or "0")


def _validate_source_rows(rows, kind):
    """Reject precision loss and ignored cells before the legacy parsers round."""
    started, totals, debit, credit = False, [], Decimal(0), Decimal(0)
    for raw in rows:
        cells = [str(c).strip() for c in raw]
        cells += [""] * max(0, 3 - len(cells))
        if kind == "trial_balance" and qb._is_trial_balance_column_header(cells[0], cells[1]):
            started = True
            continue
        if kind != "trial_balance" and cells[0] == "" and cells[1] == "Total":
            started = True
            continue
        if not started or qb._is_basis_footer_label(cells[0]) or not any(cells):
            continue
        width = 3 if kind == "trial_balance" else 2
        if any(cells[width:]):
            raise ValueError("Opening report has unsupported extra amount columns.")
        amounts = [_source_amount(value) for value in cells[1:width]]
        if kind == "trial_balance":
            if cells[0] == "TOTAL":
                totals.append(tuple(amounts))
            elif not cells[0].startswith("Total for "):
                debit += amounts[0]
                credit += amounts[1]
    if not started:
        raise ValueError(f"Opening report is missing a supported data header: {kind}")
    if kind == "trial_balance" and (len(totals) != 1 or totals[0] != (debit, credit)):
        raise ValueError("Source trial balance totals do not reconcile exactly to all account rows.")


def _mapping(entity, accounts):
    raw = (entity.entity_config.get("compare") or {}).get("qbo_account_crosswalk") or {}
    if not isinstance(raw, dict):
        raise ValueError("QuickBooks account crosswalk must be an object.")
    qb._reset_collision_registry()
    mapping = {}
    for account in sorted(accounts, key=lambda a: a.name):
        kind = account.account_type.strip().lower()
        if kind not in qb._QB_TYPE_ROOT:
            raise ValueError(f"Unsupported QuickBooks account type for {account.name}: {account.account_type}")
        target = raw.get(account.name)
        if isinstance(target, dict):
            target = target.get("local_account")
        target = target or qb.map_qb_account(account.name, account.account_type)
        _validate_account_name(target)
        if target.split(":")[0] != qb._QB_TYPE_ROOT[kind].split(":")[0]:
            raise ValueError(f"Account mapping changes the accounting type of {account.name}.")
        if account.name in mapping or target in mapping.values():
            raise ValueError(f"Duplicate or ambiguous QuickBooks account mapping: {account.name}")
        mapping[account.name] = target
    return mapping


def _name(name, mapping):
    if name in mapping:
        return name
    matches = [key for key in mapping if key.split(":")[-1].strip() == name.strip()]
    if len(matches) != 1:
        raise ValueError(f"Missing or ambiguous report account mapping: {name}")
    return matches[0]


def _chart(accounts, mapping, store, as_of):
    """Retain every source account, including accounts absent from the TB.

    The export has no historical opening dates. Open mapped accounts no later
    than the opening position while preserving any earlier canonical opening.
    Source type/detail remain inspectable alongside the canonical account root.
    """
    existing = {opened.account: opened for opened in store.load_opens()}
    chart = []
    for source in sorted(accounts, key=lambda account: account.name):
        account = mapping[source.name]
        opened = existing.get(account)
        if opened and opened.currencies and "USD" not in opened.currencies:
            raise ValueError(f"Opening chart conflicts with the existing account currency: {account}")
        effective_date = min(as_of, opened.date) if opened else as_of
        chart.append({"source_account": source.name, "account": account,
                      "account_type": source.account_type, "detail_type": source.detail_type,
                      "open_date": effective_date.isoformat(), "currency": "USD"})
    return chart


def _tie_reports(tb, bs, pnl, mapping, *, closed_position=False):
    balances = {}
    for row in tb.entries:
        debit, credit = _money(row.debit), _money(row.credit)
        if debit < 0 or credit < 0 or (debit and credit):
            raise ValueError(f"Invalid trial balance debit/credit treatment: {row.full_name}")
        name = _name(row.full_name, mapping)
        if name in balances:
            raise ValueError(f"Duplicate trial balance account: {row.full_name}")
        balances[name] = debit - credit
    if not balances or sum(balances.values()) != 0:
        raise ValueError("Opening trial balance must balance exactly; no equity plug is allowed.")
    financial = {mapping[name]: amount for name, amount in balances.items()}
    earnings = -sum(amount for account, amount in financial.items()
                    if account.startswith(("Income:", "Expenses:")))
    pl_actual = {}
    for row in pnl.rows:
        if row.row_type != "leaf":
            continue
        name = _name(row.name, mapping)
        account = mapping[name]
        if not account.startswith(("Income:", "Expenses:")):
            raise ValueError(f"P&L contains a non-income/expense account: {name}")
        if account in pl_actual:
            raise ValueError(f"Duplicate P&L account: {name}")
        pl_actual[account] = _money(row.amount) * (-1 if account.startswith("Income:") else 1)
    expected_pl = {a: n for a, n in financial.items() if a.startswith(("Income:", "Expenses:"))}
    if closed_position:
        if any(expected_pl.values()):
            raise ValueError("Completed fiscal closing position must have zero income/expense TB balances.")
        net_rows = [r for r in pnl.rows if r.name.strip().lower() in {"net income", "net profit"}]
        if len(net_rows) != 1 or _money(net_rows[0].amount) != -sum(pl_actual.values()):
            raise ValueError("Completed prior fiscal P&L does not reconcile to its net income.")
    else:
        for account in set(expected_pl) | set(pl_actual):
            delta = pl_actual.get(account, Decimal(0)) - expected_pl.get(account, Decimal(0))
            if delta:
                raise ValueError(f"P&L does not reconcile to trial balance: {account}; difference {delta:.2f} USD.")
    actual_bs, current_earnings = {}, None
    for row in bs.rows:
        if row.name.strip().lower() in {"net income", "net profit", "current year earnings", "current-year earnings"}:
            if current_earnings is not None:
                raise ValueError("Duplicate current earnings in balance sheet.")
            current_earnings = _money(row.amount)
            continue
        if row.row_type != "leaf":
            continue
        name = _name(row.name, mapping)
        account = mapping[name]
        if account.startswith(("Income:", "Expenses:")):
            raise ValueError(f"Balance sheet contains a P&L account: {name}")
        if account in actual_bs:
            raise ValueError(f"Duplicate balance sheet account: {name}")
        actual_bs[account] = _money(row.amount) * (1 if account.startswith("Assets:") else -1)
    expected_bs = {a: n for a, n in financial.items() if not a.startswith(("Income:", "Expenses:"))}
    for account in set(expected_bs) | set(actual_bs):
        delta = actual_bs.get(account, Decimal(0)) - expected_bs.get(account, Decimal(0))
        if delta:
            raise ValueError(f"Balance sheet does not reconcile to trial balance: {account}; difference {delta:.2f} USD.")
    if (current_earnings or Decimal(0)) != earnings:
        raise ValueError(f"Balance sheet current earnings differ from YTD P&L by {(current_earnings or Decimal(0)) - earnings:.2f} USD.")
    # Current earnings are derived from the carried I/E accounts. They must not
    # also be embedded as an explicit equity balance in the source TB.
    if earnings and any(n and re.sub(r"[^a-z]", "", name.lower()) in
                        {"netincome", "currentyearearnings"} for name, n in balances.items()):
        raise ValueError("Trial balance carries current earnings twice.")
    return financial


def _support(supporting, financial, mapping, source_id, cutover, as_of, entity, store):
    supporting = deepcopy(supporting or {})
    if set(supporting) - {"items", "schedules", "external"}:
        raise ValueError("Unsupported opening supporting collection.")
    sums, mutations, ids, evidence = {}, [], set(), {}
    common_fields = {"id", "account", "currency", "evidence_refs", "evidence_ids", "counterparty", "reference", "description"}
    source_fields = {
        "items": common_fields | {"kind", "original_amount", "remaining_amount", "direction", "due_date"},
        "schedules": common_fields | {"kind", "amount", "recognized_amount", "remaining_amount", "service_start", "service_end", "method", "recognition_account", "contra_account", "salvage_value"},
        "external": common_fields | {"balance"},
    }
    for collection in ("items", "schedules", "external"):
        rows = supporting.get(collection, [])
        if not isinstance(rows, list):
            raise ValueError(f"Opening {collection} support must be an array.")
        for source_row in rows:
            if not isinstance(source_row, dict):
                raise ValueError("Opening support requires object rows.")
            unsupported = set(source_row) - source_fields[collection]
            if unsupported:
                raise ValueError("Unsupported opening support source fields: " + ", ".join(sorted(unsupported)) +
                                 ". Engine history and lifecycle state cannot be imported as source facts.")
            row = deepcopy(source_row)
            identity = row.get("id")
            if not isinstance(identity, str) or not identity or (collection, identity) in ids:
                raise ValueError("Opening support requires unique stable IDs.")
            ids.add((collection, identity))
            account = row.get("account")
            account = mapping.get(account, account)
            if account not in financial or account.startswith(("Income:", "Expenses:", "Equity:")):
                raise ValueError(f"Opening support account is not a carried control balance: {account}")
            if row.get("currency") != "USD":
                raise ValueError("Opening support currency must be USD.")
            refs = row.get("evidence_refs", [])
            canonical_ids = row.get("evidence_ids", [])
            if (not isinstance(refs, list) or not isinstance(canonical_ids, list)
                    or not (refs or canonical_ids)
                    or any(not isinstance(r, str) or not r for r in refs + canonical_ids)):
                raise ValueError(f"Opening support needs inspectable supporting document references: {identity}")
            row["evidence_ids"] = []
            for evidence_id in canonical_ids:
                existing_evidence = load_record(store, "evidence", evidence_id)
                if not existing_evidence:
                    raise ValueError(f"Opening support references missing canonical supporting document: {evidence_id}")
                row["evidence_ids"].append(evidence_id)
                evidence[evidence_id] = {"existing": True, "payload": existing_evidence}
            for reference in refs:
                relative = Path(reference)
                root = entity.path.resolve()
                if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                    raise ValueError("Opening support must be a company-relative file or a canonical supporting document ID.")
                target = root / relative
                if (any((root.joinpath(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1))
                        or not target.is_file() or target.stat().st_nlink != 1 or target.stat().st_size > 32 * 1024 * 1024):
                    raise ValueError(f"Opening supporting document file is missing or unsafe: {reference}")
                raw = target.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                envelope = normalize_evidence({"schema_version": 1, "currency": "USD",
                    "source": {"namespace": "accounting.opening.documents", "account_id": "opening",
                               "object_id": relative.as_posix(), "content_hash": digest},
                    "dates": {"as_of": as_of.isoformat()},
                    "references": [{"content_hash": digest, "uri": relative.as_posix()}],
                    "observed_facts": {"source_path": relative.as_posix(), "opening_source_id": source_id}})
                evidence_id = canonical_hash(envelope)
                payload = {"id": evidence_id, "envelope": envelope, "source_identity": source_identity(envelope),
                           "facts_hash": facts_hash(envelope), "evidence_hash": evidence_hash(envelope)}
                existing_evidence = load_record(store, "evidence", evidence_id)
                if existing_evidence and existing_evidence != payload:
                    raise ValueError("Opening supporting document identity conflicts with an existing immutable record.")
                evidence[evidence_id] = {"existing": bool(existing_evidence), "payload": payload}
                if evidence_id not in row["evidence_ids"]:
                    row["evidence_ids"].append(evidence_id)
            row["account"] = account
            row["origin"] = {"mode": "opening", "source_id": source_id}
            row["opening_source_id"] = source_id
            row["as_of"] = as_of.isoformat()
            if collection == "external":
                # External support explicitly reports signed debit-positive
                # balances, including contra assets and liability credits.
                balance = _money(row.get("balance"))
                row["balance"] = format(balance, ".2f")
                row["id"] = "opening-support:" + identity
                row["kind"] = "external_support"
                mutation_table = "coverage"
            else:
                remaining = _money(row.get("remaining_amount"))
                if remaining < 0:
                    raise ValueError("Opening support remaining amount cannot be negative.")
                row["remaining_amount"] = format(remaining, ".2f")
                row["base_remaining_amount"] = format(remaining, ".2f")
                if collection == "items":
                    from .items import validate_due_date
                    due_date = validate_due_date(row)
                    if due_date is not None:
                        row["due_date"] = due_date
                    if row.get("kind") not in {"receivable", "payable", "accrual", "credit"}:
                        raise ValueError("Unsupported opening item kind.")
                    if _money(row.get("original_amount")) < remaining:
                        raise ValueError("Opening item remainder exceeds original amount.")
                    row["status"] = "open" if remaining else "settled"
                    if row["kind"] == "receivable" and not account.startswith("Assets:") or row["kind"] == "payable" and not account.startswith("Liabilities:"):
                        raise ValueError("Opening item kind conflicts with its control account.")
                    sign = 1 if account.startswith("Assets:") else -1
                    if row["kind"] == "credit":
                        if row.get("direction") not in {"debit", "credit"}:
                            raise ValueError("Opening credit support needs a debit/credit direction.")
                        sign = 1 if row["direction"] == "debit" else -1
                    balance = remaining * sign
                    mutation_table = "items"
                else:
                    if row.get("kind") not in {"prepaid", "deferred", "depreciation"}:
                        raise ValueError("Unsupported opening schedule kind.")
                    if row["kind"] != "deferred" and not account.startswith("Assets:") or row["kind"] == "deferred" and not account.startswith("Liabilities:"):
                        raise ValueError("Opening schedule kind conflicts with its control account.")
                    amount, recognized = _money(row.get("amount")), _money(row.get("recognized_amount"))
                    salvage = _money(row.get("salvage_value", "0.00")) if row["kind"] == "depreciation" else Decimal(0)
                    if row["kind"] != "depreciation" and ("salvage_value" in row or "contra_account" in row):
                        raise ValueError("salvage_value and contra_account apply only to opening schedules of kind depreciation.")
                    if amount < 0 or recognized < 0 or salvage < 0 or amount - salvage - recognized != remaining:
                        raise ValueError("Opening schedule original/recognized/remaining amounts do not reconcile.")
                    if "start_date" in row or "end_date" in row:
                        raise ValueError("Opening schedule start_date/end_date were replaced by inclusive service_start and service_end (last day of service).")
                    try:
                        start, end = _day(row.get("service_start")), _day(row.get("service_end"))
                    except (TypeError, ValueError):
                        raise ValueError(f"Opening schedule {identity} needs service_start and service_end (inclusive YYYY-MM-DD dates).") from None
                    if start > end or end < cutover or row.get("method") not in {"actual_day", "equal_month"}:
                        raise ValueError("Opening schedule needs a supported remaining service interval and method.")
                    recognition = mapping.get(row.get("recognition_account"), row.get("recognition_account"))
                    _validate_account_name(recognition)
                    if recognition not in mapping.values():
                        raise ValueError("Opening schedule recognition account is not in the mapped source chart.")
                    expected_root = "Income:" if row["kind"] == "deferred" else "Expenses:"
                    if not recognition.startswith(expected_root):
                        raise ValueError("Opening schedule recognition account conflicts with its kind.")
                    row["recognition_account"] = recognition
                    row["recognition_start"] = cutover.isoformat()
                    row["initial_remaining_amount"] = format(remaining, ".2f")
                    row["base_recognized_amount"] = format(recognized, ".2f")
                    row["status"] = "active"
                    balance = remaining * (1 if row["kind"] == "prepaid" else -1)
                    if row["kind"] == "depreciation":
                        contra = mapping.get(row.get("contra_account"), row.get("contra_account"))
                        _validate_account_name(contra)
                        if contra not in financial or not contra.startswith("Assets:") or contra == account:
                            raise ValueError("Opening depreciation schedule needs a separate Assets: contra_account with a carried balance.")
                        row["contra_account"] = contra
                        row["salvage_value"] = format(salvage, ".2f")
                        row["origin_reserved_amount"] = format(amount, ".2f")
                        balance = amount
                        sums[contra] = sums.get(contra, Decimal(0)) - recognized
                    mutation_table = "schedules"
            sums[account] = sums.get(account, Decimal(0)) + balance
            mutations.append({"table": mutation_table, "id": row["id"], "payload": row})
    differences = []
    for account, balance in sorted(financial.items()):
        if (balance and account.startswith(("Assets:", "Liabilities:"))
                and not account.startswith(("Assets:Bank:", "Assets:Cash:", "Liabilities:CreditCard:"))
                and account != "Liabilities:CreditCard"):
            delta = sums.get(account, Decimal(0)) - balance
            if delta:
                differences.append({"account": account, "ledger": format(balance, ".2f"),
                                    "support": format(sums.get(account, Decimal(0)), ".2f"),
                                    "difference": format(delta, ".2f"), "currency": "USD"})
    for account, total in sums.items():
        if total != financial[account] and not any(d["account"] == account for d in differences):
            differences.append({"account": account, "ledger": format(financial[account], ".2f"),
                                "support": format(total, ".2f"), "difference": format(total - financial[account], ".2f"), "currency": "USD"})
    for identity, record in sorted(evidence.items()):
        if not record["existing"]:
            mutations.append({"table": "evidence", "id": identity, "payload": record["payload"]})
    return mutations, differences, {identity: _hash(record["payload"]) for identity, record in evidence.items()}


def preview(entity, folder, cutover, *, fiscal_start=None, supporting=None, closed_position=False):
    """Capture source facts and target state without modifying canonical books.

    Returns a JSON-portable preview. All support mismatches stay visible and
    block application; report basis selection never sets company policy.
    """
    require_capability(entity)
    cutover = _day(cutover)
    if fiscal_start is None:
        raise ValueError("Supply the evidenced fiscal_start explicitly; fiscal policy cannot be inferred from report toggles.")
    fiscal_start = _day(fiscal_start)
    as_of = cutover - timedelta(days=1)
    if type(closed_position) is not bool or closed_position != (fiscal_start == cutover):
        raise ValueError("Fiscal-boundary cutover requires explicit closed_position and fiscal_start equal to cutover.")
    if fiscal_start > cutover:
        raise ValueError("Fiscal start must precede the cutover and identify the source YTD period.")
    folder, source_folder = _source_folder(entity, folder)
    paths = {}
    for path in qb.iter_export_files(folder):
        if path.is_symlink() or path.stat().st_nlink != 1 or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Opening source reports must be bounded regular company files without symlinks or hard links.")
        rows = qb._read_export_rows(path)
        kind = qb.detect_report_type(rows)
        if kind not in {"chart_of_accounts", "trial_balance", "balance_sheet", "profit_and_loss"}:
            continue
        if kind in paths:
            raise ValueError(f"Ambiguous opening source report: {kind}")
        if kind != "chart_of_accounts":
            if qb.detect_basis(rows) != "accrual":
                raise ValueError(f"Opening report basis must match accrual policy: {kind}")
            start, end = qb.parse_qb_date_range(qb.detect_date_range(rows))
            if (end != as_of or kind == "profit_and_loss" and
                    ((not closed_position and start != fiscal_start) or (closed_position and (start is None or start > as_of)))):
                raise ValueError(f"Opening report dates do not match exact cutover coverage: {kind}")
            _validate_source_rows(rows, kind)
        header = " ".join(" ".join(row) for row in rows[:8])
        if re.search(r"\b(?:EUR|GBP|CAD|AUD|JPY)\b|€|£", header):
            raise ValueError("Only USD opening source reports are supported.")
        paths[kind] = path
    required = {"chart_of_accounts", "trial_balance", "balance_sheet", "profit_and_loss"}
    if set(paths) != required:
        raise ValueError("Opening source requires chart of accounts, trial balance, balance sheet, and fiscal YTD P&L.")
    accounts = qb.parse_chart_of_accounts(paths["chart_of_accounts"])
    mapping = _mapping(entity, accounts)
    tb, bs, pnl = (qb.parse_trial_balance(paths["trial_balance"]), qb.parse_balance_sheet(paths["balance_sheet"]),
                   qb.parse_profit_and_loss(paths["profit_and_loss"]))
    companies = {qb._normalize_company_name(r.company) for r in (tb, bs, pnl)}
    if len(companies) != 1 or not next(iter(companies)):
        raise ValueError("Opening source reports must identify one consistent company.")
    financial = _tie_reports(tb, bs, pnl, mapping, closed_position=closed_position)
    source_id = f"quickbooks-opening-trial-balance-{cutover.isoformat()}"
    store = LedgerStore(default_store_path(entity.path))
    chart = _chart(accounts, mapping, store, as_of)
    hashes = {kind: hashlib.sha256(path.read_bytes()).hexdigest() for kind, path in sorted(paths.items())}
    return _build_plan(
        entity, store, operation="quickbooks-opening", import_source="quickbooks-opening",
        narration=f"Opening balances ({as_of.isoformat()})", financial=financial, mapping=mapping,
        chart=chart, supporting=supporting, cutover=cutover, fiscal_start=fiscal_start,
        closed_position=closed_position, source_id=source_id,
        coverage_extra={"source_hashes": hashes, "mapping_hash": _hash(mapping),
                        "source_paths": {kind: path.relative_to(entity.path.resolve()).as_posix() for kind, path in paths.items()}},
        request_extra={"source_hashes": hashes, "mapping_hash": _hash(mapping)},
        plan_extra={"folder": source_folder})


def _build_plan(entity, store, *, operation, import_source, narration, financial, mapping, chart, supporting,
                cutover, fiscal_start, closed_position, source_id, coverage_extra, request_extra, plan_extra,
                tracking=False):
    """Shared opening core: tie support to control balances and bind the accounting preview.

    QuickBooks reports and the source-neutral opening request both end here. ``tracking`` is the
    opening items of a company that turned accrual on later: the one entry is dated the tracking
    start date itself (the day before could be in a closed period), the coverage record is
    ``tracking`` and the company may already hold entries.
    """
    as_of = cutover if tracking else cutover - timedelta(days=1)
    coverage_id = TRACKING_COVERAGE_ID if tracking else "opening"
    mutations, differences, evidence_hashes = _support(supporting, financial, mapping, source_id, cutover, as_of, entity, store)
    coverage = {"id": coverage_id, "basis": "accrual", "currency": "USD", "cutover_date": cutover.isoformat(),
                "source_as_of": as_of.isoformat(), "fiscal_start": fiscal_start.isoformat(),
                "summary_from": None if closed_position else fiscal_start.isoformat(),
                "summary_to": None if closed_position else as_of.isoformat(), "closed_position": closed_position,
                "opening_source_id": source_id, **coverage_extra,
                "chart": chart,
                "verified": not differences, "support_status": "reconciled" if not differences else "incomplete",
                "support_evidence_hashes": evidence_hashes}
    facts = {"coverage": coverage, "balances": {a: format(n, ".2f") for a, n in sorted(financial.items())},
             "support": [row for row in mutations if row["table"] != "evidence"]}
    coverage["intent_hash"] = _hash(facts)
    with store.connection() as conn:
        if store.schema_version(conn) != ACCRUAL_SCHEMA_VERSION:
            raise ValueError("Opening accrual import requires an explicit schema upgrade.")
        existing = load_record(store, "coverage", coverage_id, conn)
        if existing and existing.get("intent_hash") != coverage["intent_hash"]:
            raise ValueError("Opening supporting document conflicts with the already applied cutover.")
        if not existing and not tracking and (conn.execute("SELECT 1 FROM entries UNION ALL SELECT 1 FROM balance_assertions UNION ALL SELECT 1 FROM source_transactions LIMIT 1").fetchone()
                             or any(list_records(store, table, conn) for table in TABLES if table not in {"evidence", "periods", "intents"})):
            raise ValueError("Opening cutover requires an empty target. A company that already has entries cannot take an opening; start a new accrual company at the cutover date.")
        digest = store.content_digest(conn)
    integrity = check_store_integrity(store)
    if integrity.status != "ok":
        raise ValueError("Opening target ledger integrity must be verified before migration.")
    plan = {"version": VERSION, "operation": operation, "status": "blocked" if differences else "ready",
            "replayed": bool(existing), "cutover": cutover.isoformat(),
            "fiscal_start": fiscal_start.isoformat(), "closed_position": closed_position, "supporting": deepcopy(supporting or {}),
            "coverage": coverage, "balances": facts["balances"], "support": mutations,
            "support_differences": differences, "expected_digest": digest,
            "config_hash": _hash(entity.entity_config), "preview_hash": _hash(facts),
            "import_source": import_source, "narration": narration, **({"tracking": True} if tracking else {}), **plan_extra}
    if not differences and not existing:
        from .journals import _preview_operation
        entries, opens, changes = _effects(plan)
        plan["accounting_plan"] = _preview_operation(entity, operation_id=source_id, kind="migration",
            request={"cutover": plan["cutover"], "fiscal_start": plan["fiscal_start"],
                     "chart_hash": _hash(chart), **request_extra,
                     "support_evidence_hashes": evidence_hashes, "facts_hash": plan["preview_hash"]},
            entries=entries, opens=opens, mutations=changes, evidence_ids=sorted(evidence_hashes),
            affected_dates=(as_of.isoformat(),))
    elif existing:
        operation_record = load_record(store, "intents", "operation:" + source_id)
        if not operation_record or not isinstance(operation_record.get("plan"), dict):
            raise ValueError("Opening cutover is missing its immutable original accounting preview.")
        plan["accounting_plan"] = operation_record["plan"]
    return plan


def _effects(plan):
    coverage = plan["coverage"]
    opened = _day(coverage["source_as_of"])
    postings = tuple(Posting(a, _money(n)) for a, n in sorted(plan["balances"].items()) if _money(n))
    tracking = coverage["id"] == TRACKING_COVERAGE_ID
    metadata = (("source-id", coverage["opening_source_id"]), ("import-source", plan.get("import_source", "quickbooks-opening")),
                        ("accrual-tracking-opening" if tracking else "accrual-opening", "true"), ("coverage-id", coverage["id"]),
                        ("cutover-date", coverage["cutover_date"]))
    if not coverage["closed_position"]:
        metadata += (("summary-from", coverage["summary_from"]), ("summary-to", coverage["summary_to"]))
    entry = Entry(date=opened, narration=plan.get("narration", "Reconciled accrual opening balances from QuickBooks"), meta=metadata, postings=postings)
    mutations = [SupportingMutation(row["table"], row["id"], row["payload"]) for row in plan["support"]]
    mutations.append(SupportingMutation("coverage", coverage["id"], coverage))
    opens = [Open(_day(account["open_date"]), account["account"], (account["currency"],))
             for account in coverage["chart"]]
    return [entry], opens, mutations


def apply(entity, plan):
    """Apply the exact preview atomically, or return its original receipt on replay."""
    if plan.get("version") != VERSION or plan.get("operation") not in {"quickbooks-opening", "accrual-opening"}:
        raise ValueError("Unsupported opening preview.")
    with _entity_write_lock(entity.path):
        current_entity = load_entity(entity.path)
        require_capability(current_entity)
        if plan["operation"] == "accrual-opening":
            from .opening import preview_opening
            fresh = preview_opening(current_entity, plan.get("request"))
        else:
            fresh = preview(current_entity, plan["folder"], plan["cutover"],
                            fiscal_start=plan["fiscal_start"], supporting=plan["supporting"],
                            closed_position=plan.get("closed_position", False))
        if fresh["preview_hash"] != plan.get("preview_hash") or fresh["config_hash"] != plan.get("config_hash"):
            raise ValueError("Stale opening preview: source supporting document or account mapping changed.")
        if fresh["status"] != "ready":
            raise ValueError("Opening supporting balances do not reconcile; inspect support_differences.")
        displayed = {"coverage": plan.get("coverage"), "balances": plan.get("balances"),
                     "support": [row for row in plan.get("support", []) if row.get("table") != "evidence"]}
        accounting = plan.get("accounting_plan", {})
        if (_hash(displayed) != plan.get("preview_hash") or
                canonical_hash({key: value for key, value in accounting.items() if key != "plan_hash"}) != accounting.get("plan_hash")):
            raise ValueError("Opening preview was changed; regenerate and review the exact preview.")
        coverage = fresh["coverage"]
        if fresh["replayed"]:
            from .journals import _replay_operation
            receipt = _replay_operation(current_entity, plan.get("accounting_plan", {}))
            if receipt is None:
                raise ValueError("Opening cutover is missing its canonical operation receipt.")
            return {"status": "already_applied", "replayed": True, "source_id": coverage["opening_source_id"],
                    "coverage": coverage, "entries_written": 0, "receipt": receipt}
        if fresh["expected_digest"] != plan.get("expected_digest"):
            raise ValueError("Stale opening preview: canonical ledger state changed.")
        if fresh["accounting_plan"]["plan_hash"] != plan.get("accounting_plan", {}).get("plan_hash"):
            raise ValueError("Stale opening preview: accounting state or authority changed.")
        from .journals import _apply_operation
        entries, opens, mutations = _effects(fresh)
        receipt = _apply_operation(current_entity, fresh["accounting_plan"], entries=entries, opens=opens, mutations=mutations)
        return {"status": "applied", "replayed": False, "source_id": coverage["opening_source_id"],
                "coverage": coverage, "entries_written": 1, "receipt": receipt}
