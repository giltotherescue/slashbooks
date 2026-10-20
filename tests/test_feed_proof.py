"""Tests for the month-end proof from a bank feed (``books balance prove``).

All data is synthetic.
"""

from __future__ import annotations

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
from bookkeeping.coverage import record_feed_window, record_statement  # noqa: E402
from bookkeeping.feed_proof import prove  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.readiness import DependencyVersions, assess_review_readiness  # noqa: E402
from bookkeeping.reconcile import reconcile  # noqa: E402
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
         source: bool = True, currency: str = "USD") -> None:
    """Post one two-sided entry from a feed row; ``amount`` is the ledger-sign amount on ``account``."""
    store = LedgerStore(company / "ledger.sqlite")
    store.initialize()
    meta = (("source-id", f"banksync:t{next(_ids)}"),) if source else ()
    with store.transaction() as conn:
        store.insert_opens([Open(date(2026, 1, 1), name, (currency,)) for name in (account, other)], conn)
        store.insert_entries([Entry(
            date=date.fromisoformat(day), narration="Synthetic", meta=meta,
            postings=(Posting(account, Decimal(amount), currency), Posting(other, -Decimal(amount), currency)))], conn)


def apply(company: Path, figures: dict, ref: str = "evidence:feed-balance-abc"):
    return reconcile(company, figures["account"], Decimal(figures["source_balance"]), date.fromisoformat(figures["as_of"]),
                     source_opening_balance=Decimal(figures["source_opening_balance"]),
                     source_transaction_total=Decimal(figures["source_transaction_total"]),
                     source_snapshot_at=figures["source_snapshot_at"], source_through=figures["source_through"],
                     source_evidence_refs=[ref], source_evidence_hashes={ref: "b" * 64})


class FeedProofTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.company = make_company(Path(self._tmp.name), {
            "feed-chk": CHECKING, "feed-card": CARD, "feed-sav": SAVINGS})
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
        record_feed_window(self.company, account=CHECKING, start=date(2026, 8, 1), end=date(2026, 10, 5),
                           source="feed-chk")

    def proof(self, readings, account_id="feed-chk", account_type="checking", as_of=date(2026, 10, 20), **kw):
        extra = {key: kw.pop(key) for key in ("currency", "time_zone") if key in kw}
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
            "source_snapshot_at": "2026-10-05T09:00:00Z", "source_through": "2026-09-30"})
        self.assertEqual(september["reading"]["read_day"], "2026-10-05")
        self.assertEqual(months["2026-08"]["reconcile"]["source_balance"], "500.00")
        self.assertEqual(months["2026-08"]["reconcile"]["source_opening_balance"], "0.00")

    def test_an_intraday_reading_on_the_month_end_does_not_prove_it_but_a_next_day_reading_may(self):
        intraday = {**reading("1300.00", "2026-09-30"), "retrieved_at": "2026-09-30T09:00:00Z"}
        self.assertEqual(self.proof([intraday])["2026-09"]["reason"], "no_balance_after_month_end")
        next_day = {**reading("1300.00", "2026-09-30"), "retrieved_at": "2026-10-01T06:00:00Z"}
        months = self.proof([next_day, reading("1250.00")])
        self.assertEqual(months["2026-09"]["reason"], "proven")
        self.assertEqual(months["2026-09"]["reading"]["day"], "2026-09-30")
        self.assertEqual(months["2026-09"]["reconcile"]["source_balance"], "1300.00")
        # With no as_of, the day it was read is the balance day: a read on 10-01 proves September.
        undated = {"balance": "1300.00", "retrieved_at": "2026-10-01T06:00:00Z", "data_through": "2026-10-01",
                   "partial": False}
        self.assertEqual(self.proof([undated])["2026-09"]["reason"], "proven")

    def test_days_use_the_reference_time_zone(self):
        # 09-30T23:30-07:00 is 10-01 in UTC, but still September 30 at the bank.
        late = {"balance": "1300.00", "retrieved_at": "2026-09-30T23:30:00-07:00", "data_through": "2026-10-01",
                "partial": False}
        self.assertEqual(self.proof([late])["2026-09"]["reason"], "no_balance_after_month_end")
        utc = {**late, "retrieved_at": "2026-10-01T06:30:00Z"}
        self.assertEqual(self.proof([utc])["2026-09"]["reason"], "proven", "in UTC the month end is over")
        self.assertEqual(self.proof([utc], time_zone="America/Los_Angeles")["2026-09"]["reason"],
                         "no_balance_after_month_end", "the bank's time zone wins over the offset")
        self.assertEqual(self.proof([late], time_zone="America/New_York")["2026-09"]["reason"], "proven")
        [row] = self.proof([utc], time_zone="Mars/Olympus").values()
        self.assertEqual(row["reason"], "unknown_time_zone")

    def test_a_missing_or_extra_transaction_leaves_the_month_unproven(self):
        months = self.proof([reading("1240.00")])
        self.assertEqual((months["2026-09"]["reason"], months["2026-09"]["difference"]), ("books_differ", "-10.00"))
        self.assertIsNone(months["2026-09"]["reconcile"])

    def test_a_later_matching_reading_proves_the_month_after_an_earlier_one_differs(self):
        months = self.proof([reading("1290.00", "2026-10-01"), reading("1250.00")])
        self.assertEqual(months["2026-09"]["reason"], "proven")
        self.assertEqual(months["2026-09"]["reading"]["day"], "2026-10-05")

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
        # August has a statement whose closing differs from the books: September's opening is not proven.
        record_statement(self.company, {"statement_id": "aug", "account": CHECKING, "kind": "statement",
                                        "period_start": "2026-08-01", "period_end": "2026-08-31",
                                        "opening_balance": "0.00", "closing_balance": "480.00"})
        months = self.proof([reading("1250.00")], from_month="2026-09")
        self.assertEqual((months["2026-09"]["reason"], months["2026-09"]["difference"]), ("opening_differs", "-20.00"))
        # A clean August reconciliation record is the opening.
        figures = self.proof([reading("1250.00")])["2026-08"]["reconcile"]
        apply(self.company, figures)
        months = self.proof([reading("1250.00")], from_month="2026-09")
        self.assertEqual(months["2026-09"]["reason"], "proven")
        self.assertEqual(months["2026-09"]["reconcile"]["source_opening_balance"], "500.00")

    def test_an_open_prior_record_leaves_the_opening_unproven(self):
        reconcile(self.company, CHECKING, Decimal("1.00"), date(2026, 8, 31))
        months = self.proof([reading("1250.00")])
        self.assertEqual(months["2026-08"]["reason"], "existing_record")
        self.assertEqual(months["2026-09"]["reason"], "opening_unproven")

    def test_the_first_month_needs_a_zero_opening_with_feed_rows_or_a_statement(self):
        # Books that start with an opening balance before the feed: the first feed month is not proven.
        post(self.company, CHECKING, "2026-07-31", "40.00", source=False)
        months = self.proof([reading("1290.00")])
        self.assertEqual(months["2026-08"]["reason"], "opening_unproven")
        self.assertEqual(months["2026-09"]["reason"], "opening_unproven", "nothing proves August's closing")
        record_statement(self.company, {"statement_id": "jul", "account": CHECKING, "kind": "statement",
                                        "period_start": "2026-07-01", "period_end": "2026-07-31",
                                        "opening_balance": "0.00", "closing_balance": "40.00"})
        months = self.proof([reading("1290.00")])
        self.assertEqual((months["2026-08"]["reason"], months["2026-09"]["reason"]), ("proven", "proven"))

    def test_a_quiet_zero_month_with_no_feed_rows_is_not_proven(self):
        record_feed_window(self.company, account=SAVINGS, start=date(2026, 8, 1), end=date(2026, 10, 5), source="feed-sav")
        months = self.proof([reading("0.00")], account_id="feed-sav", account_type="savings")
        self.assertEqual((months["2026-08"]["reason"], months["2026-09"]["reason"]), ("opening_unproven", "opening_unproven"))

    def test_cards_and_other_types_are_never_proven(self):
        post(self.company, CARD, "2026-09-05", "-80.00", other="Expenses:Software")
        for account_id, kind in (("feed-card", "credit"), ("feed-chk", "treasury"), ("feed-chk", "")):
            [row] = prove(self.company, [{"account_id": account_id, "account_type": kind,
                                          "readings": [reading("1250.00")]}], as_of=date(2026, 10, 20))["months"]
            self.assertEqual(row["reason"], "not_checking_or_savings")
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

    def test_a_proven_month_clears_the_statement_blockers_in_readiness(self):
        config = json.loads((self.company / "entity.json").read_text())
        config["bank_account_mappings"] = {"feed-chk": CHECKING}
        (self.company / "entity.json").write_text(json.dumps(config))

        def codes():
            document = assess_review_readiness(self.company, "2026-09", DependencyVersions(1, 1, 1, 1),
                                               as_of=date(2026, 10, 20)).to_document()
            return {item["code"] for item in document["blockers"]
                    if not item["refs"] or CHECKING in item["refs"]} & STATEMENT_CODES

        self.assertTrue(codes())
        apply(self.company, self.proof([reading("1250.00")])["2026-09"]["reconcile"])
        self.assertEqual(codes(), set())

    def test_cli_is_read_only_and_classified_as_read(self):
        input_path = Path(self._tmp.name) / "readings.json"
        input_path.write_text(json.dumps({"accounts": [{"account_id": "feed-chk", "account_type": "Checking",
                                                        "currency": "USD", "readings": [reading("1250.00")]}]}))
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


if __name__ == "__main__":
    unittest.main()
