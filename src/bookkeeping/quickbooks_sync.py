"""Deterministic, read-only QuickBooks Online snapshot reference ingestion.

Provider downloads and OAuth belong to the caller. No API access or inferred
journal entries are performed here. All monetary values use Decimal.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import tempfile

from . import quickbooks as qb

_REPORT_TYPES = {
    "ProfitAndLoss": "profit_and_loss", "BalanceSheet": "balance_sheet",
    "OpeningBalanceSheet": "balance_sheet", "TrialBalance": "trial_balance",
    "OpeningTrialBalance": "trial_balance", "GeneralLedger": "general_ledger",
    "TransactionListByAccount": "transaction_detail",
}
_TYPE_ALIASES = {
    "Accounts Receivable": "Accounts receivable (A/R)",
    "Accounts Payable": "Accounts payable (A/P)",
    "Other Current Asset": "Other current assets", "Fixed Asset": "Fixed assets",
    "Other Asset": "Other assets", "Other Current Liability": "Other current liabilities",
    "Long Term Liability": "Long-term liabilities", "Expense": "Expenses",
}
_TRANSACTION_TYPES = frozenset(("Purchase", "Deposit", "Transfer", "Invoice", "Bill",
    "Payment", "BillPayment", "SalesReceipt", "CreditMemo", "RefundReceipt",
    "VendorCredit", "JournalEntry"))


def _money(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a monetary value")
    try:
        amount = Decimal(str(value or "0"))
        if not amount.is_finite():
            raise ValueError("Non-finite monetary value")
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("Invalid monetary value") from exc


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode()


def _load(path: Path) -> dict:
    def reject(value):
        raise ValueError("Non-finite JSON number")
    value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _accounts(raw: list[dict]) -> tuple[list[dict], list[str]]:
    accounts = []
    by_id = {}
    names = set()
    warnings = []
    qb._reset_collision_registry()
    if any(not isinstance(item, dict) for item in raw):
        raise ValueError("Account records must be objects")
    for item in sorted(raw, key=lambda a: str(a.get("Id", ""))):
        account_id = str(item.get("Id", ""))
        name = item.get("FullyQualifiedName") or item.get("Name")
        if not account_id or not isinstance(name, str) or not name.strip():
            raise ValueError("Every account requires Id and name")
        if account_id in by_id or name in names:
            raise ValueError("Duplicate QuickBooks account ID or full name")
        if not isinstance(item.get("AccountType"), str):
            raise ValueError("AccountType must be text")
        if not isinstance(item.get("ParentRef", {}), dict):
            raise ValueError("Malformed account parent reference")
        account_type = _TYPE_ALIASES.get(item.get("AccountType"), item.get("AccountType", ""))
        if account_type.lower() not in qb._QB_TYPE_ROOT:
            raise ValueError(f"Unsupported QuickBooks account type: {account_type!r}")
        mapped = qb.map_qb_account(name, account_type)
        qb._reset_collision_registry()
        # A fresh mapping detects normalized-name collisions independently of order.
        base = qb.map_qb_account(name, account_type)
        qb._reset_collision_registry()
        if any(a["mapped_account"] == base for a in accounts):
            raise ValueError("QuickBooks account names collide after normalization; explicit mapping required")
        record = {"id": account_id, "name": name, "account_type": account_type,
                  "detail_type": str(item.get("AccountSubType", "")),
                  "mapped_account": mapped, "active": item.get("Active", True),
                  "parent_id": str(item.get("ParentRef", {}).get("value", ""))}
        accounts.append(record)
        by_id[account_id] = record
        names.add(name)
    for account in accounts:
        seen = {account["id"]}
        parent_id = account["parent_id"]
        while parent_id:
            if parent_id in seen or parent_id not in by_id:
                raise ValueError("Broken or cyclic QuickBooks account hierarchy")
            seen.add(parent_id)
            parent_id = by_id[parent_id]["parent_id"]
        if account["parent_id"]:
            parent = by_id[account["parent_id"]]
            if not account["name"].startswith(parent["name"] + ":"):
                raise ValueError("QuickBooks full name does not match parent hierarchy")
    return accounts, warnings


def _walk(rows: dict):
    for row in rows.get("Row", []):
        if not isinstance(row, dict):
            raise ValueError("Malformed report row")
        yield row
        if row.get("Rows"):
            yield from _walk(row["Rows"])


def _metadata(report: dict) -> tuple[str, str, str]:
    header = report.get("Header", {})
    if not isinstance(header, dict):
        raise ValueError("Report Header must be an object")
    basis = str(header.get("ReportBasis", "")).lower()
    if basis not in ("cash", "accrual"):
        raise ValueError("Report basis missing or unsupported")
    start = date.fromisoformat(header["StartPeriod"])
    end = date.fromisoformat(header["EndPeriod"])
    if start > end:
        raise ValueError("Reversed report date range")
    return basis, start.isoformat(), end.isoformat()


def _cells(row: dict, key: str = "ColData") -> list[dict]:
    cells = row.get(key, [])
    if not isinstance(cells, list) or any(not isinstance(c, dict) for c in cells):
        raise ValueError("Malformed report cells")
    return cells


def _validate_report_structure(report: dict) -> None:
    if not isinstance(report.get("Header"), dict) or not isinstance(report.get("Columns"), dict):
        raise ValueError("Report requires Header and Columns objects")
    columns = report["Columns"].get("Column")
    if not isinstance(columns, list) or any(not isinstance(c, dict) for c in columns):
        raise ValueError("Malformed report columns")
    for column in columns:
        metadata = column.get("MetaData", [])
        if not isinstance(metadata, list) or any(not isinstance(item, dict) for item in metadata):
            raise ValueError("Malformed report column metadata")
        if not isinstance(column.get("ColType", ""), str) or not isinstance(column.get("ColTitle", ""), str):
            raise ValueError("Malformed report column type/title")
    options = report["Header"].get("Option", [])
    if not isinstance(options, list) or any(not isinstance(o, dict) for o in options):
        raise ValueError("Malformed report options")
    def visit(container):
        if not isinstance(container, dict) or not isinstance(container.get("Row", []), list):
            raise ValueError("Malformed report rows")
        for row in container.get("Row", []):
            if not isinstance(row, dict):
                raise ValueError("Malformed report row")
            _cells(row)
            for key in ("Header", "Summary"):
                if key in row:
                    if not isinstance(row[key], dict):
                        raise ValueError("Malformed report row header/summary")
                    _cells(row[key])
            if "Rows" in row:
                visit(row["Rows"])
    visit(report.get("Rows", {}))


def _detail_indices(report: dict) -> dict[str, int]:
    aliases = {"tx_date": "date", "tx_type": "type", "txn_type": "type", "doc_num": "num",
               "name": "name", "memo": "memo", "split_acc": "split",
               "amount": "amount", "subt_nat_amount": "amount",
               "running_bal": "balance", "rbal_nat_amount": "balance"}
    indices = {}
    for index, column in enumerate(report.get("Columns", {}).get("Column", [])):
        metadata = {m.get("Name"): m.get("Value") for m in column.get("MetaData", [])}
        key = str(metadata.get("ColKey") or column.get("ColType", "")).lower()
        canonical = aliases.get(key)
        if canonical:
            if canonical in indices:
                raise ValueError("Ambiguous duplicate transaction-detail column")
            indices[canonical] = index
    return indices


def _explicit_empty_report(report: dict) -> bool:
    """Intuit omits money columns when it explicitly reports no data."""
    flags = [option.get("Value") for option in report["Header"].get("Option", [])
             if option.get("Name") == "NoReportData"]
    columns = report["Columns"]["Column"]
    if flags != ["true"] or len(columns) != 1 or columns[0].get("ColType") != "Account":
        return False
    for row in _walk(report.get("Rows", {})):
        for cells in (_cells(row), _cells(row.get("Header", {})), _cells(row.get("Summary", {}))):
            if len(cells) > 1:
                raise ValueError("No-data report contains unexpected monetary cells")
    return True


def parse_reference(path: Path, expected: str):
    """Read typed API reference JSON directly into the existing report models."""
    payload = _load(path)
    matches = payload.get("report_type") == expected or (expected == "general_ledger" and payload.get("report_type") == "transaction_detail")
    if payload.get("format") != "slashbooks-qbo-reference-v1" or not matches:
        raise ValueError("Invalid QuickBooks API reference type")
    if expected == "chart_of_accounts":
        return [qb.QBAccount(a["name"], a["account_type"], a["detail_type"]) for a in payload["accounts"]]
    report = payload["report"]
    _validate_report_structure(report)
    basis, start, end = _metadata(report)
    company = payload["company"]
    names = payload.get("account_names", {})
    end_date = date.fromisoformat(end)
    as_of = "As of " + end_date.strftime("%B %d, %Y")
    period = f"{date.fromisoformat(start):%m/%d/%Y} - {end_date:%m/%d/%Y}"
    columns = report.get("Columns", {}).get("Column", [])
    if expected in ("trial_balance", "balance_sheet") and _explicit_empty_report(report):
        return (qb.TrialBalance(company, as_of, basis) if expected == "trial_balance"
                else qb.BalanceSheet(company, as_of, basis))
    if expected in ("profit_and_loss", "balance_sheet"):
        if len(columns) != 2 or columns[1].get("ColType") != "Money" or columns[1].get("ColTitle", "").lower() != "total":
            raise ValueError("A single Total monetary column is required")
        model = qb.ProfitAndLoss(company, period, basis) if expected == "profit_and_loss" else qb.BalanceSheet(company, as_of, basis)
        row_class = qb.PLRow if expected == "profit_and_loss" else qb.BSRow
        seen_accounts = set()
        for row in _walk(report.get("Rows", {})):
            header = _cells(row.get("Header", {}))
            if header and header[0].get("value"):
                model.rows.append(row_class(str(header[0]["value"]), Decimal("0"), "section"))
            cells = _cells(row)
            if cells:
                if len(cells) != 2:
                    raise ValueError("Report row does not match Total columns")
                account_id = str(cells[0].get("id", ""))
                if account_id:
                    if account_id in seen_accounts:
                        raise ValueError("Duplicate account in financial report")
                    seen_accounts.add(account_id)
                name = payload.get("account_names", {}).get(account_id, cells[0].get("value", ""))
                model.rows.append(row_class(name, _money(cells[1].get("value")), "leaf"))
            summary = _cells(row.get("Summary", {}))
            if summary:
                if len(summary) != 2:
                    raise ValueError("Summary does not match Total columns")
                model.rows.append(row_class(str(summary[0].get("value", "")), _money(summary[1].get("value")), "subtotal"))
        return model
    if expected == "trial_balance":
        if (len(columns) != 3 or any(c.get("ColType") != "Money" for c in columns[1:])
                or [str(c.get("ColTitle", "")).lower() for c in columns[1:]] != ["debit", "credit"]):
            raise ValueError("Trial Balance requires Debit and Credit money columns")
        model = qb.TrialBalance(company, as_of, basis)
        seen_accounts = set()
        for row in _walk(report.get("Rows", {})):
            cells = _cells(row)
            if not cells:
                continue
            if len(cells) != 3:
                raise ValueError("Trial Balance row requires three columns")
            account_id = str(cells[0].get("id", ""))
            if account_id not in names:
                raise ValueError("Trial Balance account ID is missing from Chart of Accounts")
            if account_id in seen_accounts:
                raise ValueError("Duplicate account in Trial Balance")
            seen_accounts.add(account_id)
            name = names[account_id]
            debit, credit = _money(cells[1].get("value")), _money(cells[2].get("value"))
            if debit < 0 or credit < 0 or (debit and credit):
                raise ValueError("Invalid Trial Balance debit/credit shape")
            model.entries.append(qb.TBEntry(name, debit, credit))
        if sum((e.debit - e.credit for e in model.entries), Decimal("0")) != 0:
            raise ValueError("Trial Balance does not balance")
        return model
    if expected in ("general_ledger", "transaction_detail"):
        indices = _detail_indices(report)
        if not {"date", "type", "amount"}.issubset(indices):
            raise ValueError("Unsupported General Ledger columns; transaction comparison unavailable")
        model = qb.GeneralLedger(company, period, basis)
        def visit(container, account=""):
            for row in container.get("Row", []):
                header = _cells(row.get("Header", {}))
                current = account
                if header:
                    account_id = str(header[0].get("id", ""))
                    name = header[0].get("value", "")
                    if account_id in names:
                        current = names[account_id]
                    elif name in names.values():
                        current = name
                    elif account_id:
                        raise ValueError("General Ledger account missing from Chart of Accounts")
                cells = _cells(row)
                if cells:
                    if len(cells) != len(columns):
                        raise ValueError("General Ledger row does not match columns")
                    values = {k: str(cells[i].get("value", "")) for k, i in indices.items()}
                    if values.get("date"):
                        txn_date = date.fromisoformat(values["date"])
                        if not (date.fromisoformat(start) <= txn_date <= end_date):
                            raise ValueError("General Ledger transaction outside report period")
                        if not current:
                            raise ValueError("General Ledger account missing")
                        model.transactions.append(qb.GLTransaction(current, txn_date,
                            values["type"], values.get("num", ""), values.get("name", ""),
                            values.get("memo", ""), values.get("split", ""),
                            _money(values["amount"]), _money(values.get("balance", "0"))))
                if row.get("Rows"):
                    visit(row["Rows"], current)
        visit(report.get("Rows", {}))
        return model
    raise ValueError("Unsupported reference type")


def fingerprint(path: Path):
    payload = _load(path)
    kind = payload.get("report_type")
    if payload.get("format") != "slashbooks-qbo-reference-v1":
        raise ValueError("Unrecognized API reference")
    error = None
    try:
        parsed = parse_reference(path, kind)
        count = len(parsed) if isinstance(parsed, list) else len(getattr(parsed, "rows", getattr(parsed, "entries", getattr(parsed, "transactions", []))))
        if not count and kind not in ("general_ledger", "transaction_detail"):
            error = "Reference report is empty"
    except (ValueError, KeyError, TypeError) as exc:
        count, error = 0, str(exc)
    basis = payload.get("basis")
    period = payload.get("period")
    return qb.FileFingerprint(str(path), kind, payload.get("company"), period, basis, count, parse_error=error)


def _opening_consistency(entity, reference: Path, cutover: date) -> str | None:
    """Compare exact validated opening lines when both source reports exist."""
    slots = {slot.report_key: slot for slot in qb.inventory(reference, cutover=cutover).slots}
    if not all(slots.get(key) and slots[key].status == "present"
               for key in ("trial_balance", "balance_sheet")):
        return None
    trial = qb.import_opening(entity, reference, cutover, source="trial-balance", _preview=True)
    balance = qb.import_opening(entity, reference, cutover, source="balance-sheet", _preview=True)
    if not trial.success or not balance.success:
        return "Opening reports cannot produce independently validated opening lines"
    def canonical(result):
        return sorted((p["account"], Decimal(p["amount"]), p["currency"])
                      for p in result.proposed_postings if Decimal(p["amount"]) != 0)
    if canonical(trial) != canonical(balance):
        return "Opening Trial Balance and Balance Sheet disagree on asset/liability balances; refresh the source snapshot before applying"
    return None


def sync_import(entity, input_path: Path) -> dict:
    snapshot = _load(input_path)
    if snapshot.get("schema_version") != 1 or snapshot.get("provider") != "quickbooks":
        raise ValueError("Unsupported QuickBooks snapshot schema")
    realm = snapshot.get("realm_id")
    if not isinstance(realm, str) or not realm.isdigit() or len(realm) > 32:
        raise ValueError("Invalid QuickBooks realm ID")
    fetched = datetime.fromisoformat(snapshot["fetched_at"].replace("Z", "+00:00"))
    if fetched.tzinfo is None:
        raise ValueError("fetched_at requires a timezone")
    start, end = date.fromisoformat(snapshot["from"]), date.fromisoformat(snapshot["to"])
    if start > end or snapshot.get("accounting_basis") not in ("Cash", "Accrual"):
        raise ValueError("Invalid snapshot dates or basis")
    for key, expected_type in (("company_info", dict), ("preferences", dict), ("accounts", list),
                               ("customers", list), ("vendors", list), ("entities", dict), ("reports", dict)):
        if not isinstance(snapshot.get(key), expected_type):
            raise ValueError(f"Snapshot {key} has an invalid type")
    if not isinstance(snapshot["company_info"].get("CompanyName"), str) or not snapshot["company_info"]["CompanyName"].strip():
        raise ValueError("CompanyInfo requires CompanyName")
    accounts, warnings = _accounts(snapshot["accounts"])
    for collection in ("customers", "vendors"):
        ids = set()
        for record in snapshot[collection]:
            if not isinstance(record, dict) or not record.get("Id") or str(record["Id"]) in ids:
                raise ValueError(f"Missing or duplicate {collection} ID")
            ids.add(str(record["Id"]))
    known_ids = {a["id"] for a in accounts}
    for kind, items in snapshot["entities"].items():
        if not isinstance(items, list):
            raise ValueError("Entity collections must be lists")
        ids = set()
        for item in items:
            if not isinstance(item, dict) or not item.get("Id") or str(item["Id"]) in ids:
                raise ValueError("Missing or duplicate transaction ID")
            ids.add(str(item["Id"]))
            if "Line" in item and (not isinstance(item["Line"], list) or any(not isinstance(line, dict) for line in item["Line"])):
                raise ValueError("Malformed transaction lines")
            if item.get("TxnDate") and not start <= date.fromisoformat(item["TxnDate"]) <= end:
                raise ValueError("Transaction outside snapshot date range")
            def check(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        # Purchase.Credit is the charge/refund flag, not a credit amount.
                        if kind == "Purchase" and value is item and key == "Credit":
                            if child is not None and not isinstance(child, bool):
                                raise ValueError("Purchase Credit must be a boolean")
                            continue
                        if key in ("Amount", "TotalAmt", "Balance", "UnitPrice", "Debit", "Credit"):
                            _money(child)
                        if (key.endswith("Ref") or key.endswith("LineDetail")) and not isinstance(child, dict):
                            raise ValueError("Malformed transaction reference")
                        if key == "AccountRef" and str(child.get("value", "")) not in known_ids:
                            warnings.append(f"{kind}: unresolved account reference; reference-only transaction")
                        check(child)
                elif isinstance(value, list):
                    for child in value:
                        check(child)
            check(item)
        if kind not in _TRANSACTION_TYPES:
            warnings.append(f"Unsupported transaction type {kind!r} retained as reference only")
    currency_prefs = snapshot["preferences"].get("CurrencyPrefs", {})
    company_currency = snapshot["company_info"].get("CurrencyRef", {})
    if not isinstance(currency_prefs, dict) or not isinstance(company_currency, dict):
        raise ValueError("Malformed currency preferences/reference")
    home_currency = currency_prefs.get("HomeCurrency", {})
    if not isinstance(home_currency, dict):
        raise ValueError("Malformed home currency reference")
    currency = home_currency.get("value") or company_currency.get("value")
    multi = currency_prefs.get("MultiCurrencyEnabled")
    if not isinstance(multi, bool):
        raise ValueError("CurrencyPrefs requires boolean MultiCurrencyEnabled")
    source_bytes = _json_bytes(snapshot)
    snapshot_id = hashlib.sha256(source_bytes).hexdigest()
    # One company, one currency. A company with no entries can take the QuickBooks home currency; this
    # only checks. The opening apply adopts it in the same ledger transaction as the opening entry.
    from . import currency as currency_mod
    company = book = currency_mod.company_currency(entity.entity_config)
    if isinstance(currency, str) and currency != book and not multi:
        try:
            book = currency_mod.resolve_import_currency(entity, {currency}, "QuickBooks company (home currency)",
                                                        adopt=False, exclude=(f"sources/quickbooks/{snapshot_id}",))
        except currency_mod.CurrencyError as exc:
            warnings.append(str(exc))
    if currency != book or multi:
        warnings.append(f"Multicurrency/non-{book} snapshots are evidence only; opening application blocked")
    if snapshot["accounting_basis"] != "Cash":
        warnings.append("Accrual source evidence retained; current opening and comparison operations support cash basis only")
    root = entity.path / "sources" / "quickbooks" / snapshot_id
    counts = {"accounts": len(accounts), "customers": len(snapshot["customers"]),
              "vendors": len(snapshot["vendors"]),
              "transactions": {k: len(v) for k, v in snapshot["entities"].items()}}
    coverage = {}
    if not root.parent.resolve().is_relative_to(entity.path.resolve()):
        raise ValueError("QuickBooks evidence directory escapes entity")
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".qbo-", dir=root.parent) as tmp:
        stage = Path(tmp) / "snapshot"
        reference = stage / "reference"
        reference.mkdir(parents=True)
        (stage / "snapshot.json").write_bytes(source_bytes)
        company = str(snapshot["company_info"].get("CompanyName", ""))
        base = {"format": "slashbooks-qbo-reference-v1", "company": company,
                "account_names": {a["id"]: a["name"] for a in accounts}}
        (reference / "accounts.qbo.json").write_bytes(_json_bytes(dict(base, report_type="chart_of_accounts", accounts=accounts)))
        for key, report in snapshot["reports"].items():
            if key not in _REPORT_TYPES:
                warnings.append(f"Unsupported report {key!r} retained as evidence only")
                continue
            kind = _REPORT_TYPES[key]
            try:
                if not isinstance(report, dict):
                    raise ValueError("Malformed report object")
                _validate_report_structure(report)
                report_name = report["Header"].get("ReportName")
                if report["Header"].get("CompanyName", company) != company:
                    raise ValueError("Report company differs from snapshot company")
                expected_name = key.removeprefix("Opening")
                if report_name != expected_name:
                    raise ValueError("Report identity differs from requested report")
                report_currency = report.get("Header", {}).get("Currency", currency)
                if report_currency != currency:
                    raise ValueError("Report currency differs from company currency")
                if any(str(option.get("Name", "")).lower() in ("truncated", "istruncated")
                       and str(option.get("Value", "")).lower() == "true"
                       for option in report.get("Header", {}).get("Option", [])):
                    raise ValueError("Report is truncated")
                basis, report_start, report_end = _metadata(report)
                expected_end = (start - timedelta(days=1)).isoformat() if key.startswith("Opening") else end.isoformat()
                if basis != snapshot["accounting_basis"].lower() or report_end != expected_end:
                    raise ValueError("Report basis/date differs from requested snapshot coverage")
                if kind in ("profit_and_loss", "general_ledger", "transaction_detail") and report_start != start.isoformat():
                    raise ValueError("Report starts outside requested period")
                as_of = "As of " + date.fromisoformat(report_end).strftime("%B %d, %Y")
                period = as_of if kind in ("balance_sheet", "trial_balance") else f"{date.fromisoformat(report_start):%m/%d/%Y} - {date.fromisoformat(report_end):%m/%d/%Y}"
                payload = dict(base, report_type=kind, report=report, basis=basis, period=period)
                target = reference / f"{key}.qbo.json"
                target.write_bytes(_json_bytes(payload))
                parse_reference(target, kind)
                coverage[key] = {"status": "ready", "from": report_start, "to": report_end, "basis": basis}
                if key == "GeneralLedger":
                    required_detail = {"date", "type", "num", "name", "memo", "split", "amount", "balance"}
                    if required_detail.issubset(_detail_indices(report)):
                        # A lossless view for the legacy detail readiness slot;
                        # this is the same source, never an independent report.
                        detail = dict(payload, report_type="transaction_detail", derived_from="GeneralLedger")
                        detail_path = reference / "TransactionDetail.qbo.json"
                        detail_path.write_bytes(_json_bytes(detail))
                        if parse_reference(detail_path, "transaction_detail") != parse_reference(target, "general_ledger"):
                            raise ValueError("Transaction-detail view differs from General Ledger")
                        coverage["TransactionDetail"] = dict(coverage[key], derived_from="GeneralLedger")
                    else:
                        coverage["TransactionDetail"] = {"status": "blocked", "reason": "General Ledger lacks the complete canonical transaction-detail fields"}
                        warnings.append("General Ledger lacks complete detail fields; transaction-detail comparison blocked")
            except (ValueError, KeyError, TypeError) as exc:
                coverage[key] = {"status": "blocked", "reason": str(exc)}
                warnings.append(f"{key}: {exc}")
                target = reference / f"{key}.qbo.json"
                if target.exists():
                    target.unlink()
        for required in ("ProfitAndLoss", "BalanceSheet", "TrialBalance", "GeneralLedger",
                         "OpeningTrialBalance", "OpeningBalanceSheet"):
            coverage.setdefault(required, {"status": "missing", "reason": "Report was not included in snapshot"})
        relative_reference = str(Path("sources") / "quickbooks" / snapshot_id / "reference")
        opening_supported = snapshot["accounting_basis"] == "Cash" and currency == book and not multi
        from .currency import report_currency
        with report_currency(book):
            consistency_error = _opening_consistency(entity, reference, start) if opening_supported else None
        if consistency_error:
            warnings.append(consistency_error)
        opening_ready = opening_supported and not consistency_error and any(
            coverage[key]["status"] == "ready" for key in ("OpeningTrialBalance", "OpeningBalanceSheet"))
        result = {"success": True, "snapshot_id": snapshot_id, "reference_path": relative_reference,
                  "reference_complete": all(coverage[k]["status"] == "ready" for k in (
                      "ProfitAndLoss", "BalanceSheet", "TrialBalance", "GeneralLedger",
                      "OpeningTrialBalance", "OpeningBalanceSheet")),
                  "comparison_ready": _inventory_in(book, reference, from_date=start, to_date=end).is_ready()
                                      and currency == book and not multi,
                  "comparison_supported": snapshot["accounting_basis"] == "Cash" and currency == book and not multi,
                  "counts": counts, "coverage": coverage, "warnings": sorted(set(warnings)),
                  "opening_supported": opening_supported, "opening_ready": opening_ready,
                  "accounts": accounts, "rule_suggestions": suggest_rules(snapshot, accounts)}
        if book != company:
            result["adopts_currency"] = book  # Applying the opening sets this company currency.
        (stage / "manifest.json").write_bytes(_json_bytes(result))
        if root.exists():
            expected_files = {p.relative_to(stage) for p in stage.rglob("*") if p.is_file()}
            actual_files = {p.relative_to(root) for p in root.rglob("*") if p.is_file()}
            if (root.is_symlink() or expected_files != actual_files or any(
                    (root / rel).is_symlink() or (root / rel).read_bytes() != (stage / rel).read_bytes()
                    for rel in expected_files)):
                raise ValueError("Snapshot evidence digest collision or corruption")
        else:
            try:
                stage.rename(root)
            except FileExistsError:
                if (root / "snapshot.json").read_bytes() != source_bytes:
                    raise ValueError("Concurrent snapshot evidence mismatch")
    return result


def _inventory_in(code, reference, **kwargs):
    from .currency import report_currency
    with report_currency(code):
        return qb.inventory(reference, **kwargs)


def sync_opening(entity, input_path: Path, cutover: date, *, apply: bool = False) -> dict:
    result = sync_import(entity, input_path)
    if not result["opening_supported"]:
        from .currency import company_currency
        raise ValueError(f"Only cash-basis single-currency {company_currency(entity.entity_config)} opening balances are supported")
    if not result["opening_ready"]:
        raise ValueError("Opening source reports are missing, invalid, or inconsistent; refresh and review source warnings")
    snapshot = _load(input_path)
    if date.fromisoformat(snapshot["from"]) != cutover:
        raise ValueError("Opening cutover must match snapshot start date")
    reference = entity.path / result["reference_path"]
    readiness = _inventory_in(result.get("adopts_currency") or entity.currency, reference, cutover=cutover)
    selected = {s.report_key: s for s in readiness.slots}
    source = "trial-balance"
    slot = selected.get("trial_balance")
    if not slot or slot.status != "present":
        source, slot = "balance-sheet", selected.get("balance_sheet")
    coa = selected.get("chart_of_accounts")
    if not slot or slot.status != "present" or not coa or coa.status != "present":
        raise ValueError("Opening requires cash-basis accounts and a valid prior-day Trial Balance or Balance Sheet")
    # Reuse the exact existing opening constructor and validator in preview mode.
    target = result.get("adopts_currency")
    opening = qb.import_opening(entity, reference, cutover, source=source, _preview=True, currency=target)
    if not opening.success:
        return dict(result, applied=False, opening=asdict(opening))
    from .ledger.store import LedgerStore, default_store_path
    store = LedgerStore(default_store_path(entity.path))
    entries = store.load_entries() if store.path.exists() else []
    originals = {entry.source_id: entry for entry in entries if entry.source_id}
    reversed_ids = set()
    for entry in entries:
        original_id = dict(entry.meta).get("reverses")
        original = originals.get(original_id)
        if original is not None and original.date == entry.date and sorted(
                (p.account, -p.amount, p.currency) for p in original.postings) == sorted(
                (p.account, p.amount, p.currency) for p in entry.postings):
            reversed_ids.add(original_id)
    active = [entry for entry in entries if dict(entry.meta).get("import-source") == "quickbooks-opening"
              and entry.source_id not in reversed_ids]
    if active:
        expected = sorted((p["account"], Decimal(p["amount"]), p["currency"])
                          for p in opening.proposed_postings)
        actual = sorted((p.account, p.amount, p.currency) for entry in active for p in entry.postings)
        if actual != expected or any(entry.date != cutover for entry in active):
            opening.success = False
            opening.errors.append("Existing QuickBooks opening differs from this snapshot; use the explicit correction workflow before applying new opening balances")
            return dict(result, applied=False, opening=asdict(opening))
        return dict(result, applied=apply, already_applied=True, opening=asdict(opening))
    if any(entry.date <= cutover and entry.source_id not in reversed_ids for entry in entries):
        opening.success = False
        opening.errors.append("Existing accounting entries on or before cutover would overlap opening balances; use the explicit correction/adoption workflow")
        return dict(result, applied=False, opening=asdict(opening))
    if apply:
        from .ledger.importer import _entity_write_lock
        with _entity_write_lock(entity.path):
            if target:
                from . import currency as currency_mod
                # Check again under the lock; the opening transaction then adopts the currency.
                currency_mod.resolve_import_currency(entity, {target}, "QuickBooks company (home currency)", adopt=False,
                                                     exclude=(result["reference_path"].rsplit("/", 1)[0],))
            opening = qb.import_opening(entity, reference, cutover, source=source, currency=target,
                                        adopt_currency=bool(target))
    return dict(result, applied=apply and opening.success, opening=asdict(opening))


def suggest_rules(snapshot: dict, accounts: list[dict]) -> dict:
    """Suggest review-only exact counterparty rules from unambiguous purchases.

    At least three distinct historical transactions must agree on one account.
    Splits, item-based expenses and names with conflicting classifications are
    excluded. These examples are reference labels, never held-out backtest data.
    """
    from .ledger.normalize import normalize_description
    mapped = {a["id"]: a["mapped_account"] for a in accounts}
    groups: dict[str, dict[str, set]] = {}
    skipped = 0
    for item in snapshot["entities"].get("Purchase", []):
        counterparty = item.get("EntityRef", {}).get("name", "")
        lines = item.get("Line", [])
        refs = {str(line.get("AccountBasedExpenseLineDetail", {}).get("AccountRef", {}).get("value", ""))
                for line in lines if line.get("DetailType") == "AccountBasedExpenseLineDetail"}
        if not isinstance(counterparty, str) or not counterparty or len(refs) != 1 or not lines or len(lines) != sum(
                line.get("DetailType") == "AccountBasedExpenseLineDetail" for line in lines):
            skipped += 1
            continue
        account_id = next(iter(refs))
        if account_id not in mapped or not mapped[account_id].startswith("Expenses:"):
            skipped += 1
            continue
        key = normalize_description(counterparty)
        groups.setdefault(key, {}).setdefault(mapped[account_id], set()).add(str(item["Id"]))
    proposed, conflicts = [], []
    for key, labels in sorted(groups.items()):
        if len(labels) != 1:
            conflicts.append({"counterparty": key, "accounts": sorted(labels)})
            continue
        account, source_ids = next(iter(labels.items()))
        if len(source_ids) >= 3:
            proposed.append({"field": "counterparty", "operator": "equals", "value": key,
                             "account": account, "evidence_count": len(source_ids),
                             "source_ids": sorted(source_ids), "status": "proposed", "auto_post": False})
    return {"proposed": proposed, "conflicts": conflicts, "skipped": skipped}


def sync_rules(entity, input_path: Path, *, apply: bool = False) -> dict:
    """Translate the documented conservative rule interchange to review proposals.

    Native QBO export layouts vary; unsupported columns and semantics block
    that row instead of guessing. Applying stores proposals only and never
    changes the owner-confirmation trust ramp or current classifications.
    """
    from .ledger.model import _validate_account_name
    rows = qb._read_export_rows(input_path)
    if not rows:
        raise ValueError("Empty rules export")
    expected = ["name", "field", "operator", "value", "account", "priority", "money direction"]
    header = [c.strip().lower() for c in rows[0]]
    if sorted(header) != sorted(expected) or len(set(header)) != len(header):
        return {"success": False, "applied": False, "proposed": [],
                "unsupported": [{"row": 1, "reason": "Unsupported QuickBooks rules export layout; use documented rule interchange headers"}]}
    proposed, unsupported = [], []
    for number, row in enumerate(rows[1:], 2):
        if not any(row):
            continue
        try:
            if len(row) != len(header):
                raise ValueError("Rule row width differs from header")
            record = dict(zip(header, (c.strip() for c in row)))
            field, operator = record["field"].lower(), record["operator"].lower()
            if (field, operator) not in {("counterparty", "equals"), ("description", "contains")}:
                raise ValueError("Only counterparty equals and description contains are supported")
            if record["money direction"].lower() not in ("in", "out"):
                raise ValueError("Money direction must be in or out")
            if not record["value"] or not record["name"]:
                raise ValueError("Rule name and match value are required")
            _validate_account_name(record["account"])
            priority = int(record["priority"])
            if priority < 0:
                raise ValueError("Priority cannot be negative")
            proposed.append({"name": record["name"], "field": field, "operator": operator,
                             "value": record["value"], "account": record["account"],
                             "priority": priority, "direction": record["money direction"].lower(),
                             "status": "proposed", "auto_post": False})
        except ValueError as exc:
            unsupported.append({"row": number, "reason": str(exc)})
    proposed.sort(key=lambda item: (item["priority"], item["name"]))
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    result = {"success": not unsupported, "applied": False, "proposed": proposed,
              "unsupported": unsupported, "source_id": digest,
              "warnings": ["Rules are proposals for owner review; no transaction classifications or automatic posting are changed."]}
    if apply and not unsupported:
        target = entity.path / "learned-context" / "quickbooks-rule-proposals" / (digest + ".json")
        if not target.parent.resolve().is_relative_to(entity.path.resolve()):
            raise ValueError("QuickBooks rule proposal directory escapes entity")
        target.parent.mkdir(parents=True, exist_ok=True)
        expected_bytes = _json_bytes(result)
        if target.is_symlink() or (target.exists() and (
                not target.is_file() or target.read_bytes() != expected_bytes)):
            raise ValueError("QuickBooks rule proposal evidence is corrupted")
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(expected_bytes)
        result.update(applied=True, proposals_path=str(target.relative_to(entity.path)))
    return result


def add_parser(subparsers) -> None:
    for command in ("sync-import", "sync-opening", "sync-rules"):
        parser = subparsers.add_parser(command, help="Import a read-only QuickBooks API snapshot reference")
        parser.add_argument("input", type=Path)
        parser.add_argument("--entity", type=Path, required=True, dest="entity_path")
        if command == "sync-rules":
            parser.add_argument("--apply", action="store_true", help="Save supported proposals for review; never enables automatic posting")
        if command == "sync-opening":
            parser.add_argument("--cutover", type=date.fromisoformat, required=True)
            parser.add_argument("--apply", action="store_true", help="Explicitly post verified opening balances")


def run(args) -> int:
    from .entity import load_entity
    try:
        entity = load_entity(args.entity_path)
        if args.qb_command == "sync-opening":
            result = sync_opening(entity, args.input, args.cutover, apply=args.apply)
        elif args.qb_command == "sync-rules":
            result = sync_rules(entity, args.input, apply=args.apply)
        else:
            result = sync_import(entity, args.input)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("success") and result.get("opening", {}).get("success", True) else 1
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1
