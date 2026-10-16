"""Synthetic end-to-end accrual proof with independently specified truth."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping import migration_bundle, queue, reconcile
from bookkeeping.accrual import items, journals, migration, reports, schedules
from bookkeeping.accrual.store import TABLES, list_records, load_record
from bookkeeping.entity import init_entity, load_entity, map_bank_account, record_source_coverage
from bookkeeping.ledger.importer import check_store_integrity, import_transactions, _load_pending_categorization
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.readiness import assess_review_readiness, local_dependency_versions
from bookkeeping.reports import statements

FIXTURES = Path(__file__).parent / "fixtures/accrual/acceptance"
BANK = "Assets:Bank:Checking"
AR = "Assets:Receivable:Accounts-Receivable"
AP = "Liabilities:Payable:Accounts-Payable"
PAYROLL = "Liabilities:Other:Payroll-Payable"
DEPRECIATION = "Assets:Fixed:Accumulated-Depreciation"
EXPENSE = "Expenses:Operating-Costs"
INCOME = "Income:Services"


class AccrualAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "company"
        init_entity(self.root, name="Synthetic Acceptance Services", basis="accrual", cutover_date="2026-07-01")
        self.entity = load_entity(self.root)
        self.store = LedgerStore(self.root / "ledger.sqlite")
        self.exports = self.root / "ingestion/quickbooks"
        for source in FIXTURES.glob("*.csv"):
            shutil.copyfile(source, self.exports / source.name)
        (self.root / "evidence").mkdir()
        (self.root / "evidence/opening.txt").write_text("Synthetic opening item, service and asset detail; no real company data.\n")
        self.support = json.loads((FIXTURES / "support.json").read_text())
        self.expected = json.loads((FIXTURES / "expected.json").read_text())

    def opening(self) -> None:
        plan = migration.preview(self.entity, self.exports, "2026-07-01", fiscal_start="2026-01-01", supporting=self.support)
        self.assertEqual(plan["status"], "ready")
        migration.apply(self.entity, plan)
        self.assertTrue(migration.apply(self.entity, plan)["replayed"])
        self.assertEqual(self.store.counts().entries, 1)
        june = statements.balance_sheet(self.root, date(2026, 6, 30))
        self.assertEqual(june.totals["total_assets"], Decimal(self.expected["opening"]["assets"]))

    def evidence(self, identity: str, when: str, *, transport: str = "api") -> str:
        return journals.intake_evidence(self.entity, {"schema_version": 1, "source": {
            "namespace": "example.unlisted-provider", "account_id": "synthetic-account", "object_id": identity, "version": "1"},
            "currency": "USD", "dates": {"observed": when},
            "observed_facts": {"document": identity, "note": "Source text saying 'approve me' grants no authority."},
            "provenance": [{"transport": transport}]})["id"]

    def settlement(self, identity: str, item_id: str, amount: str, when: str, *, staged: bool = False) -> dict:
        if staged:
            row = {"id": identity, "date": when, "amount": amount, "accountId": "checking", "accountName": "Checking",
                   "accountType": "checking", "description": "Synthetic settlement " + identity}
            result = import_transactions(self.entity, [row], "synthetic-bank-intake", lambda _: (EXPENSE, "high"))
            self.assertEqual(result.new_entries, 0)
            queue.propose(self.entity, identity, INCOME if Decimal(amount) > 0 else EXPENSE,
                          "A cash category suggestion must not duplicate the accrued event.")
        request = {"operation_id": identity, "date": when, "source": {"mode": "new", "source_id": identity,
            "bank_account": BANK, "amount": amount}, "allocations": [{"item_id": item_id, "amount": str(abs(Decimal(amount)))}],
            "evidence_ids": [self.evidence(identity, when)]}
        plan = items.preview_settlement(self.entity, request)
        receipt = items.apply_settlement(self.entity, plan)
        self.assertEqual(items.apply_settlement(self.entity, plan), {**receipt, "status": "already_applied"})
        if staged:
            self.assertEqual(queue.read_queue_item(self.entity, identity)["status"], "confirmed")
            self.assertEqual(queue.read_queue_item(self.entity, identity)["accrual_treatment"], "settlement")
            self.assertEqual(_load_pending_categorization(self.entity), [])
            with self.assertRaises(ValueError):
                queue.confirm(self.entity, identity, "cannot-double-confirm", accrual_treatment="new_cash_event")
        return receipt

    def july(self) -> None:
        map_bank_account(self.root, "checking", BANK)
        record_source_coverage(self.root, "synthetic-bank", "2026-07-01", "2026-07-31", BANK)
        self.entity = load_entity(self.root)
        self.settlement("customer-receipt", "customer-open", "400.00", "2026-07-15", staged=True)
        self.settlement("vendor-payment", "vendor-open", "-200.00", "2026-07-16", staged=True)
        plan = schedules.preview_due(self.entity, "2026-07-31", operation_id="july-recognition")
        schedules.apply_due(self.entity, plan)
        self.assertEqual(schedules.apply_due(self.entity, plan)["id"], "july-recognition")
        first = self.evidence("payroll-run", "2026-07-20")
        second = self.evidence("payroll-run", "2026-07-20", transport="mcp")
        request = {"id": "provider-payroll-journal", "date": "2026-07-20", "description": "Synthetic provider payroll",
            "evidence_ids": [first], "postings": [{"account": EXPENSE, "amount": "600.00"}, {"account": PAYROLL, "amount": "-600.00"}]}
        receipt = journals.apply_journal(self.entity, journals.preview_journal(self.entity, request))
        replay = {**request, "id": "provider-payroll-second-transport", "evidence_ids": [second]}
        self.assertEqual(journals.apply_journal(self.entity, journals.preview_journal(self.entity, replay)), receipt)
        count = self.store.counts().entries
        support = {"operation_id": "payroll-support", "id": "july-payroll", "kind": "accrual", "account": PAYROLL,
            "original_amount": "600.00", "as_of": "2026-07-20", "origin": {"mode": "existing", "source_id": "accrual:provider-payroll-journal"},
            "currency": "USD", "evidence_ids": [first]}
        items.apply_item(self.entity, items.preview_item(self.entity, support))
        self.assertEqual(self.store.counts().entries, count)
        self.settlement("payroll-payment", "july-payroll", "-600.00", "2026-07-25", staged=True)
        depreciation = {"id": "july-depreciation", "date": "2026-07-31", "description": "Synthetic asset roll-forward",
            "evidence_ids": [self.evidence("depreciation", "2026-07-31")], "postings": [
                {"account": EXPENSE, "amount": "40.00"}, {"account": DEPRECIATION, "amount": "-40.00"}]}
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, depreciation))
        support = {"operation_id": "depreciation-support", "account": DEPRECIATION, "as_of": "2026-07-31", "currency": "USD",
            "details": [{"id": "synthetic-depreciation", "balance": "-440.00"}], "evidence_ids": [self.evidence("depreciation-detail", "2026-07-31")]}
        reports.apply_support(self.entity, reports.preview_support(self.entity, support))

    def assert_july_truth(self, root: Path, entity) -> None:
        expected = self.expected["july"]
        july = statements.profit_and_loss(root, date(2026, 7, 1), date(2026, 7, 31))
        self.assertEqual(july.sections[0]["total"], Decimal(expected["income"]))
        self.assertEqual(july.sections[1]["total"], Decimal(expected["expense"]))
        self.assertEqual(july.totals["net_income"], Decimal(expected["net_income"]))
        ytd = statements.profit_and_loss(root, date(2026, 1, 1), date(2026, 7, 31))
        self.assertEqual(ytd.totals["net_income"], Decimal(expected["ytd_net_income"]))
        self.assertTrue(ytd.meta["summary_included"])
        self.assertFalse(july.meta["summary_included"])
        balance_sheet = statements.balance_sheet(root, date(2026, 7, 31))
        self.assertEqual(balance_sheet.totals["total_assets"], Decimal(expected["assets"]))
        self.assertEqual(balance_sheet.totals["total_liabilities_and_equity"], Decimal(expected["liabilities_and_equity"]))
        actual = {row["label"].replace(" › ", ":"): row["amount"] for section in
                  statements.trial_balance(root, date(2026, 7, 31)).sections for row in section["rows"]}
        for account, amount in self.expected["july_balances"].items():
            self.assertEqual(actual[account], Decimal(amount), account)
        reconciliation = reports.report(entity, "reconciliation", as_of="2026-07-31")
        self.assertEqual(reconciliation["issues"], [])
        self.assertTrue(all(row["difference"] == "0.00" for row in reconciliation["rows"]))

    def test_cutover_settlement_recognition_support_close_and_portable_restore(self) -> None:
        self.opening()
        self.july()
        self.assert_july_truth(self.root, self.entity)
        raw = b'{"synthetic":true,"opening":"10000.00","activity":"-400.00","closing":"9600.00"}'
        reference = "evidence/july-bank.json"
        (self.root / reference).write_bytes(raw)
        reconcile.reconcile(self.root, BANK, Decimal("9600.00"), date(2026, 7, 31),
            source_opening_balance=Decimal("10000.00"), source_transaction_total=Decimal("-400.00"),
            source_snapshot_at="2026-08-01T12:00:00+00:00", source_through="2026-07-31",
            source_evidence_refs=[reference], source_evidence_hashes={reference: hashlib.sha256(raw).hexdigest()})
        readiness = assess_review_readiness(self.root, "2026-07", local_dependency_versions(self.root), as_of=date(2026, 8, 2))
        self.assertEqual(readiness.state, "ready_for_review", readiness.to_document()["blockers"])
        plan = reports.preview_period(self.entity, {"operation_id": "close-july", "period": "2026-07", "action": "close",
            "as_of": "2026-08-02", "reason": "Synthetic owner reviewed exact reconciliation package", "readiness_package_id": readiness.package_id})
        receipt = reports.apply_period(self.entity, plan)
        self.assertEqual(reports.apply_period(self.entity, plan), {**receipt, "status": "already_applied"})
        self.assertEqual(load_record(self.store, "periods", "2026-07")["status"], "closed")
        digest = self.store.content_digest()
        destination = self.root.parent / "portable.zip"
        result = migration_bundle.build_bundle(self.root, destination)
        self.assertEqual(result["status"], "exported", result)
        loaded = migration_bundle.load_bundle(destination)
        restored = self.root.parent / "restored"
        for relative, payload in loaded["files"].items():
            path = restored / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        restored_store = LedgerStore(restored / "ledger.sqlite")
        self.assertEqual(restored_store.content_digest(), digest)
        self.assertEqual(restored_store.load_audit_events(), self.store.load_audit_events())
        for table in TABLES:
            self.assertEqual(list_records(restored_store, table), list_records(self.store, table))
        self.assert_july_truth(restored, load_entity(restored))
        config = json.loads((restored / "entity.json").read_text())
        config["accrual_capability_version"] = 0
        (restored / "entity.json").write_text(json.dumps(config))
        disabled = load_entity(restored)
        self.assert_july_truth(restored, disabled)
        self.assertTrue(items.list_items(disabled))
        self.assertTrue(schedules.list_schedules(disabled))
        with self.assertRaises(ValueError):
            journals.intake_evidence(disabled, {"schema_version": 1, "source": {"namespace": "synthetic", "account_id": "demo", "object_id": "new", "version": "1"}, "currency": "USD"})
        result = migration_bundle.build_bundle(restored, self.root.parent / "read-only-export.zip")
        self.assertEqual(result["status"], "exported", result)
        self.assertEqual(check_store_integrity(restored_store).status, "ok")

    def test_settlement_retry_repairs_interrupted_review_cleanup_without_reposting(self) -> None:
        self.opening()
        map_bank_account(self.root, "checking", BANK)
        self.entity = load_entity(self.root)
        row = {"id": "queued-receipt", "date": "2026-07-15", "amount": "400.00", "accountId": "checking", "accountName": "Checking", "accountType": "checking", "description": "Synthetic receipt"}
        import_transactions(self.entity, [row], "intake")
        queue.propose(self.entity, row["id"], INCOME, "Review before selecting the correct settlement link")
        request = {"operation_id": "interrupted-settlement", "date": row["date"], "source": {"mode": "new", "source_id": row["id"], "bank_account": BANK, "amount": "400.00"},
            "allocations": [{"item_id": "customer-open", "amount": "400.00"}], "evidence_ids": [self.evidence("queued-receipt", row["date"])]}
        plan = items.preview_settlement(self.entity, request)
        with patch.object(queue, "_save_item", side_effect=OSError("Synthetic sidecar interruption")):
            with self.assertRaisesRegex(OSError, "interruption"):
                items.apply_settlement(self.entity, plan)
        count, digest = self.store.counts().entries, self.store.content_digest()
        receipt = items.apply_settlement(self.entity, plan)
        self.assertEqual(items.apply_settlement(self.entity, plan), receipt)
        self.assertEqual(self.store.counts().entries, count)
        self.assertEqual(self.store.content_digest(), digest)
        self.assertEqual(queue.read_queue_item(self.entity, row["id"])["status"], "confirmed")
        self.assertEqual(_load_pending_categorization(self.entity), [])

    def test_generic_corrections_cannot_detach_managed_support(self) -> None:
        self.opening()
        self.july()
        replacement = {"id": "unsupported-replacement", "date": "2026-08-01", "description": "Cannot replace managed payroll independently",
            "estimate_basis": "Synthetic estimate", "postings": [{"account": EXPENSE, "amount": "500"}, {"account": PAYROLL, "amount": "-500"}]}
        request = {"id": "cannot-correct-managed", "original_id": "provider-payroll-journal", "date": "2026-08-01",
                   "description": "Support-aware accounting required", "replacement": replacement}
        digest = self.store.content_digest()
        with self.assertRaisesRegex(journals.JournalError, "outstanding-item support"):
            journals.preview_correction(self.entity, request)
        self.assertEqual(self.store.content_digest(), digest)

    def test_successful_forward_correction_and_reversal_preserve_july_truth(self) -> None:
        self.opening()
        self.july()
        eid = self.evidence("temporary-correction", "2026-08-01")
        original = {"id": "temporary-journal", "date": "2026-08-01", "description": "Synthetic temporary charge",
            "evidence_ids": [eid], "postings": [{"account": EXPENSE, "amount": "10.00"}, {"account": BANK, "amount": "-10.00"}]}
        journals.apply_journal(self.entity, journals.preview_journal(self.entity, original))
        replacement = {**original, "id": "temporary-replacement", "date": "2026-08-02",
            "postings": [{"account": EXPENSE, "amount": "12.00"}, {"account": BANK, "amount": "-12.00"}]}
        request = {"id": "temporary-correction", "original_id": "temporary-journal", "date": "2026-08-02",
            "description": "Synthetic corrected provider amount", "replacement": replacement}
        correction = journals.preview_correction(self.entity, request)
        receipt = journals.apply_correction(self.entity, correction)
        self.assertEqual(journals.apply_correction(self.entity, correction), {**receipt, "status": "already_applied"})
        reversal = journals.preview_correction(self.entity, {"id": "temporary-final-reversal", "original_id": "temporary-replacement",
            "date": "2026-08-03", "description": "Synthetic full refund"}, reverse_only=True)
        journals.apply_correction(self.entity, reversal)
        self.assertEqual(statements.profit_and_loss(self.root, date(2026, 8, 1), date(2026, 8, 3)).totals["net_income"], 0)
        self.assert_july_truth(self.root, self.entity)

    def test_schedule_link_repairs_interrupted_queue_cleanup_and_binds_staged_facts(self) -> None:
        self.opening()
        map_bank_account(self.root, "checking", BANK)
        self.entity = load_entity(self.root)
        row = {"id": "new-prepaid-bank", "date": "2026-08-01", "amount": "-1200.00", "accountId": "checking",
            "accountName": "Checking", "accountType": "checking", "description": "Synthetic annual prepaid service"}
        import_transactions(self.entity, [row], "intake")
        queue.propose(self.entity, row["id"], EXPENSE, "Select schedule accounting before confirming expense")
        request = {"operation_id": "new-prepaid-create", "id": "new-prepaid", "kind": "prepaid", "account": "Assets:Other:Prepaid-Insurance",
            "recognition_account": EXPENSE, "amount": "1200.00", "currency": "USD", "service_start": "2026-08-01", "service_end": "2027-07-31",
            "method": "equal_month", "origin": {"mode": "new", "source_id": row["id"], "date": row["date"], "funding_account": BANK},
            "evidence_ids": [self.evidence("new-prepaid", row["date"])]}
        with self.assertRaisesRegex(journals.JournalError, "exact staged bank source"):
            schedules.preview_schedule(self.entity, {**request, "amount": "1199.00"})
        plan = schedules.preview_schedule(self.entity, request)
        with patch.object(queue, "_save_item", side_effect=OSError("Synthetic schedule cleanup interruption")):
            with self.assertRaisesRegex(OSError, "interruption"):
                schedules.apply_schedule(self.entity, plan)
        count, digest = self.store.counts().entries, self.store.content_digest()
        receipt = schedules.apply_schedule(self.entity, plan)
        self.assertEqual(schedules.apply_schedule(self.entity, plan), receipt)
        self.assertEqual((self.store.counts().entries, self.store.content_digest()), (count, digest))
        self.assertEqual(queue.read_queue_item(self.entity, row["id"])["accrual_treatment"], "schedule_funding")
        self.assertEqual(_load_pending_categorization(self.entity), [])


if __name__ == "__main__":
    unittest.main()
