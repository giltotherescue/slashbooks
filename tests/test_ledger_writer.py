from __future__ import annotations

"""Tests for src/bookkeeping/ledger/writer.py"""

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.ledger.model import Balance, Entry, Ledger, Open, Posting  # noqa: E402
from bookkeeping.ledger.writer import (  # noqa: E402
    _sanitize,
    render_balance,
    render_entry,
    render_header,
    render_ledger,
    render_open,
    render_session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    txn_date: date = date(2026, 1, 15),
    narration: str = "Test transaction",
    payee: str | None = None,
    debit_account: str = "Assets:Checking",
    credit_account: str = "Income:Services",
    amount: Decimal = Decimal("100.00"),
    meta: tuple = (),
    tags: tuple = (),
) -> Entry:
    return Entry(
        date=txn_date,
        narration=narration,
        payee=payee,
        meta=meta,
        tags=tags,
        postings=(
            Posting(debit_account, amount),
            Posting(credit_account, -amount),
        ),
    )


# ---------------------------------------------------------------------------
# render_header
# ---------------------------------------------------------------------------


class TestRenderHeader(unittest.TestCase):
    def test_contains_required_options(self) -> None:
        h = render_header("My Books")
        self.assertIn('option "title" "My Books"', h)
        self.assertIn('option "operating_currency" "USD"', h)
        self.assertIn('option "inferred_tolerance_default" "USD:0.005"', h)

    def test_ends_with_blank_line(self) -> None:
        h = render_header("Test")
        self.assertTrue(h.endswith("\n"))

    def test_title_with_double_quote_escaped(self) -> None:
        h = render_header('Books "2026"')
        self.assertIn('\\"2026\\"', h)

    def test_title_newline_stripped(self) -> None:
        h = render_header("Books\nEvil")
        self.assertNotIn("\n", h.split('option "title"')[1].split("\n")[0])


# ---------------------------------------------------------------------------
# render_open
# ---------------------------------------------------------------------------


class TestRenderOpen(unittest.TestCase):
    def test_basic_open_no_currency(self) -> None:
        o = Open(date=date(2026, 1, 1), account="Assets:Checking")
        line = render_open(o)
        self.assertEqual(line, "2026-01-01 open Assets:Checking")

    def test_open_with_currency(self) -> None:
        o = Open(date=date(2026, 1, 1), account="Assets:Checking", currencies=("USD",))
        line = render_open(o)
        self.assertEqual(line, "2026-01-01 open Assets:Checking USD")

    def test_no_trailing_newline(self) -> None:
        o = Open(date=date(2026, 1, 1), account="Assets:Checking")
        self.assertNotIn("\n", render_open(o))


# ---------------------------------------------------------------------------
# render_balance
# ---------------------------------------------------------------------------


class TestRenderBalance(unittest.TestCase):
    def test_basic_balance(self) -> None:
        b = Balance(date=date(2026, 2, 1), account="Assets:Checking", amount=Decimal("1234.56"))
        line = render_balance(b)
        self.assertEqual(line, "2026-02-01 balance Assets:Checking 1234.56 USD")

    def test_negative_balance(self) -> None:
        b = Balance(date=date(2026, 2, 1), account="Liabilities:CreditCard", amount=Decimal("-500.00"))
        line = render_balance(b)
        self.assertIn("-500.00", line)

    def test_integer_amount_gets_dot_zero_zero(self) -> None:
        # Amount must be quantized at model level; but we test the formatter
        b = Balance(date=date(2026, 2, 1), account="Assets:Checking", amount=Decimal("1000.00"))
        line = render_balance(b)
        self.assertIn("1000.00", line)


# ---------------------------------------------------------------------------
# render_entry
# ---------------------------------------------------------------------------


class TestRenderEntry(unittest.TestCase):
    def test_narration_only_format(self) -> None:
        e = _make_entry(narration="Invoice payment")
        text = render_entry(e)
        self.assertIn('2026-01-15 * "Invoice payment"', text)

    def test_payee_and_narration_format(self) -> None:
        e = _make_entry(narration="Invoice payment", payee="Acme Corp")
        text = render_entry(e)
        self.assertIn('2026-01-15 * "Acme Corp" "Invoice payment"', text)

    def test_postings_indented(self) -> None:
        e = _make_entry()
        text = render_entry(e)
        self.assertIn("  Assets:Checking  100.00 USD", text)
        self.assertIn("  Income:Services  -100.00 USD", text)

    def test_integer_amounts_emit_dot_zero_zero(self) -> None:
        e = _make_entry(amount=Decimal("250.00"))
        text = render_entry(e)
        self.assertIn("250.00", text)
        self.assertIn("-250.00", text)

    def test_metadata_on_entry(self) -> None:
        e = _make_entry(meta=(("source-id", "txn_abc"),))
        text = render_entry(e)
        self.assertIn('  source-id: "txn_abc"', text)

    def test_tags_on_header_line(self) -> None:
        e = _make_entry(tags=("import-2026",))
        text = render_entry(e)
        first_line = text.split("\n")[0]
        self.assertIn("#import-2026", first_line)

    def test_ends_with_blank_line(self) -> None:
        e = _make_entry()
        text = render_entry(e)
        self.assertTrue(text.endswith("\n"))

    # --- Injection / escaping ---

    def test_narration_with_double_quote_escaped(self) -> None:
        e = _make_entry(narration='Payment "rush" fee')
        text = render_entry(e)
        # The rendered line should have escaped quotes
        first_line = text.split("\n")[0]
        self.assertIn('\\"rush\\"', first_line)

    def test_narration_with_newline_stripped(self) -> None:
        e = _make_entry(narration="Payment\nevil line")
        text = render_entry(e)
        # The newline must be stripped: the header line must not contain a raw \n
        # (i.e., the whole rendered text is still parseable and the header line
        # doesn't end prematurely due to an injected newline in the narration).
        first_line = text.split("\n")[0]
        # The narration in the file is quoted; what matters is no raw newline
        # is left inside the quoted string that would break the directive line.
        self.assertNotIn("\n", first_line)
        # The sanitized text should have the control char removed (joined without space)
        self.assertIn("Paymentevil line", first_line)

    def test_narration_with_cr_stripped(self) -> None:
        e = _make_entry(narration="Payment\revil")
        text = render_entry(e)
        first_line = text.split("\n")[0]
        # CR is a control character — must not appear in the output line
        self.assertNotIn("\r", first_line)
        # Remaining text is joined (no space inserted for stripped control char)
        self.assertIn("Paymentevil", first_line)

    def test_narration_with_beancount_fragment_escaped(self) -> None:
        # A narration that looks like a directive must not corrupt the file
        e = _make_entry(narration='2026-01-01 open Assets:Evil "injected"')
        text = render_entry(e)
        # The narration is wrapped in quotes; the inner quotes should be escaped
        # and the directive text should not appear unquoted at start of line
        self.assertNotIn('\n2026-01-01 open', text)

    def test_payee_with_newline_stripped(self) -> None:
        e = _make_entry(narration="Valid", payee="Payee\nevil")
        text = render_entry(e)
        first_line = text.split("\n")[0]
        # No raw newline should appear inside the quoted payee value
        self.assertNotIn("\n", first_line)
        # Sanitized: control char stripped, text joined
        self.assertIn("Payeeevil", first_line)

    def test_metadata_value_with_newline_stripped(self) -> None:
        e = _make_entry(meta=(("source-id", "abc\nevil"),))
        text = render_entry(e)
        # Find the source-id metadata line
        meta_line = [ln for ln in text.split("\n") if "source-id" in ln][0]
        # No raw newline inside the metadata line
        self.assertNotIn("\n", meta_line)
        # Control char stripped: text joined
        self.assertIn("abcevil", meta_line)

    def test_metadata_value_with_quote_escaped(self) -> None:
        e = _make_entry(meta=(("source-id", 'abc"def'),))
        text = render_entry(e)
        # The embedded double-quote must be escaped as \"
        self.assertIn('\\"', text)
        # The specific escaped sequence
        self.assertIn('abc\\"def', text)

    def test_round_trip_narration_with_sanitized_quote(self) -> None:
        """Round-trip: sanitized narration survives write→parse."""
        from bookkeeping.ledger.validator import parse_ledger

        e = _make_entry(narration='Payment "rush" fee')
        opens = [
            Open(date=date(2026, 1, 1), account="Assets:Checking", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Income:Services", currencies=("USD",)),
        ]
        text = render_ledger(opens, [e], [])
        parsed = parse_ledger(text)
        self.assertEqual(len(parsed["entries"]), 1)
        # The narration should have escaped quotes preserved as literal quote chars
        self.assertIn("rush", parsed["entries"][0].narration)


# ---------------------------------------------------------------------------
# render_session
# ---------------------------------------------------------------------------


class TestRenderSession(unittest.TestCase):
    def test_pushtag_poptag_wrapping(self) -> None:
        e1 = _make_entry(txn_date=date(2026, 1, 15))
        text = render_session([e1], session_id="test-session-001")
        self.assertIn("pushtag #import-test-session-001", text)
        self.assertIn("poptag #import-test-session-001", text)

    def test_entries_date_ordered(self) -> None:
        e1 = _make_entry(txn_date=date(2026, 1, 20), narration="Later")
        e2 = _make_entry(txn_date=date(2026, 1, 10), narration="Earlier")
        text = render_session([e1, e2], session_id="s1")
        pos_earlier = text.index("Earlier")
        pos_later = text.index("Later")
        self.assertLess(pos_earlier, pos_later)

    def test_deterministic_output(self) -> None:
        e1 = _make_entry()
        t1 = render_session([e1], session_id="s1")
        t2 = render_session([e1], session_id="s1")
        self.assertEqual(t1, t2)

    def test_session_id_not_generated_internally(self) -> None:
        """Session ID must come from caller; two calls with same ID are identical."""
        e1 = _make_entry()
        t1 = render_session([e1], session_id="fixed-id")
        t2 = render_session([e1], session_id="fixed-id")
        self.assertEqual(t1, t2)


# ---------------------------------------------------------------------------
# render_ledger
# ---------------------------------------------------------------------------


class TestRenderLedger(unittest.TestCase):
    def _make_ledger_parts(self):
        opens = [
            Open(date=date(2026, 1, 1), account="Assets:Checking", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Income:Services", currencies=("USD",)),
            Open(date=date(2026, 1, 1), account="Expenses:Software", currencies=("USD",)),
        ]
        entries = [
            Entry(
                date=date(2026, 1, 15),
                narration="Client invoice",
                payee="Acme Corp",
                meta=(("source-id", "txn_001"),),
                postings=(
                    Posting("Assets:Checking", Decimal("1500.00")),
                    Posting("Income:Services", Decimal("-1500.00")),
                ),
            ),
            Entry(
                date=date(2026, 1, 20),
                narration="Software subscription",
                meta=(("source-id", "txn_002"),),
                postings=(
                    Posting("Expenses:Software", Decimal("49.00")),
                    Posting("Assets:Checking", Decimal("-49.00")),
                ),
            ),
        ]
        balances = [
            Balance(date=date(2026, 2, 1), account="Assets:Checking", amount=Decimal("1451.00")),
        ]
        return opens, entries, balances

    def test_header_present(self) -> None:
        opens, entries, balances = self._make_ledger_parts()
        text = render_ledger(opens, entries, balances, title="Test Books")
        self.assertIn('option "title" "Test Books"', text)

    def test_opens_before_entries(self) -> None:
        opens, entries, balances = self._make_ledger_parts()
        text = render_ledger(opens, entries, balances)
        open_pos = text.index("open Assets:Checking")
        entry_pos = text.index("Client invoice")
        self.assertLess(open_pos, entry_pos)

    def test_entries_date_ordered(self) -> None:
        opens, entries, balances = self._make_ledger_parts()
        text = render_ledger(opens, entries, balances)
        pos1 = text.index("Client invoice")
        pos2 = text.index("Software subscription")
        self.assertLess(pos1, pos2)

    def test_balance_assertion_present(self) -> None:
        opens, entries, balances = self._make_ledger_parts()
        text = render_ledger(opens, entries, balances)
        self.assertIn("balance Assets:Checking", text)

    def test_empty_ledger_produces_valid_header(self) -> None:
        text = render_ledger([], [], [], title="Empty")
        self.assertIn('option "title" "Empty"', text)
        self.assertIn("operating_currency", text)

    def test_deterministic_byte_for_byte(self) -> None:
        opens, entries, balances = self._make_ledger_parts()
        t1 = render_ledger(opens, entries, balances, title="Test")
        t2 = render_ledger(opens, entries, balances, title="Test")
        self.assertEqual(t1, t2)


# ---------------------------------------------------------------------------
# _sanitize helper
# ---------------------------------------------------------------------------


class TestSanitize(unittest.TestCase):
    def test_strips_newline(self) -> None:
        self.assertNotIn("\n", _sanitize("hello\nworld"))

    def test_strips_cr(self) -> None:
        self.assertNotIn("\r", _sanitize("hello\rworld"))

    def test_strips_null(self) -> None:
        self.assertNotIn("\x00", _sanitize("hello\x00world"))

    def test_escapes_double_quote(self) -> None:
        self.assertEqual(_sanitize('say "hi"'), 'say \\"hi\\"')

    def test_strips_tab(self) -> None:
        self.assertNotIn("\t", _sanitize("hello\tworld"))

    def test_clean_string_unchanged(self) -> None:
        self.assertEqual(_sanitize("hello world"), "hello world")


if __name__ == "__main__":
    unittest.main()
