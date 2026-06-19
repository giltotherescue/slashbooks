"""Tests for src/bookkeeping/connectors/csvsource.py (Unit U7).

Covers:
- Normalized contract: every key present and correct type/value
- Fingerprint stability across re-parses
- Charge (positive) vs payment/credit (negative) sign convention
- Boundary filtering: side=before includes boundary row, excludes later
- Boundary filtering: side=after mirror
- Excluded count reported correctly
- Unknown header → ValueError naming expected columns
- Propose → confirm round-trip writes entity.json
- Parse without confirmed mapping refuses and proposes
- Leading apostrophe stripped from Reference
- Empty Category tolerated (emits None)
- Deterministic fingerprint recipe (manual verification)
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import textwrap
import unittest
from decimal import Decimal
from pathlib import Path

from bookkeeping.connectors.csvsource import (
    _EXPECTED_COLUMNS,
    _fingerprint,
    _propose_mapping,
    _strip_reference,
    import_csv,
    parse_amex_csv,
    resolve_mapping,
    write_confirmed_mapping,
)


# ---------------------------------------------------------------------------
# Helpers / fixture paths
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "amex"
_FIXTURE_CSV = _FIXTURES / "activity.csv"

# The 12-column header exactly as Amex exports it
_AMEX_HEADER = (
    "Date,Receipt,Description,Amount,Extended Details,"
    "Appears On Your Statement As,Address,City/State,"
    "Zip Code,Country,Reference,Category"
)

# A minimal valid row template (charge)
# Note: Reference uses only a LEADING apostrophe (Amex CSV format: '320260690001122334,
# not '320260690001122334'). Using a raw string to avoid Python string quoting confusion.
_CHARGE_ROW = (
    "03/10/2026,,ACME SERVICES       SAN JOSE            CA,"
    "99.00,Extended detail here,ACME SERVICES,,,,,"
    "\x27320260690001122334,Software"  # \x27 = single apostrophe (leading only)
)

# A minimal valid row template (payment / credit — negative amount)
_PAYMENT_ROW = (
    "03/15/2026,,MOBILE PAYMENT - THANK YOU,"
    "-500.00,MOBILE PAYMENT - THANK YOU,MOBILE PAYMENT - THANK YOU,,,,,"
    "\x27320260740002233445,"  # \x27 = single apostrophe (leading only)
)


def _make_csv(*rows: str) -> str:
    """Build a CSV string from header + given row strings."""
    lines = [_AMEX_HEADER] + list(rows)
    return "\n".join(lines) + "\n"


def _write_csv(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


class TempDir(unittest.TestCase):
    """Base class that sets up / tears down a temp directory."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.tmp = Path(self._tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. Normalized contract: every key present and correct
# ---------------------------------------------------------------------------

class TestNormalizedContract(TempDir):
    """parse_amex_csv returns dicts matching the normalized transaction contract."""

    def test_all_keys_present_on_charge(self) -> None:
        """Every key from the normalized contract is present in a charge row."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))

        txns, excluded = parse_amex_csv(
            csv_path,
            account_id="Liabilities:CreditCard:Amex-CSV",
            account_name="Amex Card (CSV)",
            ledger_account="Liabilities:CreditCard:Amex-CSV",
        )
        self.assertEqual(excluded, 0)
        self.assertEqual(len(txns), 1)

        txn = txns[0]
        expected_keys = {
            "id", "date", "description", "originalDescription",
            "amount", "creditAmount", "debitAmount", "currency",
            "category", "type", "reference", "pending",
            "pendingTransactionId", "accountId", "accountName",
            "accountNumberLast4", "bankId", "bank",
        }
        self.assertEqual(set(txn.keys()), expected_keys)

    def test_charge_field_values(self) -> None:
        """Charge row has correct field values."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))

        txns, _ = parse_amex_csv(
            csv_path,
            account_id="Liabilities:CreditCard:Amex-CSV",
            account_name="Amex Card (CSV)",
            ledger_account="Liabilities:CreditCard:Amex-CSV",
        )
        txn = txns[0]

        self.assertEqual(txn["date"], "2026-03-10")
        self.assertEqual(txn["description"], "ACME SERVICES       SAN JOSE            CA")
        self.assertIsNotNone(txn["originalDescription"])
        self.assertEqual(txn["amount"], "-99.00")
        self.assertEqual(txn["debitAmount"], "99.00")
        self.assertIsNone(txn["creditAmount"])
        self.assertEqual(txn["currency"], "USD")
        self.assertEqual(txn["category"], "Software")
        self.assertEqual(txn["type"], "credit_card")
        self.assertEqual(txn["reference"], "320260690001122334")  # apostrophe stripped
        self.assertIs(txn["pending"], False)
        self.assertIsNone(txn["pendingTransactionId"])
        self.assertEqual(txn["accountId"], "Liabilities:CreditCard:Amex-CSV")
        self.assertEqual(txn["accountName"], "Amex Card (CSV)")
        self.assertIsNone(txn["accountNumberLast4"])
        self.assertEqual(txn["bankId"], "amex-csv")
        self.assertIsNone(txn["bank"])

    def test_id_prefix(self) -> None:
        """Transaction id starts with 'csv:'."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))
        txns, _ = parse_amex_csv(csv_path, account_id="Liabilities:CreditCard:Amex-CSV")
        self.assertTrue(txns[0]["id"].startswith("csv:"))

    def test_id_length(self) -> None:
        """Transaction id is 'csv:' + 16 hex chars = 20 chars total."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))
        txns, _ = parse_amex_csv(csv_path, account_id="Liabilities:CreditCard:Amex-CSV")
        txn_id = txns[0]["id"]
        self.assertEqual(len(txn_id), 20)  # 'csv:' (4) + 16 hex chars


# ---------------------------------------------------------------------------
# 2. Fingerprint stability
# ---------------------------------------------------------------------------

class TestFingerprintStability(TempDir):

    def test_same_row_same_fingerprint(self) -> None:
        """Parsing the same CSV twice produces identical fingerprints."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))

        acct = "Liabilities:CreditCard:Amex-CSV"
        txns1, _ = parse_amex_csv(csv_path, account_id=acct, ledger_account=acct)
        txns2, _ = parse_amex_csv(csv_path, account_id=acct, ledger_account=acct)

        self.assertEqual(txns1[0]["id"], txns2[0]["id"])

    def test_fingerprint_differs_for_different_amounts(self) -> None:
        """Different amounts produce different fingerprints."""
        row1 = _CHARGE_ROW  # 99.00
        row2 = row1.replace(",99.00,", ",100.00,")
        csv1 = self.tmp / "a.csv"
        csv2 = self.tmp / "b.csv"
        _write_csv(csv1, _make_csv(row1))
        _write_csv(csv2, _make_csv(row2))

        acct = "Liabilities:CreditCard:Amex-CSV"
        txns1, _ = parse_amex_csv(csv1, account_id=acct, ledger_account=acct)
        txns2, _ = parse_amex_csv(csv2, account_id=acct, ledger_account=acct)
        self.assertNotEqual(txns1[0]["id"], txns2[0]["id"])

    def test_fingerprint_differs_for_different_accounts(self) -> None:
        """Different account IDs produce different fingerprints for the same row."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))

        txns_a, _ = parse_amex_csv(
            csv_path,
            account_id="Liabilities:CreditCard:Amex-CSV",
            ledger_account="Liabilities:CreditCard:Amex-CSV",
        )
        txns_b, _ = parse_amex_csv(
            csv_path,
            account_id="Liabilities:CreditCard:Amex-Other",
            ledger_account="Liabilities:CreditCard:Amex-Other",
        )
        self.assertNotEqual(txns_a[0]["id"], txns_b[0]["id"])

    def test_deterministic_fingerprint_recipe(self) -> None:
        """Fingerprint matches manual computation of the documented recipe."""
        from bookkeeping.ledger.normalize import normalize_description

        account_id = "Liabilities:CreditCard:Amex-CSV"
        iso_date = "2026-03-10"
        amount = Decimal("99.00")
        description = "ACME SERVICES       SAN JOSE            CA"
        stripped_ref = "320260690001122334"

        norm_desc = normalize_description(description)
        amount_str = format(amount, "f")
        payload = f"{account_id}|{iso_date}|{amount_str}|{norm_desc}|{stripped_ref}"
        expected = "csv:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

        actual = _fingerprint(account_id, iso_date, amount, description, stripped_ref)
        self.assertEqual(actual, expected)

    def test_fixture_csv_stable(self) -> None:
        """Fixture CSV fingerprints are stable across two consecutive parses."""
        acct = "Liabilities:CreditCard:Amex-CSV"
        txns1, _ = parse_amex_csv(_FIXTURE_CSV, account_id=acct, ledger_account=acct)
        txns2, _ = parse_amex_csv(_FIXTURE_CSV, account_id=acct, ledger_account=acct)
        ids1 = [t["id"] for t in txns1]
        ids2 = [t["id"] for t in txns2]
        self.assertEqual(ids1, ids2)


# ---------------------------------------------------------------------------
# 3. Sign convention
# ---------------------------------------------------------------------------

class TestSignConvention(TempDir):

    def test_charge_positive_amount_debit(self) -> None:
        """Positive CSV amount (charge) → NEGATIVE feed-axis amount, debitAmount set."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))  # amount = 99.00
        txns, _ = parse_amex_csv(csv_path, account_id="Liabilities:CreditCard:Amex-CSV")
        txn = txns[0]
        self.assertEqual(txn["amount"], "-99.00")
        self.assertEqual(txn["debitAmount"], "99.00")
        self.assertIsNone(txn["creditAmount"])

    def test_payment_negative_amount_credit(self) -> None:
        """Negative CSV amount (payment) → POSITIVE feed-axis amount, creditAmount = abs."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_PAYMENT_ROW))  # amount = -500.00
        txns, _ = parse_amex_csv(csv_path, account_id="Liabilities:CreditCard:Amex-CSV")
        txn = txns[0]
        self.assertEqual(txn["amount"], "500.00")
        self.assertIsNone(txn["debitAmount"])
        self.assertEqual(txn["creditAmount"], "500.00")

    def test_both_rows_together(self) -> None:
        """Mixed charge + payment in same file, both directions correct."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW, _PAYMENT_ROW))
        txns, _ = parse_amex_csv(csv_path, account_id="Liabilities:CreditCard:Amex-CSV")
        self.assertEqual(len(txns), 2)

        charge = next(t for t in txns if t["amount"] == "-99.00")
        payment = next(t for t in txns if t["amount"] == "500.00")

        self.assertEqual(charge["debitAmount"], "99.00")
        self.assertIsNone(charge["creditAmount"])

        self.assertEqual(payment["creditAmount"], "500.00")
        self.assertIsNone(payment["debitAmount"])

    def test_zero_amount_treated_as_charge(self) -> None:
        """Zero-amount row has debitAmount='0.00', creditAmount=None."""
        row = _CHARGE_ROW.replace(",99.00,", ",0.00,")
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(row))
        txns, _ = parse_amex_csv(csv_path, account_id="Liabilities:CreditCard:Amex-CSV")
        txn = txns[0]
        self.assertEqual(txn["amount"], "0.00")
        self.assertEqual(txn["debitAmount"], "0.00")
        self.assertIsNone(txn["creditAmount"])


# ---------------------------------------------------------------------------
# 4. Boundary filtering
# ---------------------------------------------------------------------------

_BEFORE_BOUNDARY_ROW = (
    "03/10/2026,,BEFORE BOUNDARY INC,25.00,,,,,,"
    ",\x27320260690011111111,Software"
)
_ON_BOUNDARY_ROW = (
    "03/15/2026,,ON BOUNDARY INC,50.00,,,,,,"
    ",\x27320260740022222222,Software"
)
_AFTER_BOUNDARY_ROW = (
    "03/20/2026,,AFTER BOUNDARY INC,75.00,,,,,,"
    ",\x27320260790033333333,Software"
)


class TestBoundaryFiltering(TempDir):

    def _three_row_csv(self) -> Path:
        p = self.tmp / "activity.csv"
        _write_csv(
            p,
            _make_csv(
                _BEFORE_BOUNDARY_ROW,   # 2026-03-10
                _ON_BOUNDARY_ROW,       # 2026-03-15
                _AFTER_BOUNDARY_ROW,    # 2026-03-20
            ),
        )
        return p

    def test_side_before_includes_boundary_row(self) -> None:
        """side=before → dates <= boundary_date included; boundary row included."""
        csv_path = self._three_row_csv()
        txns, excluded = parse_amex_csv(
            csv_path,
            account_id="Liabilities:CreditCard:Amex-CSV",
            boundary_date="2026-03-15",
            side="before",
        )
        dates = [t["date"] for t in txns]
        self.assertIn("2026-03-10", dates)
        self.assertIn("2026-03-15", dates)
        self.assertNotIn("2026-03-20", dates)
        self.assertEqual(excluded, 1)

    def test_side_before_excludes_later_rows(self) -> None:
        """side=before: row after boundary date is excluded and counted."""
        csv_path = self._three_row_csv()
        _, excluded = parse_amex_csv(
            csv_path,
            account_id="Liabilities:CreditCard:Amex-CSV",
            boundary_date="2026-03-15",
            side="before",
        )
        self.assertEqual(excluded, 1)

    def test_side_after_excludes_boundary_row_itself(self) -> None:
        """side=after → dates > boundary_date included; boundary date row excluded."""
        csv_path = self._three_row_csv()
        txns, excluded = parse_amex_csv(
            csv_path,
            account_id="Liabilities:CreditCard:Amex-CSV",
            boundary_date="2026-03-15",
            side="after",
        )
        dates = [t["date"] for t in txns]
        self.assertNotIn("2026-03-10", dates)
        self.assertNotIn("2026-03-15", dates)
        self.assertIn("2026-03-20", dates)
        self.assertEqual(excluded, 2)

    def test_no_boundary_includes_all(self) -> None:
        """With no boundary_date, all rows are included."""
        csv_path = self._three_row_csv()
        txns, excluded = parse_amex_csv(
            csv_path,
            account_id="Liabilities:CreditCard:Amex-CSV",
            boundary_date=None,
        )
        self.assertEqual(len(txns), 3)
        self.assertEqual(excluded, 0)

    def test_boundary_exactly_before_all_rows_excludes_all_when_after(self) -> None:
        """side=after with boundary after all rows → all rows excluded."""
        csv_path = self._three_row_csv()
        txns, excluded = parse_amex_csv(
            csv_path,
            account_id="Liabilities:CreditCard:Amex-CSV",
            boundary_date="2026-12-31",
            side="after",
        )
        self.assertEqual(len(txns), 0)
        self.assertEqual(excluded, 3)

    def test_no_overlap_around_boundary(self) -> None:
        """side=before and side=after on same boundary produce complementary sets, no row on both sides."""
        csv_path = self._three_row_csv()
        before_txns, before_excl = parse_amex_csv(
            csv_path,
            account_id="Liabilities:CreditCard:Amex-CSV",
            boundary_date="2026-03-15",
            side="before",
        )
        after_txns, after_excl = parse_amex_csv(
            csv_path,
            account_id="Liabilities:CreditCard:Amex-CSV",
            boundary_date="2026-03-15",
            side="after",
        )
        before_ids = {t["id"] for t in before_txns}
        after_ids = {t["id"] for t in after_txns}
        self.assertEqual(before_ids & after_ids, set(), "No id should appear on both sides")
        self.assertEqual(len(before_txns) + len(after_txns), 3)


# ---------------------------------------------------------------------------
# 5. Unknown header → error naming expected columns
# ---------------------------------------------------------------------------

class TestUnknownHeader(TempDir):

    def test_missing_columns_error_message(self) -> None:
        """ValueError names the missing columns in plain English."""
        bad_csv = self.tmp / "bad.csv"
        bad_csv.write_text("Date,Amount,Notes\n01/01/2026,10.00,foo\n", encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            parse_amex_csv(bad_csv, account_id="X")

        msg = str(ctx.exception)
        self.assertIn("Missing column", msg)
        # At least a few known missing column names should appear
        self.assertIn("Description", msg)

    def test_error_names_expected_columns(self) -> None:
        """The error message also lists all expected columns for easy reference."""
        bad_csv = self.tmp / "totally_wrong.csv"
        bad_csv.write_text("Foo,Bar\n1,2\n", encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            parse_amex_csv(bad_csv, account_id="X")

        msg = str(ctx.exception)
        # The error message should mention 'Expected all of:' or similar
        # and include at least Date and Amount
        self.assertIn("Date", msg)
        self.assertIn("Amount", msg)

    def test_exact_header_does_not_raise(self) -> None:
        """Exact 12-column header parses without error (even with empty rows)."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _AMEX_HEADER + "\n")  # header only
        # Empty file (header only) should parse to zero transactions without error
        txns, _ = parse_amex_csv(csv_path, account_id="X")
        self.assertEqual(txns, [])


# ---------------------------------------------------------------------------
# 6. Leading apostrophe stripped from Reference
# ---------------------------------------------------------------------------

class TestReferenceStripping(TempDir):

    def test_leading_apostrophe_stripped(self) -> None:
        """Reference column value with leading apostrophe is stripped."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))  # ref = '320260690001122334
        txns, _ = parse_amex_csv(csv_path, account_id="X")
        self.assertEqual(txns[0]["reference"], "320260690001122334")
        self.assertFalse(txns[0]["reference"].startswith("'"))

    def test_reference_without_apostrophe_unchanged(self) -> None:
        """Reference without apostrophe passes through unchanged."""
        self.assertEqual(_strip_reference("12345678"), "12345678")

    def test_empty_reference_becomes_none(self) -> None:
        """Empty Reference column emits None in the normalized output."""
        row = (
            "03/10/2026,,NO REF ROW,10.00,,,,,,,,"
        )
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(row))
        txns, _ = parse_amex_csv(csv_path, account_id="X")
        self.assertIsNone(txns[0]["reference"])

    def test_fixture_references_stripped(self) -> None:
        """All references in fixture CSV are stripped (no leading apostrophe)."""
        txns, _ = parse_amex_csv(
            _FIXTURE_CSV,
            account_id="Liabilities:CreditCard:Amex-CSV",
        )
        for txn in txns:
            ref = txn.get("reference")
            if ref is not None:
                self.assertFalse(
                    ref.startswith("'"),
                    f"Reference {ref!r} still has leading apostrophe",
                )


# ---------------------------------------------------------------------------
# 7. Empty Category tolerated
# ---------------------------------------------------------------------------

class TestEmptyCategory(TempDir):

    def test_empty_category_emits_none(self) -> None:
        """Empty Category column emits None (not empty string)."""
        # _PAYMENT_ROW has empty category
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_PAYMENT_ROW))
        txns, _ = parse_amex_csv(csv_path, account_id="X")
        self.assertIsNone(txns[0]["category"])

    def test_non_empty_category_preserved(self) -> None:
        """Non-empty Category is preserved as-is."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))  # category = "Software"
        txns, _ = parse_amex_csv(csv_path, account_id="X")
        self.assertEqual(txns[0]["category"], "Software")

    def test_fixture_empty_categories(self) -> None:
        """Fixture rows with empty Category yield None, not empty string."""
        txns, _ = parse_amex_csv(
            _FIXTURE_CSV,
            account_id="Liabilities:CreditCard:Amex-CSV",
        )
        for txn in txns:
            cat = txn.get("category")
            self.assertNotEqual(cat, "", f"Expected None for empty category, got empty string")


# ---------------------------------------------------------------------------
# 8. Account mapping: propose → confirm round-trip
# ---------------------------------------------------------------------------

class TestAccountMappingRoundTrip(TempDir):

    def _make_entity_json(self, extra: dict | None = None) -> Path:
        """Write a minimal entity.json, return its path."""
        entity_json = self.tmp / "entity.json"
        data: dict = {
            "name": "Test Entity",
            "business_type": "consulting",
            "fiscal_year_start": "01-01",
        }
        if extra:
            data.update(extra)
        entity_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return entity_json

    def test_propose_when_no_mapping(self) -> None:
        """resolve_mapping returns a proposal when no mapping exists."""
        self._make_entity_json()
        entity_config = json.loads((self.tmp / "entity.json").read_text())
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))

        mapping = resolve_mapping(entity_config, csv_path)
        self.assertTrue(mapping.get("proposed"))
        self.assertFalse(mapping.get("confirmed"))

    def test_write_confirmed_mapping_persists(self) -> None:
        """write_confirmed_mapping writes mapping into entity.json correctly."""
        self._make_entity_json()
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))

        write_confirmed_mapping(
            self.tmp,
            csv_path,
            account_name="Amex Card (CSV)",
            ledger_account="Liabilities:CreditCard:Amex-CSV",
            boundary_date="2026-03-31",
            side="before",
        )

        entity_config = json.loads((self.tmp / "entity.json").read_text())
        mappings = entity_config.get("csv_account_mappings", {})
        self.assertIn("activity", mappings)
        m = mappings["activity"]
        self.assertEqual(m["account_name"], "Amex Card (CSV)")
        self.assertEqual(m["ledger_account"], "Liabilities:CreditCard:Amex-CSV")
        self.assertEqual(m["boundary_date"], "2026-03-31")
        self.assertEqual(m["side"], "before")
        self.assertTrue(m["confirmed"])

    def test_confirmed_mapping_not_proposed(self) -> None:
        """After confirm, resolve_mapping returns confirmed mapping without proposed=True."""
        self._make_entity_json()
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))

        write_confirmed_mapping(
            self.tmp,
            csv_path,
            account_name="Amex Card (CSV)",
            ledger_account="Liabilities:CreditCard:Amex-CSV",
        )

        entity_config = json.loads((self.tmp / "entity.json").read_text())
        mapping = resolve_mapping(entity_config, csv_path)
        self.assertFalse(mapping.get("proposed", False))
        self.assertTrue(mapping.get("confirmed"))

    def test_write_confirmed_mapping_preserves_other_keys(self) -> None:
        """write_confirmed_mapping does not clobber unrelated entity.json keys."""
        self._make_entity_json({"fiscal_year_start": "04-01"})
        csv_path = self.tmp / "activity.csv"

        write_confirmed_mapping(
            self.tmp, csv_path,
            account_name="Amex Card (CSV)",
            ledger_account="Liabilities:CreditCard:Amex-CSV",
        )

        entity_config = json.loads((self.tmp / "entity.json").read_text())
        self.assertEqual(entity_config["fiscal_year_start"], "04-01")

    def test_propose_only_ignores_existing_mapping(self) -> None:
        """propose_only=True always returns a fresh proposal."""
        self._make_entity_json({
            "csv_account_mappings": {
                "activity": {
                    "account_name": "Custom Name",
                    "ledger_account": "Liabilities:CreditCard:Custom",
                    "boundary_date": None,
                    "side": "before",
                    "confirmed": True,
                }
            }
        })
        csv_path = self.tmp / "activity.csv"
        entity_config = json.loads((self.tmp / "entity.json").read_text())

        proposal = resolve_mapping(entity_config, csv_path, propose_only=True)
        self.assertTrue(proposal.get("proposed"))

    def test_write_confirmed_mapping_atomic_no_orphan_tmp(self) -> None:
        """After write_confirmed_mapping, no .tmp sibling file exists."""
        self._make_entity_json()
        csv_path = self.tmp / "activity.csv"

        write_confirmed_mapping(
            self.tmp, csv_path,
            account_name="Amex Card (CSV)",
            ledger_account="Liabilities:CreditCard:Amex-CSV",
        )

        tmp_path = self.tmp / "entity.json.tmp"
        self.assertFalse(tmp_path.exists(), "Orphaned .tmp file should not exist after write")


# ---------------------------------------------------------------------------
# 9. import_csv refuses without confirmed mapping
# ---------------------------------------------------------------------------

class TestImportCsvRefusal(TempDir):

    def test_refuses_without_confirmed_mapping(self) -> None:
        """import_csv returns proposed=True when mapping is absent."""
        entity_config = {"name": "Test"}
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))

        result = import_csv(entity_config, csv_path)
        self.assertTrue(result["proposed"])
        self.assertEqual(result["transactions"], [])
        self.assertIsNotNone(result["proposal"])

    def test_imports_with_confirmed_mapping(self) -> None:
        """import_csv returns transactions when confirmed mapping exists."""
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW, _PAYMENT_ROW))

        entity_config = {
            "name": "Test",
            "csv_account_mappings": {
                "activity": {
                    "account_name": "Amex Card (CSV)",
                    "ledger_account": "Liabilities:CreditCard:Amex-CSV",
                    "boundary_date": None,
                    "side": "before",
                    "confirmed": True,
                }
            },
        }

        result = import_csv(entity_config, csv_path)
        self.assertFalse(result["proposed"])
        self.assertEqual(len(result["transactions"]), 2)
        self.assertEqual(result["excluded_count"], 0)

    def test_reimport_same_csv_fingerprints_unchanged(self) -> None:
        """Parsing the same CSV twice yields identical ids (idempotency basis)."""
        entity_config = {
            "name": "Test",
            "csv_account_mappings": {
                "activity": {
                    "account_name": "Amex Card (CSV)",
                    "ledger_account": "Liabilities:CreditCard:Amex-CSV",
                    "boundary_date": None,
                    "side": "before",
                    "confirmed": True,
                }
            },
        }
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW, _PAYMENT_ROW))

        r1 = import_csv(entity_config, csv_path)
        r2 = import_csv(entity_config, csv_path)
        self.assertEqual(
            [t["id"] for t in r1["transactions"]],
            [t["id"] for t in r2["transactions"]],
        )

    def test_unconfirmed_mapping_treated_as_absent(self) -> None:
        """A mapping with confirmed=False is treated as absent (proposed returned)."""
        entity_config = {
            "name": "Test",
            "csv_account_mappings": {
                "activity": {
                    "account_name": "Amex Card (CSV)",
                    "ledger_account": "Liabilities:CreditCard:Amex-CSV",
                    "boundary_date": None,
                    "side": "before",
                    "confirmed": False,  # ← not confirmed
                }
            },
        }
        csv_path = self.tmp / "activity.csv"
        _write_csv(csv_path, _make_csv(_CHARGE_ROW))

        result = import_csv(entity_config, csv_path)
        self.assertTrue(result["proposed"])


# ---------------------------------------------------------------------------
# 10. Fixture CSV end-to-end
# ---------------------------------------------------------------------------

class TestFixtureCSV(unittest.TestCase):

    def test_fixture_parses_expected_row_count(self) -> None:
        """Fixture CSV parses all 9 data rows without error."""
        txns, excluded = parse_amex_csv(
            _FIXTURE_CSV,
            account_id="Liabilities:CreditCard:Amex-CSV",
            ledger_account="Liabilities:CreditCard:Amex-CSV",
        )
        self.assertEqual(excluded, 0)
        self.assertEqual(len(txns), 9)

    def test_fixture_two_payments_are_credits(self) -> None:
        """The two payment rows in the fixture have positive feed-axis amount and creditAmount set."""
        txns, _ = parse_amex_csv(
            _FIXTURE_CSV,
            account_id="Liabilities:CreditCard:Amex-CSV",
        )
        payments = [t for t in txns if t["creditAmount"] is not None]
        self.assertEqual(len(payments), 2)
        for p in payments:
            self.assertIsNone(p["debitAmount"])
            self.assertGreater(float(p["amount"]), 0)

    def test_fixture_all_ids_unique(self) -> None:
        """All transaction ids in the fixture CSV are unique."""
        txns, _ = parse_amex_csv(
            _FIXTURE_CSV,
            account_id="Liabilities:CreditCard:Amex-CSV",
        )
        ids = [t["id"] for t in txns]
        self.assertEqual(len(ids), len(set(ids)))

    def test_fixture_boundary_side_before(self) -> None:
        """Fixture filtered with side=before at 2026-02-15 includes Jan/Feb rows only."""
        txns, excluded = parse_amex_csv(
            _FIXTURE_CSV,
            account_id="Liabilities:CreditCard:Amex-CSV",
            boundary_date="2026-02-15",
            side="before",
        )
        for txn in txns:
            self.assertLessEqual(txn["date"], "2026-02-15")
        self.assertGreater(excluded, 0)

    def test_fixture_boundary_side_after(self) -> None:
        """Fixture filtered with side=after at 2026-02-15 includes rows after 2026-02-15."""
        txns, excluded = parse_amex_csv(
            _FIXTURE_CSV,
            account_id="Liabilities:CreditCard:Amex-CSV",
            boundary_date="2026-02-15",
            side="after",
        )
        for txn in txns:
            self.assertGreater(txn["date"], "2026-02-15")
        self.assertGreater(excluded, 0)

    def test_fixture_no_double_inclusion_across_boundary(self) -> None:
        """side=before and side=after at same boundary date partition fixture with no overlap."""
        before_txns, _ = parse_amex_csv(
            _FIXTURE_CSV,
            account_id="Liabilities:CreditCard:Amex-CSV",
            boundary_date="2026-02-15",
            side="before",
        )
        after_txns, _ = parse_amex_csv(
            _FIXTURE_CSV,
            account_id="Liabilities:CreditCard:Amex-CSV",
            boundary_date="2026-02-15",
            side="after",
        )
        before_ids = {t["id"] for t in before_txns}
        after_ids = {t["id"] for t in after_txns}
        self.assertEqual(before_ids & after_ids, set())
        self.assertEqual(len(before_ids) + len(after_ids), 9)


if __name__ == "__main__":
    unittest.main()
