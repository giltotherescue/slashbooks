"""Deterministic synthetic companies for the golden output tests.

Every name, amount and document is fictional. Ids, dates and text are fixed, and
the builders run under the frozen clock from ``golden_support``.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

from golden_support import FROZEN_NOW, QB_FIXTURES, frozen_clock, reset_ids

BANK = "Assets:Bank:Checking"
CARD = "Liabilities:CreditCard:Company"
TS = FROZEN_NOW.strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(txn_id: str, when: str, amount: str, description: str, *, card: bool = False) -> dict:
    return {"id": txn_id, "date": when, "amount": amount, "description": description,
            "accountId": "card" if card else "checking",
            "accountName": "Company Card" if card else "Operating Checking",
            "accountType": "credit_card" if card else "checking"}


# ---------------------------------------------------------------------------
# (a) Cash company: three months of bank and card activity
# ---------------------------------------------------------------------------

CASH_ROWS = [
    # January
    _row("chk-0102-owner", "2026-01-02", "20000.00", "Owner contribution wire"),
    _row("chk-0105-rent", "2026-01-05", "-1800.00", "Maple Street Properties rent"),
    _row("chk-0108-client", "2026-01-08", "6500.00", "Orbit Labs invoice 1001 payment"),
    _row("chk-0112-sub", "2026-01-12", "-1250.00", "Delta Design subcontractor"),
    _row("chk-0115-ins", "2026-01-15", "-210.40", "Granite Insurance premium"),
    _row("chk-0120-client", "2026-01-20", "3200.00", "Kestrel Foods invoice 1002 payment"),
    _row("chk-0125-fee", "2026-01-25", "-15.00", "Bank monthly service fee"),
    _row("chk-0131-cardpay", "2026-01-31", "-642.18", "Company Card payment thank you"),
    _row("crd-0103-sw", "2026-01-03", "-79.00", "CloudNotes software subscription", card=True),
    _row("crd-0110-meal", "2026-01-10", "-86.35", "Juniper Bistro client lunch", card=True),
    _row("crd-0118-travel", "2026-01-18", "-412.80", "Skyway Airlines ticket", card=True),
    _row("crd-0122-mkt", "2026-01-22", "-120.00", "Spark Ads campaign", card=True),
    _row("crd-0127-sw", "2026-01-27", "-24.03", "Hostly web hosting", card=True),
    _row("crd-0131-payment", "2026-01-31", "642.18", "Payment thank you", card=True),
    # February
    _row("chk-0203-rent", "2026-02-03", "-1800.00", "Maple Street Properties rent"),
    _row("chk-0206-client", "2026-02-06", "4800.00", "Orbit Labs invoice 1003 payment"),
    _row("chk-0210-sub", "2026-02-10", "-1250.00", "Delta Design subcontractor"),
    _row("chk-0213-client", "2026-02-13", "2750.00", "Harbor Cafe invoice 1004 payment"),
    _row("chk-0217-draw", "2026-02-17", "-2000.00", "Owner draw transfer"),
    _row("chk-0224-tax", "2026-02-24", "-480.00", "State franchise tax payment"),
    _row("chk-0225-fee", "2026-02-25", "-15.00", "Bank monthly service fee"),
    _row("chk-0228-cardpay", "2026-02-28", "-318.55", "Company Card payment thank you"),
    _row("crd-0204-sw", "2026-02-04", "-79.00", "CloudNotes software subscription", card=True),
    _row("crd-0209-meal", "2026-02-09", "-54.20", "Juniper Bistro team lunch", card=True),
    _row("crd-0215-office", "2026-02-15", "-131.75", "Paper Pine office supplies", card=True),
    _row("crd-0219-sw", "2026-02-19", "-24.03", "Hostly web hosting", card=True),
    _row("crd-0221-mkt", "2026-02-21", "-30.57", "Spark Ads campaign", card=True),
    _row("crd-0228-payment", "2026-02-28", "318.55", "Payment thank you", card=True),
    # March (the last weeks stay in the review queue)
    _row("chk-0303-rent", "2026-03-03", "-1800.00", "Maple Street Properties rent"),
    _row("chk-0305-client", "2026-03-05", "7200.00", "Orbit Labs invoice 1005 payment"),
    _row("chk-0311-sub", "2026-03-11", "-1500.00", "Delta Design subcontractor"),
    _row("chk-0316-client", "2026-03-16", "1900.00", "Kestrel Foods invoice 1006 payment"),
    _row("chk-0320-ins", "2026-03-20", "-210.40", "Granite Insurance premium"),
    _row("chk-0325-fee", "2026-03-25", "-15.00", "Bank monthly service fee"),
    _row("chk-0326-mystery", "2026-03-26", "-249.99", "ACH debit REF 88213"),
    _row("chk-0330-deposit", "2026-03-30", "875.00", "Mobile deposit"),
    _row("crd-0304-sw", "2026-03-04", "-79.00", "CloudNotes software subscription", card=True),
    _row("crd-0312-travel", "2026-03-12", "-268.40", "Rail Express tickets", card=True),
    _row("crd-0319-meal", "2026-03-19", "-47.10", "Juniper Bistro client lunch", card=True),
    _row("crd-0324-sw", "2026-03-24", "-24.03", "Hostly web hosting", card=True),
    _row("crd-0327-unknown", "2026-03-27", "-96.00", "QX Supplies order 7741", card=True),
]

CASH_CATEGORIES = {
    "owner contribution": "Equity:Owner-Contributions",
    "rent": "Expenses:Rent",
    "invoice": "Income:Consulting",
    "subcontractor": "Expenses:Subcontractors",
    "insurance": "Expenses:Insurance",
    "service fee": "Expenses:Fees",
    "software": "Expenses:Software",
    "hosting": "Expenses:Software",
    "lunch": "Expenses:Meals",
    "airlines": "Expenses:Travel",
    "rail": "Expenses:Travel",
    "ads": "Expenses:Marketing",
    "office": "Expenses:Office",
    "owner draw": "Equity:Owner-Draws",
    "tax": "Expenses:Taxes",
    "deposit": "Income:Consulting",
}
CASH_UNCATEGORIZED = {"chk-0326-mystery", "crd-0327-unknown", "chk-0330-deposit"}


def _category_for(row: dict) -> str:
    text = row["description"].lower()
    for key, category in CASH_CATEGORIES.items():
        if key in text:
            return category
    return ""


def _prepare_entity(root: Path, **options):
    from bookkeeping.entity import init_entity, load_entity
    init_entity(root, **options)
    return load_entity(root)


def build_cash(root: Path) -> None:
    """Cash company: Jan-Mar activity, categories, a reconciliation and a published month."""
    root = root.resolve()
    reset_ids()
    with frozen_clock():
        from bookkeeping import queue
        from bookkeeping.entity import add_account, load_entity, map_bank_account, record_source_coverage
        from bookkeeping.ledger.importer import import_transactions
        from bookkeeping.queue import set_taught
        from bookkeeping.reconcile import reconcile

        _prepare_entity(root, name="Harbor Studio Cash", business_type="consulting", legal_structure="llc")
        for account in (CARD, "Expenses:Rent", "Expenses:Insurance"):
            add_account(root, account, open_date="2025-01-01")
        map_bank_account(root, "checking", BANK)
        map_bank_account(root, "card", CARD)
        record_source_coverage(root, "Operating Checking", "2026-01-01", "2026-03-31", BANK)
        record_source_coverage(root, "Company Card", "2026-01-01", "2026-03-31", CARD)
        entity = load_entity(root)

        # A person taught one counterparty before intake: later rows are suggested from it.
        set_taught(entity, "MAPLE STREET PROPERTIES RENT", "Expenses:Rent", by="owner", by_name="Owner", ts=TS)
        import_transactions(entity, CASH_ROWS, "golden-cash-intake", intake_only=True, ts=TS)
        for row in CASH_ROWS:
            category = _category_for(row)
            if row["id"] in CASH_UNCATEGORIZED or not category:
                continue
            if row["id"] in {"chk-0131-cardpay", "chk-0228-cardpay", "crd-0131-payment", "crd-0228-payment", "crd-0118-travel"}:
                continue  # transfers and the split are proposed below
            queue.propose(entity, row["id"], category, "Synthetic golden categorization")
            queue.confirm(entity, row["id"], "golden-cash-review", ts=TS)
        queue.propose(entity, "chk-0330-deposit", "Income:Consulting", "Synthetic: still waiting for a person")
        # One purchase split across two categories.
        queue.propose_split(entity, "crd-0118-travel", "Synthetic split airfare and meals",
                            ["Expenses:Travel=300.00", "Expenses:Meals=112.80"])
        queue.confirm_split(entity, "crd-0118-travel", "golden-cash-review", ts=TS)
        for pair in (("chk-0131-cardpay", "crd-0131-payment"), ("chk-0228-cardpay", "crd-0228-payment")):
            proposal = queue.propose_transfer(entity, list(pair), "Synthetic card payment between own accounts")
            queue.confirm_transfer(entity, proposal["source_id"], "golden-cash-review", ts=TS)
        return _finish_cash(root, entity)


def _finish_cash(root: Path, entity) -> None:
    from bookkeeping.reconcile import reconcile

    from bookkeeping.ledger.store import LedgerStore
    entries = LedgerStore(root / "ledger.sqlite").load_entries()
    (root / "evidence").mkdir(exist_ok=True)
    for account, label in ((BANK, "checking"), (CARD, "card")):
        for month, last_day in (("2026-01", date(2026, 1, 31)), ("2026-02", date(2026, 2, 28))):
            def total(through: str, since: str = "0000-00-00") -> Decimal:
                return sum((p.amount for e in entries for p in e.postings
                            if p.account == account and since <= e.date.isoformat() <= through), Decimal(0))
            opening = total(f"{month}-01") - total(f"{month}-01", f"{month}-01")  # balance before the month
            closing = total(last_day.isoformat())
            reference = f"evidence/{label}-{month}.json"
            raw = json.dumps({"synthetic": True, "statement": label, "period": month}).encode()
            (root / reference).write_bytes(raw)
            reconcile(root, account, closing, last_day,
                      source_opening_balance=opening, source_transaction_total=closing - opening,
                      source_snapshot_at=f"2026-{last_day.month + 1:02d}-02T12:00:00+00:00",
                      source_through=last_day.isoformat(),
                      source_evidence_refs=[reference], source_evidence_hashes={reference: hashlib.sha256(raw).hexdigest()})
    # Publish (close) January: a person records it in entity.json.
    path = root / "entity.json"
    config = json.loads(path.read_text())
    config["closed_periods"] = ["2026-01"]
    config["closed_through"] = "2026-01-31"
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# (b) Native accrual company
# ---------------------------------------------------------------------------

AR = "Assets:Receivable:Accounts-Receivable"
AP = "Liabilities:Payable:Accounts-Payable"
PREPAID = "Assets:Other:Prepaid-Expenses"
EQUIPMENT = "Assets:Fixed:Equipment"
ACCUM = "Assets:Fixed:Accumulated-Depreciation"
DEFERRED = "Liabilities:Other:Deferred-Revenue"
ACCRUED = "Liabilities:Other:Accrued-Liabilities"
INCOME = "Income:Consulting"
SOFTWARE = "Expenses:Software"
UTILITIES = "Expenses:Utilities"
BAD_DEBT = "Expenses:Bad-Debt"
BONUS = "Expenses:Bonus"


def _evidence(entity, object_id: str, when: str = "2026-01-01") -> str:
    from bookkeeping.accrual import journals
    return journals.intake_evidence(entity, {"schema_version": 1, "source": {
        "namespace": "example.golden", "account_id": "golden-account", "object_id": object_id, "version": "1"},
        "currency": "USD", "dates": {"observed": when},
        "observed_facts": {"document": object_id, "note": "Synthetic supporting document."}})["id"]


def build_accrual(root: Path) -> None:
    """Native accrual company: opening, items, settlements, schedules, journals, a rule and a closed month."""
    root = root.resolve()
    reset_ids()
    with frozen_clock():
        from bookkeeping.accrual import items, journals, opening, reports, rules, schedules
        from bookkeeping.entity import add_account, load_entity, map_bank_account, record_source_coverage
        from bookkeeping.ledger.store import LedgerStore
        from bookkeeping.readiness import assess_review_readiness, local_dependency_versions
        from bookkeeping.reconcile import reconcile

        _prepare_entity(root, name="Pinecone Advisory Accrual", basis="accrual", cutover_date="2026-01-01",
                        business_type="consulting", legal_structure="llc")
        for account in (UTILITIES, BAD_DEBT, BONUS, "Expenses:Insurance"):
            add_account(root, account, open_date="2025-12-31")
        map_bank_account(root, "checking", BANK)
        record_source_coverage(root, "Operating Checking", "2026-01-01", "2026-03-31", BANK)
        (root / "evidence").mkdir(exist_ok=True)
        (root / "evidence/open-items.txt").write_text("Synthetic open items list, prepaid, deferred and asset detail.\n")
        entity = load_entity(root)

        plan = opening.preview_opening(entity, {
            "operation_id": "golden-opening", "cutover": "2026-01-01", "evidence_refs": ["evidence/open-items.txt"],
            "lines": [{"account": BANK, "amount": "30000.00"}, {"account": AR, "amount": "4000.00"},
                      {"account": PREPAID, "amount": "600.00"}, {"account": EQUIPMENT, "amount": "2400.00"},
                      {"account": ACCUM, "amount": "-1000.00"}, {"account": AP, "amount": "-1500.00"},
                      {"account": DEFERRED, "amount": "-600.00"}, {"account": "Equity:Opening-Balances", "amount": "-33900.00"}],
            "items": [
                {"id": "inv-open-1", "kind": "receivable", "account": AR, "counterparty": "Orbit Labs", "reference": "INV-0950",
                 "due_date": "2025-12-20", "open_amount": "4000.00"},
                {"id": "bill-open-1", "kind": "payable", "account": AP, "counterparty": "Delta Design", "reference": "BILL-311",
                 "due_date": "2026-01-15", "open_amount": "1500.00"}],
            "schedules": [
                {"id": "insurance-h2", "kind": "prepaid", "account": PREPAID, "recognition_account": "Expenses:Insurance",
                 "amount": "1200.00", "recognized_amount": "600.00", "service_start": "2025-07-01", "service_end": "2026-06-30",
                 "method": "equal_month"},
                {"id": "retainer-h2", "kind": "deferred", "account": DEFERRED, "recognition_account": INCOME,
                 "amount": "1200.00", "recognized_amount": "600.00", "service_start": "2025-07-01", "service_end": "2026-06-30",
                 "method": "equal_month"},
                {"id": "laptop-old", "kind": "depreciation", "account": EQUIPMENT, "contra_account": ACCUM,
                 "recognition_account": "Expenses:Depreciation", "amount": "2400.00", "salvage_value": "0.00",
                 "recognized_amount": "1000.00", "service_start": "2025-03-01", "service_end": "2027-02-28", "method": "equal_month"}]})
        assert plan["status"] == "ready", plan.get("support_differences")
        opening.apply_opening(entity, plan)

        def item(op: str, item_id: str, kind: str, amount: str, when: str, who: str, counter: str, due: str = "", **extra):
            request = {"operation_id": op, "id": item_id, "kind": kind, "account": AR if kind == "receivable" else AP,
                       "currency": "USD", "original_amount": amount, "as_of": when, "counterparty": who,
                       "origin": {"mode": "new", "source_id": op + "-source", "counter_account": counter},
                       "evidence_ids": [_evidence(entity, op, when)]}
            if due:
                request["due_date"] = due
            request.update(extra)
            return items.apply_item(entity, items.preview_item(entity, request))

        def payment(op: str, when: str, cash: str, allocations: list[tuple[str, str]], **extra):
            request = {"operation_id": op, "date": when, "evidence_ids": [_evidence(entity, op, when)],
                       "source": {"mode": "new", "source_id": op + "-bank", "bank_account": BANK, "amount": cash},
                       "allocations": [{"item_id": i, "amount": a} for i, a in allocations]}
            request.update(extra)
            return items.apply_settlement(entity, items.preview_settlement(entity, request))

        # January: invoices, bills and payments.
        item("inv-1001-create", "inv-1001", "receivable", "6000.00", "2026-01-10", "Orbit Labs", INCOME, "2026-02-09")
        item("inv-1003-create", "inv-1003", "receivable", "900.00", "2026-01-12", "Kestrel Foods", INCOME, "2026-02-11")
        payment("pay-bill-open-1", "2026-01-15", "-1500.00", [("bill-open-1", "1500.00")])
        payment("pay-inv-open-1", "2026-01-20", "2500.00", [("inv-open-1", "2500.00")])
        # An auto-reversing estimate, reversed on February 1.
        journals.apply_journal(entity, journals.preview_journal(entity, {
            "id": "utilities-estimate-jan", "date": "2026-01-31", "description": "Estimated January utilities",
            "estimate_basis": "Synthetic three-month average", "reversal_date": "2026-02-01",
            "postings": [{"account": UTILITIES, "amount": "450.00"}, {"account": ACCRUED, "amount": "-450.00"}]}))
        # February: overpayment credit applied to a new invoice, bill, prepaid, deferred, asset.
        payment("pay-inv-1001", "2026-02-05", "6500.00", [("inv-1001", "6000.00")],
                excess={"id": "orbit-credit", "account": AR, "reference": "Overpayment"})
        journals.due_reversals(entity, "2026-02-01", apply=True)
        item("bill-2001-create", "bill-2001", "payable", "800.00", "2026-02-03", "Granite Hosting", SOFTWARE, "2026-03-05")
        item("inv-1002-create", "inv-1002", "receivable", "3000.00", "2026-02-15", "Orbit Labs", INCOME, "2026-03-17")
        items.apply_offset(entity, items.preview_offset(entity, {
            "operation_id": "apply-orbit-credit", "date": "2026-02-16", "credit_item_id": "orbit-credit",
            "target_item_id": "inv-1002", "amount": "500.00", "reason": "Apply overpayment"}))
        schedules.apply_schedule(entity, schedules.preview_schedule(entity, {
            "operation_id": "sw-annual-create", "id": "sw-annual", "kind": "prepaid", "account": PREPAID,
            "recognition_account": SOFTWARE, "currency": "USD", "amount": "1200.00", "service_start": "2026-02-01",
            "service_end": "2027-01-31", "method": "equal_month",
            "origin": {"mode": "new", "source_id": "sw-annual-payment", "date": "2026-02-01", "funding_account": BANK},
            "evidence_ids": [_evidence(entity, "sw-annual", "2026-02-01")]}))
        schedules.apply_schedule(entity, schedules.preview_schedule(entity, {
            "operation_id": "retainer-new-create", "id": "retainer-new", "kind": "deferred", "account": DEFERRED,
            "recognition_account": INCOME, "currency": "USD", "amount": "3600.00", "service_start": "2026-02-01",
            "service_end": "2026-07-31", "method": "equal_month",
            "origin": {"mode": "new", "source_id": "retainer-new-receipt", "date": "2026-02-01", "funding_account": BANK},
            "evidence_ids": [_evidence(entity, "retainer-new", "2026-02-01")]}))
        schedules.apply_schedule(entity, schedules.preview_schedule(entity, {
            "operation_id": "laptop-new-create", "id": "laptop-new", "kind": "depreciation", "account": EQUIPMENT,
            "contra_account": ACCUM, "recognition_account": "Expenses:Depreciation", "currency": "USD", "amount": "3600.00",
            "service_start": "2026-02-01", "service_end": "2029-01-31", "method": "equal_month",
            "origin": {"mode": "new", "source_id": "laptop-new-purchase", "date": "2026-02-10", "funding_account": BANK},
            "evidence_ids": [_evidence(entity, "laptop-new", "2026-02-10")]}))
        payment("pay-bill-2001", "2026-02-20", "-800.00", [("bill-2001", "800.00")])
        # March: estimate and true-up, write-off, payments and a pending reversal.
        item("bonus-estimate-create", "bonus-estimate", "accrual", "1000.00", "2026-02-28", "Staff", BONUS, "2026-04-15",
             estimate_basis="Synthetic target bonus pool", evidence_ids=[])
        item("bonus-actual-create", "bonus-actual", "payable", "700.00", "2026-03-10", "Staff", BONUS)
        items.apply_adjustment(entity, items.preview_adjustment(entity, {
            "operation_id": "bonus-true-up", "item_id": "bonus-estimate", "action": "true_up", "actual_item_id": "bonus-actual",
            "date": "2026-03-10", "counter_account": BONUS, "reason": "Actual bonus approved"}))
        items.apply_adjustment(entity, items.preview_adjustment(entity, {
            "operation_id": "inv-1003-writeoff", "item_id": "inv-1003", "action": "writeoff", "amount": "300.00",
            "date": "2026-03-20", "counter_account": BAD_DEBT, "source_id": "inv-1003-writeoff-source",
            "reason": "Synthetic uncollectible balance", "evidence_ids": [_evidence(entity, "inv-1003-writeoff", "2026-03-20")]}))
        payment("pay-inv-1002", "2026-03-18", "2500.00", [("inv-1002", "2500.00")])
        payment("pay-bonus-actual", "2026-03-25", "-700.00", [("bonus-actual", "700.00")])
        journals.apply_journal(entity, journals.preview_journal(entity, {
            "id": "utilities-estimate-mar", "date": "2026-03-31", "description": "Estimated March utilities",
            "estimate_basis": "Synthetic three-month average", "reversal_date": "2026-04-01",
            "postings": [{"account": UTILITIES, "amount": "470.00"}, {"account": ACCRUED, "amount": "-470.00"}]}))
        # Recognition through the end of each month.
        for month_end in ("2026-01-31", "2026-02-28", "2026-03-31"):
            due = schedules.preview_due(entity, month_end, operation_id="recognize-" + month_end)
            schedules.apply_due(entity, due)
        # A saved rule: what to propose for one supplier. A rule never records anything.
        rules.set_apply(entity, rules.set_preview(entity, {
            "operation_id": "rule-granite", "counterparty": "Granite Hosting", "treatment": "prepaid", "term_months": 12,
            "method": "equal_month", "note": "Annual plan, paid up front", "set_by": "owner", "source": "person",
            "set_at": "2026-02-01T10:00:00Z"}))
        # Support, reconciliation and a closed (published) month.
        entries = LedgerStore(root / "ledger.sqlite").load_entries()
        january_bank = sum((p.amount for e in entries for p in e.postings if p.account == BANK and e.date.isoformat() <= "2026-01-31"), Decimal(0))
        january_opening = sum((p.amount for e in entries for p in e.postings if p.account == BANK and e.date.isoformat() < "2026-01-01"), Decimal(0))
        reference = "evidence/bank-2026-01.json"
        raw = b'{"synthetic":true,"statement":"checking","period":"2026-01"}'
        (root / reference).write_bytes(raw)
        reconcile(root, BANK, january_bank, date(2026, 1, 31), source_opening_balance=january_opening,
                  source_transaction_total=january_bank - january_opening, source_snapshot_at="2026-02-02T12:00:00+00:00",
                  source_through="2026-01-31", source_evidence_refs=[reference],
                  source_evidence_hashes={reference: hashlib.sha256(raw).hexdigest()})
        readiness = assess_review_readiness(root, "2026-01", local_dependency_versions(root), as_of=date(2026, 8, 5))
        assert readiness.state == "ready_for_review", readiness.to_document()["blockers"]
        plan = reports.preview_period(load_entity(root), {
            "operation_id": "close-2026-01", "period": "2026-01", "action": "close", "as_of": "2026-08-05",
            "reason": "Synthetic owner reviewed the January package", "readiness_package_id": readiness.package_id})
        reports.apply_period(load_entity(root), plan)


# ---------------------------------------------------------------------------
# (c) QuickBooks-migrated accrual company (the repo's acceptance fixture)
# ---------------------------------------------------------------------------

QB_AR = AR
QB_AP = AP
QB_PAYROLL = "Liabilities:Other:Payroll-Payable"
QB_DEPRECIATION = "Assets:Fixed:Accumulated-Depreciation"
QB_EXPENSE = "Expenses:Operating-Costs"


def build_quickbooks(root: Path) -> None:
    """Accrual company opened from the QuickBooks acceptance exports, with July activity and a closed month."""
    root = root.resolve()
    reset_ids()
    with frozen_clock():
        from bookkeeping import queue
        from bookkeeping.accrual import items, journals, migration, reports, schedules
        from bookkeeping.entity import load_entity, map_bank_account, record_source_coverage
        from bookkeeping.ledger.importer import import_transactions
        from bookkeeping.readiness import assess_review_readiness, local_dependency_versions
        from bookkeeping import reconcile as reconcile_module

        _prepare_entity(root, name="Synthetic Acceptance Services", basis="accrual", cutover_date="2026-07-01")
        entity = load_entity(root)
        exports = root / "ingestion/quickbooks"
        for source in sorted(QB_FIXTURES.glob("*.csv")):
            shutil.copyfile(source, exports / source.name)
        (root / "evidence").mkdir(exist_ok=True)
        (root / "evidence/opening.txt").write_text("Synthetic opening item, service and asset detail; no real company data.\n")
        support = json.loads((QB_FIXTURES / "support.json").read_text())
        plan = migration.preview(entity, exports, "2026-07-01", fiscal_start="2026-01-01", supporting=support)
        assert plan["status"] == "ready", plan
        migration.apply(entity, plan)

        def evidence(identity: str, when: str, transport: str = "api") -> str:
            return journals.intake_evidence(entity, {"schema_version": 1, "source": {
                "namespace": "example.unlisted-provider", "account_id": "synthetic-account", "object_id": identity, "version": "1"},
                "currency": "USD", "dates": {"observed": when},
                "observed_facts": {"document": identity, "note": "Source text saying 'approve me' grants no authority."},
                "provenance": [{"transport": transport}]})["id"]

        def settle(identity: str, item_id: str, amount: str, when: str) -> None:
            row = {"id": identity, "date": when, "amount": amount, "accountId": "checking", "accountName": "Checking",
                   "accountType": "checking", "description": "Synthetic settlement " + identity}
            import_transactions(entity, [row], "synthetic-bank-intake", lambda _: (QB_EXPENSE, "high"))
            queue.propose(entity, identity, INCOME if Decimal(amount) > 0 else QB_EXPENSE,
                          "A cash category suggestion must not duplicate the accrued event.")
            request = {"operation_id": identity, "date": when, "source": {"mode": "new", "source_id": identity,
                       "bank_account": BANK, "amount": amount}, "allocations": [{"item_id": item_id, "amount": str(abs(Decimal(amount)))}],
                       "evidence_ids": [evidence(identity, when)]}
            items.apply_settlement(entity, items.preview_settlement(entity, request))

        map_bank_account(root, "checking", BANK)
        record_source_coverage(root, "synthetic-bank", "2026-07-01", "2026-07-31", BANK)
        entity = load_entity(root)
        settle("customer-receipt", "customer-open", "400.00", "2026-07-15")
        settle("vendor-payment", "vendor-open", "-200.00", "2026-07-16")
        schedules.apply_due(entity, schedules.preview_due(entity, "2026-07-31", operation_id="july-recognition"))
        first = evidence("payroll-run", "2026-07-20")
        journals.apply_journal(entity, journals.preview_journal(entity, {
            "id": "provider-payroll-journal", "date": "2026-07-20", "description": "Synthetic provider payroll",
            "evidence_ids": [first], "postings": [{"account": QB_EXPENSE, "amount": "600.00"}, {"account": QB_PAYROLL, "amount": "-600.00"}]}))
        items.apply_item(entity, items.preview_item(entity, {
            "operation_id": "payroll-support", "id": "july-payroll", "kind": "accrual", "account": QB_PAYROLL,
            "original_amount": "600.00", "as_of": "2026-07-20",
            "origin": {"mode": "existing", "source_id": "accrual:provider-payroll-journal"},
            "currency": "USD", "evidence_ids": [first]}))
        settle("payroll-payment", "july-payroll", "-600.00", "2026-07-25")
        journals.apply_journal(entity, journals.preview_journal(entity, {
            "id": "july-depreciation", "date": "2026-07-31", "description": "Synthetic asset roll-forward",
            "evidence_ids": [evidence("depreciation", "2026-07-31")],
            "postings": [{"account": QB_EXPENSE, "amount": "40.00"}, {"account": QB_DEPRECIATION, "amount": "-40.00"}]}))
        reports.apply_support(entity, reports.preview_support(entity, {
            "operation_id": "depreciation-support", "account": QB_DEPRECIATION, "as_of": "2026-07-31", "currency": "USD",
            "details": [{"id": "synthetic-depreciation", "balance": "-440.00"}],
            "evidence_ids": [evidence("depreciation-detail", "2026-07-31")]}))
        raw = b'{"synthetic":true,"opening":"10000.00","activity":"-400.00","closing":"9600.00"}'
        reference = "evidence/july-bank.json"
        (root / reference).write_bytes(raw)
        reconcile_module.reconcile(root, BANK, Decimal("9600.00"), date(2026, 7, 31),
            source_opening_balance=Decimal("10000.00"), source_transaction_total=Decimal("-400.00"),
            source_snapshot_at="2026-08-01T12:00:00+00:00", source_through="2026-07-31",
            source_evidence_refs=[reference], source_evidence_hashes={reference: hashlib.sha256(raw).hexdigest()})
        readiness = assess_review_readiness(root, "2026-07", local_dependency_versions(root), as_of=date(2026, 8, 5))
        assert readiness.state == "ready_for_review", readiness.to_document()["blockers"]
        plan = reports.preview_period(load_entity(root), {"operation_id": "close-july", "period": "2026-07", "action": "close",
            "as_of": "2026-08-05", "reason": "Synthetic owner reviewed exact reconciliation package",
            "readiness_package_id": readiness.package_id})
        reports.apply_period(load_entity(root), plan)
