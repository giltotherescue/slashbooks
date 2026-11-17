"""Tests for the month-end proof from a bank feed (``books balance prove``).

All data is synthetic.
"""

from __future__ import annotations

import contextlib
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
import hashlib
import io
import itertools
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bookkeeping.cli import main  # noqa: E402
from bookkeeping.coverage import check_coverage, record_feed_window, record_statement  # noqa: E402
from bookkeeping.feed_proof import prove  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.readiness import DependencyVersions, assess_review_readiness  # noqa: E402
from bookkeeping.reconcile import record_basis, reconcile  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402
from test_tieout import CARD, CHECKING, make_company, stage  # noqa: E402

SAVINGS = "Assets:Bank:Savings"
STATEMENT_CODES = {"reconciliation_missing", "reconciliation_evidence_missing", "reconciliation_not_clean",
                   "opening_evidence_missing", "source_statement_incomplete", "source_evidence_provenance_missing"}
_ids = itertools.count()


def reading(balance: str, day: str = "2026-10-05", **kw) -> dict:
    """A reading for ``day`` that was read on that day; every month end before it is over."""
    return {"balance": balance, "as_of": day, "retrieved_at": f"{day}T09:00:00Z", "data_through": day,
            "partial": False, **kw}


def post(company: Path, account: str, day: str, amount: str, *, other: str = "Income:Consulting",
         source: bool = True, currency: str = "USD", meta: tuple = (), source_id: str | None = None) -> str | None:
    """Post one two-sided entry, by default a bank feed row; ``amount`` is the ledger-sign amount on ``account``.

    ``source_id`` gives another source ID, for example a CSV row; ``source=False`` gives none.
    """
    store = LedgerStore(company / "ledger.sqlite")
    store.initialize()
    if source_id is None and source:
        # A bank feed row as ``books ingest --source banksync`` writes it: a namespaced ID and the provider mark.
        source_id, meta = f"banksync:t{next(_ids)}", (("feed-provider", "banksync"),) + meta
    meta = ((("source-id", source_id),) if source_id else ()) + meta
    with store.transaction() as conn:
        store.insert_opens([Open(date(2026, 1, 1), name, (currency,)) for name in (account, other)], conn)
        store.insert_entries([Entry(
            date=date.fromisoformat(day), narration="Synthetic", meta=meta,
            postings=(Posting(account, Decimal(amount), currency), Posting(other, -Decimal(amount), currency)))], conn)
    return source_id


def apply(company: Path, figures: dict, ref: str = "evidence:feed-balance-abc"):
    return reconcile(company, figures["account"], Decimal(figures["source_balance"]), date.fromisoformat(figures["as_of"]),
                     source_opening_balance=Decimal(figures["source_opening_balance"]),
                     source_transaction_total=Decimal(figures["source_transaction_total"]),
                     source_snapshot_at=figures["source_snapshot_at"], source_through=figures["source_through"],
                     source_evidence_refs=[ref], source_evidence_hashes={ref: "b" * 64},
                     proof_basis=figures.get("proof_basis"))


def month_end(balance: str, day: str) -> dict:
    """A reading for the month end ``day``, read early the next day after the safety margin."""
    after = date.fromisoformat(day).toordinal() + 1
    return {**reading(balance, day), "retrieved_at": f"{date.fromordinal(after).isoformat()}T06:00:00Z"}


class FeedProofTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.company = make_company(Path(self._tmp.name), {
            "feed-chk": CHECKING, "feed-card": CARD, "feed-sav": SAVINGS})
        self.books()
        record_feed_window(self.company, account=CHECKING, start=date(2026, 8, 1), end=date(2026, 10, 5),
                           source="feed-chk")

    def reset_books(self):
        """Start the books again with only the rows of ``books``; feed windows and records stay."""
        for path in self.company.glob("ledger.sqlite*"):
            path.unlink()
        self.books()

    def books(self):
        store = LedgerStore(self.company / "ledger.sqlite")
        store.initialize()
        with store.transaction() as conn:
            store.set_meta("canonical", "true", conn)
            store.set_meta("account_catalog", "sqlite", conn)
            store.insert_opens([Open(date(2026, 1, 1), name, ("USD",)) for name in (SAVINGS, CARD)], conn)
        post(self.company, CHECKING, "2026-08-20", "500.00")
        post(self.company, CHECKING, "2026-09-10", "1000.00")
        post(self.company, CHECKING, "2026-09-25", "-200.00", other="Expenses:Software")
        post(self.company, CHECKING, "2026-10-02", "-50.00", other="Expenses:Software")

    def proof(self, readings, account_id="feed-chk", account_type="checking", as_of=date(2026, 10, 20), **kw):
        extra = {"time_zone": "UTC", **{key: kw.pop(key) for key in ("currency", "time_zone") if key in kw}}
        extra = {key: value for key, value in extra.items() if value is not None}
        document = prove(self.company, [{"account_id": account_id, "account_type": account_type,
                                         "readings": readings, **extra}], as_of=as_of, **kw)
        return {row["month"]: row for row in document["months"]}

    def test_feed_balance_proves_every_finished_month_it_covers(self):
        months = self.proof([reading("1250.00")])
        self.assertEqual(sorted(months), ["2026-08", "2026-09"])
        september = months["2026-09"]
        self.assertEqual((september["status"], september["reason"]), ("proven", "proven"))
        self.assertEqual(september["reconcile"], {
            "account": CHECKING, "as_of": "2026-09-30", "source_balance": "1300.00",
            "source_opening_balance": "500.00", "source_transaction_total": "800.00",
            "source_snapshot_at": "2026-10-05T09:00:00Z", "source_through": "2026-09-30",
            "proof_basis": "feed_computed"})
        self.assertEqual(september["basis"], "feed_computed", "computed back from a reading on 10-05")
        self.assertEqual(september["reading"]["read_day"], "2026-10-05")
        self.assertEqual(months["2026-08"]["reconcile"]["source_balance"], "500.00")
        self.assertEqual(months["2026-08"]["reconcile"]["source_opening_balance"], "0.00")

    def test_an_intraday_reading_on_the_month_end_does_not_prove_it_but_a_next_day_reading_may(self):
        intraday = {**reading("1300.00", "2026-09-30"), "retrieved_at": "2026-09-30T09:00:00Z"}
        self.assertEqual(self.proof([intraday])["2026-09"]["reason"], "no_balance_after_month_end")
        next_day = {**reading("1300.00", "2026-09-30"), "retrieved_at": "2026-10-01T06:00:00Z"}
        early = {**next_day, "retrieved_at": "2026-10-01T05:59:59Z"}
        self.assertEqual(self.proof([early])["2026-09"]["reason"], "no_balance_after_month_end", "inside the margin")
        months = self.proof([next_day, reading("1250.00")])
        self.assertEqual(months["2026-09"]["reason"], "proven")
        self.assertEqual(months["2026-09"]["reading"]["day"], "2026-09-30")
        self.assertEqual(months["2026-09"]["reconcile"]["source_balance"], "1300.00")
        # With no as_of, the day it was read is the balance day: a read on 10-01 proves September.
        undated = {"balance": "1300.00", "retrieved_at": "2026-10-01T06:00:00Z", "data_through": "2026-10-01",
                   "partial": False}
        self.assertEqual(self.proof([undated])["2026-09"]["reason"], "proven")

    def test_the_bank_time_zone_is_required_and_decides_when_the_month_end_is_over(self):
        undated = lambda at: {"balance": "1300.00", "retrieved_at": at, "data_through": "2026-10-01", "partial": False}
        [row] = self.proof([undated("2026-10-01T12:00:00Z")], time_zone=None).values()
        self.assertEqual(row["reason"], "time_zone_required")
        [row] = self.proof([undated("2026-10-01T12:00:00Z")], time_zone="Mars/Olympus").values()
        self.assertEqual(row["reason"], "unknown_time_zone")
        ny = "America/New_York"
        # 02:00 UTC on 10-01 is 22:00 on 09-30 in New York: the bank day is not over.
        self.assertEqual(self.proof([undated("2026-10-01T02:00:00Z")], time_zone=ny)["2026-09"]["reason"],
                         "no_balance_after_month_end")
        # 09:30 UTC is 05:30 in New York: inside the six-hour margin.
        self.assertEqual(self.proof([undated("2026-10-01T09:30:00Z")], time_zone=ny)["2026-09"]["reason"],
                         "no_balance_after_month_end")
        self.assertEqual(self.proof([undated("2026-10-01T10:30:00Z")], time_zone=ny)["2026-09"]["reason"], "proven")
        # 09-30T23:30-07:00 is 10-01 in UTC, but still September 30 at a Los Angeles bank.
        late = {**undated("2026-09-30T23:30:00-07:00")}
        self.assertEqual(self.proof([late], time_zone="America/Los_Angeles")["2026-09"]["reason"], "no_balance_after_month_end")
        self.assertEqual(self.proof([late], time_zone="UTC")["2026-09"]["reason"], "proven", "06:30 UTC on 10-01")

    def test_the_margin_is_six_real_hours_on_a_daylight_saving_day(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        from bookkeeping.feed_proof import day_closed_at
        ny = ZoneInfo("America/New_York")
        # March 8 2026 is the spring-forward day: its midnight is 05:00 UTC, so the margin ends at 11:00 UTC.
        self.assertEqual(day_closed_at(date(2026, 3, 7), ny), datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc))
        # November 1 2026 is the fall-back day: its midnight is 04:00 UTC.
        self.assertEqual(day_closed_at(date(2026, 10, 31), ny), datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(day_closed_at(date(2026, 9, 30), ny), datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc))

    def test_a_balance_as_of_a_time_after_it_was_read_is_not_used(self):
        item = {**reading("1250.00"), "as_of": "2026-10-05T10:00:00+00:00", "retrieved_at": "2026-10-05T09:00:00Z"}
        self.assertEqual(self.proof([item])["2026-09"]["reason"], "no_balance_after_month_end")
        item = {**item, "as_of": "2026-10-05T05:00:00-04:00"}  # 09:00 UTC, the same instant
        self.assertEqual(self.proof([item])["2026-09"]["reason"], "proven")

    def test_unreadable_waiting_rows_block_the_proof(self):
        path = self.company / "staging" / "pending-categorization.json"
        path.write_text("{not json")
        [row] = self.proof([reading("1250.00")]).values()
        self.assertEqual(row["reason"], "waiting_unreadable")
        for rows in ([{"id": "w", "date": "someday", "amount": "-9.00", "accountId": "feed-chk"}],
                     [{"id": "w", "date": "2026-10-03", "amount": "nine", "accountId": "feed-chk"}],
                     [{"id": "w", "date": "2026-10-03", "accountId": "feed-chk"}],
                     ["not an object"],
                     [{"id": "csv:1", "date": "2026-10-03", "amount": "-9.00", "accountId": CHECKING, "csvMappingKey": "unknown"}],
                     [{"id": "x", "date": "2026-10-03", "amount": "-9.00", "accountName": 123}]):
            stage(self.company, rows)
            [row] = self.proof([reading("1250.00")]).values()
            self.assertEqual(row["reason"], "waiting_unreadable", rows)
        # A broken row of another account does not block this one.
        stage(self.company, [{"id": "w", "date": "someday", "amount": "-9.00", "accountId": "feed-sav"}])
        self.assertEqual(self.proof([reading("1250.00")])["2026-09"]["reason"], "proven")

    def test_a_missing_or_extra_transaction_leaves_the_month_unproven(self):
        months = self.proof([reading("1240.00")])
        self.assertEqual((months["2026-09"]["reason"], months["2026-09"]["difference"]), ("books_differ", "-10.00"))
        self.assertIsNone(months["2026-09"]["reconcile"])

    def test_an_earlier_reading_that_differs_refuses_the_month_even_when_a_later_one_agrees(self):
        months = self.proof([reading("1290.00", "2026-10-01"), reading("1250.00")])
        self.assertEqual((months["2026-09"]["reason"], months["2026-09"]["difference_on"],
                          months["2026-09"]["difference"]), ("books_differ", "2026-10-01", "-10.00"))
        self.assertIsNone(months["2026-09"]["reconcile"])

    def test_a_reading_for_the_month_end_that_differs_decides_the_month(self):
        month_end = {**reading("1290.00", "2026-09-30"), "retrieved_at": "2026-10-01T06:00:00Z"}
        months = self.proof([month_end, reading("1250.00")])
        self.assertEqual((months["2026-09"]["reason"], months["2026-09"]["reading"]["day"]), ("books_differ", "2026-09-30"))

    def test_a_row_dated_in_the_wrong_month_is_found_only_by_a_month_end_reading(self):
        # The bank posted 100.00 on 09-30, the books dated it 10-01. Both agree on 10-05.
        post(self.company, CHECKING, "2026-10-01", "100.00")
        months = self.proof([reading("1350.00")])
        self.assertEqual(months["2026-09"]["reason"], "proven", "the documented limit: not found")
        self.assertEqual(months["2026-09"]["reconcile"]["source_balance"], "1300.00")
        month_end = {**reading("1400.00", "2026-09-30"), "retrieved_at": "2026-10-01T06:00:00Z"}
        self.assertEqual(self.proof([month_end, reading("1350.00")])["2026-09"]["reason"], "books_differ")

    def test_the_opening_must_chain_from_the_month_before(self):
        # A clean August reconciliation record is the opening.
        apply(self.company, self.proof([reading("1250.00")])["2026-08"]["reconcile"])
        months = self.proof([reading("1250.00")], from_month="2026-09")
        self.assertEqual(months["2026-09"]["reason"], "proven")
        self.assertEqual(months["2026-09"]["reconcile"]["source_opening_balance"], "500.00")
        # An August statement whose closing differs from that record and from the books: September is refused.
        record_statement(self.company, {"statement_id": "aug", "account": CHECKING, "kind": "statement",
                                        "period_start": "2026-08-01", "period_end": "2026-08-31",
                                        "opening_balance": "0.00", "closing_balance": "480.00"})
        for _ in range(2):
            row = self.proof([reading("1250.00")], from_month="2026-09")["2026-09"]
            self.assertEqual((row["reason"], row["statement_id"], row["statement_date"], row["difference"]),
                             ("statement_differs", "aug", "2026-08-31", "-20.00"))
            # Also without the record.
            (self.company / "reports" / "reconciliation.json").unlink(missing_ok=True)

    def test_an_open_prior_record_leaves_the_opening_unproven(self):
        reconcile(self.company, CHECKING, Decimal("1.00"), date(2026, 8, 31))
        months = self.proof([reading("1250.00")])
        self.assertEqual(months["2026-08"]["reason"], "existing_record")
        self.assertEqual(months["2026-09"]["reason"], "opening_unproven")

    def test_an_opening_entry_and_sourced_rows_prove_the_first_feed_month_as_computed(self):
        # Rule 7b: an opening entry before the feed, then only rows from a source. The opening is computed back.
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        months = self.proof([reading("1290.00")])
        self.assertEqual([(months[m]["reason"], months[m]["basis"]) for m in ("2026-08", "2026-09")],
                         [("proven", "feed_computed"), ("proven", "feed_computed")])
        self.assertEqual(months["2026-08"]["reconcile"], {
            "account": CHECKING, "as_of": "2026-08-31", "source_balance": "540.00",
            "source_opening_balance": "40.00", "source_transaction_total": "500.00",
            "source_snapshot_at": "2026-10-05T09:00:00Z", "source_through": "2026-08-31",
            "proof_basis": "feed_computed"})
        self.assertEqual(months["2026-09"]["reconcile"]["source_opening_balance"], "540.00")
        # An opening entry dated on the first feed day is also the one opening entry.
        self.reset_books()
        post(self.company, CHECKING, "2026-08-01", "40.00", source=False)
        self.assertEqual(self.proof([reading("1290.00")])["2026-08"]["basis"], "feed_computed")

    def test_the_basis_is_feed_reading_only_for_a_month_end_reading_with_a_read_opening(self):
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        readings = [month_end("540.00", "2026-08-31"), month_end("1340.00", "2026-09-30"), reading("1290.00")]
        # The opening comes from rule 7b: computed, even with a reading on the month end.
        months = self.proof(readings)
        self.assertEqual([months[m]["basis"] for m in ("2026-08", "2026-09")], ["feed_computed", "feed_computed"])
        # A July statement gives the opening: August is a reading, and September chains from it.
        record_statement(self.company, {"statement_id": "jul", "account": CHECKING, "kind": "statement",
                                        "period_start": "2026-07-01", "period_end": "2026-07-31",
                                        "opening_balance": "0.00", "closing_balance": "40.00"})
        months = self.proof(readings)
        self.assertEqual([months[m]["basis"] for m in ("2026-08", "2026-09")], ["feed_reading", "feed_reading"])
        self.assertEqual(months["2026-08"]["reconcile"]["proof_basis"], "feed_reading")
        # Without the month-end reading for September, it is computed back from 10-05.
        months = self.proof([readings[0], readings[2]])
        self.assertEqual([months[m]["basis"] for m in ("2026-08", "2026-09")], ["feed_reading", "feed_computed"])
        # A recorded opening keeps its basis: a feed_computed record makes the next month computed.
        figures = self.proof(readings)["2026-08"]["reconcile"]
        apply(self.company, {**figures, "proof_basis": "feed_computed"})
        self.assertEqual(self.proof(readings, from_month="2026-09")["2026-09"]["basis"], "feed_computed")
        apply(self.company, figures)
        months = self.proof(readings)
        self.assertEqual((months["2026-08"]["reason"], months["2026-08"]["basis"]), ("already_reconciled", "feed_reading"))
        self.assertEqual(months["2026-09"]["basis"], "feed_reading")

    def test_one_unsourced_posting_in_the_range_refuses_the_computed_opening(self):
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        post(self.company, CHECKING, "2026-09-12", "-5.00", other="Expenses:Software", source=False)
        months = self.proof([reading("1285.00")])
        for month in ("2026-08", "2026-09"):
            self.assertEqual((months[month]["reason"], months[month]["unsourced_count"], months[month]["unsourced_from"]),
                             ("unsourced_postings", 1, "2026-09-12"), month)
            self.assertIsNone(months[month]["reconcile"])
        # An unsourced posting after the newest reading is outside the range.
        self.reset_books()
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        post(self.company, CHECKING, "2026-10-09", "-5.00", other="Expenses:Software", source=False)
        self.assertEqual(self.proof([reading("1290.00")])["2026-08"]["reason"], "proven")

    def test_an_exact_reversal_of_a_sourced_row_is_not_an_unsourced_posting(self):
        # A category correction reverses a feed row without a source and posts the replacement with one.
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        original = post(self.company, CHECKING, "2026-09-03", "-30.00", other="Expenses:Software")
        post(self.company, CHECKING, "2026-09-03", "30.00", other="Expenses:Software", source=False,
             meta=(("reverses", original),))
        post(self.company, CHECKING, "2026-09-03", "-30.00", other="Expenses:Travel", source_id="reclassify:abc",
             meta=(("correction-of", original),))
        self.assertEqual(self.proof([reading("1260.00")])["2026-08"]["reason"], "proven")
        # A second reversal of the same row, on another day and for another amount, is unsourced.
        post(self.company, CHECKING, "2026-09-04", "31.00", other="Expenses:Software", source=False,
             meta=(("reverses", original),))
        self.assertEqual(self.proof([reading("1291.00")])["2026-08"]["reason"], "unsourced_postings")

    def test_an_opening_import_with_a_source_id_is_the_opening_entry_not_a_bank_row(self):
        opening = (("import-source", "statement-opening"),)
        first = post(self.company, CHECKING, "2026-07-31", "50.00", source=True, meta=opening)
        # The opening was replaced: its exact reversal on the same day and a new opening cancel out of the count.
        post(self.company, CHECKING, "2026-07-31", "-50.00", source=False, meta=(("reverses", first),) + opening)
        post(self.company, CHECKING, "2026-08-01", "40.00", source=True, meta=opening)
        self.assertEqual(self.proof([reading("1290.00")])["2026-08"]["basis"], "feed_computed")
        # An opening import after the first feed day is not a bank row.
        self.reset_books()
        post(self.company, CHECKING, "2026-08-02", "40.00", source=True, meta=opening)
        months = self.proof([reading("1290.00")])
        self.assertEqual((months["2026-08"]["reason"], months["2026-08"]["unsourced_from"]),
                         ("unsourced_postings", "2026-08-02"))
        # With no bank row at all, only an opening, nothing is proven.
        record_feed_window(self.company, account=SAVINGS, start=date(2026, 8, 1), end=date(2026, 10, 5), source="feed-sav")
        post(self.company, SAVINGS, "2026-08-01", "40.00", source=True, meta=opening)
        self.assertEqual(self.proof([reading("40.00")], account_id="feed-sav", account_type="savings")["2026-08"]["reason"],
                         "no_feed_rows")

    def test_two_opening_entries_or_an_opening_entry_after_the_feed_start_are_refused(self):
        post(self.company, CHECKING, "2026-07-30", "20.00", source=False)
        post(self.company, CHECKING, "2026-07-31", "20.00", source=False)
        months = self.proof([reading("1290.00")])
        self.assertEqual((months["2026-08"]["reason"], months["2026-08"]["opening_entries"]), ("opening_entry_unclear", 2))
        # A row from a source before the feed starts is not the opening entry either.
        self.reset_books()
        post(self.company, CHECKING, "2026-07-31", "20.00", source=False)
        post(self.company, CHECKING, "2026-07-15", "20.00")
        self.assertEqual(self.proof([reading("1290.00")])["2026-08"]["reason"], "opening_entry_unclear")
        # An opening entry dated after the first feed day is an unsourced posting.
        self.reset_books()
        post(self.company, CHECKING, "2026-08-02", "40.00", source=False)
        months = self.proof([reading("1290.00")])
        self.assertEqual((months["2026-08"]["reason"], months["2026-08"]["unsourced_from"]),
                         ("unsourced_postings", "2026-08-02"))

    def test_a_computed_opening_needs_the_feed_without_a_gap_from_its_first_day(self):
        record_feed_window(self.company, account=SAVINGS, start=date(2026, 8, 1), end=date(2026, 8, 20), source="feed-sav")
        record_feed_window(self.company, account=SAVINGS, start=date(2026, 8, 25), end=date(2026, 10, 5), source="feed-sav")
        post(self.company, SAVINGS, "2026-07-31", "10.00", source=False)
        post(self.company, SAVINGS, "2026-09-15", "40.00")
        months = self.proof([reading("50.00")], account_id="feed-sav", account_type="savings")
        self.assertEqual((months["2026-08"]["reason"], months["2026-08"]["uncovered_from"],
                          months["2026-08"]["uncovered_to"]), ("feed_incomplete", "2026-08-21", "2026-08-24"))
        # September's own days are covered, but its computed opening crosses the gap.
        self.assertEqual((months["2026-09"]["reason"], months["2026-09"]["feed_from"],
                          months["2026-09"]["uncovered_from"]), ("feed_incomplete", "2026-08-01", "2026-08-21"))

    def test_a_computed_opening_needs_a_complete_reading_after_the_month_end(self):
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        for readings in ([reading("1290.00", partial=True)], [reading("540.00", "2026-08-30")], []):
            self.assertEqual(self.proof(readings)["2026-08"]["reason"], "no_balance_after_month_end", readings)

    def test_the_newest_reading_must_also_equal_the_books(self):
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        # 10-01 agrees with the books (1340.00); the newest reading, on 10-05, is 10.00 below them.
        months = self.proof([reading("1340.00", "2026-10-01"), reading("1280.00")])
        for month in ("2026-08", "2026-09"):
            row = months[month]
            self.assertEqual((row["reason"], row["difference"], row["difference_on"], row["books_balance"]),
                             ("newest_reading_differs", "-10.00", "2026-10-05", "1290.00"), month)
        # A reading after the feed windows cannot be checked against the books, so it is not the newest one.
        months = self.proof([reading("1290.00"), reading("1.00", "2026-10-09")])
        self.assertEqual(months["2026-08"]["reason"], "proven")
        # Rows that wait in review up to the newest reading refuse the computed opening.
        stage(self.company, [{"id": "w", "date": "2026-10-04", "amount": "-9.00", "accountId": "feed-chk"}])
        self.assertEqual(self.proof([reading("1340.00", "2026-10-01"), reading("1290.00")])["2026-08"]["reason"],
                         "waiting_in_review")

    def test_a_month_with_no_rows_is_computed_back_from_a_later_reading(self):
        record_feed_window(self.company, account=SAVINGS, start=date(2026, 8, 1), end=date(2026, 10, 5), source="feed-sav")
        post(self.company, SAVINGS, "2026-09-15", "40.00")
        months = self.proof([reading("40.00")], account_id="feed-sav", account_type="savings")
        self.assertEqual([(months[m]["reason"], months[m]["reconcile"]["source_balance"]) for m in ("2026-08", "2026-09")],
                         [("proven", "0.00"), ("proven", "40.00")])

    def test_a_quiet_zero_month_with_no_feed_rows_is_not_proven(self):
        record_feed_window(self.company, account=SAVINGS, start=date(2026, 8, 1), end=date(2026, 10, 5), source="feed-sav")
        months = self.proof([reading("0.00")], account_id="feed-sav", account_type="savings")
        self.assertEqual((months["2026-08"]["reason"], months["2026-09"]["reason"]), ("no_feed_rows", "no_feed_rows"))

    def test_reasons_carry_the_figures_for_one_sentence(self):
        # The feed does not go back to July.
        july = self.proof([reading("1250.00")], from_month="2026-07")["2026-07"]
        self.assertEqual((july["reason"], july["feed_from"], july["uncovered_from"], july["uncovered_to"]),
                         ("feed_incomplete", "2026-08-01", "2026-07-01", "2026-07-31"))
        # The bank and the books differ by an amount on a day.
        september = self.proof([reading("1240.00")])["2026-09"]
        self.assertEqual((september["reason"], september["difference"], september["difference_on"]),
                         ("books_differ", "-10.00", "2026-10-05"))
        # This account type needs a statement.
        [row] = prove(self.company, [{"account_id": "feed-card", "account_type": "Credit Card", "time_zone": "UTC",
                                      "readings": [reading("80.00")]}], as_of=date(2026, 10, 20))["months"]
        self.assertEqual((row["reason"], row["account_type"]), ("not_checking_or_savings", "creditcard"))

    def test_cards_and_other_types_are_never_proven(self):
        post(self.company, CARD, "2026-09-05", "-80.00", other="Expenses:Software")
        # Also with an opening entry and only sourced rows, where rule 7b would apply to a bank account.
        post(self.company, CARD, "2026-07-31", "-10.00", other="Expenses:Software", source=False)
        record_feed_window(self.company, account=CARD, start=date(2026, 8, 1), end=date(2026, 10, 5), source="feed-card")
        for account_id, kind in (("feed-card", "credit"), ("feed-card", "checking"), ("feed-chk", "loan"),
                                 ("feed-chk", "treasury"), ("feed-chk", "")):
            [row] = prove(self.company, [{"account_id": account_id, "account_type": kind,
                                          "readings": [reading("1250.00")], "time_zone": "UTC"}],
                                as_of=date(2026, 10, 20))["months"]
            self.assertEqual((row["reason"], row["reconcile"], row["basis"]), ("not_checking_or_savings", None, None))
        [row] = prove(self.company, [{"account_id": "nope", "account_type": "checking"}], as_of=date(2026, 10, 20))["months"]
        self.assertEqual(row["reason"], "unmapped")

    def test_two_feeds_on_one_ledger_account_are_not_proven(self):
        config = json.loads((self.company / "entity.json").read_text())
        config["bank_account_mappings"]["feed-chk-2"] = CHECKING
        (self.company / "entity.json").write_text(json.dumps(config))
        [row] = self.proof([reading("1250.00")]).values()
        self.assertEqual(row["reason"], "shared_ledger_account")

    def test_currency_must_match_and_other_currencies_block(self):
        self.assertEqual(self.proof([reading("1250.00")], currency="USD")["2026-09"]["reason"], "proven")
        [row] = self.proof([reading("1250.00")], currency="EUR").values()
        self.assertEqual(row["reason"], "currency_mismatch")
        post(self.company, CHECKING, "2026-09-12", "5.00", currency="EUR")
        [row] = self.proof([reading("1250.00")]).values()
        self.assertEqual(row["reason"], "other_currency")

    def test_an_unreadable_posting_blocks_the_account(self):
        with LedgerStore(self.company / "ledger.sqlite").connection() as conn:
            conn.execute("UPDATE postings SET amount='not a number' WHERE account=? AND amount='-200.00'", (CHECKING,))
            conn.commit()
        [row] = self.proof([reading("1250.00")]).values()
        self.assertEqual(row["reason"], "unreadable_postings")

    def test_feed_gap_or_late_start_leaves_the_month_unproven(self):
        record_feed_window(self.company, account=SAVINGS, start=date(2026, 9, 12), end=date(2026, 10, 5), source="feed-sav")
        post(self.company, SAVINGS, "2026-09-15", "40.00")
        months = self.proof([reading("40.00")], account_id="feed-sav", account_type="savings")
        self.assertEqual(months["2026-09"]["reason"], "feed_incomplete")
        months = self.proof([reading("1250.00", "2026-10-08", data_through="2026-10-08")])
        self.assertEqual(months["2026-09"]["reason"], "feed_incomplete")

    def test_pending_partial_or_undated_readings_are_not_used(self):
        for item in (reading("1250.00", data_through="2026-10-04"), reading("1250.00", partial=True),
                     reading("1250.00", partial="false"), reading("1250.00", partial="yes"), reading("1250.00", partial=0),
                     {key: value for key, value in reading("1250.00").items() if key != "partial"},
                     {**reading("1250.00"), "retrieved_at": "2026-10-05T09:00:00"},
                     {**reading("1250.00"), "as_of": "2026-10-05T00:00:00"},
                     reading("1300.00", "2026-09-29")):
            months = self.proof([item])
            self.assertEqual(months["2026-09"]["reason"], "no_balance_after_month_end", item)
        self.assertEqual(self.proof([reading("1250.00")], as_of=date(2026, 10, 4))["2026-09"]["reason"],
                         "no_balance_after_month_end")

    def test_waiting_rows_block_the_proof(self):
        stage(self.company, [{"id": "w", "date": "2026-10-03", "amount": "-9.00", "accountId": "feed-chk"}])
        self.assertEqual(self.proof([reading("1250.00")])["2026-09"]["reason"], "waiting_in_review")

    def test_recorded_months_are_idempotent_and_statement_records_are_kept(self):
        months = self.proof([reading("1250.00")])
        for month in ("2026-08", "2026-09"):
            self.assertTrue(apply(self.company, months[month]["reconcile"]).is_clean)
        again = self.proof([reading("1250.00")])
        self.assertEqual([again[m]["reason"] for m in ("2026-08", "2026-09")], ["already_reconciled", "already_reconciled"])
        self.assertTrue(all(row["status"] == "not_proven" for row in again.values()))
        # A later change to the books makes the feed record stale, so the feed checks it again.
        post(self.company, CHECKING, "2026-09-28", "-1.00", other="Expenses:Software")
        self.assertEqual(self.proof([reading("1250.00")])["2026-09"]["reason"], "books_differ")

    def test_a_back_dated_row_in_a_computed_month_unproves_it_on_recheck(self):
        self.only_checking()
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        months = self.proof([reading("1290.00")])
        for month in ("2026-08", "2026-09"):
            self.assertEqual(months[month]["basis"], "feed_computed")
            self.assertTrue(apply(self.company, months[month]["reconcile"]).is_clean)
        self.assertNotIn("reconciliation_not_clean", self.codes("2026-08"))
        # The feed delivers a row dated in August after the month was recorded.
        post(self.company, CHECKING, "2026-08-25", "-30.00", other="Expenses:Software")
        months = self.proof([reading("1290.00")])
        self.assertEqual([(months[m]["reason"], months[m]["difference"]) for m in ("2026-08", "2026-09")],
                         [("books_differ", "30.00"), ("books_differ", "30.00")])
        self.assertIn("reconciliation_not_clean", self.codes("2026-08"))
        # A new reading that includes the row proves August again, with the new closing.
        months = self.proof([reading("1260.00", "2026-10-05")])
        self.assertEqual((months["2026-08"]["reason"], months["2026-08"]["reconcile"]["source_balance"]),
                         ("proven", "510.00"))

    def only_checking(self):
        config = json.loads((self.company / "entity.json").read_text())
        config["bank_account_mappings"] = {"feed-chk": CHECKING}
        (self.company / "entity.json").write_text(json.dumps(config))

    def readiness(self, period: str = "2026-09", **kw) -> dict:
        return assess_review_readiness(self.company, period, DependencyVersions(1, 1, 1, 1),
                                       as_of=date(2026, 10, 20), **kw).to_document()

    def codes(self, period: str = "2026-09", **kw) -> set:
        return {item["code"] for item in self.readiness(period, **kw)["blockers"]
                if not item["refs"] or CHECKING in item["refs"]}

    def test_a_proven_month_clears_the_statement_blockers_in_readiness(self):
        self.only_checking()
        self.assertTrue(self.codes() & STATEMENT_CODES)
        apply(self.company, self.proof([reading("1250.00")])["2026-09"]["reconcile"])
        self.assertEqual(self.codes() & STATEMENT_CODES, set())
        # With the flag off, a feed_computed month is ready like any other reconciled month.
        self.assertEqual((self.readiness()["state"], self.readiness()["blockers"]), ("ready_for_review", []))

    def test_require_statements_blocks_months_checked_against_the_bank_connection(self):
        self.only_checking()
        readings = [month_end("500.00", "2026-08-31"), month_end("1300.00", "2026-09-30"), reading("1250.00")]
        reading_figures = self.proof(readings)["2026-09"]["reconcile"]
        computed_figures = self.proof([reading("1250.00")])["2026-09"]["reconcile"]
        self.assertEqual((reading_figures["proof_basis"], computed_figures["proof_basis"]),
                         ("feed_reading", "feed_computed"))
        for figures in (reading_figures, computed_figures):
            apply(self.company, figures)
            self.assertEqual(self.codes(), set(), "the flag is off by default")
            document = self.readiness(require_statements=True)
            [blocker] = [item for item in document["blockers"] if item["code"] == "statement_required"]
            self.assertEqual(blocker["refs"], [CHECKING])
            self.assertIn("--statement-id", blocker["action"])
            self.assertEqual(self.codes(require_statements=True), {"statement_required"})
            self.assertIs(document["require_statements"], True)
            self.assertEqual(document["reconciliations"][0]["proof_basis"], figures["proof_basis"])
            self.assertNotIn("require_statements", self.readiness())
        # A month reconciled from a statement is not blocked.
        ref = f"ingestion/statements/files/{'c' * 64}.csv"
        reconcile(self.company, CHECKING, Decimal("1300.00"), date(2026, 9, 30),
                  source_opening_balance=Decimal("500.00"), source_transaction_total=Decimal("800.00"),
                  source_snapshot_at="2026-10-02T12:00:00+00:00", source_through="2026-09-30",
                  source_evidence_refs=[ref], source_evidence_hashes={ref: "c" * 64})
        self.assertEqual(self.codes(require_statements=True), set())
        self.assertEqual(self.readiness(require_statements=True)["reconciliations"][0]["proof_basis"], "statement")
        # The CLI flag.
        out = io.StringIO()
        apply(self.company, computed_figures)
        with redirect_stdout(out):
            main(["readiness", "--entity", str(self.company), "--period", "2026-09", "--as-of", "2026-10-20",
                  "--require-statements"])
        self.assertIn("statement_required", {item["code"] for item in json.loads(out.getvalue())["blockers"]})

    def test_the_proof_basis_round_trips_through_the_record_coverage_and_readiness(self):
        self.only_checking()
        figures = self.proof([reading("1250.00")])["2026-09"]["reconcile"]
        result = apply(self.company, figures)
        self.assertEqual(result.proof_basis, "feed_computed")
        self.assertIn("Proof basis:     feed_computed", result.to_text())
        [record] = [r for r in json.loads((self.company / "reports" / "reconciliation.json").read_text())
                    if r["as_of"] == "2026-09-30"]
        self.assertEqual(record["proof_basis"], "feed_computed")
        [account] = check_coverage(self.company, date(2026, 9, 1), date(2026, 9, 30), accounts=[CHECKING])["accounts"]
        self.assertEqual(account["months"][0]["proof_basis"], "feed_computed")
        self.assertEqual(self.readiness()["reconciliations"][0]["proof_basis"], "feed_computed")
        self.assertEqual(self.proof([reading("1250.00")])["2026-09"]["basis"], "feed_computed")
        # A stale record proves nothing in coverage.
        post(self.company, CHECKING, "2026-09-28", "-1.00", other="Expenses:Software")
        [account] = check_coverage(self.company, date(2026, 9, 1), date(2026, 9, 30), accounts=[CHECKING])["accounts"]
        self.assertNotIn("proof_basis", account["months"][0])
        # The CLI stores the basis it is given.
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["reconcile", "--entity", str(self.company), "--account", CHECKING, "--as-of", "2026-09-30",
                         "--source-balance", "1299.00", "--source-evidence-ref", "evidence:feed-balance-x",
                         "--proof-basis", "feed_reading"])
        self.assertEqual(code, 0)
        [record] = [r for r in json.loads((self.company / "reports" / "reconciliation.json").read_text())
                    if r["as_of"] == "2026-09-30"]
        self.assertEqual(record["proof_basis"], "feed_reading")

    def test_reconcile_refuses_a_proof_basis_that_its_evidence_does_not_support(self):
        def run(basis, refs):
            return reconcile(self.company, CHECKING, Decimal("1300.00"), date(2026, 9, 30),
                             source_evidence_refs=refs, proof_basis=basis)
        archive = f"ingestion/statements/files/{'a' * 64}.pdf"
        for basis, refs in (("feed_computed", ["stmt-9"]), ("feed_reading", []),
                            ("feed_reading", ["evidence:feed-balance-a", "stmt-9"]),
                            ("statement", ["evidence:feed-balance-a"]), ("statement", ["stmt-9"]),
                            ("statement", ["evidence:feed-balance-1", archive]), ("statement", []),
                            ("bank", ["stmt-9"])):
            with self.assertRaises(ValueError, msg=(basis, refs)):
                run(basis, refs)
        self.assertFalse((self.company / "reports" / "reconciliation.json").exists())
        self.assertEqual(run("statement", [archive]).proof_basis, "statement")
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(["reconcile", "--entity", str(self.company), "--statement-id", "s1", "--proof-basis",
                         "statement"])
            self.assertEqual(code, 1)
            code = main(["reconcile", "--entity", str(self.company), "--account", CHECKING, "--as-of", "2026-09-30",
                         "--source-balance", "1.00", "--source-evidence-ref", "stmt-9", "--proof-basis", "feed_reading"])
            self.assertEqual(code, 1)
        self.assertIn("evidence:feed-balance-", err.getvalue())

    def test_a_record_without_a_proof_basis_gets_the_weaker_reading_of_its_evidence(self):
        feed, archive = ["evidence:feed-balance-a"], [f"ingestion/statements/files/{'d' * 64}.pdf"]
        cases = (({"source_evidence_refs": feed}, "feed_computed"),
                 ({"source_evidence_refs": archive}, "statement"),
                 ({"source_evidence_refs": ["evidence/checking.json"]}, None),
                 ({"source_evidence_refs": []}, None),
                 ({"proof_basis": "feed_reading", "source_evidence_refs": feed}, "feed_reading"),
                 ({"proof_basis": "feed_reading", "source_evidence_refs": archive}, "statement"),
                 ({"proof_basis": "statement", "source_evidence_refs": feed}, "feed_computed"),
                 ({"proof_basis": "statement", "source_evidence_refs": ["paper"]}, None),
                 ({"proof_basis": "statement", "source_evidence_refs": [*archive, "note"]}, None),
                 ({"proof_basis": "guess", "source_evidence_refs": ["paper"]}, None))
        for record, basis in cases:
            self.assertEqual(record_basis(record), basis, record)

    def test_a_later_statement_that_agrees_outranks_and_replaces_the_feed_record(self):
        self.only_checking()
        months = self.proof([reading("1250.00")])
        for month in ("2026-08", "2026-09"):
            apply(self.company, months[month]["reconcile"])
        record_statement(self.company, {"statement_id": "sep", "account": CHECKING, "kind": "statement",
                                        "period_start": "2026-09-01", "period_end": "2026-09-30",
                                        "opening_balance": "500.00", "closing_balance": "1300.00",
                                        "evidence_ref": f"ingestion/statements/files/{'e' * 64}.csv",
                                        "evidence_sha256": "e" * 64})
        # The statement outranks the feed record at once: coverage and readiness count the month as a statement.
        coverage = check_coverage(self.company, date(2026, 9, 1), date(2026, 9, 30), accounts=[CHECKING])
        self.assertEqual(coverage["accounts"][0]["months"][0]["proof_basis"], "statement")
        self.assertEqual(coverage["findings"], [])
        self.assertEqual(self.codes(require_statements=True), set())
        self.assertEqual(self.proof([reading("1250.00")])["2026-09"]["reason"], "already_reconciled")
        # Reconciling from the statement replaces the feed record; the feed then leaves it alone.
        ref = f"ingestion/statements/files/{'e' * 64}.csv"
        reconcile(self.company, CHECKING, Decimal("1300.00"), date(2026, 9, 30),
                  source_opening_balance=Decimal("500.00"), source_transaction_total=Decimal("800.00"),
                  source_snapshot_at="2026-10-02T12:00:00+00:00", source_through="2026-09-30",
                  source_evidence_refs=[ref], source_evidence_hashes={ref: "e" * 64})
        records = [r for r in json.loads((self.company / "reports" / "reconciliation.json").read_text())
                   if r["as_of"] == "2026-09-30"]
        self.assertEqual([record_basis(r) for r in records], ["statement"])
        post(self.company, CHECKING, "2026-09-29", "-2.00", other="Expenses:Software")
        self.assertEqual(self.proof([reading("1247.00")])["2026-09"]["reason"], "existing_record")

    def test_a_feed_record_goes_stale_and_a_statement_month_is_not_proven_again_from_the_feed(self):
        months = self.proof([reading("1250.00")])
        apply(self.company, months["2026-09"]["reconcile"])
        record_statement(self.company, {"statement_id": "sep", "account": CHECKING, "kind": "statement",
                                        "period_start": "2026-09-01", "period_end": "2026-09-30",
                                        "opening_balance": "500.00", "closing_balance": "1299.00"})
        post(self.company, CHECKING, "2026-09-29", "-1.00", other="Expenses:Software")
        row = self.proof([reading("1249.00")])["2026-09"]
        self.assertEqual((row["reason"], row["statement_ids"], row["reconcile"]), ("statement_available", ["sep"], None))

    def test_a_later_statement_that_disagrees_shows_the_difference_and_blocks_readiness(self):
        self.only_checking()
        months = self.proof([reading("1250.00")])
        for month in ("2026-08", "2026-09"):
            apply(self.company, months[month]["reconcile"])
        self.assertEqual(self.codes(), set())
        # The statement closes 10.00 below the books: the existing tie-out difference shows and blocks.
        record_statement(self.company, {"statement_id": "sep", "account": CHECKING, "kind": "statement",
                                        "period_start": "2026-09-01", "period_end": "2026-09-30",
                                        "opening_balance": "500.00", "closing_balance": "1290.00"})
        document = self.readiness()
        self.assertEqual(document["state"], "blocked")
        codes = {item["code"] for item in document["blockers"]}
        self.assertTrue({"statement_tieout_differs", "statement_feed_differs"} <= codes, codes)
        [differs] = [item for item in document["blockers"] if item["code"] == "statement_tieout_differs"]
        self.assertIn("difference -10.00", differs["message"])
        self.assertNotIn("proof_basis", document["coverage"][0]["months"][0])
        # An opening and a closing 10.00 lower: the tie-out of the activity matches, but the month end differs.
        record_statement(self.company, {"statement_id": "sep", "account": CHECKING, "kind": "statement",
                                        "period_start": "2026-09-01", "period_end": "2026-09-30",
                                        "opening_balance": "490.00", "closing_balance": "1290.00"}, replace=True)
        document = self.readiness()
        self.assertEqual(document["coverage"][0]["months"][0]["tieout"]["status"], "matches")
        [feed] = [item for item in document["blockers"] if item["code"] == "statement_feed_differs"]
        self.assertIn("1,290.00", feed["message"])
        self.assertIn("1,300.00", feed["message"])
        self.assertEqual(document["state"], "blocked")

    # -- Review findings: each attack case is a test ------------------------------------------------------------

    def test_review_a5_every_reading_for_the_anchor_day_must_equal_the_books(self):
        first = month_end("1300.00", "2026-09-30")
        later = {**month_end("1250.00", "2026-09-30"), "retrieved_at": "2026-10-03T06:00:00Z",
                 "data_through": "2026-10-03"}
        for readings in ([first, later], [later, first]):
            row = self.proof(readings)["2026-09"]
            self.assertEqual((row["reason"], row["difference"], row["difference_on"], row["reconcile"]),
                             ("books_differ", "-50.00", "2026-09-30", None))

    def test_review_a5b_a_reading_between_the_month_end_and_the_anchor_that_differs_refuses(self):
        # The bank posted 100.00 on 09-30; the books dated it 10-03. The 10-01 reading shows it.
        post(self.company, CHECKING, "2026-10-03", "100.00")
        row = self.proof([reading("1400.00", "2026-10-01"), reading("1350.00")])["2026-09"]
        self.assertEqual((row["reason"], row["difference"], row["difference_on"]), ("books_differ", "100.00", "2026-10-01"))

    def test_review_a5c_a_balance_as_of_late_on_the_month_end_in_the_bank_time_zone(self):
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        post(self.company, CHECKING, "2026-09-29", "-2000.00", other="Expenses:Software")
        late = {"balance": "-660.00", "as_of": "2026-10-01T03:30:00+00:00", "retrieved_at": "2026-10-01T12:00:00+00:00",
                "data_through": "2026-09-30", "partial": False}
        row = self.proof([late], time_zone="America/New_York")["2026-09"]
        self.assertEqual((row["reason"], row["basis"], row["reading"]["day"]), ("proven", "feed_computed", "2026-09-30"))

    def test_review_a2_a_row_with_a_source_id_outside_the_connection_is_not_a_bank_row(self):
        # The opening entry is 30.00 too low, and a made-up row of 30.00 fills the gap.
        post(self.company, CHECKING, "2026-07-31", "10.00", source=False)
        for source_id in ("csv:plug-1", "manual:plug-2", "stripe:po_3"):
            self.reset_books()
            post(self.company, CHECKING, "2026-07-31", "10.00", source=False)
            post(self.company, CHECKING, "2026-08-15", "30.00", other="Equity:Plug", source_id=source_id)
            row = self.proof([reading("1290.00")])["2026-08"]
            self.assertEqual((row["reason"], row["unsourced_from"]), ("unsourced_postings", "2026-08-15"), source_id)
        # A row counts only with the ingest mark of the provider that its ID names.
        for source_id, mark, reason in (("plaid:x1", "plaid", "proven"),
                                        ("stripe-financial-connections:x2", "stripe-financial-connections", "proven"),
                                        ("legacyraw3", "banksync", "proven"),
                                        ("plaid:x4", None, "unsourced_postings"),
                                        ("plaid:x5", "banksync", "unsourced_postings"),
                                        ("legacyraw6", None, "unsourced_postings"),
                                        ("legacyraw7", "plaid", "unsourced_postings"),
                                        ("acme:x8", "acme", "unsourced_postings")):
            self.reset_books()
            post(self.company, CHECKING, "2026-07-31", "10.00", source=False)
            post(self.company, CHECKING, "2026-08-15", "30.00", source_id=source_id,
                 meta=(("feed-provider", mark),) if mark else ())
            self.assertEqual(self.proof([reading("1290.00")])["2026-08"]["reason"], reason, (source_id, mark))

    def test_review_a1_a_derived_opening_absorbs_a_row_the_connection_never_delivered(self):
        """The documented limit: the opening entry was set to fit the balance, so a missing row is inside it."""
        self.only_checking()
        # The bank: opening 40.00 and a -30.00 row on 09-03 that the connection never delivered.
        post(self.company, CHECKING, "2026-07-31", "10.00", source=False)
        months = self.proof([reading("1260.00")])
        self.assertEqual([(months[m]["reason"], months[m]["basis"], months[m]["opening_evidence"]) for m in months],
                         [("proven", "feed_computed", "derived")] * 2)
        self.assertEqual(months["2026-08"]["reconcile"]["source_balance"], "510.00", "the bank had 540.00")
        for month in ("2026-08", "2026-09"):
            apply(self.company, months[month]["reconcile"])
        document = self.readiness("2026-08")
        self.assertEqual(document["state"], "ready_for_review")
        self.assertEqual((document["reconciliations"][0]["proof_basis"], document["reconciliations"][0]["opening_evidence"]),
                         ("feed_computed", "derived"))
        self.assertEqual(document["coverage"][0]["months"][0]["opening_evidence"], "derived")
        # A note for the accountant, not a blocker.
        [note] = [item for item in document["warnings"] if item["code"] == "opening_not_independent"]
        self.assertEqual(note["refs"], [CHECKING])
        self.assertEqual(self.codes("2026-08", require_statements=True), {"statement_required"})

    def test_review_a3_a_reversal_that_removes_a_delivered_row_is_not_from_the_connection(self):
        post(self.company, CHECKING, "2026-07-31", "10.00", source=False)
        original = post(self.company, CHECKING, "2026-09-03", "-30.00", other="Expenses:Software")
        post(self.company, CHECKING, "2026-09-03", "30.00", other="Income:Other", source=False,
             meta=(("reverses", original),))
        row = self.proof([reading("1260.00")])["2026-08"]
        self.assertEqual((row["reason"], row["unsourced_from"]), ("unsourced_postings", "2026-09-03"))
        self.reset_books()
        post(self.company, CHECKING, "2026-07-31", "10.00", source=False)
        original = post(self.company, CHECKING, "2026-08-30", "-30.00", other="Expenses:Software")
        post(self.company, CHECKING, "2026-09-02", "30.00", other="Income:Other", source=False,
             meta=(("reverses", original),))
        self.assertEqual(self.proof([reading("1260.00")])["2026-08"]["reason"], "unsourced_postings")

    def test_review_a4_a_second_opening_row_on_the_first_feed_day_is_refused(self):
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        post(self.company, CHECKING, "2026-08-01", "25.00", other="Equity:Opening", source_id="csv:opening")
        row = self.proof([reading("1315.00")])["2026-08"]
        self.assertEqual((row["reason"], row["opening_entries"]), ("opening_entry_unclear", 2))

    def test_review_a7_a_row_dated_across_a_month_end_stays_a_documented_limit(self):
        """The bank posted 100.00 on 08-31; the books dated it 09-02. Only a 10-05 reading: not found."""
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        post(self.company, CHECKING, "2026-09-02", "100.00")
        months = self.proof([reading("1390.00")])
        self.assertEqual([(months[m]["reason"], months[m]["reconcile"]["source_balance"]) for m in ("2026-08", "2026-09")],
                         [("proven", "540.00"), ("proven", "1440.00")], "the bank had 640.00 on 08-31")
        # A reading for the month end finds it.
        self.assertEqual(self.proof([month_end("640.00", "2026-08-31"), reading("1390.00")])["2026-08"]["reason"],
                         "books_differ")

    def test_review_a8_a_statement_whose_cycle_does_not_end_on_a_month_end_is_checked(self):
        self.only_checking()
        months = self.proof([reading("1250.00")])
        mid = {"statement_id": "mid", "account": CHECKING, "kind": "statement", "period_start": "2026-09-15",
               "period_end": "2026-10-14", "opening_balance": "1400.00", "closing_balance": "1150.00"}
        record_statement(self.company, mid)
        # The statement says 1,400.00 on 09-14; the books hold 1,500.00.
        for month in ("2026-08", "2026-09"):
            row = self.proof([reading("1250.00")])[month]
            self.assertEqual((row["reason"], row["statement_id"], row["statement_date"], row["difference"]),
                             ("statement_differs", "mid", "2026-09-14", "-100.00"), month)
        # Months recorded before the statement arrived: coverage and readiness block.
        (self.company / "ingestion" / "statements" / "mid.json").unlink()
        for month in ("2026-08", "2026-09"):
            apply(self.company, months[month]["reconcile"])
        record_statement(self.company, mid)
        document = self.readiness()
        self.assertEqual(document["state"], "blocked")
        [blocker] = [item for item in document["blockers"] if item["code"] == "statement_feed_differs"]
        self.assertIn("1,400.00", blocker["message"])
        self.assertIn("1,500.00", blocker["message"])
        self.assertNotIn("proof_basis", document["coverage"][0]["months"][0])

    def test_review_a8b_a_statement_with_the_same_closing_and_another_opening_blocks(self):
        self.only_checking()
        months = self.proof([reading("1250.00")])
        for month in ("2026-08", "2026-09"):
            apply(self.company, months[month]["reconcile"])
        record_statement(self.company, {"statement_id": "sep", "account": CHECKING, "kind": "statement",
                                        "period_start": "2026-09-01", "period_end": "2026-09-30",
                                        "opening_balance": "450.00", "closing_balance": "1300.00",
                                        "evidence_ref": f"ingestion/statements/files/{'f' * 64}.csv"})
        document = self.readiness()
        self.assertEqual(document["state"], "blocked")
        self.assertTrue({"statement_feed_differs", "statement_tieout_differs"}
                        <= {item["code"] for item in document["blockers"]})
        self.assertNotIn("proof_basis", document["coverage"][0]["months"][0])

    def test_review_a9_a12_require_statements_accepts_only_archived_statements(self):
        self.only_checking()
        figures = self.proof([reading("1250.00")])["2026-09"]["reconcile"]

        def record(refs, basis=None):
            return reconcile(self.company, CHECKING, Decimal(figures["source_balance"]), date(2026, 9, 30),
                             source_opening_balance=Decimal(figures["source_opening_balance"]),
                             source_transaction_total=Decimal(figures["source_transaction_total"]),
                             source_snapshot_at=figures["source_snapshot_at"], source_through=figures["source_through"],
                             source_evidence_refs=refs, source_evidence_hashes={ref: "a" * 64 for ref in refs},
                             proof_basis=basis)
        # A statement basis with a made-up reference, or with mixed references, is refused.
        for refs in (["plaid-balance-123"], ["evidence:feed-balance-1", "note"]):
            with self.assertRaises(ValueError, msg=refs):
                record(refs, "statement")
        # A record that does not say what it was checked against is blocked under the flag.
        record(["plaid-balance-123"])
        document = self.readiness(require_statements=True)
        self.assertNotIn("proof_basis", document["reconciliations"][0])
        self.assertEqual(self.codes(require_statements=True), {"statement_required"})
        self.assertEqual(self.codes(), set())
        # A missing record is blocked too.
        (self.company / "reports" / "reconciliation.json").write_text("[]\n")
        self.assertIn("statement_required", self.codes(require_statements=True))

    def test_review_a9b_a_statement_for_another_account_does_not_count(self):
        self.only_checking()
        months = self.proof([reading("1250.00")])
        for month in ("2026-08", "2026-09"):
            apply(self.company, months[month]["reconcile"])
        record_statement(self.company, {"statement_id": "sav", "account": SAVINGS, "kind": "statement",
                                        "period_start": "2026-09-01", "period_end": "2026-09-30",
                                        "opening_balance": "500.00", "closing_balance": "1300.00",
                                        "evidence_ref": f"ingestion/statements/files/{'9' * 64}.csv"})
        self.assertEqual(self.codes(require_statements=True), {"statement_required"})

    def test_review_a9c_a_card_cannot_take_a_bank_connection_basis(self):
        for basis in ("feed_reading", "feed_computed"):
            with self.assertRaises(ValueError):
                reconcile(self.company, CARD, Decimal("-80.00"), date(2026, 9, 30),
                          source_evidence_refs=["evidence:feed-balance-c"], proof_basis=basis)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(["reconcile", "--entity", str(self.company), "--account", CARD, "--as-of", "2026-09-30",
                         "--source-balance", "-80.00", "--source-evidence-ref", "evidence:feed-balance-c",
                         "--proof-basis", "feed_reading"])
        self.assertEqual(code, 1)
        self.assertIn("Assets:", err.getvalue())

    def test_review_a11_the_ledger_refuses_amounts_below_a_cent(self):
        # A sub-cent posting cannot reach the books, so it cannot make the books and the bank round alike.
        with self.assertRaises(ValueError):
            post(self.company, CHECKING, "2026-09-12", "0.005")

    def test_review_b1_an_opening_imported_as_an_ordinary_row_is_not_a_zero_opening(self):
        # The real 07-31 balance arrives as a CSV row on 08-01: not a bank row, so no zero opening and no feed_reading.
        post(self.company, CHECKING, "2026-08-01", "40.00", other="Equity:Opening", source_id="csv:opening-row")
        row = self.proof([month_end("540.00", "2026-08-31"), reading("1290.00")])["2026-08"]
        self.assertEqual((row["reason"], row["basis"], row["opening_evidence"]), ("proven", "feed_computed", "derived"))
        # The same row with a bank feed source ID is a bank row (the connection delivered it).
        self.reset_books()
        post(self.company, CHECKING, "2026-08-01", "40.00", other="Equity:Opening")
        self.assertEqual(self.proof([month_end("540.00", "2026-08-31"), reading("1290.00")])["2026-08"]["basis"],
                         "feed_reading")

    # -- Opening evidence --------------------------------------------------------------------------------------

    def test_opening_evidence_is_statement_only_for_a_printed_or_statement_backed_opening(self):
        self.only_checking()
        cases = ((("opening-source", "printed"),), "statement"), ((("opening-source", "person"),), "derived"), \
                ((("opening-source", "agent"),), "derived"), ((("opening-source", "system"),), "derived"), \
                ((), "derived")
        for index, (meta, evidence) in enumerate(cases):
            self.reset_books()
            for path in self.company.glob("reports/reconciliation.json"):
                path.unlink()
            post(self.company, CHECKING, "2026-07-31", "40.00", source_id=f"statement-opening-{index}",
                 meta=(("import-source", "statement-opening"),) + meta)
            months = self.proof([reading("1290.00")])
            self.assertEqual([(months[m]["basis"], months[m]["opening_evidence"]) for m in ("2026-08", "2026-09")],
                             [("feed_computed", evidence)] * 2, meta)
            self.assertNotIn("opening_evidence", months["2026-08"]["reconcile"], "no caller passes it")
            for month in ("2026-08", "2026-09"):
                apply(self.company, months[month]["reconcile"])
            [record] = [r for r in json.loads((self.company / "reports" / "reconciliation.json").read_text())
                        if r["as_of"] == "2026-09-30"]
            self.assertNotIn("opening_evidence", record)
            # Coverage and readiness derive it from the books.
            [account] = check_coverage(self.company, date(2026, 9, 1), date(2026, 9, 30), accounts=[CHECKING])["accounts"]
            self.assertEqual(account["months"][0]["opening_evidence"], evidence)
            document = self.readiness()
            self.assertEqual(document["reconciliations"][0]["opening_evidence"], evidence)
            self.assertEqual("opening_not_independent" in {item["code"] for item in document["warnings"]},
                             evidence == "derived")

    def test_opening_evidence_from_a_statement_that_backs_the_books(self):
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        record_statement(self.company, {"statement_id": "jul", "account": CHECKING, "kind": "statement",
                                        "period_start": "2026-07-01", "period_end": "2026-07-31",
                                        "opening_balance": "0.00", "closing_balance": "40.00"})
        months = self.proof([reading("1290.00")])
        self.assertEqual([(months[m]["basis"], months[m]["opening_evidence"]) for m in ("2026-08", "2026-09")],
                         [("feed_computed", "statement")] * 2)
        # A feed_reading month has no opening_evidence.
        self.assertNotIn("opening_evidence", self.proof([month_end("540.00", "2026-08-31"), reading("1290.00")])["2026-08"])

    def test_round2_reconcile_takes_no_opening_evidence_from_a_caller(self):
        with self.assertRaises(TypeError):
            reconcile(self.company, CHECKING, Decimal("1300.00"), date(2026, 9, 30),
                      source_evidence_refs=["evidence:feed-balance-z"], proof_basis="feed_computed",
                      opening_evidence="person")
        with redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["reconcile", "--entity", str(self.company), "--account", CHECKING, "--as-of", "2026-09-30",
                  "--source-balance", "1300.00", "--source-evidence-ref", "evidence:feed-balance-z",
                  "--proof-basis", "feed_computed", "--opening-evidence", "person"])

    def test_round2_a_typed_opening_of_a_person_is_derived(self):
        # The 40.00 / 30.00 example with an opening that a person typed to fit today's balance.
        self.only_checking()
        post(self.company, CHECKING, "2026-07-31", "10.00", source=False,
             meta=(("import-source", "statement-opening"), ("opening-source", "person")))
        row = self.proof([reading("1260.00")])["2026-08"]
        self.assertEqual((row["reason"], row["reconcile"]["source_balance"], row["opening_evidence"]),
                         ("proven", "510.00", "derived"), "the bank had 540.00")

    def test_round2_a_correction_counts_only_once_and_only_for_the_same_row(self):
        from bookkeeping.feed_proof import _classify, _Posting
        day = date(2026, 9, 3)
        original = _Posting(day, Decimal("-30.00"), "banksync:t1", False, None, None, None, "banksync")
        reversal = _Posting(day, Decimal("30.00"), None, False, None, "banksync:t1", None, "banksync")

        def kinds(*postings):
            return [(posting.amount, kind) for posting, kind in _classify([original, reversal, *postings])]
        for new_id in ("fix1", "reclassify:abc"):
            keep = _Posting(day, Decimal("-30.00"), new_id, False, None, None, "banksync:t1", "banksync")
            extra = _Posting(day, Decimal("20.00"), new_id, False, None, None, "banksync:t1", "banksync")
            self.assertEqual(kinds(keep, extra), [(Decimal("-30.00"), "none"), (Decimal("20.00"), "none")], new_id)
            self.assertEqual(kinds(keep), [(Decimal("-30.00"), "source")], new_id)
        changed = _Posting(date(2026, 9, 5), Decimal("-99.00"), "fix2", False, None, None, "banksync:t1", "banksync")
        # A correction to another day and amount does not count, and the reversal stays: both are not bank rows.
        self.assertEqual(kinds(changed), [(Decimal("-30.00"), "source"), (Decimal("30.00"), "none"),
                                          (Decimal("-99.00"), "none")])
        # A correction without a reversal is a second copy of the row: it is a correction of a row that stays.
        duplicate = _Posting(day, Decimal("-30.00"), "reclassify:x", False, None, None, "banksync:t1", "banksync")
        self.assertEqual([k for p, k in _classify([original, duplicate])], ["source", "source"])

    def test_round2_a_stale_earlier_reading_says_a_later_reading_agrees(self):
        # The bank posts on 10-02 a -7.00 row dated 09-30; the 10-01 reading did not include it.
        post(self.company, CHECKING, "2026-09-30", "-7.00", other="Expenses:Software")
        row = self.proof([reading("1300.00", "2026-10-01"), reading("1243.00")])["2026-09"]
        self.assertEqual((row["reason"], row["difference"], row["difference_on"], row["later_reading_agrees"]),
                         ("books_differ", "7.00", "2026-10-01", True))
        row = self.proof([reading("1240.00")])["2026-09"]
        self.assertEqual((row["reason"], row["later_reading_agrees"]), ("books_differ", False))

    def test_round2_an_unconfirmed_statement_period_says_so_when_it_blocks(self):
        record_statement(self.company, {"statement_id": "unc", "account": CHECKING, "kind": "statement",
                                        "period_start": "2026-09-10", "period_end": "2026-10-09",
                                        "opening_balance": "999.00", "closing_balance": "1999.00"},
                         extra={"period_confirmed": False})
        row = self.proof([reading("1250.00")])["2026-09"]
        self.assertEqual((row["reason"], row["statement_id"], row["period_confirmed"]),
                         ("statement_differs", "unc", False))

    def test_round2_require_statements_for_a_card_and_a_checking_account(self):
        self.only_checking()
        config = json.loads((self.company / "entity.json").read_text())
        config["bank_account_mappings"]["feed-card"] = CARD
        (self.company / "entity.json").write_text(json.dumps(config))
        post(self.company, CARD, "2026-09-05", "-80.00", other="Expenses:Software")
        months = self.proof([reading("1250.00")])
        for month in ("2026-08", "2026-09"):
            apply(self.company, months[month]["reconcile"])

        def card_codes(**kw):
            return {item["code"] for item in self.readiness(**kw)["blockers"] if CARD in item["refs"]}
        self.assertNotIn("statement_required", card_codes())
        self.assertIn("statement_required", card_codes(require_statements=True))
        card_ref = f"ingestion/statements/files/{'f' * 64}.csv"
        reconcile(self.company, CARD, Decimal("-80.00"), date(2026, 9, 30), source_opening_balance=Decimal("0.00"),
                  source_transaction_total=Decimal("-80.00"), source_snapshot_at="2026-10-02T12:00:00+00:00",
                  source_through="2026-09-30", source_evidence_refs=[card_ref], source_evidence_hashes={card_ref: "f" * 64})
        self.assertNotIn("statement_required", card_codes(require_statements=True))
        self.assertIn("statement_required", self.codes(require_statements=True))
        ref = f"ingestion/statements/files/{'c' * 64}.csv"
        reconcile(self.company, CHECKING, Decimal("1300.00"), date(2026, 9, 30),
                  source_opening_balance=Decimal("500.00"), source_transaction_total=Decimal("800.00"),
                  source_snapshot_at="2026-10-02T12:00:00+00:00", source_through="2026-09-30",
                  source_evidence_refs=[ref], source_evidence_hashes={ref: "c" * 64})
        self.assertNotIn("statement_required", self.codes(require_statements=True))

    def test_round2_only_a_connected_feed_ingest_marks_rows_as_delivered(self):
        """A generic ingest stores x1 and banksync:x2 as written: neither counts. A BankSync ingest's rows count."""
        from bookkeeping.ledger.store import LedgerStore as Store
        rows = Path(self._tmp.name) / "rows.json"

        def ingest(items, *extra):
            rows.write_text(json.dumps(items))
            with redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["ingest", str(rows), "--entity", str(self.company), "--no-feed-window", *extra]), 0)

        def marks():
            with Store(self.company / "ledger.sqlite").connection() as conn:
                return {sid: dict(json.loads(meta)).get("feed-provider")
                        for sid, meta in conn.execute("SELECT source_id, metadata_json FROM entries")}

        def confirm_all():
            staged = json.loads((self.company / "staging" / "pending-categorization.json").read_text())
            with redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                for row in staged:
                    main(["queue", "propose", "--entity", str(self.company), "--source-id", row["id"],
                          "--category", "Income:Consulting", "--reasoning", "test"])
                for item in (self.company / "review-queue").glob("*.json"):
                    main(["queue", "confirm", "--entity", str(self.company), "--item", item.stem])
        forged = {"accountId": "feed-chk", "description": "made up", "feed_provider": "banksync"}
        ingest([{**forged, "id": "x1", "date": "2026-08-01", "amount": 40.0},
                {**forged, "id": "banksync:x2", "date": "2026-08-02", "amount": 5.0}])
        ingest([{"id": "y3", "accountId": "feed-chk", "date": "2026-08-03", "amount": 6.0, "description": "feed"}],
               "--source", "banksync")
        ingest([{"id": "p4", "accountId": "feed-chk", "date": "2026-08-04", "amount": 7.0, "description": "feed"}],
               "--source", "plaid")
        confirm_all()
        found = marks()
        self.assertTrue({"x1", "banksync:x2", "banksync:y3", "plaid:p4"} <= set(found), found)
        self.assertEqual((found["x1"], found["banksync:x2"]), (None, None), "a generic ingest has no mark")
        self.assertEqual((found["banksync:y3"], found["plaid:p4"]), ("banksync", "plaid"))
        from bookkeeping.feed_proof import _load_books
        kinds = {day.isoformat(): kind for day, kind, _ in _load_books(self.company, [CHECKING])[CHECKING].kinds
                 if day.isoformat() in {"2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"}}
        self.assertEqual(kinds, {"2026-08-01": "none", "2026-08-02": "none", "2026-08-03": "source",
                                 "2026-08-04": "source"})

    # -- Round 3 ----------------------------------------------------------------------------------------------

    def run_cli(self, *argv):
        with redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def ingest(self, rows, *extra):
        path = Path(self._tmp.name) / "rows.json"
        path.write_text(json.dumps(rows))
        code, text = self.run_cli("ingest", str(path), "--entity", str(self.company), "--no-feed-window", *extra)
        self.assertEqual(code, 0, text)

    def entry_marks(self):
        with LedgerStore(self.company / "ledger.sqlite").connection() as conn:
            return {sid: dict(json.loads(meta)).get("feed-provider")
                    for sid, meta in conn.execute("SELECT source_id, metadata_json FROM entries")}

    def confirm(self, source_id, *propose):
        code, text = self.run_cli("queue", *(propose or ("propose",)), "--entity", str(self.company),
                                  "--source-id", source_id, *(() if propose else ("--category", "Income:Consulting")),
                                  "--reasoning", "test")
        self.assertEqual(code, 0, text)
        item = source_id.replace(":", "_")
        code, text = self.run_cli("queue", "confirm", "--entity", str(self.company), "--item", item)
        self.assertEqual(code, 0, text)

    def test_round3_an_edit_of_the_staging_file_cannot_create_or_keep_the_mark(self):
        staging = self.company / "staging" / "pending-categorization.json"
        # A generic ingest, then an edit that adds the old field: no mark.
        self.ingest([{"id": "x1", "accountId": "feed-chk", "date": "2026-08-03", "amount": 6.0, "description": "row"}])
        rows = json.loads(staging.read_text())
        for row in rows:
            row["feed_provider"] = "banksync"
        staging.write_text(json.dumps(rows))
        self.confirm("x1")
        self.assertIsNone(self.entry_marks()["x1"])
        # A row from a BankSync ingest keeps the mark; the same row edited in staging loses it.
        self.ingest([{"id": "y2", "accountId": "feed-chk", "date": "2026-08-04", "amount": 7.0, "description": "feed"},
                     {"id": "y3", "accountId": "feed-chk", "date": "2026-08-05", "amount": 8.0, "description": "feed"}],
                    "--source", "banksync")
        rows = json.loads(staging.read_text())
        for row in rows:
            if row["id"] == "banksync:y3":
                row["amount"] = 80.0
        staging.write_text(json.dumps(rows))
        self.confirm("banksync:y2")
        self.confirm("banksync:y3")
        marks = self.entry_marks()
        self.assertEqual((marks["banksync:y2"], marks["banksync:y3"]), ("banksync", None))
        # The record is in the hash-chained audit log of the ledger store.
        events = [event for event in LedgerStore(self.company / "ledger.sqlite").load_audit_events()
                  if event["type"] == "feed-intake"]
        self.assertEqual([sorted(event["payload"]["rows"]) for event in events], [["banksync:y2", "banksync:y3"]])

    def test_feed_intake_record_with_a_broken_audit_chain_gives_no_mark_and_no_proof(self):
        import sqlite3
        from bookkeeping.source_ids import _row_digest
        staging = self.company / "staging" / "pending-categorization.json"
        self.ingest([{"id": "x2", "accountId": "feed-chk", "date": "2026-08-02", "amount": 5.0, "description": "row"}])
        digest = _row_digest(json.loads(staging.read_text())[0])
        forged = json.dumps({"provider": "banksync", "rows": {"x2": digest}})
        with contextlib.closing(sqlite3.connect(self.company / "ledger.sqlite")) as conn:
            conn.execute("INSERT INTO audit_events (type, ts, payload_json, prev_hash, record_hash) "
                         "VALUES ('feed-intake', '2026-08-02T00:00:00Z', ?, 'bogus', 'bogus')", (forged,))
            conn.commit()
        self.confirm("x2")
        self.assertIsNone(self.entry_marks()["x2"])
        # An intact feed ingest still gets the mark when the chain is whole, and loses it when the chain breaks.
        self.reset_books()
        self.ingest([{"id": "y2", "accountId": "feed-chk", "date": "2026-08-04", "amount": 7.0, "description": "feed"}],
                    "--source", "banksync")
        self.confirm("banksync:y2")
        self.assertEqual(self.entry_marks()["banksync:y2"], "banksync")
        from bookkeeping.feed_proof import _load_books
        delivered = [kind for _, kind, _ in _load_books(self.company, [CHECKING])[CHECKING].kinds].count("source")
        self.assertGreaterEqual(delivered, 1)
        with contextlib.closing(sqlite3.connect(self.company / "ledger.sqlite")) as conn:
            conn.execute("UPDATE audit_events SET record_hash = 'bogus' WHERE type = 'feed-intake'")
            conn.commit()
        after = [kind for _, kind, _ in _load_books(self.company, [CHECKING])[CHECKING].kinds].count("source")
        self.assertEqual(after, 0)

    def test_failed_feed_ingest_leaves_no_intake_record(self):
        row = {"id": "banksync:v1", "accountId": "feed-chk", "date": "2026-08-03", "amount": 6.0, "description": "row"}
        path = Path(self._tmp.name) / "rows.json"
        path.write_text(json.dumps([{**row, "id": "v1"}]))
        # The import refuses a damaged review file after the feed record would exist.
        staging = self.company / "staging" / "pending-categorization.json"
        staging.parent.mkdir(exist_ok=True)
        staging.write_text('{"not": "a list"}')
        code, text = self.run_cli("ingest", str(path), "--entity", str(self.company), "--no-feed-window",
                                  "--source", "banksync")
        self.assertEqual(code, 1, text)
        store = LedgerStore(self.company / "ledger.sqlite")
        self.assertEqual([e for e in store.load_audit_events() if e["type"] == "feed-intake"], [])
        self.assertEqual(store.verify_audit_chain(), [])
        # A generic ingest of the same row, with the same ID and digest, gets no mark.
        staging.unlink()
        self.ingest([row])
        self.confirm("banksync:v1")
        self.assertIsNone(self.entry_marks()["banksync:v1"])

    def test_round3_a_split_that_puts_a_second_posting_on_the_bank_account_does_not_count(self):
        self.only_checking()
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        self.ingest([{"id": "s1", "accountId": "feed-chk", "date": "2026-08-10", "amount": -100.0,
                      "description": "split me"}], "--source", "banksync")
        self.confirm("banksync:s1", "propose-split", "--posting", f"{CHECKING}=60.00",
                     "--posting", "Expenses:Software=40.00")
        from bookkeeping.feed_proof import _load_books
        side = _load_books(self.company, [CHECKING])[CHECKING]
        self.assertEqual([kind for day, kind, _ in side.kinds if day == date(2026, 8, 10)], ["none", "none"])
        # Books: 40 + 500 - 100 + 60 + 1000 - 200 - 50 = 1250 on 10-05.
        row = self.proof([reading("1250.00")])["2026-08"]
        self.assertEqual((row["reason"], row["unsourced_from"]), ("unsourced_postings", "2026-08-10"))

    def test_round3_a_statement_import_with_a_period_from_rows_reports_period_confirmed_false(self):
        from bookkeeping import statement_import as si
        fixture = ROOT / "tests" / "fixtures" / "statements" / "chase_like_checking.csv"
        si.save_profile(self.company, fixture, name="chase-checking", ledger_account=CHECKING, overrides={})
        code, text = self.run_cli("statement", "import", str(fixture), "--entity", str(self.company),
                                  "--profile", "chase-checking", "--period-from-rows")
        self.assertEqual(code, 0, text)
        stage(self.company, [])  # Only the statement record matters here, not its rows waiting in review.
        record_feed_window(self.company, account=CHECKING, start=date(2026, 1, 1), end=date(2026, 2, 5), source="feed-chk")
        post(self.company, CHECKING, "2026-01-10", "100.00")
        # The statement says 1,000.00 on 01-01, before its first row; the books hold 0.00.
        row = self.proof([reading("100.00", "2026-02-05")], as_of=date(2026, 2, 20), from_month="2026-01")["2026-01"]
        self.assertEqual((row["reason"], row["statement_date"], row["difference"], row["period_confirmed"]),
                         ("statement_differs", "2026-01-01", "1000.00", False))

    def tamper_record_hash(self, where="type = 'feed-intake'"):
        """Change one character of a record hash in place, then restore the file time (delete journal mode)."""
        import os
        import sqlite3
        path = self.company / "ledger.sqlite"
        before = path.stat()
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("UPDATE audit_events SET record_hash = substr(record_hash, 1, length(record_hash) - 1) || "
                         f"CASE substr(record_hash, -1) WHEN '0' THEN '1' ELSE '0' END WHERE {where}")
            conn.commit()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return before

    def test_an_edit_with_the_same_size_and_file_time_is_not_trusted_from_an_earlier_read(self):
        from bookkeeping.source_ids import feed_intake_index
        self.ingest([{"id": "y2", "accountId": "feed-chk", "date": "2026-08-04", "amount": 7.0, "description": "feed"}],
                    "--source", "banksync")
        self.assertIn("banksync:y2", feed_intake_index(self.company))
        before = self.tamper_record_hash()
        after = (self.company / "ledger.sqlite").stat()
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
        self.assertTrue(LedgerStore(self.company / "ledger.sqlite").verify_audit_chain())
        self.assertEqual(feed_intake_index(self.company), {})
        self.confirm("banksync:y2")
        self.assertIsNone(self.entry_marks()["banksync:y2"])

    def test_only_a_feed_intake_event_can_be_discarded(self):
        store = LedgerStore(self.company / "ledger.sqlite")
        with store.transaction() as conn:
            store.append_audit_event("note", {"text": "x"}, conn)
        events = store.load_audit_events()
        newest = events[-1]
        self.assertNotEqual(newest["type"], "feed-intake")
        with self.assertRaises(ValueError):
            store._discard_last_audit_event(newest["type"], newest["record_hash"])
        self.assertEqual(len(store.load_audit_events()), len(events))
        self.assertEqual(store.verify_audit_chain(), [])

    def test_balance_prove_says_audit_chain_invalid_when_a_record_hash_is_edited(self):
        input_path = Path(self._tmp.name) / "readings.json"
        input_path.write_text(json.dumps({"accounts": [{"account_id": "feed-chk", "account_type": "Checking",
                                                        "currency": "USD", "time_zone": "America/New_York",
                                                        "readings": [reading("1250.00")]}]}))

        def prove_cli():
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["balance", "prove", "--entity", str(self.company), "--input", str(input_path),
                             "--as-of", "2026-10-20"])
            return code, [row["reason"] for row in json.loads(out.getvalue())["months"]]

        store = LedgerStore(self.company / "ledger.sqlite")
        with store.transaction() as conn:
            store.append_audit_event("note", {"text": "x"}, conn)
        self.assertEqual(prove_cli(), (0, ["proven", "proven"]))
        self.tamper_record_hash("id = (SELECT MAX(id) FROM audit_events)")
        self.assertEqual(prove_cli(), (0, ["audit_chain_invalid", "audit_chain_invalid"]))

    def test_cli_is_read_only_and_classified_as_read(self):
        input_path = Path(self._tmp.name) / "readings.json"
        input_path.write_text(json.dumps({"accounts": [{"account_id": "feed-chk", "account_type": "Checking",
                                                        "currency": "USD", "time_zone": "America/New_York",
                                                        "readings": [reading("1250.00")]}]}))
        files = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.company.rglob("*") if p.is_file()}
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["balance", "prove", "--entity", str(self.company), "--input", str(input_path),
                         "--as-of", "2026-10-20"])
        document = json.loads(out.getvalue())
        self.assertEqual((code, [row["status"] for row in document["months"]]), (0, ["proven", "proven"]))
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.company.rglob("*") if p.is_file()}
        self.assertEqual(files, after)
        spec = command_spec(["balance", "prove", "--entity", "e", "--input", "i.json", "--as-of", "2026-10-20"])
        self.assertFalse(spec["mutates"])
        self.assertEqual((spec["entity_fields"], spec["input_fields"]), (["entity"], ["input"]))



class IntakeOnlyFeedIngestTests(unittest.TestCase):
    """A feed ingest with ``--intake-only`` writes a ``feed-intake`` event and no seal; the books stay valid."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.company = Path(self._tmp.name) / "books"
        self.cli("entity", "init", str(self.company), "--name", "Synthetic Co", "--business-type", "consulting",
                 "--cutover-date", "2026-01-01", expect=0)
        self.cli("entity", "bank-account-map", str(self.company), "--feed-account-id", "acct_1",
                 "--account", CHECKING, "--provider", "plaid", expect=0)

    def cli(self, *argv, expect=None):
        with redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(list(argv))
        text = out.getvalue() + err.getvalue()
        if expect is not None:
            self.assertEqual(code, expect, text)
        return code, text

    def row(self, row_id="r1", day="2026-08-03", amount=6.0):
        return {"id": row_id, "accountId": "acct_1", "date": day, "amount": amount, "description": "feed row"}

    def ingest(self, rows, *extra, expect=0):
        path = Path(self._tmp.name) / "rows.json"
        path.write_text(json.dumps(rows))
        return self.cli("ingest", str(path), "--entity", str(self.company), "--no-feed-window", *extra, expect=expect)

    def integrity(self):
        from bookkeeping.ledger.importer import check_store_integrity
        return check_store_integrity(LedgerStore(self.company / "ledger.sqlite")).status

    def add_account(self, name):
        return self.cli("entity", "account-add", str(self.company), "--account", name)

    def test_intake_only_feed_ingest_then_account_add_works(self):
        self.ingest([self.row()], "--source", "plaid", "--intake-only")
        events = LedgerStore(self.company / "ledger.sqlite").load_audit_events()
        self.assertIn("feed-intake", [event["type"] for event in events])
        self.assertEqual(LedgerStore(self.company / "ledger.sqlite").verify_audit_chain(), [])
        self.assertEqual(self.integrity(), "ok")
        code, text = self.add_account("Expenses:SyntheticTools")
        self.assertEqual(code, 0, text)

    def test_intake_only_feed_ingest_on_new_books_then_confirm_and_prove(self):
        self.ingest([self.row("r1", "2026-08-03", 6.0)], "--source", "plaid", "--intake-only")
        self.cli("queue", "propose", "--entity", str(self.company), "--source-id", "plaid:r1",
                 "--category", "Income:Consulting", "--reasoning", "test", expect=0)
        self.cli("queue", "confirm", "--entity", str(self.company), "--item", "plaid_r1", expect=0)
        with LedgerStore(self.company / "ledger.sqlite").connection() as conn:
            marks = {sid: dict(json.loads(meta)).get("feed-provider")
                     for sid, meta in conn.execute("SELECT source_id, metadata_json FROM entries")}
        self.assertEqual(marks, {"plaid:r1": "plaid"})
        self.assertEqual(self.integrity(), "ok")
        from bookkeeping.feed_proof import _load_books
        side = _load_books(self.company, [CHECKING])[CHECKING]
        self.assertEqual([kind for day, kind, _ in side.kinds if day == date(2026, 8, 3)], ["source"])
        self.assertFalse(side.chain_invalid)
        # The feed proof through the CLI: the row counts as delivered, so August is proven.
        record_feed_window(self.company, account=CHECKING, start=date(2026, 8, 1), end=date(2026, 10, 5),
                           source="acct_1")
        input_path = Path(self._tmp.name) / "readings.json"
        input_path.write_text(json.dumps({"accounts": [{"account_id": "acct_1", "account_type": "checking",
                                                        "currency": "USD", "time_zone": "UTC",
                                                        "readings": [reading("6.00")]}]}))
        code, text = self.cli("balance", "prove", "--entity", str(self.company), "--input", str(input_path),
                              "--as-of", "2026-10-20")
        self.assertEqual(code, 0, text)
        months = {row["month"]: row["reason"] for row in json.loads(text)["months"]}
        self.assertEqual(months["2026-08"], "proven", text)

    def test_a_real_unfinished_ledger_write_is_still_incomplete_after_an_intake_only_ingest(self):
        self.ingest([self.row()], "--source", "plaid", "--intake-only")
        store = LedgerStore(self.company / "ledger.sqlite")
        with store.transaction() as conn:
            store.append_audit_event("intent", {"session_id": "half-done"}, conn)
        self.assertEqual(self.integrity(), "incomplete-write")
        code, text = self.add_account("Expenses:SyntheticTools")
        self.assertEqual(code, 1, text)
        self.assertIn("integrity is invalid", text)

    def test_integrity_stays_ok_after_a_normal_and_a_generic_ingest_that_follow_an_intake_only_one(self):
        self.ingest([self.row("r1")], "--source", "plaid", "--intake-only")
        self.assertEqual(self.integrity(), "ok")
        self.ingest([self.row("r2", "2026-08-04", 7.0)], "--source", "plaid")
        self.assertEqual(self.integrity(), "ok")
        self.ingest([self.row("g3", "2026-08-05", 8.0)])
        self.assertEqual(self.integrity(), "ok")
        code, text = self.add_account("Expenses:SyntheticTools")
        self.assertEqual(code, 0, text)

if __name__ == "__main__":
    unittest.main()
