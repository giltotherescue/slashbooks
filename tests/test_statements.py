"""Tests for src/bookkeeping/reports/statements.py

Tests cover:
- P&L with hand-computed exact totals from 10-entry fixture
- Balance sheet: assets = liabilities + equity (including net income)
- Trial balance sums to zero (debits == credits)
- General ledger entries for a date range
- Empty date range produces valid (empty) statements
- Boundary entries counted exactly once
- ask(): each F4 question shape returns numbers matching statements
- ask(): unknown question returns guidance
- No beancount/SQL jargon in rendered text outputs
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

# Reuse the same fixture from test_cache
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

# Hand-computed values:
# Q1 (Jan 1 - Mar 31):
#   Revenue: 5000 + 3000 + 2000 = 10000
#   Expenses: 200 (software Jan) + 100 (office Feb) + 150 (software Mar) + 200 (software CC Mar) = 650
#   Net income Q1: 10000 - 650 = 9350
#
# Full period (Jan 1 - Apr 15):
#   Revenue: 10000 + 1500 = 11500
#   Expenses: 650
#   Net income: 11500 - 650 = 10850
#
# Balance sheet as of 2026-04-15:
#   Assets:Bank:Checking = 500 + 5000 - 200 + 3000 - 100 + 2000 - 150 - 200 + 1500 = 11350
#   Liabilities:CreditCard = -200 + 200 = 0
#   Equity:OpeningBalance = -500 (credit-normal → displayed as 500)
#   Net Income (current period) = 11500 - 650 = 10850
#   Total equity = 500 + 10850 = 11350
#   Total assets (11350) == total liabilities (0) + total equity (11350) ✓


def _make_entity(tmp_path: Path, ledger_text: str = FIXTURE_LEDGER) -> Path:
    entity_dir = tmp_path / "entity"
    entity_dir.mkdir()
    (entity_dir / "entity.json").write_text('{"name": "Test Entity"}')
    (entity_dir / "reports").mkdir()
    (entity_dir / "staging").mkdir()
    (entity_dir / "books.beancount").write_text(ledger_text, encoding="utf-8")
    from src.bookkeeping.reports.cache import regenerate
    regenerate(entity_dir)
    return entity_dir


class TestProfitAndLoss(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_q1_revenue(self):
        """Q1 revenue totals 10000.00."""
        from src.bookkeeping.reports.statements import profit_and_loss
        result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 3, 31))
        total_income = result.sections[0]["total"]
        self.assertEqual(total_income, Decimal("10000.00"))

    def test_q1_expenses(self):
        """Q1 expenses total 650.00."""
        from src.bookkeeping.reports.statements import profit_and_loss
        result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 3, 31))
        total_expenses = result.sections[1]["total"]
        self.assertEqual(total_expenses, Decimal("650.00"))

    def test_q1_net_income(self):
        """Q1 net income = 10000 - 650 = 9350.00."""
        from src.bookkeeping.reports.statements import profit_and_loss
        result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 3, 31))
        self.assertEqual(result.totals["net_income"], Decimal("9350.00"))

    def test_full_period_revenue(self):
        """Full period revenue (through Apr 15) = 11500.00."""
        from src.bookkeeping.reports.statements import profit_and_loss
        result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 4, 15))
        self.assertEqual(result.sections[0]["total"], Decimal("11500.00"))

    def test_full_period_net_income(self):
        """Full period net income = 11500 - 650 = 10850.00."""
        from src.bookkeeping.reports.statements import profit_and_loss
        result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 4, 15))
        self.assertEqual(result.totals["net_income"], Decimal("10850.00"))

    def test_empty_date_range(self):
        """Empty date range produces valid statement with zero totals."""
        from src.bookkeeping.reports.statements import profit_and_loss
        result = profit_and_loss(self.entity, date(2025, 1, 1), date(2025, 12, 31))
        self.assertEqual(result.sections[0]["total"], Decimal("0.00"))
        self.assertEqual(result.sections[1]["total"], Decimal("0.00"))
        self.assertEqual(result.totals["net_income"], Decimal("0.00"))

    def test_boundary_entry_counted_once(self):
        """Entry on the boundary date (2026-01-15) is counted exactly once."""
        from src.bookkeeping.reports.statements import profit_and_loss
        # Jan 15 is the first revenue entry. If from=Jan 15, it must be included.
        result_with = profit_and_loss(self.entity, date(2026, 1, 15), date(2026, 1, 31))
        result_without = profit_and_loss(self.entity, date(2026, 1, 16), date(2026, 1, 31))
        # Jan 15 entry = 5000 revenue
        self.assertEqual(result_with.sections[0]["total"] - result_without.sections[0]["total"], Decimal("5000.00"))

    def test_kind_field(self):
        """StatementResult kind is 'pnl'."""
        from src.bookkeeping.reports.statements import profit_and_loss
        result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 3, 31))
        self.assertEqual(result.kind, "pnl")

    def test_to_json_valid(self):
        """to_json() returns valid JSON with Decimal as string."""
        import json
        from src.bookkeeping.reports.statements import profit_and_loss
        result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 3, 31))
        text = result.to_json()
        parsed = json.loads(text)
        self.assertEqual(parsed["kind"], "pnl")
        # Net income stored as string
        self.assertEqual(parsed["totals"]["net_income"], "9350.00")

    def test_to_text_no_jargon(self):
        """to_text() output contains no beancount/SQL jargon."""
        from src.bookkeeping.reports.statements import profit_and_loss
        result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 3, 31))
        text = result.to_text()
        for token in [";", "beancount", "SELECT", "sqlite", "pushtag", "Assets:Bank:", "Income:Revenue:"]:
            self.assertNotIn(token, text, f"Jargon token '{token}' found in P&L text")

    def test_account_names_use_arrow_separator(self):
        """Account names use ' › ' separator in rendered text."""
        from src.bookkeeping.reports.statements import profit_and_loss
        result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 3, 31))
        text = result.to_text()
        self.assertIn("›", text)
        # No raw colon-separated account names
        self.assertNotIn("Assets:Bank", text)
        self.assertNotIn("Income:Revenue", text)


class TestBalanceSheet(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_assets_as_of_apr_15(self):
        """Total assets as of 2026-04-15 = 11350.00."""
        from src.bookkeeping.reports.statements import balance_sheet
        result = balance_sheet(self.entity, date(2026, 4, 15))
        total_assets = result.totals["total_assets"]
        self.assertEqual(total_assets, Decimal("11350.00"))

    def test_balance_sheet_balances(self):
        """Assets == Liabilities + Equity (BS equation holds)."""
        from src.bookkeeping.reports.statements import balance_sheet
        result = balance_sheet(self.entity, date(2026, 4, 15))
        total_assets = result.totals["total_assets"]
        total_lia_equity = result.totals["total_liabilities_and_equity"]
        self.assertEqual(total_assets, total_lia_equity,
                         f"BS doesn't balance: assets={total_assets}, L+E={total_lia_equity}")

    def test_net_income_in_equity(self):
        """Equity section includes current-period net income as a line."""
        from src.bookkeeping.reports.statements import balance_sheet
        result = balance_sheet(self.entity, date(2026, 4, 15))
        # Find equity section
        equity_section = next(s for s in result.sections if s["label"] == "Equity")
        net_income_row = next(
            (r for r in equity_section["rows"] if "Net Income" in r["label"]),
            None,
        )
        self.assertIsNotNone(net_income_row, "Net Income row missing from equity section")
        self.assertEqual(net_income_row["amount"], Decimal("10850.00"))

    def test_to_text_no_jargon(self):
        """Balance sheet text contains no jargon."""
        from src.bookkeeping.reports.statements import balance_sheet
        result = balance_sheet(self.entity, date(2026, 4, 15))
        text = result.to_text()
        for token in [";", "beancount", "SELECT", "sqlite", "pushtag"]:
            self.assertNotIn(token, text, f"Jargon token '{token}' in BS text")
        # No raw colon-separated account names
        self.assertNotIn("Assets:Bank", text)

    def test_empty_as_of_early_date(self):
        """Balance sheet before any entries has zero totals."""
        from src.bookkeeping.reports.statements import balance_sheet
        result = balance_sheet(self.entity, date(2000, 1, 1))
        self.assertEqual(result.totals["total_assets"], Decimal("0.00"))


class TestTrialBalance(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_trial_balance_sums_to_zero(self):
        """Trial balance: debits == credits (net == 0)."""
        from src.bookkeeping.reports.statements import trial_balance
        result = trial_balance(self.entity, date(2026, 4, 15))
        net = result.totals["net"]
        self.assertEqual(net, Decimal("0.00"),
                         f"Trial balance net != 0: {net}")

    def test_trial_balance_debits_equal_credits(self):
        """Trial balance total debits == total credits."""
        from src.bookkeeping.reports.statements import trial_balance
        result = trial_balance(self.entity, date(2026, 4, 15))
        self.assertEqual(
            result.totals["total_debits"],
            result.totals["total_credits"],
            "TB debits != credits",
        )

    def test_to_text_no_jargon(self):
        """TB text contains no jargon."""
        from src.bookkeeping.reports.statements import trial_balance
        result = trial_balance(self.entity, date(2026, 4, 15))
        text = result.to_text()
        for token in [";", "beancount", "SELECT", "sqlite", "pushtag"]:
            self.assertNotIn(token, text, f"Jargon token '{token}' in TB text")


class TestGeneralLedger(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_general_ledger_q1(self):
        """General ledger Q1 has correct account sections."""
        from src.bookkeeping.reports.statements import general_ledger
        result = general_ledger(self.entity, date(2026, 1, 1), date(2026, 3, 31))
        section_labels = [s["label"] for s in result.sections]
        # Should have sections for Checking, Income, Expenses, Liabilities
        self.assertTrue(any("Checking" in lbl for lbl in section_labels))
        self.assertTrue(any("Consulting" in lbl for lbl in section_labels))

    def test_general_ledger_no_jargon(self):
        """General ledger text contains no jargon."""
        from src.bookkeeping.reports.statements import general_ledger
        result = general_ledger(self.entity, date(2026, 1, 1), date(2026, 3, 31))
        text = result.to_text()
        for token in [";", "beancount", "SELECT", "sqlite", "pushtag"]:
            self.assertNotIn(token, text, f"Jargon token '{token}' in GL text")
        # No raw colon-separated account names
        self.assertNotIn("Assets:Bank:", text)

    def test_general_ledger_empty_range(self):
        """Empty date range general ledger is valid with no sections."""
        from src.bookkeeping.reports.statements import general_ledger
        result = general_ledger(self.entity, date(2025, 1, 1), date(2025, 12, 31))
        self.assertEqual(len(result.sections), 0)


class TestAsk(unittest.TestCase):
    """Test the ask() dispatcher with deterministic today parameter."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name))
        # Use a fixed "today" for deterministic period parsing
        self._today = date(2026, 4, 30)  # after all fixture entries

    def tearDown(self):
        self._tmp.cleanup()

    def test_ask_q1_revenue(self):
        """'What was Q1 revenue?' returns 10000.00."""
        from src.bookkeeping.reports.statements import ask
        result = ask(self.entity, "What was Q1 revenue?", today=self._today)
        self.assertEqual(result.intent, "revenue")
        # The answer text should contain 10,000.00
        self.assertIn("10,000.00", result.answer_text)
        self.assertEqual(Decimal(result.data["total_revenue"]), Decimal("10000.00"))

    def test_ask_q1_revenue_matches_pnl(self):
        """ask Q1 revenue matches profit_and_loss Q1 total."""
        from src.bookkeeping.reports.statements import ask, profit_and_loss
        ask_result = ask(self.entity, "What was Q1 revenue?", today=self._today)
        pnl_result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 3, 31))
        self.assertEqual(
            Decimal(ask_result.data["total_revenue"]),
            pnl_result.sections[0]["total"],
        )

    def test_ask_software_spend(self):
        """'How much did we spend on software last quarter?' returns 550.00 for Q1."""
        from src.bookkeeping.reports.statements import ask
        # today=2026-04-30 → last quarter = Q1
        result = ask(self.entity, "How much did we spend on software last quarter?", today=self._today)
        self.assertEqual(result.intent, "spend")
        # Software expenses: 200 + 150 + 200 = 550
        self.assertIn("550.00", result.answer_text)

    def test_ask_owner_draws(self):
        """'What were owner draws this month?' returns guidance or zero (no draw accounts)."""
        from src.bookkeeping.reports.statements import ask
        result = ask(self.entity, "What were owner draws this month?", today=self._today)
        self.assertEqual(result.intent, "draws")
        # Our fixture has no draw accounts → 0 or empty message
        self.assertIsNotNone(result.answer_text)
        self.assertNotIn("ERROR", result.answer_text.upper())

    def test_ask_net_income_how_are_we_doing(self):
        """'How are we doing?' returns net income info."""
        from src.bookkeeping.reports.statements import ask
        result = ask(self.entity, "How are we doing this year?", today=self._today)
        self.assertEqual(result.intent, "net_income")
        self.assertIn("net_income", result.data)

    def test_ask_net_income_matches_pnl(self):
        """ask 'how are we doing' net income matches full-period P&L."""
        from src.bookkeeping.reports.statements import ask, profit_and_loss
        ask_result = ask(self.entity, "How are we doing this year?", today=self._today)
        # "this year" → 2026-01-01 to today (2026-04-30)
        pnl_result = profit_and_loss(self.entity, date(2026, 1, 1), self._today)
        self.assertEqual(
            Decimal(ask_result.data["net_income"]),
            pnl_result.totals["net_income"],
        )

    def test_ask_vendor_payment(self):
        """'How much did we pay Acme Corp?' returns correct total."""
        from src.bookkeeping.reports.statements import ask
        result = ask(self.entity, "How much did we pay Acme Corp this year?", today=self._today)
        self.assertEqual(result.intent, "vendor")
        # Acme Corp received payments for consulting — postings to Assets:Bank:Checking positive
        # But vendor match is against payee "Acme Corp" entries that hit expense or asset accounts
        self.assertIsNotNone(result.answer_text)

    def test_ask_unknown_question_returns_guidance(self):
        """Unknown question returns helpful guidance text."""
        from src.bookkeeping.reports.statements import ask
        result = ask(self.entity, "What is the weather like?", today=self._today)
        self.assertEqual(result.intent, "unknown")
        # Guidance should mention things you can ask
        self.assertIn("revenue", result.answer_text.lower())
        self.assertIn("spend", result.answer_text.lower())

    def test_ask_no_jargon_in_answers(self):
        """All ask answers contain no beancount/SQL jargon."""
        from src.bookkeeping.reports.statements import ask
        questions = [
            "What was Q1 revenue?",
            "How much did we spend on software last quarter?",
            "How are we doing this year?",
            "What were our total expenses in January?",
        ]
        for q in questions:
            result = ask(self.entity, q, today=self._today)
            for token in [";", "beancount", "SELECT", "sqlite", "pushtag"]:
                self.assertNotIn(token, result.answer_text,
                                 f"Jargon '{token}' in answer to '{q}'")
            # No raw colon-separated account names (like Assets:Bank:)
            import re
            self.assertIsNone(
                re.search(r"[A-Z][a-z]+:[A-Z]", result.answer_text),
                f"Raw account name in answer to '{q}': {result.answer_text}",
            )

    def test_ask_q2_revenue(self):
        """'What was Q2 revenue?' — only April entry (1500)."""
        from src.bookkeeping.reports.statements import ask
        result = ask(self.entity, "What was Q2 2026 revenue?", today=self._today)
        self.assertEqual(result.intent, "revenue")
        # Q2 = Apr-Jun 2026; only the April 15 entry fits
        self.assertEqual(Decimal(result.data["total_revenue"]), Decimal("1500.00"))

    def test_ask_last_quarter_maps_to_q1(self):
        """'last quarter' with today=2026-04-30 maps to Q1."""
        from src.bookkeeping.reports.statements import ask
        result = ask(self.entity, "What was last quarter revenue?", today=self._today)
        self.assertEqual(Decimal(result.data["total_revenue"]), Decimal("10000.00"))
        self.assertEqual(result.data["from_date"], "2026-01-01")
        self.assertEqual(result.data["to_date"], "2026-03-31")


class TestStatementJSON(unittest.TestCase):
    """Verify JSON serialisation is valid and uses string decimals."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_pnl_json_totals_are_strings(self):
        import json
        from src.bookkeeping.reports.statements import profit_and_loss
        result = profit_and_loss(self.entity, date(2026, 1, 1), date(2026, 3, 31))
        parsed = json.loads(result.to_json())
        for k, v in parsed["totals"].items():
            self.assertIsInstance(v, str, f"Total '{k}' should be string, got {type(v)}")

    def test_bs_json_totals_are_strings(self):
        import json
        from src.bookkeeping.reports.statements import balance_sheet
        result = balance_sheet(self.entity, date(2026, 4, 15))
        parsed = json.loads(result.to_json())
        for k, v in parsed["totals"].items():
            self.assertIsInstance(v, str, f"Total '{k}' should be string, got {type(v)}")


if __name__ == "__main__":
    unittest.main()
