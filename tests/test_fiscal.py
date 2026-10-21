"""Fiscal settings use reviewed, audited changes and calendar-safe boundaries."""
from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bookkeeping import fiscal
from bookkeeping.accrual.journals import JournalError
from bookkeeping.entity import init_entity, load_entity
from bookkeeping.ledger.importer import check_integrity
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.remote_contract import command_spec
from bookkeeping.preparation_runtime import supported_preparation_command
from bookkeeping.automation_policy import PreparationDenied
from golden_support import run_cli


class FiscalYearTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "company"
        init_entity(self.root, name="Synthetic Company")
        self.entity = load_entity(self.root)

    def test_preview_apply_idempotence_and_audit(self):
        argv = ["entity", "set-fiscal-year", str(self.root), "--start", "07-01"]
        original = (self.root / "entity.json").read_bytes()
        before = LedgerStore(self.root / "ledger.sqlite").content_digest()
        code, out, _ = run_cli(argv)
        self.assertEqual(code, 0)
        preview = json.loads(out)
        self.assertEqual((self.root / "entity.json").read_bytes(), original)
        self.assertEqual(LedgerStore(self.root / "ledger.sqlite").content_digest(), before)
        code, out, _ = run_cli(argv + ["--apply", "--plan-hash", preview["plan_hash"]])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "applied")
        self.assertEqual(load_entity(self.root).entity_config["fiscal_year_start"], "07-01")
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        events = LedgerStore(self.root / "ledger.sqlite").load_audit_events()
        self.assertIn("fiscal-year-set", [e["type"] for e in events])
        self.assertEqual(fiscal.apply(self.entity, "07-01")["status"], "already_applied")
        self.assertFalse(command_spec(argv)["mutates"])
        self.assertTrue(command_spec(argv + ["--apply"])["mutates"])
        with self.assertRaises(PreparationDenied):
            supported_preparation_command(argv + ["--apply"])

    def test_stale_and_invalid_settings_do_not_write(self):
        plan = fiscal.preview(self.entity, "07-01")
        fiscal.apply(self.entity, "04-01")
        with self.assertRaises(JournalError) as caught:
            fiscal.apply(self.entity, "07-01", plan["plan_hash"])
        self.assertEqual(caught.exception.code, "stale_preview")
        for value in ("2-01", "02-29", "04-31", "00-01", "13-01", "07-15", "52-week", None, 7):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fiscal.preview(self.entity, value)
        self.assertEqual(load_entity(self.root).entity_config["fiscal_year_start"], "04-01")

    def test_boundaries_and_leap_year(self):
        for day, expected in ((date(2026, 6, 30), date(2025, 7, 1)),
                              (date(2026, 7, 1), date(2026, 7, 1)),
                              (date(2028, 2, 29), date(2027, 7, 1))):
            self.assertEqual(fiscal.year_start(day, "07-01"), expected)
        from bookkeeping.accrual.schedules import default_tax_treatment
        record = {"kind": "prepaid", "origin_date": "2026-06-30", "service_start": "2026-08-01", "service_end": "2027-06-30"}
        self.assertEqual(default_tax_treatment(record, "07-01"), "expense_when_paid")
        record["service_end"] = "2027-07-01"
        self.assertEqual(default_tax_treatment(record, "07-01"), "capitalize")


    def test_imported_summary_calendar_cannot_be_reinterpreted(self):
        import golden_companies
        root = Path(self.temp.name) / "imported"
        golden_companies.build_quickbooks(root)
        entity = load_entity(root)
        before = (root / "entity.json").read_bytes()
        with self.assertRaises(JournalError) as caught:
            fiscal.apply(entity, "07-01")
        self.assertEqual(caught.exception.code, "fiscal_year_conflicts_with_opening")
        self.assertEqual((root / "entity.json").read_bytes(), before)

    def test_file_failure_does_not_append_audit(self):
        from unittest.mock import patch
        store = LedgerStore(self.root / "ledger.sqlite")
        before = store.load_audit_events()
        original = (self.root / "entity.json").read_bytes()
        with patch("os.replace", side_effect=OSError("synthetic rename failure")):
            with self.assertRaises(OSError):
                fiscal.apply(self.entity, "07-01")
        self.assertEqual(store.load_audit_events(), before)
        self.assertEqual((self.root / "entity.json").read_bytes(), original)
        self.assertFalse((self.root / "entity.json.fiscal.tmp").exists())
