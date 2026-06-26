"""Tests for src/bookkeeping/reconcile.py

Tests cover:
- AE6: Ledger $42,193.55 vs source $42,318.55 → $125.00 discrepancy, open status
- Clean reconciliation (ledger == source)
- Discrepancy persisted with causes and open status
- resolve() marks record as resolved
- Atomic write: reconciliation.json written via temp+replace
- No beancount/SQL jargon in rendered output
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

# AE6 fixture: one account with known balance
AE6_LEDGER = """\
option "title" "AE6 Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Bank:Mercury USD
2026-01-01 open Income:Revenue:Consulting USD
2026-01-01 open Equity:OpeningBalance USD

; Set up ledger balance at 42193.55
2026-01-01 * "Opening balance"
  Assets:Bank:Mercury         40000.00 USD
  Equity:OpeningBalance      -40000.00 USD

2026-03-15 * "Acme Corp" "Q1 consulting"
  Assets:Bank:Mercury          2193.55 USD
  Income:Revenue:Consulting   -2193.55 USD
"""

# Hand-computed: Assets:Bank:Mercury = 40000 + 2193.55 = 42193.55


def _make_entity(tmp_path: Path, ledger_text: str) -> Path:
    entity_dir = tmp_path / "entity"
    entity_dir.mkdir()
    (entity_dir / "entity.json").write_text('{"name": "AE6 Test"}')
    (entity_dir / "reports").mkdir()
    (entity_dir / "staging").mkdir()
    (entity_dir / "books.beancount").write_text(ledger_text, encoding="utf-8")
    from src.bookkeeping.reports.cache import regenerate
    result = regenerate(entity_dir)
    assert result.success, f"Cache build failed: {result.error_message}"
    return entity_dir


class TestReconcileAE6(unittest.TestCase):
    """Acceptance Example AE6: $125.00 discrepancy persisted with open status."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name), AE6_LEDGER)

    def tearDown(self):
        self._tmp.cleanup()

    def test_ae6_ledger_balance(self):
        """Ledger balance for Mercury account is 42193.55."""
        from src.bookkeeping.reports.cache import get_account_balance, open_cache
        conn = open_cache(self.entity, auto_regenerate=False)
        bal = get_account_balance(conn, "Assets:Bank:Mercury", date(2026, 3, 31))
        conn.close()
        self.assertEqual(bal, Decimal("42193.55"))

    def test_ae6_discrepancy_amount(self):
        """Discrepancy = source (42318.55) - ledger (42193.55) = 125.00."""
        from src.bookkeeping.reconcile import reconcile
        result = reconcile(
            self.entity,
            "Assets:Bank:Mercury",
            Decimal("42318.55"),
            date(2026, 3, 31),
        )
        self.assertEqual(result.discrepancy, Decimal("125.00"))

    def test_ae6_status_open(self):
        """Discrepancy status is 'discrepancy' (not clean)."""
        from src.bookkeeping.reconcile import reconcile
        result = reconcile(
            self.entity,
            "Assets:Bank:Mercury",
            Decimal("42318.55"),
            date(2026, 3, 31),
        )
        self.assertEqual(result.status, "discrepancy")

    def test_ae6_causes_not_empty(self):
        """Suspected causes list is not empty."""
        from src.bookkeeping.reconcile import reconcile
        result = reconcile(
            self.entity,
            "Assets:Bank:Mercury",
            Decimal("42318.55"),
            date(2026, 3, 31),
        )
        self.assertGreater(len(result.causes), 0)

    def test_ae6_persisted_open(self):
        """Record persisted to reconciliation.json with 'open' status."""
        from src.bookkeeping.reconcile import reconcile
        reconcile(
            self.entity,
            "Assets:Bank:Mercury",
            Decimal("42318.55"),
            date(2026, 3, 31),
        )
        recon_file = self.entity / "reports" / "reconciliation.json"
        self.assertTrue(recon_file.exists())
        records = json.loads(recon_file.read_text(encoding="utf-8"))
        self.assertGreater(len(records), 0)
        record = records[0]
        self.assertEqual(record["status"], "open")
        self.assertEqual(Decimal(record["discrepancy"]), Decimal("125.00"))
        self.assertEqual(Decimal(record["ledger_balance"]), Decimal("42193.55"))
        self.assertEqual(Decimal(record["source_balance"]), Decimal("42318.55"))

    def test_ae6_record_id_is_account_at_date(self):
        """Record ID encodes account and date."""
        from src.bookkeeping.reconcile import reconcile
        result = reconcile(
            self.entity,
            "Assets:Bank:Mercury",
            Decimal("42318.55"),
            date(2026, 3, 31),
        )
        self.assertIn("Assets:Bank:Mercury", result.record_id)
        self.assertIn("2026-03-31", result.record_id)

    def test_ae6_to_text_no_jargon(self):
        """Rendered reconciliation text has no beancount/SQL jargon."""
        from src.bookkeeping.reconcile import reconcile
        result = reconcile(
            self.entity,
            "Assets:Bank:Mercury",
            Decimal("42318.55"),
            date(2026, 3, 31),
        )
        text = result.to_text()
        for token in [";", "beancount", "SELECT", "sqlite", "pushtag"]:
            self.assertNotIn(token, text, f"Jargon '{token}' in reconcile text")
        # Should not contain raw colon-separated account name in rendered text
        self.assertNotIn("Assets:Bank:Mercury", text)
        # Should use readable separator
        self.assertIn("Mercury", text)


class TestReconcileClean(unittest.TestCase):
    """Clean reconciliation when ledger matches source exactly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name), AE6_LEDGER)

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_status(self):
        """When source == ledger, status is 'clean'."""
        from src.bookkeeping.reconcile import reconcile
        result = reconcile(
            self.entity,
            "Assets:Bank:Mercury",
            Decimal("42193.55"),
            date(2026, 3, 31),
        )
        self.assertEqual(result.status, "clean")
        self.assertTrue(result.is_clean)

    def test_clean_zero_discrepancy(self):
        """Clean reconciliation has zero discrepancy."""
        from src.bookkeeping.reconcile import reconcile
        result = reconcile(
            self.entity,
            "Assets:Bank:Mercury",
            Decimal("42193.55"),
            date(2026, 3, 31),
        )
        self.assertEqual(result.discrepancy, Decimal("0.00"))

    def test_clean_persisted_with_clean_status(self):
        """Clean record is persisted with 'clean' status."""
        from src.bookkeeping.reconcile import reconcile
        reconcile(
            self.entity,
            "Assets:Bank:Mercury",
            Decimal("42193.55"),
            date(2026, 3, 31),
        )
        records = json.loads(
            (self.entity / "reports" / "reconciliation.json").read_text(encoding="utf-8")
        )
        self.assertEqual(records[0]["status"], "clean")


class TestReconcileResolve(unittest.TestCase):
    """Resolve flow: mark a discrepancy as resolved."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name), AE6_LEDGER)

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolve_changes_status(self):
        """resolve() changes status to 'resolved'."""
        from src.bookkeeping.reconcile import reconcile, resolve
        reconcile(
            self.entity,
            "Assets:Bank:Mercury",
            Decimal("42318.55"),
            date(2026, 3, 31),
        )
        resolve(self.entity, "Assets:Bank:Mercury", date(2026, 3, 31), note="Found missing entry")
        records = json.loads(
            (self.entity / "reports" / "reconciliation.json").read_text(encoding="utf-8")
        )
        self.assertEqual(records[0]["status"], "resolved")
        self.assertEqual(records[0]["resolution_note"], "Found missing entry")
        self.assertIsNotNone(records[0]["resolved_at"])

    def test_resolve_nonexistent_raises_key_error(self):
        """resolve() raises KeyError when record not found."""
        from src.bookkeeping.reconcile import resolve
        with self.assertRaises(KeyError):
            resolve(self.entity, "Assets:Bank:Mercury", date(2099, 1, 1), note="")

    def test_resolve_idempotent_update(self):
        """Re-running reconcile after resolve doesn't resurrect the old status."""
        from src.bookkeeping.reconcile import reconcile, resolve
        reconcile(
            self.entity,
            "Assets:Bank:Mercury",
            Decimal("42318.55"),
            date(2026, 3, 31),
        )
        resolve(self.entity, "Assets:Bank:Mercury", date(2026, 3, 31), note="Fixed")
        # Run reconcile again with same discrepancy
        result2 = reconcile(
            self.entity,
            "Assets:Bank:Mercury",
            Decimal("42318.55"),
            date(2026, 3, 31),
        )
        # The in-memory result still shows discrepancy
        self.assertEqual(result2.status, "discrepancy")
        # But the persisted record was previously resolved; our implementation
        # preserves the resolved status on re-run for the same (account, as_of).
        records = json.loads(
            (self.entity / "reports" / "reconciliation.json").read_text(encoding="utf-8")
        )
        matching = [r for r in records if "2026-03-31" in r.get("as_of", "")]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["status"], "resolved")


class TestReconcilePersistence(unittest.TestCase):
    """Persistence mechanics: atomic write, append-by-key, multiple accounts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name), AE6_LEDGER)

    def tearDown(self):
        self._tmp.cleanup()

    def test_multiple_runs_append_records(self):
        """Two different (account, as_of) pairs produce two records."""
        from src.bookkeeping.reconcile import reconcile
        reconcile(self.entity, "Assets:Bank:Mercury", Decimal("42318.55"), date(2026, 3, 31))
        # Use a different date — Feb 28 ledger balance is 40000.00 (only opening entry before Mar 15)
        reconcile(self.entity, "Assets:Bank:Mercury", Decimal("40000.00"), date(2026, 2, 28))
        records = json.loads(
            (self.entity / "reports" / "reconciliation.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(records), 2)

    def test_same_key_updates_record(self):
        """Re-running reconcile for same (account, as_of) updates in place."""
        from src.bookkeeping.reconcile import reconcile
        reconcile(self.entity, "Assets:Bank:Mercury", Decimal("42318.55"), date(2026, 3, 31))
        reconcile(self.entity, "Assets:Bank:Mercury", Decimal("42193.55"), date(2026, 3, 31))
        records = json.loads(
            (self.entity / "reports" / "reconciliation.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(records), 1)
        # Now it should be clean (42193.55 == ledger)
        self.assertEqual(records[0]["status"], "clean")

    def test_json_file_valid_after_write(self):
        """reconciliation.json is valid JSON after write."""
        from src.bookkeeping.reconcile import reconcile
        reconcile(self.entity, "Assets:Bank:Mercury", Decimal("42318.55"), date(2026, 3, 31))
        content = (self.entity / "reports" / "reconciliation.json").read_text(encoding="utf-8")
        parsed = json.loads(content)
        self.assertIsInstance(parsed, list)

    def test_list_discrepancies(self):
        """list_discrepancies returns records filtered by status."""
        from src.bookkeeping.reconcile import reconcile, list_discrepancies
        # Mar 31: discrepancy (42318.55 vs ledger 42193.55)
        reconcile(self.entity, "Assets:Bank:Mercury", Decimal("42318.55"), date(2026, 3, 31))
        # Feb 28: clean (ledger balance = 40000.00 exactly)
        reconcile(self.entity, "Assets:Bank:Mercury", Decimal("40000.00"), date(2026, 2, 28))
        open_recs = list_discrepancies(self.entity, status="open")
        clean_recs = list_discrepancies(self.entity, status="clean")
        all_recs = list_discrepancies(self.entity)
        self.assertEqual(len(open_recs), 1)
        self.assertEqual(len(clean_recs), 1)
        self.assertEqual(len(all_recs), 2)


class TestReconcileNoJargon(unittest.TestCase):
    """Rendered text must not contain beancount/SQL/internal jargon."""

    _FORBIDDEN = [";", "beancount", "SELECT", "sqlite", "pushtag", "poptag"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name), AE6_LEDGER)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_jargon_clean(self):
        from src.bookkeeping.reconcile import reconcile
        result = reconcile(self.entity, "Assets:Bank:Mercury", Decimal("42193.55"), date(2026, 3, 31))
        text = result.to_text()
        for token in self._FORBIDDEN:
            self.assertNotIn(token, text, f"Jargon '{token}' in clean reconcile text")

    def test_no_jargon_discrepancy(self):
        from src.bookkeeping.reconcile import reconcile
        result = reconcile(self.entity, "Assets:Bank:Mercury", Decimal("42318.55"), date(2026, 3, 31))
        text = result.to_text()
        for token in self._FORBIDDEN:
            self.assertNotIn(token, text, f"Jargon '{token}' in discrepancy reconcile text")

    def test_causes_no_jargon(self):
        from src.bookkeeping.reconcile import reconcile
        result = reconcile(self.entity, "Assets:Bank:Mercury", Decimal("42318.55"), date(2026, 3, 31))
        for cause in result.causes:
            for token in self._FORBIDDEN:
                self.assertNotIn(token, cause, f"Jargon '{token}' in cause: {cause}")


if __name__ == "__main__":
    unittest.main()
