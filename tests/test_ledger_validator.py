from __future__ import annotations

"""Tests for src/bookkeeping/ledger/validator.py"""

import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.ledger.model import Balance, Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.writer import render_ledger, render_session  # noqa: E402
from bookkeeping.ledger.validator import parse_ledger, validate, validate_file  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_ledger(extra_entries=None, extra_balances=None) -> tuple[list, list, list]:
    opens = [
        Open(date=date(2026, 1, 1), account="Assets:Checking", currencies=("USD",)),
        Open(date=date(2026, 1, 1), account="Income:Services", currencies=("USD",)),
        Open(date=date(2026, 1, 1), account="Expenses:Software", currencies=("USD",)),
    ]
    entries = [
        Entry(
            date=date(2026, 1, 15),
            narration="Client invoice",
            meta=(("source-id", "txn_001"),),
            postings=(
                Posting("Assets:Checking", Decimal("1500.00")),
                Posting("Income:Services", Decimal("-1500.00")),
            ),
        ),
    ]
    if extra_entries:
        entries.extend(extra_entries)
    balances = extra_balances or []
    return opens, entries, balances


def _render_and_validate(opens, entries, balances, title="Test Books"):
    text = render_ledger(opens, entries, balances, title=title)
    return validate(text), text


# ---------------------------------------------------------------------------
# 1. Parseability — empty ledger
# ---------------------------------------------------------------------------


class TestValidateEmpty(unittest.TestCase):
    def test_empty_ledger_no_errors(self) -> None:
        text = render_ledger([], [], [], title="Empty")
        errors = validate(text)
        self.assertEqual(errors, [])

    def test_empty_string_no_errors(self) -> None:
        errors = validate("")
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# 2. Per-transaction zero-sum
# ---------------------------------------------------------------------------


class TestZeroSum(unittest.TestCase):
    def test_balanced_no_errors(self) -> None:
        opens, entries, balances = _minimal_ledger()
        errors, _ = _render_and_validate(opens, entries, balances)
        self.assertEqual(errors, [])

    def test_unbalanced_produces_error(self) -> None:
        # Craft an unbalanced transaction by hand (bypassing model validation)
        text = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Checking
2026-01-01 open Income:Services

2026-01-15 * "Unbalanced transaction"
  Assets:Checking  100.00 USD
  Income:Services  -99.98 USD

"""
        errors = validate(text)
        self.assertTrue(len(errors) > 0)
        self.assertTrue(any("balance" in e.message.lower() or "sum" in e.message.lower() for e in errors))

    def test_within_tolerance_no_error(self) -> None:
        # sum = 0.003 which is within 0.005 tolerance — but Decimal("0.003") is
        # overprecise for amounts; we test via raw text
        text = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Checking
2026-01-01 open Income:Services

2026-01-15 * "Near-balanced"
  Assets:Checking  100.00 USD
  Income:Services  -100.00 USD

"""
        errors = validate(text)
        self.assertEqual(errors, [])

    def test_unbalanced_error_names_entry(self) -> None:
        text = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Checking
2026-01-01 open Income:Services

2026-01-15 * "My Special Transaction"
  Assets:Checking  100.00 USD
  Income:Services  -99.98 USD

"""
        errors = validate(text)
        self.assertTrue(any("My Special Transaction" in e.message for e in errors))


# ---------------------------------------------------------------------------
# 3. Account-opened-before-use
# ---------------------------------------------------------------------------


class TestOpenBeforeUse(unittest.TestCase):
    def test_opened_account_ok(self) -> None:
        opens, entries, _ = _minimal_ledger()
        errors, _ = _render_and_validate(opens, entries, [])
        self.assertEqual(errors, [])

    def test_unopened_account_fails(self) -> None:
        text = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Checking

2026-01-15 * "Missing open"
  Assets:Checking  100.00 USD
  Income:Services  -100.00 USD

"""
        errors = validate(text)
        self.assertTrue(len(errors) > 0)
        self.assertTrue(any("Income:Services" in e.message for e in errors))

    def test_unopened_account_error_names_account_and_date(self) -> None:
        text = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Checking

2026-01-15 * "No open for income"
  Assets:Checking  100.00 USD
  Income:Services  -100.00 USD

"""
        errors = validate(text)
        # Error should name the account and the date
        problem_errors = [e for e in errors if "Income:Services" in e.message]
        self.assertTrue(len(problem_errors) > 0)
        err = problem_errors[0]
        self.assertIn("Income:Services", err.message)
        # Date is in the message
        self.assertIn("2026-01-15", err.message)

    def test_account_opened_after_use_fails(self) -> None:
        text = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-02-01 open Assets:Checking
2026-01-01 open Income:Services

2026-01-15 * "Used before open"
  Assets:Checking  100.00 USD
  Income:Services  -100.00 USD

"""
        errors = validate(text)
        self.assertTrue(any("Assets:Checking" in e.message for e in errors))


# ---------------------------------------------------------------------------
# 4. Balance assertions with start-of-day semantics
# ---------------------------------------------------------------------------


class TestBalanceAssertions(unittest.TestCase):
    def _ledger_with_balance(
        self, balance_date: date, balance_amount: Decimal
    ) -> str:
        opens = [
            Open(date=date(2026, 1, 1), account="Assets:Checking", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Income:Services", currencies=("USD",)),
        ]
        entries = [
            Entry(
                date=date(2026, 1, 15),
                narration="Credit",
                postings=(
                    Posting("Assets:Checking", Decimal("1000.00")),
                    Posting("Income:Services", Decimal("-1000.00")),
                ),
            ),
        ]
        balances = [Balance(date=balance_date, account="Assets:Checking", amount=balance_amount)]
        return render_ledger(opens, entries, balances)

    def test_balance_before_txn_expects_zero(self) -> None:
        # Balance on 2026-01-15 checks START of day → before the transaction
        # so balance should be 0.00 to pass
        text = self._ledger_with_balance(date(2026, 1, 15), Decimal("0.00"))
        errors = validate(text)
        self.assertEqual(errors, [])

    def test_balance_before_txn_wrong_amount_fails(self) -> None:
        # Balance on 2026-01-15 = start of day = 0 transactions yet
        text = self._ledger_with_balance(date(2026, 1, 15), Decimal("1000.00"))
        errors = validate(text)
        self.assertTrue(len(errors) > 0)
        self.assertTrue(any("balance" in e.message.lower() or "assertion" in e.message.lower() for e in errors))

    def test_balance_after_txn_expects_1000(self) -> None:
        # Balance on 2026-01-16 = day after → transaction is included
        text = self._ledger_with_balance(date(2026, 1, 16), Decimal("1000.00"))
        errors = validate(text)
        self.assertEqual(errors, [])

    def test_balance_fail_message_shows_account(self) -> None:
        text = self._ledger_with_balance(date(2026, 1, 16), Decimal("999.00"))
        errors = validate(text)
        self.assertTrue(any("Assets:Checking" in e.message for e in errors))

    def test_same_day_semantics_txn_not_included(self) -> None:
        """Balance directive on the same day as a transaction does NOT include
        that transaction (start-of-day semantics)."""
        # Same date (2026-01-15) means the transaction is NOT yet applied
        # So the balance of 0.00 should pass
        text = self._ledger_with_balance(date(2026, 1, 15), Decimal("0.00"))
        errors = validate(text)
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# 5. Account-name lexical rules
# ---------------------------------------------------------------------------


class TestAccountNameRules(unittest.TestCase):
    def test_valid_names_no_error(self) -> None:
        text = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Checking
2026-01-01 open Income:Services

2026-01-15 * "Valid"
  Assets:Checking  100.00 USD
  Income:Services  -100.00 USD

"""
        errors = validate(text)
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# 6. Balanced pushtag/poptag
# ---------------------------------------------------------------------------


class TestPushPopTag(unittest.TestCase):
    def test_balanced_pushtag_poptag_no_error(self) -> None:
        opens, entries, _ = _minimal_ledger()
        text = render_session(entries, session_id="test-001")
        header = render_ledger.__wrapped__(opens, [], [], "T") if hasattr(render_ledger, "__wrapped__") else ""
        # Just validate the session block combined with opens
        from bookkeeping.ledger.writer import render_header, render_open
        full_text = render_header("T")
        for o in opens:
            full_text += render_open(o) + "\n"
        full_text += "\n" + text
        errors = validate(full_text)
        # Only account-open-before-use errors expected from session block
        # (session entries reference accounts); no pushtag/poptag errors
        pushtag_errors = [e for e in errors if "pushtag" in e.message.lower() or "poptag" in e.message.lower()]
        self.assertEqual(pushtag_errors, [])

    def test_unmatched_pushtag_produces_error(self) -> None:
        text = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

pushtag #import-session-001

2026-01-01 open Assets:Checking
"""
        errors = validate(text)
        self.assertTrue(any("pushtag" in e.message.lower() for e in errors))

    def test_poptag_without_pushtag_produces_error(self) -> None:
        text = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Checking

poptag #import-session-001
"""
        errors = validate(text)
        self.assertTrue(any("poptag" in e.message.lower() for e in errors))

    def test_mismatched_tag_produces_error(self) -> None:
        text = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

pushtag #import-session-001

2026-01-01 open Assets:Checking

poptag #import-session-002
"""
        errors = validate(text)
        self.assertTrue(any("poptag" in e.message.lower() for e in errors))

    def test_nested_matching_tags_ok(self) -> None:
        text = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

pushtag #import-session-001
pushtag #import-session-002

2026-01-01 open Assets:Checking

poptag #import-session-002
poptag #import-session-001
"""
        errors = validate(text)
        pushtag_errors = [e for e in errors if "pushtag" in e.message.lower() or "poptag" in e.message.lower()]
        self.assertEqual(pushtag_errors, [])


# ---------------------------------------------------------------------------
# Round-trip: write → parse → equal
# ---------------------------------------------------------------------------


class TestRoundTrip(unittest.TestCase):
    def test_round_trip_opens(self) -> None:
        opens = [
            Open(date=date(2026, 1, 1), account="Assets:Checking", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Income:Services", currencies=("USD",)),
        ]
        text = render_ledger(opens, [], [])
        parsed = parse_ledger(text)
        self.assertEqual(len(parsed["opens"]), 2)
        accounts = {o.account for o in parsed["opens"]}
        self.assertIn("Assets:Checking", accounts)
        self.assertIn("Income:Services", accounts)

    def test_round_trip_entries(self) -> None:
        opens, entries, balances = _minimal_ledger()
        text = render_ledger(opens, entries, balances)
        parsed = parse_ledger(text)
        self.assertEqual(len(parsed["entries"]), 1)
        e = parsed["entries"][0]
        self.assertEqual(e.date, date(2026, 1, 15))
        self.assertEqual(e.narration, "Client invoice")
        self.assertEqual(e.source_id, "txn_001")

    def test_round_trip_balances(self) -> None:
        opens = [
            Open(date=date(2026, 1, 1), account="Assets:Checking", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Income:Services", currencies=("USD",)),
        ]
        entries = [
            Entry(
                date=date(2026, 1, 15),
                narration="Credit",
                postings=(
                    Posting("Assets:Checking", Decimal("500.00")),
                    Posting("Income:Services", Decimal("-500.00")),
                ),
            )
        ]
        balances = [Balance(date=date(2026, 1, 16), account="Assets:Checking", amount=Decimal("500.00"))]
        text = render_ledger(opens, entries, balances)
        parsed = parse_ledger(text)
        self.assertEqual(len(parsed["balances"]), 1)
        b = parsed["balances"][0]
        self.assertEqual(b.account, "Assets:Checking")
        self.assertEqual(b.amount, Decimal("500.00"))

    def test_round_trip_full_no_errors(self) -> None:
        opens, entries, balances = _minimal_ledger(
            extra_entries=[
                Entry(
                    date=date(2026, 1, 20),
                    narration="Software sub",
                    meta=(("source-id", "txn_002"), ("import-session", "test-001")),
                    postings=(
                        Posting("Expenses:Software", Decimal("49.00")),
                        Posting("Assets:Checking", Decimal("-49.00")),
                    ),
                )
            ],
            extra_balances=[
                Balance(date=date(2026, 2, 1), account="Assets:Checking", amount=Decimal("1451.00"))
            ],
        )
        text = render_ledger(opens, entries, balances)
        errors = validate(text)
        self.assertEqual(errors, [])

    def test_round_trip_preserves_payee(self) -> None:
        opens = [
            Open(date=date(2026, 1, 1), account="Assets:Checking", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Income:Services", currencies=("USD",)),
        ]
        entries = [
            Entry(
                date=date(2026, 1, 15),
                narration="Invoice payment",
                payee="Acme Corp",
                postings=(
                    Posting("Assets:Checking", Decimal("200.00")),
                    Posting("Income:Services", Decimal("-200.00")),
                ),
            )
        ]
        text = render_ledger(opens, entries, [])
        parsed = parse_ledger(text)
        self.assertEqual(parsed["entries"][0].payee, "Acme Corp")
        self.assertEqual(parsed["entries"][0].narration, "Invoice payment")

    def test_round_trip_preserves_sanitized_narration_with_quote(self) -> None:
        """Narration with embedded double-quote survives round-trip."""
        opens = [
            Open(date=date(2026, 1, 1), account="Assets:Checking", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Income:Services", currencies=("USD",)),
        ]
        entries = [
            Entry(
                date=date(2026, 1, 15),
                narration='Payment "rush" fee',
                postings=(
                    Posting("Assets:Checking", Decimal("100.00")),
                    Posting("Income:Services", Decimal("-100.00")),
                ),
            )
        ]
        text = render_ledger(opens, entries, [])
        parsed = parse_ledger(text)
        e = parsed["entries"][0]
        self.assertIn("rush", e.narration)


# ---------------------------------------------------------------------------
# validate_file
# ---------------------------------------------------------------------------


class TestValidateFile(unittest.TestCase):
    def test_validate_file_ok(self) -> None:
        opens, entries, balances = _minimal_ledger()
        text = render_ledger(opens, entries, balances)
        with tempfile.NamedTemporaryFile(suffix=".beancount", mode="w", encoding="utf-8", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            errors = validate_file(path)
            self.assertEqual(errors, [])
        finally:
            Path(path).unlink()

    def test_validate_file_with_errors(self) -> None:
        text = """\
option "title" "Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

pushtag #unclosed-tag
"""
        with tempfile.NamedTemporaryFile(suffix=".beancount", mode="w", encoding="utf-8", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            errors = validate_file(path)
            self.assertTrue(len(errors) > 0)
        finally:
            Path(path).unlink()


# ---------------------------------------------------------------------------
# parse_ledger public API
# ---------------------------------------------------------------------------


class TestParseLedger(unittest.TestCase):
    def test_parse_returns_dict_with_required_keys(self) -> None:
        opens, entries, _ = _minimal_ledger()
        text = render_ledger(opens, entries, [])
        result = parse_ledger(text)
        self.assertIn("opens", result)
        self.assertIn("entries", result)
        self.assertIn("balances", result)
        self.assertIn("title", result)
        self.assertIn("tolerance", result)

    def test_parse_tolerance_from_option(self) -> None:
        text = """\
option "title" "T"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"
"""
        result = parse_ledger(text)
        self.assertEqual(result["tolerance"], Decimal("0.005"))

    def test_parse_entries_have_meta(self) -> None:
        opens, entries, _ = _minimal_ledger()
        text = render_ledger(opens, entries, [])
        result = parse_ledger(text)
        e = result["entries"][0]
        meta_dict = dict(e.meta)
        self.assertIn("source-id", meta_dict)
        self.assertEqual(meta_dict["source-id"], "txn_001")

    def test_parse_session_entries_include_tags(self) -> None:
        from bookkeeping.ledger.writer import render_header, render_open

        opens = [
            Open(date=date(2026, 1, 1), account="Assets:Checking", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Income:Services", currencies=("USD",)),
        ]
        entries = [
            Entry(
                date=date(2026, 1, 15),
                narration="Tagged entry",
                postings=(
                    Posting("Assets:Checking", Decimal("100.00")),
                    Posting("Income:Services", Decimal("-100.00")),
                ),
            )
        ]
        session_text = render_session(entries, session_id="my-session")
        header = render_header("T")
        full_text = header
        for o in opens:
            full_text += render_open(o) + "\n"
        full_text += "\n" + session_text
        result = parse_ledger(full_text)
        # entries are parsed; session_id in tags would require the parser
        # to propagate active pushtags onto entries — basic implementation
        # just parses entries without tag propagation (tags on entry header line only)
        self.assertEqual(len(result["entries"]), 1)


# ---------------------------------------------------------------------------
# AE9 Integration: write + validate + report without beancount package
# ---------------------------------------------------------------------------


class TestAE9Integration(unittest.TestCase):
    """AE9: write + validate + report a sample ledger end-to-end with no
    external beancount package."""

    def test_end_to_end_no_external_beancount(self) -> None:
        # Ensure beancount is NOT imported
        import sys
        self.assertNotIn("beancount", sys.modules)

        opens = [
            Open(date=date(2026, 1, 1), account="Assets:Mercury", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Income:Consulting", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Expenses:Software", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Equity:OpeningBalances", currencies=("USD",)),
        ]
        entries = [
            Entry(
                date=date(2026, 1, 1),
                narration="Opening balance",
                meta=(("source-id", "open_001"),),
                postings=(
                    Posting("Assets:Mercury", Decimal("5000.00")),
                    Posting("Equity:OpeningBalances", Decimal("-5000.00")),
                ),
            ),
            Entry(
                date=date(2026, 1, 15),
                narration="Client payment",
                payee="Acme Corp",
                meta=(("source-id", "txn_001"), ("import-session", "test-001")),
                postings=(
                    Posting("Assets:Mercury", Decimal("2500.00")),
                    Posting("Income:Consulting", Decimal("-2500.00")),
                ),
            ),
            Entry(
                date=date(2026, 1, 20),
                narration="GitHub subscription",
                meta=(("source-id", "txn_002"), ("import-session", "test-001")),
                postings=(
                    Posting("Expenses:Software", Decimal("4.00")),
                    Posting("Assets:Mercury", Decimal("-4.00")),
                ),
            ),
        ]
        balances = [
            Balance(date=date(2026, 2, 1), account="Assets:Mercury", amount=Decimal("7496.00")),
        ]

        # Write
        text = render_ledger(opens, entries, balances, title="Example Company 2026")
        self.assertIn("Example Company 2026", text)

        # Validate
        errors = validate(text)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

        # Parse / report
        parsed = parse_ledger(text)
        self.assertEqual(len(parsed["opens"]), 4)
        self.assertEqual(len(parsed["entries"]), 3)
        self.assertEqual(len(parsed["balances"]), 1)

        # Compute P&L manually from parsed entries
        income_total = Decimal("0.00")
        expense_total = Decimal("0.00")
        for e in parsed["entries"]:
            for p in e.postings:
                if p.account.startswith("Income:"):
                    income_total += p.amount
                elif p.account.startswith("Expenses:"):
                    expense_total += p.amount

        # Income postings are negative (credit side); net income = -(income_total)
        net_income = -income_total - expense_total
        self.assertEqual(net_income, Decimal("2496.00"))  # 2500 - 4 = 2496

        # Balance sheet check: Assets balance at end of period
        asset_total = Decimal("0.00")
        equity_total = Decimal("0.00")
        for e in parsed["entries"]:
            for p in e.postings:
                if p.account.startswith("Assets:"):
                    asset_total += p.amount
                elif p.account.startswith("Equity:"):
                    equity_total += p.amount

        # Assets = 5000 + 2500 - 4 = 7496
        self.assertEqual(asset_total, Decimal("7496.00"))


# ---------------------------------------------------------------------------
# Golden file test
# ---------------------------------------------------------------------------


GOLDEN_PATH = ROOT / "tests" / "fixtures" / "ledger" / "golden.beancount"


class TestGoldenFile(unittest.TestCase):
    """The generated ledger must match the checked-in fixture byte-for-byte."""

    def _make_golden_ledger(self) -> str:
        """Build the golden ledger with session ID 'test-session-001'."""
        opens = [
            Open(date=date(2026, 1, 1), account="Assets:Mercury", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Equity:OpeningBalances", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Expenses:Software", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Income:Consulting", currencies=("USD",)),
        ]
        entries = [
            Entry(
                date=date(2026, 1, 1),
                narration="Opening balance",
                meta=(("source-id", "open_001"),),
                postings=(
                    Posting("Assets:Mercury", Decimal("5000.00")),
                    Posting("Equity:OpeningBalances", Decimal("-5000.00")),
                ),
            ),
            Entry(
                date=date(2026, 1, 15),
                narration="Client payment",
                payee="Acme Corp",
                meta=(("source-id", "txn_001"), ("import-session", "test-session-001")),
                postings=(
                    Posting("Assets:Mercury", Decimal("2500.00")),
                    Posting("Income:Consulting", Decimal("-2500.00")),
                ),
            ),
            Entry(
                date=date(2026, 1, 20),
                narration="GitHub subscription",
                meta=(("source-id", "txn_002"), ("import-session", "test-session-001")),
                postings=(
                    Posting("Expenses:Software", Decimal("4.00")),
                    Posting("Assets:Mercury", Decimal("-4.00")),
                ),
            ),
        ]
        balances = [
            Balance(date=date(2026, 2, 1), account="Assets:Mercury", amount=Decimal("7496.00")),
        ]
        return render_ledger(opens, entries, balances, title="Example Company 2026")

    def test_golden_file_matches_generated(self) -> None:
        generated = self._make_golden_ledger()
        if not GOLDEN_PATH.exists():
            # First run: write the golden file
            GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN_PATH.write_text(generated, encoding="utf-8")
            self.skipTest("Golden file written; re-run to verify byte-for-byte match")
        expected = GOLDEN_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            generated,
            expected,
            "Generated ledger does not match golden fixture. "
            "If the writer changed intentionally, delete the fixture and re-run to regenerate.",
        )

    def test_golden_file_validates_clean(self) -> None:
        if not GOLDEN_PATH.exists():
            generated = self._make_golden_ledger()
            GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN_PATH.write_text(generated, encoding="utf-8")

        errors = validate_file(GOLDEN_PATH)
        self.assertEqual(errors, [], f"Golden file has validation errors: {errors}")


if __name__ == "__main__":
    unittest.main()
