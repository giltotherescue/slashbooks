"""Synthetic full-TB cutover, failure atomicity, and portable state proofs."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from bookkeeping.accrual import migration
from bookkeeping.accrual.store import TABLES, list_records, load_record
from bookkeeping.entity import init_entity, load_entity
from bookkeeping.ledger.importer import _atomic_ledger_write, check_store_integrity
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.ledger.store import LedgerStore
from bookkeeping import compare, hosted_adoption, migration_bundle, quickbooks


FIXTURES = Path(__file__).parent / "fixtures/accrual/quickbooks"


class AccrualMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "company"
        init_entity(self.root, name="Synthetic Services", basis="accrual", cutover_date="2026-07-01")
        self.entity = load_entity(self.root)
        self.store = LedgerStore(self.root / "ledger.sqlite")
        self.exports = self.root / "qb-exports"
        shutil.copytree(FIXTURES, self.exports)
        (self.root / "evidence").mkdir(exist_ok=True)
        (self.root / "evidence/opening.pdf").write_bytes(b"%PDF synthetic opening support")
        self.support = json.loads((self.exports / "support.json").read_text())

    def preview(self, **kwargs):
        return migration.preview(self.entity, self.exports, "2026-07-01",
                                 fiscal_start="2026-01-01", supporting=kwargs.get("supporting", self.support))

    def change(self, name, old, new):
        path = self.exports / name
        path.write_text(path.read_text().replace(old, new))

    def test_midyear_preview_preserves_every_account_and_explicit_coverage_without_writes(self):
        before = self.store.content_digest()
        plan = self.preview()
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual(len(plan["balances"]), 11)
        self.assertEqual(plan["balances"]["Income:Services"], "-10000.00")
        self.assertEqual(plan["balances"]["Expenses:Operating-Costs"], "3200.00")
        self.assertEqual(plan["balances"]["Equity:Retained-Earnings"], "-5000.00")
        self.assertNotIn("Equity:Opening-Balances", plan["balances"])
        self.assertEqual(plan["coverage"]["summary_to"], "2026-06-30")
        self.assertEqual(plan["coverage"]["summary_from"], "2026-01-01")
        self.assertTrue(plan["coverage"]["verified"])
        for row in plan["support"]:
            if row["table"] in {"items", "schedules"}:
                self.assertEqual(row["payload"]["origin"]["mode"], "opening")
                self.assertTrue(row["payload"]["evidence_ids"])

    def test_support_difference_is_exact_visible_and_blocks_activation(self):
        self.support["items"][0]["remaining_amount"] = "999.99"
        plan = self.preview()
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["support_differences"], [{"account": "Assets:Receivable:Accounts-Receivable",
                         "ledger": "1000.00", "support": "999.99", "difference": "-0.01", "currency": "USD"}])
        before = self.store.content_digest()
        with self.assertRaisesRegex(ValueError, "supporting balances"):
            migration.apply(self.entity, plan)
        self.assertEqual(self.store.content_digest(), before)

    def test_missing_detail_and_missing_or_unsafe_evidence_never_claim_readiness(self):
        plan = self.preview(supporting={})
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(len(plan["support_differences"]), 7)
        for bad in ("evidence/missing.pdf", "../secret.pdf", "/tmp/secret.pdf"):
            with self.subTest(bad=bad):
                support = deepcopy(self.support)
                support["items"][0]["evidence_refs"] = [bad]
                with self.assertRaisesRegex(ValueError, "evidence"):
                    self.preview(supporting=support)

    def test_unknown_mixed_basis_wrong_currency_and_dates_fail_closed(self):
        for filename, before, after, error in (
            ("trial_balance.csv", "Accrual Basis", "Cash Basis", "basis"),
            ("trial_balance.csv", "Accrual Basis", "Unknown Basis", "basis"),
            ("accounts.csv", "Fixed Assets", "Unsupported Type", "account type"),
            ("trial_balance.csv", "June 30, 2026", "June 29, 2026", "dates"),
            ("profit_and_loss.csv", "Synthetic Services", "Synthetic Services EUR", "USD"),
            ("balance_sheet.csv", "Synthetic Services", "Another Synthetic Company", "consistent company"),
        ):
            with self.subTest(filename=filename, after=after):
                path = self.exports / filename
                original = path.read_bytes()
                self.change(filename, before, after)
                with self.assertRaisesRegex(ValueError, error):
                    self.preview()
                path.write_bytes(original)
        support = deepcopy(self.support)
        support["items"][0]["currency"] = "EUR"
        with self.assertRaisesRegex(ValueError, "currency"):
            self.preview(supporting=support)

    def test_report_tieout_and_mapping_never_use_a_plug_or_drop_material_accounts(self):
        for filename, before, after, error in (
            ("trial_balance.csv", "Checking,10000", "Checking,9999", "totals do not reconcile"),
            ("profit_and_loss.csv", "Services,10000", "Services,9999", "P&L does not reconcile"),
            ("balance_sheet.csv", "Retained Earnings,5000", "Retained Earnings,5001", "Balance sheet does not reconcile"),
            ("balance_sheet.csv", "Net Income,6800", "Net Income,6801", "current earnings"),
            ("accounts.csv", "Equipment,Fixed Assets,Fixed Assets", "Missing Equipment,Fixed Assets,Fixed Assets", "mapping"),
        ):
            with self.subTest(filename=filename, after=after):
                path = self.exports / filename
                original = path.read_bytes()
                self.change(filename, before, after)
                with self.assertRaisesRegex(ValueError, error):
                    self.preview()
                path.write_bytes(original)
        self.entity.entity_config["compare"] = {"qbo_account_crosswalk": {"Equipment": "Expenses:Equipment"}}
        with self.assertRaisesRegex(ValueError, "accounting type"):
            self.preview()

    def test_exact_minor_units_fiscal_policy_and_support_signs_are_required(self):
        for value in ("1.001", "NaN", float("nan"), True):
            support = deepcopy(self.support)
            support["items"][0]["remaining_amount"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.preview(supporting=support)
        with self.assertRaisesRegex(ValueError, "fiscal_start"):
            migration.preview(self.entity, self.exports, "2026-07-01", supporting=self.support)
        self.support["external"][1]["balance"] = "400.00"
        difference = next(row for row in self.preview()["support_differences"] if "Depreciation" in row["account"])
        self.assertEqual(difference["difference"], "800.00")

    def test_apply_replay_and_bundle_roundtrip_preserve_full_canonical_state(self):
        plan = self.preview()
        receipt = migration.apply(self.entity, plan)
        self.assertEqual(receipt["entries_written"], 1)
        entries = self.store.load_entries()
        self.assertEqual(entries[0].date, date(2026, 6, 30))
        self.assertEqual(sum(p.amount for p in entries[0].postings), 0)
        self.assertEqual(check_store_integrity(self.store).status, "ok")
        self.assertEqual(quickbooks.invalid_opening_entries(self.entity), [])
        after = self.store.content_digest()
        self.assertTrue(migration.apply(self.entity, plan)["replayed"])
        self.assertTrue(migration.apply(self.entity, self.preview())["replayed"])
        self.assertEqual(self.store.content_digest(), after)
        destination = Path(self.tmp.name).resolve() / "snapshot.zip"
        result = migration_bundle.build_bundle(self.root, destination)
        self.assertEqual(result["status"], "exported", result)
        loaded = migration_bundle.load_bundle(destination)
        manifest = loaded["manifest"]
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["source_entity"]["basis"], "accrual")
        self.assertEqual(manifest["source_entity"]["accrual_capability_version"], 1)
        self.assertEqual(manifest["ledger"]["schema_version"], 2)
        self.assertEqual(set(manifest["ledger"]["tables"]), set(migration_bundle.TABLES) | {"accrual_" + n for n in TABLES})
        restored = Path(self.tmp.name) / "restored.sqlite"
        restored.write_bytes(loaded["files"]["ledger.sqlite"])
        restored_store = LedgerStore(restored)
        self.assertEqual(restored_store.content_digest(), after)
        for table in TABLES:
            self.assertEqual(list_records(restored_store, table), list_records(self.store, table))
        self.assertEqual(restored_store.load_audit_events(), self.store.load_audit_events())
        frame = hosted_adoption._frame(loaded, {"manifest_sha256": migration_bundle._sha(migration_bundle._json_bytes(manifest))})
        self.assertEqual(json.loads(frame[3]), manifest)
        self.assertIn(loaded["files"]["ledger.sqlite"], frame)

    def test_portable_capture_rejects_changed_retained_support_or_source_bytes(self):
        migration.apply(self.entity, self.preview())
        for path in (self.root / "evidence/opening.pdf", self.exports / "balance_sheet.csv"):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                path.write_bytes(before + b"\nChanged archival source bytes")
                result = migration_bundle.build_bundle(self.root, self.root.parent / "blocked.zip")
                self.assertEqual(result["status"], "blocked")
                self.assertIn("evidence_hash_mismatch", result["blockers"])
                path.write_bytes(before)

    def test_apply_rejects_evidence_account_mapping_or_preview_tampering(self):
        plan = self.preview()
        for mutation in ("balances", "accounting"):
            modified = deepcopy(plan)
            if mutation == "balances":
                modified["balances"]["Income:Services"] = "-1.00"
            else:
                modified["accounting_plan"]["entries"][0]["postings"][0]["amount"] = "1.00"
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, "preview was changed"):
                migration.apply(self.entity, modified)
        (self.root / "evidence/opening.pdf").write_bytes(b"%PDF changed source facts")
        with self.assertRaisesRegex(ValueError, "Stale"):
            migration.apply(self.entity, plan)
        plan = self.preview()
        path = self.root / "entity.json"
        config = json.loads(path.read_text())
        config["compare"] = {"qbo_account_crosswalk": {"Equipment": "Assets:Fixed:Hardware"}}
        path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, "Stale"):
            migration.apply(self.entity, plan)
        self.assertEqual(self.store.counts().entries, 0)

    def test_source_precision_and_false_report_totals_cannot_be_rounded_or_ignored(self):
        for filename, old, new in (
            ("trial_balance.csv", "Checking,10000", "Checking,10000.001"),
            ("profit_and_loss.csv", "Services,10000", "Services,10000.001"),
            ("balance_sheet.csv", "Checking,10000", "Checking,10000.001"),
            ("trial_balance.csv", "TOTAL,17200,17200", "TOTAL,17201,17201"),
        ):
            with self.subTest(filename=filename):
                path = self.exports / filename
                original = path.read_bytes()
                self.change(filename, old, new)
                with self.assertRaises(ValueError):
                    self.preview()
                path.write_bytes(original)

    def test_preview_applies_after_company_relocation_without_absolute_source_references(self):
        plan = self.preview()
        self.assertEqual(plan["folder"], "qb-exports")
        self.assertNotIn(str(self.root), json.dumps(plan))
        relocated = self.root.parent / "relocated"
        self.root.rename(relocated)
        moved = load_entity(relocated)
        result = migration.apply(moved, plan)
        self.assertEqual(result["entries_written"], 1)
        self.assertEqual(LedgerStore(relocated / "ledger.sqlite").counts().entries, 1)

    def test_source_reports_must_be_durable_company_files_without_symlink_or_escape(self):
        with self.assertRaisesRegex(ValueError, "Stage opening source"):
            migration.preview(self.entity, FIXTURES, "2026-07-01", fiscal_start="2026-01-01", supporting=self.support)
        with self.assertRaisesRegex(ValueError, "without symlinks or escapes"):
            migration.preview(self.entity, "qb-exports/../qb-exports", "2026-07-01", fiscal_start="2026-01-01", supporting=self.support)
        link = self.root / "ingestion/source-link"
        link.symlink_to(self.exports, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "without symlinks or escapes"):
            migration.preview(self.entity, link, "2026-07-01", fiscal_start="2026-01-01", supporting=self.support)
        original = self.exports / "trial_balance.csv"
        original.rename(self.root / "evidence/source-tb.csv")
        original.symlink_to(self.root / "evidence/source-tb.csv")
        with self.assertRaisesRegex(ValueError, "without symlinks or hard links"):
            self.preview()

    def test_fiscal_boundary_carries_closed_equity_without_prior_year_income(self):
        for name in ("trial_balance.csv", "balance_sheet.csv", "profit_and_loss.csv"):
            self.change(name, "June 30, 2026", "December 31, 2026")
        self.change("trial_balance.csv", "Retained Earnings,,5000", "Retained Earnings,,11800")
        self.change("trial_balance.csv", "Services,,10000", "Services,,0")
        self.change("trial_balance.csv", "Operating Costs,3200,", "Operating Costs,0,")
        self.change("trial_balance.csv", "TOTAL,17200,17200", "TOTAL,14000,14000")
        self.change("balance_sheet.csv", "Retained Earnings,5000", "Retained Earnings,11800")
        self.change("balance_sheet.csv", "Net Income,6800", "Net Income,0")
        for row in self.support["schedules"]:
            row["start_date"], row["end_date"] = "2026-07-01", "2027-07-01"
        # New-year policy is explicit and the completed prior-year P&L remains
        # evidence, without becoming a new-year opening revenue balance.
        self.entity.entity_config["cutover_date"] = "2027-01-01"
        (self.root / "entity.json").write_text(json.dumps(self.entity.entity_config))
        plan = migration.preview(self.entity, self.exports, "2027-01-01", fiscal_start="2027-01-01",
                                 closed_position=True, supporting=self.support)
        self.assertIsNone(plan["coverage"]["summary_from"])
        self.assertIsNone(plan["coverage"]["summary_to"])
        self.assertTrue(plan["coverage"]["closed_position"])
        migration.apply(self.entity, plan)
        entry = self.store.load_entries()[0]
        self.assertFalse(any(p.account.startswith(("Income:", "Expenses:")) for p in entry.postings))
        self.assertEqual(next(p.amount for p in entry.postings if p.account == "Equity:Retained-Earnings"), Decimal("-11800.00"))

    def test_period_lock_and_stale_ledger_preview_fail_without_partial_opening(self):
        plan = self.preview()
        _atomic_ledger_write(self.entity, [], [], "metadata-change", None, "Synthetic state change")
        # Audit-only events do not change the financial/support digest; a
        # canonical supporting change does, and is rejected as populated.
        from bookkeeping.accrual.store import SupportingMutation
        _atomic_ledger_write(self.entity, [], [], "period-change", None, "Synthetic period lock",
            supporting_mutations=[SupportingMutation("periods", "2026-06", {"id": "2026-06", "status": "closed"})])
        with self.assertRaisesRegex(ValueError, "closed"):
            migration.apply(self.entity, plan)
        self.assertEqual(self.store.counts().entries, 0)

    def test_populated_target_cannot_accept_opening(self):
        entry = Entry(date(2026, 7, 1), "Synthetic financial effect", postings=(
            Posting("Assets:Bank:Checking", Decimal("10.00")), Posting("Income:Services", Decimal("-10.00"))))
        _atomic_ledger_write(self.entity, [], [entry], "populated-fixture", None, "Synthetic populated fixture")
        with self.assertRaisesRegex(ValueError, "empty target"):
            self.preview()

    def test_accrual_tb_comparison_finds_offsetting_account_errors(self):
        migration.apply(self.entity, self.preview())
        material, immaterial = compare._compare_tb(self.entity, self.exports, date(2026, 6, 30), Decimal("100"), Decimal("1"))
        self.assertEqual((material, immaterial), ([], []))
        with self.store.transaction() as conn:
            conn.execute("UPDATE postings SET account='Assets:Bank:Wrong' WHERE account='Assets:Bank:Checking'")
        material, immaterial = compare._compare_tb(self.entity, self.exports, date(2026, 6, 30), Decimal("100"), Decimal("1"))
        self.assertEqual({d.account for d in material}, {"Assets:Bank:Checking", "Assets:Bank:Wrong"})
        self.assertEqual(immaterial, [])

    def test_inventory_default_remains_cash_and_explicit_basis_does_not_change_policy(self):
        before = (self.root / "entity.json").read_bytes()
        cash = quickbooks.inventory(self.exports, cutover=date(2026, 7, 1))
        accrual = quickbooks.inventory(self.exports, cutover=date(2026, 7, 1), expected_basis="accrual", fiscal_start=date(2026, 1, 1))
        self.assertFalse(cash.is_ready())
        self.assertTrue(accrual.is_ready(), accrual.to_dict())
        self.assertEqual((self.root / "entity.json").read_bytes(), before)
