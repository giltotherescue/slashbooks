"""Tests for src/bookkeeping/reports/cache.py

Tests cover:
- Cache regeneration from valid ledger
- Validation gate: invalid ledger halts, previous cache intact
- Simulated mid-population crash leaves old cache readable
- Stale-cache detection via sha256
- Hand-edit ledger → regenerate → statements reflect edit
- No beancount/SQL jargon in any meta output
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

# Fixture ledger text — 10 balanced entries with known hand-computed values:
#
# Revenue (Income:Revenue:Consulting): credits = -5000.00 - 3000.00 - 2000.00 = -10000
#   Displayed as positive 10000.00
# Expenses (Expenses:Software): debits = 200.00 + 150.00 = 350.00
# Expenses (Expenses:Office): debit = 100.00
# Total expenses = 450.00
# Net income = 10000.00 - 450.00 = 9550.00
#
# Assets:Bank:Checking balance at end:
#   +5000.00 +3000.00 +2000.00 -200.00 -150.00 -100.00 = 9550.00
# Equity:OpeningBalance: -500.00 (credit)
# Liabilities:CreditCard: -200.00 (credit, i.e. 200 owed)
#   after payment: -200.00 + 200.00 = 0
#
# We use 10 entries total.

FIXTURE_LEDGER = """\
option "title" "Test Entity"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Bank:Checking USD
2026-01-01 open Income:Revenue:Consulting USD
2026-01-01 open Expenses:Software USD
2026-01-01 open Expenses:Office USD
2026-01-01 open Equity:OpeningBalance USD
2026-01-01 open Liabilities:CreditCard USD

; Entry 1: Opening balance
2026-01-01 * "Opening balance"
  Assets:Bank:Checking         500.00 USD
  Equity:OpeningBalance       -500.00 USD

; Entry 2: Revenue January
2026-01-15 * "Acme Corp" "Consulting January"
  Assets:Bank:Checking        5000.00 USD
  Income:Revenue:Consulting  -5000.00 USD

; Entry 3: Software subscription
2026-01-20 * "Acme Software" "Monthly subscription"
  Expenses:Software            200.00 USD
  Assets:Bank:Checking        -200.00 USD

; Entry 4: Revenue February
2026-02-15 * "Acme Corp" "Consulting February"
  Assets:Bank:Checking        3000.00 USD
  Income:Revenue:Consulting  -3000.00 USD

; Entry 5: Office supplies
2026-02-20 * "Office Depot" "Paper and pens"
  Expenses:Office              100.00 USD
  Assets:Bank:Checking        -100.00 USD

; Entry 6: Revenue March
2026-03-15 * "Acme Corp" "Consulting March"
  Assets:Bank:Checking        2000.00 USD
  Income:Revenue:Consulting  -2000.00 USD

; Entry 7: Software Q2
2026-03-20 * "Acme Software" "Q1 software renewal"
  Expenses:Software            150.00 USD
  Assets:Bank:Checking        -150.00 USD

; Entry 8: Credit card charge
2026-03-25 * "Amazon" "Server costs"
  Expenses:Software            200.00 USD
  Liabilities:CreditCard      -200.00 USD

; Entry 9: Credit card payment
2026-03-31 * "Credit card payment"
  Liabilities:CreditCard       200.00 USD
  Assets:Bank:Checking        -200.00 USD

; Entry 10: Revenue April
2026-04-15 * "Beta LLC" "Project work"
  Assets:Bank:Checking        1500.00 USD
  Income:Revenue:Consulting  -1500.00 USD
"""

# Known values for hand-computed assertions
# Q1 (Jan-Mar) revenue = 5000 + 3000 + 2000 = 10000
# Q1 expenses = 200 (software) + 100 (office) + 150 (software) + 200 (software CC) = 650
# Q1 net income = 10000 - 650 = 9350
# Full period (Jan-Apr) revenue = 10000 + 1500 = 11500
# Full period (Jan-Apr) expenses = 650
# Full period net income = 11500 - 650 = 10850
# Assets:Bank:Checking as of 2026-04-30:
#   500 + 5000 - 200 + 3000 - 100 + 2000 - 150 - 200 + 1500 = 11350
# Assets:Bank:Checking as of 2026-03-31:
#   500 + 5000 - 200 + 3000 - 100 + 2000 - 150 - 200 = 9850
# Liabilities:CreditCard as of 2026-03-31:
#   -200 + 200 = 0 (net zero after payment)
# Equity:OpeningBalance = -500
# Trial balance: sum of all balances must be zero


def _make_entity(tmp_path: Path, ledger_text: str = FIXTURE_LEDGER) -> Path:
    """Create a minimal entity directory with books.beancount."""
    entity_dir = tmp_path / "entity"
    entity_dir.mkdir()
    (entity_dir / "entity.json").write_text('{"name": "Test Entity"}')
    (entity_dir / "reports").mkdir()
    (entity_dir / "staging").mkdir()
    (entity_dir / "books.beancount").write_text(ledger_text, encoding="utf-8")
    return entity_dir


class TestCacheRegenerate(unittest.TestCase):
    def test_regenerate_success(self):
        """Happy path: regenerate produces cache.sqlite."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports.cache import regenerate
            result = regenerate(entity)
            self.assertTrue(result.success, f"Expected success: {result.error_message}")
            self.assertTrue((entity / "reports" / "cache.sqlite").exists())
            self.assertEqual(len(result.validation_errors), 0)
            self.assertNotEqual(result.ledger_sha256, "")

    def test_regenerate_schema_tables(self):
        """Cache has the expected schema tables."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports.cache import regenerate
            regenerate(entity)
            conn = sqlite3.connect(str(entity / "reports" / "cache.sqlite"))
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            conn.close()
            self.assertIn("meta", tables)
            self.assertIn("accounts", tables)
            self.assertIn("entries", tables)
            self.assertIn("postings", tables)
            self.assertIn("balance_assertions", tables)

    def test_regenerate_accounts_populated(self):
        """Cache accounts table contains the expected accounts."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports.cache import regenerate
            regenerate(entity)
            conn = sqlite3.connect(str(entity / "reports" / "cache.sqlite"))
            names = {r[0] for r in conn.execute("SELECT name FROM accounts").fetchall()}
            conn.close()
            self.assertIn("Assets:Bank:Checking", names)
            self.assertIn("Income:Revenue:Consulting", names)
            self.assertIn("Expenses:Software", names)

    def test_regenerate_entries_count(self):
        """Cache entries table has 10 entries."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports.cache import regenerate
            regenerate(entity)
            conn = sqlite3.connect(str(entity / "reports" / "cache.sqlite"))
            count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            conn.close()
            self.assertEqual(count, 10)

    def test_regenerate_amounts_as_text_decimal(self):
        """Posting amounts are stored as TEXT decimal strings (not floats)."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports.cache import regenerate
            regenerate(entity)
            conn = sqlite3.connect(str(entity / "reports" / "cache.sqlite"))
            samples = conn.execute("SELECT amount FROM postings LIMIT 5").fetchall()
            conn.close()
            for row in samples:
                val = row[0]
                self.assertIsInstance(val, str, "Amount must be TEXT, not float")
                # Should parse as Decimal without loss
                d = Decimal(val)
                self.assertEqual(d.quantize(Decimal("0.01")), d)

    def test_regenerate_meta_sha256(self):
        """Meta table stores ledger_sha256 matching the file."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports.cache import regenerate
            result = regenerate(entity)
            conn = sqlite3.connect(str(entity / "reports" / "cache.sqlite"))
            row = conn.execute("SELECT value FROM meta WHERE key='ledger_sha256'").fetchone()
            conn.close()
            self.assertEqual(row[0], result.ledger_sha256)
            # Verify against file hash
            file_sha = hashlib.sha256(
                (entity / "books.beancount").read_bytes()
            ).hexdigest()
            self.assertEqual(result.ledger_sha256, file_sha)

    def test_regenerate_falls_back_when_entity_mount_rejects_sqlite(self):
        """SQLite disk I/O failures under reports/ fall back to a temp cache."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            cache_root = Path(tmp) / "cache-root"
            from src.bookkeeping.reports import cache as cache_mod

            real_connect = sqlite3.connect

            def connect_or_fail(path, *args, **kwargs):
                if str(entity / "reports") in str(path):
                    raise sqlite3.OperationalError("disk I/O error")
                return real_connect(path, *args, **kwargs)

            with patch.dict(os.environ, {"BOOKS_CACHE_DIR": str(cache_root)}):
                with patch.object(cache_mod.sqlite3, "connect", side_effect=connect_or_fail):
                    result = cache_mod.regenerate(entity)
                    self.assertTrue(result.success, result.error_message)
                    self.assertTrue(result.cache_path.startswith(str(cache_root)))
                    self.assertFalse((entity / "reports" / "cache.sqlite").exists())
                    self.assertFalse(cache_mod.is_stale(entity))

                    conn = cache_mod.open_cache(entity, auto_regenerate=False)
                    count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                    conn.close()
                    self.assertEqual(count, 10)


class TestCacheValidationGate(unittest.TestCase):
    def test_invalid_ledger_halts_no_cache_produced(self):
        """Invalid ledger halts, no cache.sqlite produced."""
        # Use an unbalanced entry (off by 50.00) to trigger a real validation error
        bad_ledger = """\
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"
2026-01-01 open Assets:Bank:Checking USD
2026-01-01 open Income:Revenue:Consulting USD
2026-01-15 * "Unbalanced entry"
  Assets:Bank:Checking        100.00 USD
  Income:Revenue:Consulting   -50.00 USD
"""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp), ledger_text=bad_ledger)
            from src.bookkeeping.reports.cache import regenerate
            # Remove any pre-existing cache
            cache_path = entity / "reports" / "cache.sqlite"
            if cache_path.exists():
                cache_path.unlink()
            result = regenerate(entity)
            self.assertFalse(result.success)
            self.assertFalse(cache_path.exists())

    def test_invalid_ledger_previous_cache_intact(self):
        """Invalid ledger: previous cache.sqlite is untouched and readable."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports.cache import regenerate
            # Build a valid cache first
            result1 = regenerate(entity)
            self.assertTrue(result1.success)
            cache_path = entity / "reports" / "cache.sqlite"
            old_mtime = cache_path.stat().st_mtime

            # Now replace ledger with an unbalanced entry (real validation error)
            bad_ledger = """\
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"
2026-01-01 open Assets:Bank:Checking USD
2026-01-01 open Income:Revenue:Consulting USD
2026-01-15 * "Unbalanced entry"
  Assets:Bank:Checking        100.00 USD
  Income:Revenue:Consulting   -50.00 USD
"""
            (entity / "books.beancount").write_text(bad_ledger, encoding="utf-8")
            result2 = regenerate(entity)
            self.assertFalse(result2.success)

            # Old cache still exists and is readable
            self.assertTrue(cache_path.exists())
            conn = sqlite3.connect(str(cache_path))
            count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            conn.close()
            self.assertEqual(count, 10)

    def test_invalid_ledger_reports_error_info(self):
        """Failed CacheResult has non-empty error_message."""
        with tempfile.TemporaryDirectory() as tmp:
            # Ledger with unbalanced entry
            bad_ledger = """\
option "operating_currency" "USD"
2026-01-01 open Assets:Bank:Checking USD
2026-01-01 open Income:Revenue:Consulting USD
2026-01-15 * "Unbalanced"
  Assets:Bank:Checking   100.00 USD
  Income:Revenue:Consulting -50.00 USD
"""
            entity = _make_entity(Path(tmp), ledger_text=bad_ledger)
            from src.bookkeeping.reports.cache import regenerate
            result = regenerate(entity)
            self.assertFalse(result.success)
            self.assertGreater(len(result.error_message), 0)

    def test_mid_population_crash_leaves_old_cache_readable(self):
        """Simulated crash during _populate leaves old cache.sqlite intact."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports import cache as cache_mod

            # Build valid cache first
            result1 = cache_mod.regenerate(entity)
            self.assertTrue(result1.success)

            # Verify old cache is readable
            cache_path = entity / "reports" / "cache.sqlite"
            conn = sqlite3.connect(str(cache_path))
            old_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            conn.close()
            self.assertEqual(old_count, 10)

            # Simulate crash during _populate by patching it to raise
            original_populate = cache_mod._populate

            def crashing_populate(conn, parsed, sha256):
                raise RuntimeError("Simulated crash mid-population")

            with patch.object(cache_mod, "_populate", crashing_populate):
                result2 = cache_mod.regenerate(entity)

            self.assertFalse(result2.success)

            # Old cache still intact
            self.assertTrue(cache_path.exists())
            conn = sqlite3.connect(str(cache_path))
            count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            conn.close()
            self.assertEqual(count, 10)

            # Temp file was cleaned up
            new_path = entity / "reports" / "cache_new.sqlite"
            self.assertFalse(new_path.exists())


class TestCacheStaleness(unittest.TestCase):
    def test_is_stale_absent(self):
        """is_stale returns True when cache.sqlite absent."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports.cache import is_stale
            self.assertTrue(is_stale(entity))

    def test_is_stale_after_edit(self):
        """is_stale returns True after ledger file is edited."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports.cache import regenerate, is_stale
            regenerate(entity)
            self.assertFalse(is_stale(entity))
            # Edit the ledger
            books = entity / "books.beancount"
            books.write_text(books.read_text(encoding="utf-8") + "\n; edited\n", encoding="utf-8")
            self.assertTrue(is_stale(entity))

    def test_is_not_stale_after_regenerate(self):
        """is_stale returns False immediately after regeneration."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports.cache import regenerate, is_stale
            regenerate(entity)
            self.assertFalse(is_stale(entity))


class TestCacheEditReflected(unittest.TestCase):
    def test_edit_ledger_regenerate_reflects(self):
        """Hand-editing the ledger then regenerating reflects edit in postings."""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports.cache import regenerate, iter_postings, open_cache

            regenerate(entity)

            # Add a new revenue entry
            new_entry = """\n2026-04-30 * "Extra Client" "Bonus work"\n  Assets:Bank:Checking        9999.00 USD\n  Income:Revenue:Consulting  -9999.00 USD\n"""
            books = entity / "books.beancount"
            books.write_text(books.read_text(encoding="utf-8") + new_entry, encoding="utf-8")

            result = regenerate(entity)
            self.assertTrue(result.success)

            conn = open_cache(entity, auto_regenerate=False)
            amounts = [row[4] for row in iter_postings(conn, account="Income:Revenue:Consulting")]
            conn.close()
            self.assertIn(Decimal("-9999.00"), amounts)


class TestCacheQueryHelpers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name))
        from src.bookkeeping.reports.cache import regenerate
        regenerate(self.entity)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_account_balance_checking(self):
        """Assets:Bank:Checking balance as of 2026-03-31 matches hand-computed value."""
        from src.bookkeeping.reports.cache import get_account_balance, open_cache
        conn = open_cache(self.entity, auto_regenerate=False)
        bal = get_account_balance(conn, "Assets:Bank:Checking", date(2026, 3, 31))
        conn.close()
        # 500 + 5000 - 200 + 3000 - 100 + 2000 - 150 - 200 = 9850
        self.assertEqual(bal, Decimal("9850.00"))

    def test_get_account_balance_income(self):
        """Income:Revenue:Consulting balance as of 2026-03-31."""
        from src.bookkeeping.reports.cache import get_account_balance, open_cache
        conn = open_cache(self.entity, auto_regenerate=False)
        bal = get_account_balance(conn, "Income:Revenue:Consulting", date(2026, 3, 31))
        conn.close()
        # -5000 - 3000 - 2000 = -10000 (credit-normal)
        self.assertEqual(bal, Decimal("-10000.00"))

    def test_iter_postings_filters_by_date(self):
        """iter_postings with date filter returns only matching rows."""
        from src.bookkeeping.reports.cache import iter_postings, open_cache
        conn = open_cache(self.entity, auto_regenerate=False)
        rows = list(iter_postings(conn, from_date=date(2026, 2, 1), to_date=date(2026, 2, 28)))
        conn.close()
        dates = {row[0] for row in rows}
        self.assertTrue(all(d >= "2026-02-01" and d <= "2026-02-28" for d in dates))
        # Should include Feb 15 (revenue) and Feb 20 (office)
        self.assertGreater(len(rows), 0)

    def test_iter_postings_boundary_inclusive(self):
        """Boundary dates are included exactly once."""
        from src.bookkeeping.reports.cache import iter_postings, open_cache
        conn = open_cache(self.entity, auto_regenerate=False)
        # 2026-01-15 should be included when from=2026-01-15
        rows_with = list(iter_postings(conn, from_date=date(2026, 1, 15), to_date=date(2026, 1, 15)))
        rows_without = list(iter_postings(conn, from_date=date(2026, 1, 16), to_date=date(2026, 1, 31)))
        conn.close()
        self.assertGreater(len(rows_with), 0)
        dates_without = {row[0] for row in rows_without}
        self.assertNotIn("2026-01-15", dates_without)


class TestNoJargonInCacheOutput(unittest.TestCase):
    """Cache meta and rendered outputs must not contain beancount/SQL jargon."""

    _FORBIDDEN = [";", "beancount", "SELECT", "sqlite", "pushtag", "poptag"]

    def test_no_jargon_in_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            from src.bookkeeping.reports.cache import regenerate, open_cache
            regenerate(entity)
            conn = open_cache(entity, auto_regenerate=False)
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            conn.close()
            for key, value in rows:
                for forbidden in self._FORBIDDEN:
                    self.assertNotIn(forbidden, key, f"Jargon '{forbidden}' found in meta key: {key}")
                    self.assertNotIn(forbidden, value or "", f"Jargon '{forbidden}' found in meta value: {value}")


if __name__ == "__main__":
    unittest.main()
