"""Deterministic, read-only review readiness for one calendar period.

The engine decides whether the accounting evidence is complete.  An embedding
host supplies authenticated dependency versions and must compare them with live
state again before a human publication or close.  A readiness package is never
authority to publish, close, reopen, or change policy.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .coverage import check_coverage, readiness_items as coverage_readiness_items
from .entity import load_entity
from .ledger.projections import render_store_ledger
from .ledger.store import LedgerStore, default_store_path
from .ledger.validator import validate
from .reports.statements import balance_sheet, profit_and_loss, trial_balance
from .queue import ACTIONABLE_QUEUE_STATUSES


_PERIOD = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\Z")


def _canonical(value: Any) -> bytes:
    def scalar(item: Any) -> str:
        if isinstance(item, (date, Decimal)):
            return str(item)
        raise TypeError(f"Unsupported readiness value: {type(item).__name__}")

    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      default=scalar).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True)
class DependencyVersions:
    """Trusted versions captured by the host for one engine assessment."""

    books_revision: int
    source_revision: int
    reply_revision: int
    policy_version: int

    def __post_init__(self) -> None:
        for name, value in self.to_document().items():
            if type(value) is not int or not 0 <= value <= 2**53 - 1:
                raise ValueError(f"{name} must be a nonnegative integer.")

    def to_document(self) -> dict[str, int]:
        return {
            "books_revision": self.books_revision,
            "source_revision": self.source_revision,
            "reply_revision": self.reply_revision,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class ReadinessAssessment:
    document: Mapping[str, Any]

    @property
    def state(self) -> str:
        return str(self.document["state"])

    @property
    def package_id(self) -> str:
        return str(self.document["package_id"])

    def to_document(self) -> dict[str, Any]:
        # Canonical JSON also provides a defensive deep copy of nested values.
        return json.loads(_canonical(self.document))


def _period_dates(period: str) -> tuple[date, date]:
    if not isinstance(period, str) or not _PERIOD.fullmatch(period):
        raise ValueError("Period must use YYYY-MM format.")
    year, month = map(int, period.split("-"))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _verified_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _finite_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


def _read_json(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, None
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        return None, type(exc).__name__


def _account_from_mapping(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return str(value.get("account") or value.get("ledger_account") or "")
    return ""


def _source_rows(config: Mapping[str, Any], start: date, end: date,
                 required_accounts: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Normalize affirmative expected-source coverage without inferring activity."""
    coverage = config.get("source_coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    candidates: list[dict[str, Any]] = []
    for item in config.get("declared_sources") or []:
        if isinstance(item, str):
            candidate = {"source": item, **(coverage.get(item) if isinstance(coverage.get(item), Mapping) else {})}
        elif isinstance(item, Mapping):
            name = str(item.get("name") or item.get("source") or "")
            candidate = {"source": name, **item}
            if isinstance(coverage.get(name), Mapping):
                candidate.update(coverage[name])
        else:
            candidate = {"source": "", "invalid": True}
        candidates.append(candidate)

    mapped: list[tuple[str, str]] = []
    for field in ("bank_account_mappings", "csv_account_mappings"):
        values = config.get(field)
        if not isinstance(values, Mapping):
            continue
        for source, value in values.items():
            account = _account_from_mapping(value)
            if account:
                mapped.append((str(source), account))
    represented = {str(item.get("account") or "") for item in candidates}
    for source, account in mapped:
        if account not in represented:
            bounds = coverage.get(source) if isinstance(coverage.get(source), Mapping) else {}
            candidates.append({**bounds, "source": source, "account": account})
    represented = {str(item.get("account") or "") for item in candidates}
    for account in required_accounts:
        if account not in represented:
            bounds = coverage.get(account) if isinstance(coverage.get(account), Mapping) else {}
            candidates.append({**bounds, "source": account, "account": account})

    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        source = str(candidate.get("source") or "")
        account = str(candidate.get("account") or "")
        raw_from = str(candidate.get("coverage_from") or "")
        raw_to = str(candidate.get("coverage_to") or "")
        status = "complete"
        try:
            available_from = date.fromisoformat(raw_from)
            available_to = date.fromisoformat(raw_to)
            from .entity import coverage_spans
            if not coverage_spans(candidate, start, end) and (available_from > start or available_to < end):
                status = "partial"
        except ValueError:
            status = "missing" if not raw_from and not raw_to else "invalid"
        rows.append({
            "source": source,
            "account": account or None,
            "requested_from": start.isoformat(),
            "requested_to": end.isoformat(),
            "coverage_from": raw_from or None,
            "coverage_to": raw_to or None,
            "coverage_status": status,
        })
    return sorted(rows, key=lambda row: (str(row["source"]), str(row["account"])))


def _period_item(item: Mapping[str, Any], start: date, end: date) -> bool | None:
    raw = str(item.get("date") or "")[:10]
    try:
        return start <= date.fromisoformat(raw) <= end
    except ValueError:
        return None


def _open_work(entity_path: Path, start: date, end: date) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    blockers: list[dict[str, str]] = []
    snapshot: list[dict[str, Any]] = []
    pending, error = _read_json(entity_path / "staging" / "pending-categorization.json")
    if error or (pending is not None and not isinstance(pending, list)):
        blockers.append({"code": "staged_transactions_unreadable", "message": "Staged transactions could not be verified.", "refs": []})
    elif isinstance(pending, list):
        for item in pending:
            if not isinstance(item, Mapping) or _period_item(item, start, end) is None:
                blockers.append({"code": "staged_transaction_invalid", "message": "A staged transaction has no valid period.", "refs": []})
            elif _period_item(item, start, end):
                ref = str(item.get("id") or "unknown")
                snapshot.append({"kind": "staged", "ref": ref, "date": str(item.get("date"))[:10]})
    for path in sorted((entity_path / "review-queue").glob("*.json")):
        item, error = _read_json(path)
        if error or not isinstance(item, Mapping):
            blockers.append({"code": "proposal_unreadable", "message": "A review proposal could not be verified.", "refs": [path.name]})
            continue
        if item.get("status") not in ACTIONABLE_QUEUE_STATUSES:
            continue
        in_period = _period_item(item, start, end)
        if in_period is None:
            blockers.append({"code": "proposal_period_invalid", "message": "An open proposal has no valid period.", "refs": [path.name]})
        elif in_period:
            snapshot.append({"kind": "proposal", "ref": str(item.get("source_id") or path.stem), "date": str(item.get("date"))[:10]})
    duplicates, error = _read_json(entity_path / "staging" / "duplicate-candidates.json")
    if error or (duplicates is not None and not isinstance(duplicates, list)):
        blockers.append({"code": "duplicate_candidates_unreadable", "message": "Duplicate candidates could not be verified.", "refs": []})
    elif isinstance(duplicates, list):
        for item in duplicates:
            if not isinstance(item, Mapping) or item.get("status") != "duplicate-candidate":
                continue
            transaction = item.get("transaction") if isinstance(item.get("transaction"), Mapping) else item
            in_period = _period_item(transaction, start, end)
            if in_period is None:
                blockers.append({"code": "duplicate_period_invalid", "message": "An unresolved duplicate has no valid period.", "refs": [str(item.get("source_id") or "unknown")]})
            elif in_period:
                snapshot.append({"kind": "duplicate", "ref": str(item.get("source_id") or "unknown"), "date": str(transaction.get("date"))[:10]})
    if snapshot:
        refs = sorted(item["ref"] for item in snapshot)
        blockers.append({"code": "unresolved_period_work", "message": "The period still has staged transactions, proposals, or duplicate decisions.", "refs": refs})
    return blockers, sorted(snapshot, key=lambda item: (item["kind"], item["date"], item["ref"]))


def _question_blockers(entity_path: Path, start: date, end: date) -> tuple[list[dict[str, Any]], list[Any]]:
    data, error = _read_json(entity_path / "reports" / "open-questions.json")
    if error or (data is not None and not isinstance(data, list)):
        return ([{"code": "questions_unreadable", "message": "Open questions could not be verified.", "refs": []}], [])
    open_items: list[Any] = []
    for index, item in enumerate(data or []):
        if not isinstance(item, Mapping):
            open_items.append({"index": index, "status": "open"})
            continue
        status = str(item.get("status") or "open").lower()
        if status in {"closed", "resolved", "answered"}:
            continue
        raw_period = str(item.get("period") or "")
        raw_date = str(item.get("date") or "")[:10]
        applies = raw_period == start.isoformat()[:7]
        if raw_date:
            try:
                applies = start <= date.fromisoformat(raw_date) <= end
            except ValueError:
                applies = True
        elif not raw_period:
            applies = True
        if applies:
            open_items.append({"index": index, "source_id": item.get("source_id"), "status": status})
    if not open_items:
        return [], []
    refs = [str(item.get("source_id") or f"question:{item['index']}") for item in open_items]
    return ([{"code": "unresolved_questions", "message": "The period has unanswered accounting questions.", "refs": sorted(refs)}], open_items)


def _report_refs(entity_path: Path, start: date, end: date, *, provisional: bool) -> list[dict[str, Any]]:
    reports = [
        profit_and_loss(entity_path, start, end),
        balance_sheet(entity_path, end),
        trial_balance(entity_path, end),
    ]
    result = []
    for report in reports:
        payload = json.loads(report.to_json())
        result.append({
            "kind": report.kind,
            "period_from": start.isoformat(),
            "period_to": end.isoformat(),
            "totals": payload["totals"],
            "sha256": _hash(payload),
            "provisional": provisional,
        })
    return result


def _annotate_blockers(blockers: list[dict[str, Any]], path: Path, period: str, start: date, end: date,
                       sources: list[dict[str, Any]]) -> None:
    """Give each blocker the account/period it concerns and the exact next command."""
    by_source = {str(row["source"]): row for row in sources}
    for item in blockers:
        if item.get("action"):
            continue
        refs = item.get("refs") or []
        ref = str(refs[0]) if refs else ""
        code = item["code"]
        if code == "source_coverage_incomplete":
            row = by_source.get(ref, {})
            have = f"{row.get('coverage_from') or 'no date'} through {row.get('coverage_to') or 'no date'}"
            item["message"] = (f"Source {ref} (account {row.get('account') or 'unbound'}) has recorded coverage {have}, "
                               f"but period {period} needs {start} through {end}.")
            item["action"] = (f'After you confirm the source covers it, run: books entity source-coverage {path} --source "{ref}" '
                              f"--from {start} --to {end}. Coverage is additive; earlier ranges stay.")
        elif code == "source_account_missing":
            item["message"] = f"Source {ref} is not bound to a ledger account for period {period}."
            item["action"] = (f'Run: books entity source-coverage {path} --source "{ref}" --from {start} --to {end} '
                              "--account <ledger account such as Assets:Bank:Checking>")
        elif code in {"reconciliation_missing", "reconciliation_not_clean", "opening_evidence_missing",
                      "source_statement_incomplete", "source_evidence_provenance_missing", "cutover_opening_mismatch"}:
            item["message"] = f"{item['message']} Account {ref}, period {period}."
            item["action"] = (f"Run: books reconcile --entity {path} --account {ref} --source-balance <statement ending balance> --as-of {end} "
                              f"--source-opening-balance <statement opening balance> --source-transaction-total <signed activity {start} to {end}> "
                              f"--source-snapshot-at <ISO time you read the statement> --source-through {end} --source-evidence-ref <statement id>")
        elif code in {"period_current", "period_future"}:
            item["action"] = f"Wait until {period} has ended, then run: books readiness --entity {path} --period {period} --as-of {end + timedelta(days=1)}"
        elif code == "unresolved_period_work":
            item["message"] = f"Period {period} still has staged transactions, proposals, or duplicate decisions: {', '.join(map(str, refs[:5]))}."
            item["action"] = f"Run: books queue list --entity {path} and confirm, correct, or withdraw each item."
        elif code == "unresolved_questions":
            item["message"] = f"Period {period} has unanswered accounting questions: {', '.join(map(str, refs[:5]))}."
            item["action"] = f"Run: books queue list --entity {path}; answer each question, then rerun books readiness --entity {path} --period {period} --as-of {end + timedelta(days=1)}."
        elif code == "source_scope_missing":
            item["action"] = (f'Run: books entity source-coverage {path} --source "<source name>" --from {start} --to {end} '
                              "--account <ledger account>")


def assess_review_readiness(entity_path: Path | str, period: str,
                            dependencies: DependencyVersions, *, as_of: date) -> ReadinessAssessment:
    """Assess one immutable period snapshot without writing company state."""
    if not isinstance(dependencies, DependencyVersions):
        raise TypeError("Trusted DependencyVersions are required.")
    if not isinstance(as_of, date):
        raise TypeError("A deterministic as_of date is required.")
    start, end = _period_dates(period)
    entity = load_entity(entity_path)
    if entity.basis == "accrual":
        from .accrual.reports import is_credit_card_account
    store_path = default_store_path(entity.path)
    partial_opening = None
    if entity.basis == "accrual" and store_path.is_file():
        from .accrual.reports import opening_coverage
        with LedgerStore(store_path).connection() as conn:
            opening = opening_coverage(conn)
        if opening and opening.get("verified") is True and opening.get("support_status") == "reconciled":
            cutover = date.fromisoformat(opening["cutover_date"])
            if start < cutover <= end and opening.get("source_as_of") == (cutover - timedelta(days=1)).isoformat():
                start, partial_opening = cutover, opening
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    accrual_snapshot = None
    period_status = "completed" if end < as_of else ("current" if start <= as_of <= end else "future")
    if period_status != "completed":
        blockers.append({"code": f"period_{period_status}", "message": "Final review readiness requires a completed calendar period.", "refs": [period]})

    work_blockers, open_work = _open_work(entity.path, start, end)
    blockers.extend(work_blockers)
    question_blockers, open_questions = _question_blockers(entity.path, start, end)
    blockers.extend(question_blockers)

    ledger_balances: dict[str, Decimal] = {}
    opening_balances: dict[str, Decimal] = {}
    card_activity: set[str] = set()
    account_catalog: set[str] = set()
    if store_path.is_file():
        store = LedgerStore(store_path)
        account_catalog = store.load_account_names()
        for entry in store.load_entries(to_date=end):
            for posting in entry.postings:
                ledger_balances[posting.account] = ledger_balances.get(posting.account, Decimal("0")) + posting.amount
                if entity.basis == "accrual" and entry.date >= start:
                    if is_credit_card_account(posting.account):
                        card_activity.add(posting.account)
                if partial_opening and entry.date < start:
                    opening_balances[posting.account] = opening_balances.get(posting.account, Decimal("0")) + posting.amount

    if entity.basis == "accrual":
        card_activity.update(account for account, balance in ledger_balances.items() if balance and is_credit_card_account(account))
    sources = _source_rows(entity.entity_config, start, end, tuple(sorted(card_activity)))

    # Source coverage per account and day of the month (statements, feed
    # windows, declared coverage, then transaction dates).
    coverage_accounts = sorted({str(source["account"]) for source in sources
                                if str(source["account"] or "").startswith(("Assets:", "Liabilities:"))})
    coverage: list[dict[str, Any]] = []
    coverage_items: tuple[list[dict[str, Any]], list[dict[str, Any]]] = ([], [])
    verified_accounts: set[str] = set()
    if coverage_accounts:
        try:
            coverage_document = check_coverage(entity.path, start, end, accounts=coverage_accounts)
        except Exception as exc:  # noqa: BLE001 - any failure must block, not pass
            coverage_items = ([{"code": "source_coverage_unverifiable",
                                "message": "Source coverage could not be checked.", "refs": [type(exc).__name__]}], [])
        else:
            coverage_items = coverage_readiness_items(coverage_document)
            coverage = [{
                "account": row["account"], "label": row["label"], "status": row["status"],
                "verified": row["verified"], "months": row["months"],
            } for row in coverage_document["accounts"]]
            # Statements or feed windows that cover every day of the period
            # are affirmative coverage, the same as declared coverage dates.
            verified_accounts = {row["account"] for row in coverage_document["accounts"]
                                 if row["status"] == "complete" and row["verified"]}

    if not sources:
        blockers.append({"code": "source_scope_missing", "message": "No expected source accounts are declared for this period.", "refs": []})
    for source in sources:
        ref = str(source["source"] or "unknown")
        if not source["account"]:
            blockers.append({"code": "source_account_missing", "message": "An expected source is not bound to a ledger account.", "refs": [ref]})
        if source["coverage_status"] != "complete" and source["account"] not in verified_accounts:
            blockers.append({"code": "source_coverage_incomplete", "message": "Affirmative source coverage does not span the full period.", "refs": [ref]})
    blockers.extend(coverage_items[0])
    warnings.extend(coverage_items[1])

    source_accounts = sorted({str(source["account"]) for source in sources if source["account"]})
    for account in source_accounts:
        if (not account.startswith(("Assets:", "Liabilities:"))
                or account not in account_catalog):
            blockers.append({
                "code": "source_account_invalid",
                "message": "A declared source account is absent from the canonical asset/liability account catalog.",
                "refs": [account],
            })

    reconciliations, rec_error = _read_json(entity.path / "reports" / "reconciliation.json")
    if rec_error or not isinstance(reconciliations, list):
        reconciliations = []
        blockers.append({"code": "reconciliation_evidence_missing", "message": "Period-end reconciliation evidence is missing or unreadable.", "refs": []})
    rec_by_account = {
        str(item.get("account")): item for item in reconciliations
        if isinstance(item, Mapping) and item.get("as_of") == end.isoformat()
    }
    normalized_recs = []
    for account in source_accounts:
        record = rec_by_account.get(account)
        if record is None:
            blockers.append({"code": "reconciliation_missing", "message": "An expected source account lacks period-end reconciliation.", "refs": [account]})
            continue
        ledger_balance = _finite_decimal(record.get("ledger_balance"))
        source_balance = _finite_decimal(record.get("source_balance"))
        discrepancy = _finite_decimal(record.get("discrepancy"))
        opening = _finite_decimal(record.get("source_opening_balance"))
        activity = _finite_decimal(record.get("source_transaction_total"))
        residual = _finite_decimal(record.get("source_integrity_residual"))
        actual_ledger = ledger_balances.get(account, Decimal("0")) if account in account_catalog else None
        safe = (
            record.get("status") == "clean"
            and all(value is not None for value in (
                ledger_balance, source_balance, discrepancy, opening, activity, residual, actual_ledger
            ))
            and abs(ledger_balance - actual_ledger) < Decimal("0.01")
            and abs(source_balance - ledger_balance - discrepancy) < Decimal("0.01")
            and abs(opening + activity - source_balance - residual) < Decimal("0.01")
            and abs(discrepancy) < Decimal("0.01")
            and abs(residual) < Decimal("0.01")
        )
        through = str(record.get("source_through") or "")[:10]
        try:
            through_date = date.fromisoformat(through)
            through_complete = through_date >= end
        except ValueError:
            through_date = None
            through_complete = False
        snapshot_at = _verified_timestamp(record.get("source_snapshot_at"))
        snapshot_coherent = bool(
            snapshot_at is not None and through_date is not None
            and through_date <= snapshot_at.date() <= as_of
        )
        if not safe:
            blockers.append({"code": "reconciliation_not_clean", "message": "A source account is not cleanly reconciled with source-period integrity.", "refs": [account]})
        if opening is None or activity is None:
            blockers.append({"code": "opening_evidence_missing", "message": "Reconciliation lacks affirmative opening balance and source activity evidence.", "refs": [account]})
        if partial_opening and opening != opening_balances.get(account, Decimal("0")):
            blockers.append({"code": "cutover_opening_mismatch", "message": "Partial-period source opening must equal the verified ledger balance immediately before cutover.", "refs": [account, partial_opening["source_as_of"]]})
        if not snapshot_coherent or not through_complete:
            blockers.append({"code": "source_statement_incomplete", "message": "Reconciliation lacks a dated source snapshot through period end.", "refs": [account]})
        evidence_refs = record.get("source_evidence_refs")
        evidence_hashes = record.get("source_evidence_hashes")
        provenance_complete = (
            isinstance(evidence_refs, list) and bool(evidence_refs)
            and len(evidence_refs) == len(set(evidence_refs))
            and all(isinstance(value, str) and bool(value.strip()) for value in evidence_refs)
            and isinstance(evidence_hashes, Mapping)
            and set(evidence_hashes) == set(evidence_refs)
            and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                    for value in evidence_hashes.values())
        )
        if not provenance_complete:
            blockers.append({"code": "source_evidence_provenance_missing", "message": "Reconciliation figures are not bound to verified source records.", "refs": [account]})
        normalized_recs.append({key: record.get(key) for key in (
            "record_id", "account", "as_of", "ledger_balance", "source_balance", "discrepancy", "status",
            "source_opening_balance", "source_transaction_total", "source_integrity_residual",
            "source_snapshot_at", "source_through", "source_evidence_refs", "source_evidence_hashes")})

    ledger_digest = None
    reports: list[dict[str, Any]] = []
    if not store_path.is_file():
        blockers.append({"code": "canonical_ledger_missing", "message": "The canonical ledger store is unavailable for readiness validation.", "refs": []})
    else:
        store = LedgerStore(store_path)
        try:
            ledger_digest = store.content_digest()
            from .ledger.importer import check_store_integrity
            if check_store_integrity(store).status != "ok":
                blockers.append({"code": "ledger_seal_invalid", "message": "Canonical state does not match its final audited seal.", "refs": []})
            audit_errors = store.verify_audit_chain()
            validation_errors = validate(render_store_ledger(store_path))
            if audit_errors or validation_errors:
                refs = [str(item) for item in [*audit_errors, *validation_errors][:20]]
                blockers.append({"code": "ledger_validation_failed", "message": "The ledger or its audit chain failed deterministic validation.", "refs": refs})
            reports = _report_refs(entity.path, start, end, provisional=True)
            if entity.basis == "accrual":
                from .accrual.reports import support_snapshot, unresolved, opening_coverage, coverage_metadata
                with store.connection() as conn:
                    coverage_metadata(conn, "pnl", start, end)
                    blockers.extend(unresolved(conn, end))
                    accrual_snapshot = {"coverage": opening_coverage(conn), "support": support_snapshot(conn, end),
                                        "periods": [dict(row) for row in conn.execute("SELECT * FROM accrual_periods ORDER BY id")]}
        except Exception as exc:
            blockers.append({"code": "report_validation_failed", "message": "Draft financial reports could not be validated.", "refs": [type(exc).__name__]})

    _annotate_blockers(blockers, entity.path, period, start, end, sources)
    blockers = sorted(blockers, key=lambda item: (item["code"], item["refs"], item["message"]))
    warnings = sorted(warnings, key=lambda item: (item["code"], item["refs"], item["message"]))
    state = "ready_for_review" if not blockers else "blocked"
    for report in reports:
        report["provisional"] = state != "ready_for_review"
    snapshot = {
        "ledger_digest": ledger_digest,
        "source_scope": sources,
        "open_work": open_work,
        "open_questions": open_questions,
        "coverage": coverage,
        "reconciliations": sorted(normalized_recs, key=lambda item: str(item.get("account"))),
        "reports": reports,
        "configuration_fingerprint": _hash({"entity": entity.entity_config, "trust_policy": entity.trust_policy}),
        "accrual_state": accrual_snapshot,
    }
    evidence_fingerprint = _hash(snapshot)
    document: dict[str, Any] = {
        "schema_version": 1,
        "basis": entity.basis,
        "state": state,
        "period": period,
        "period_status": period_status,
        **dependencies.to_document(),
        "evidence_fingerprint": evidence_fingerprint,
        "blockers": blockers,
        "warnings": warnings,
        "sources": sources,
        "coverage": coverage,
        "reconciliations": sorted(normalized_recs, key=lambda item: str(item.get("account"))),
        "reports": reports,
    }
    if entity.basis == "accrual":
        document.update(activity_from=start.isoformat(), activity_to=end.isoformat(),
                        partial_post_cutover=partial_opening is not None,
                        imported_summary_through=partial_opening["source_as_of"] if partial_opening else None)
    document["package_id"] = _hash(document)
    return ReadinessAssessment(document)


def local_dependency_versions(entity_path: Path | str) -> DependencyVersions:
    """Zero-version local snapshot for the human CLI, never hosted authority."""
    load_entity(entity_path)
    return DependencyVersions(0, 0, 0, 0)


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("readiness", help="Assess one period for final human review")
    parser.add_argument("--entity", required=True, type=Path)
    parser.add_argument("--period", required=True, help="Calendar period in YYYY-MM format")
    parser.add_argument("--as-of", required=True, help="Deterministic evaluation date in YYYY-MM-DD format")
    parser.add_argument("--output", type=Path, default=None, help="Write the canonical assessment JSON")


def run(args: Any) -> int:
    try:
        as_of = date.fromisoformat(args.as_of)
        assessment = assess_review_readiness(
            args.entity, args.period, local_dependency_versions(args.entity), as_of=as_of,
        )
    except (FileNotFoundError, TypeError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    rendered = json.dumps(assessment.to_document(), indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    # Producing a blocked assessment is successful inspection. Callers consume state.
    return 0
