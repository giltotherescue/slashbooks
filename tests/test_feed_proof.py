"""Tests for the month-end proof from a bank feed (``books balance prove``).

All data is synthetic.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
import hashlib
import io
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
from bookkeeping.ledger.model import Open  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.readiness import DependencyVersions, assess_review_readiness  # noqa: E402
from bookkeeping.reconcile import reconcile  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402
from test_tieout import CARD, CHECKING, make_company, post, stage  # noqa: E402

SAVINGS = "Assets:Bank:Savings"
STATEMENT_CODES = {"reconciliation_missing", "reconciliation_evidence_missing", "reconciliation_not_clean",
                   "opening_evidence_missing", "source_statement_incomplete", "source_evidence_provenance_missing"}


def reading(balance: str, day: str = "2026-10-05", **kw) -> dict:
    return {"balance": balance, "as_of": day, "retrieved_at": f"{day}T09:00:00Z", "data_through": day, **kw}


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
        document = prove(self.company, [{"account_id": account_id, "account_type": account_type,
                                         "readings": readings}], as_of=as_of, **kw)
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
        self.assertEqual(months["2026-08"]["reconcile"]["source_balance"], "500.00")
        self.assertEqual(months["2026-08"]["reconcile"]["source_opening_balance"], "0.00")

    def test_a_reading_on_the_month_end_needs_no_later_activity(self):
        months = self.proof([reading("1300.00", "2026-09-30"), reading("1250.00")])
        self.assertEqual(months["2026-09"]["reading"]["day"], "2026-09-30")
        self.assertEqual(months["2026-09"]["reconcile"]["source_balance"], "1300.00")

    def test_a_missing_or_extra_transaction_leaves_the_month_unproven(self):
        months = self.proof([reading("1240.00")])
        self.assertEqual((months["2026-09"]["reason"], months["2026-09"]["difference"]), ("books_differ", "-10.00"))
        self.assertIsNone(months["2026-09"]["reconcile"])

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

    def test_feed_gap_or_late_start_leaves_the_month_unproven(self):
        record_feed_window(self.company, account=SAVINGS, start=date(2026, 9, 12), end=date(2026, 10, 5), source="feed-sav")
        post(self.company, SAVINGS, "2026-09-15", "40.00")
        months = self.proof([reading("40.00")], account_id="feed-sav", account_type="savings")
        self.assertEqual(months["2026-09"]["reason"], "feed_incomplete")
        # The window ends before the reading day.
        months = self.proof([reading("1250.00", "2026-10-08", data_through="2026-10-08")])
        self.assertEqual(months["2026-09"]["reason"], "feed_incomplete")

    def test_pending_partial_or_undated_readings_are_not_used(self):
        for item in (reading("1250.00", data_through="2026-10-04"), reading("1250.00", partial=True),
                     {**reading("1250.00"), "retrieved_at": "2026-10-05T09:00:00"},
                     reading("1300.00", "2026-09-29")):
            months = self.proof([item])
            self.assertEqual(months["2026-09"]["reason"], "no_balance_after_month_end", item)
        # A reading after the day of the check is not used either.
        self.assertEqual(self.proof([reading("1250.00")], as_of=date(2026, 10, 4))["2026-09"]["reason"],
                         "no_balance_after_month_end")

    def test_waiting_rows_block_the_proof(self):
        stage(self.company, [{"id": "w", "date": "2026-10-03", "amount": "-9.00", "accountId": "feed-chk"}])
        self.assertEqual(self.proof([reading("1250.00")])["2026-09"]["reason"], "waiting_in_review")

    def test_months_after_the_proof_record_are_idempotent_and_statement_records_are_kept(self):
        figures = self.proof([reading("1250.00")])["2026-09"]["reconcile"]
        ref = "evidence:feed-balance-abc"
        result = reconcile(self.company, figures["account"], Decimal(figures["source_balance"]), date(2026, 9, 30),
                           source_opening_balance=Decimal(figures["source_opening_balance"]),
                           source_transaction_total=Decimal(figures["source_transaction_total"]),
                           source_snapshot_at=figures["source_snapshot_at"], source_through=figures["source_through"],
                           source_evidence_refs=[ref], source_evidence_hashes={ref: "b" * 64})
        self.assertTrue(result.is_clean)
        self.assertEqual(self.proof([reading("1250.00")])["2026-09"]["reason"], "already_reconciled")
        # A later change to the books makes the feed record stale, so the feed checks it again.
        post(self.company, CHECKING, "2026-09-28", "-1.00", other="Expenses:Software")
        self.assertEqual(self.proof([reading("1250.00")])["2026-09"]["reason"], "books_differ")
        # A record from a statement that does not match is left for a person.
        reconcile(self.company, CHECKING, Decimal("1.00"), date(2026, 8, 31))
        self.assertEqual(self.proof([reading("1249.00")])["2026-08"]["reason"], "existing_record")

    def test_a_proven_month_clears_the_statement_blockers_in_readiness(self):
        record_statement(self.company, {"statement_id": "unused", "account": SAVINGS, "kind": "feed_sync",
                                        "period_start": "2026-09-01", "period_end": "2026-09-30"})
        config = json.loads((self.company / "entity.json").read_text())
        config["bank_account_mappings"] = {"feed-chk": CHECKING}
        (self.company / "entity.json").write_text(json.dumps(config))

        def codes():
            document = assess_review_readiness(self.company, "2026-09", DependencyVersions(1, 1, 1, 1),
                                               as_of=date(2026, 10, 20)).to_document()
            return {item["code"] for item in document["blockers"]
                    if not item["refs"] or CHECKING in item["refs"]} & STATEMENT_CODES

        self.assertTrue(codes())
        figures = self.proof([reading("1250.00")])["2026-09"]["reconcile"]
        ref = "evidence:feed-balance-sep"
        reconcile(self.company, figures["account"], Decimal(figures["source_balance"]), date(2026, 9, 30),
                  source_opening_balance=Decimal(figures["source_opening_balance"]),
                  source_transaction_total=Decimal(figures["source_transaction_total"]),
                  source_snapshot_at=figures["source_snapshot_at"], source_through=figures["source_through"],
                  source_evidence_refs=[ref], source_evidence_hashes={ref: "c" * 64})
        self.assertEqual(codes(), set())

    def test_cli_is_read_only_and_classified_as_read(self):
        input_path = Path(self._tmp.name) / "readings.json"
        input_path.write_text(json.dumps({"accounts": [{"account_id": "feed-chk", "account_type": "Checking",
                                                        "readings": [reading("1250.00")]}]}))
        files = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.company.rglob("*") if p.is_file()}
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["balance", "prove", "--entity", str(self.company), "--input", str(input_path),
                         "--as-of", "2026-10-20", "--from", "2026-09"])
        document = json.loads(out.getvalue())
        self.assertEqual((code, [row["month"] for row in document["months"]]), (0, ["2026-09"]))
        self.assertEqual(document["months"][0]["status"], "proven")
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.company.rglob("*") if p.is_file()}
        self.assertEqual(files, after)
        spec = command_spec(["balance", "prove", "--entity", "e", "--input", "i.json", "--as-of", "2026-10-20"])
        self.assertFalse(spec["mutates"])
        self.assertEqual((spec["entity_fields"], spec["input_fields"]), (["entity"], ["input"]))


if __name__ == "__main__":
    unittest.main()
