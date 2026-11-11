"""Tests for transfer matches between own accounts and for transfers recorded two times.

A card payment shows in the bank file and in the card file. These tests make
sure that the two rows can be recorded as one transfer, and that the books
show when they were recorded two times. All data is synthetic.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping import queue  # noqa: E402
from bookkeeping import statement_import as si  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.coverage import check_coverage, readiness_items  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.import_check import check_import  # noqa: E402
from bookkeeping.connectors.bankcsv import ParsedCsv, ParsedRow, _statement_metadata  # noqa: E402
from bookkeeping.ledger.importer import reverse_and_correct, reverse_entry  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

CHECKING = "Assets:Bank:Checking"
VISA = "Liabilities:CreditCard:Visa"
AMEX = "Liabilities:CreditCard:Amex"
FIXTURES = ROOT / "tests" / "fixtures" / "statements"
TS = "2026-11-01T00:00:00Z"


def make_company(root: Path) -> Path:
    company = root / "co"
    (company / "staging").mkdir(parents=True)
    (company / "entity.json").write_text(json.dumps({
        "name": "Synthetic Co", "business_type": "consulting",
        "bank_account_mappings": {"feed-chk": CHECKING, "feed-visa": VISA, "feed-amex": AMEX},
    }) + "\n")
    (company / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n")
    store = LedgerStore(company / "ledger.sqlite")
    store.initialize()
    with store.transaction() as conn:
        store.set_meta("canonical", "true", conn)
        store.insert_opens([Open(date(2026, 1, 1), name, ("USD",)) for name in (
            CHECKING, VISA, AMEX, "Expenses:Software", "Income:Consulting")], conn)
    return company


def stage(company: Path, *rows: tuple[str, str, str, str, str]) -> None:
    """Add staged rows: (id, feed account ID, date, amount, description)."""
    path = company / "staging" / "pending-categorization.json"
    existing = json.loads(path.read_text()) if path.exists() else []
    existing += [{"id": sid, "accountId": feed, "date": day, "amount": amount, "description": text}
                 for sid, feed, day, amount, text in rows]
    path.write_text(json.dumps(existing) + "\n")


def staged_ids(company: Path) -> list[str]:
    path = company / "staging" / "pending-categorization.json"
    return [row["id"] for row in json.loads(path.read_text())] if path.exists() else []


def entry_ids(company: Path) -> dict[str, int]:
    with LedgerStore(company / "ledger.sqlite").connection() as conn:
        return {str(sid): int(eid) for eid, sid in conn.execute("SELECT id, source_id FROM entries")}


def ledger_rows(company: Path) -> list[tuple]:
    with LedgerStore(company / "ledger.sqlite").connection() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT e.id, e.date, e.source_id, p.account, p.amount FROM entries e "
            "JOIN postings p ON p.entry_id = e.id ORDER BY e.id, p.id")]


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TransferMatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.company = make_company(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    @property
    def entity(self):
        return load_entity(self.company)

    def post(self, source_id: str, category: str) -> int:
        queue.propose(self.entity, source_id, category, "The owner chose this account.")
        queue.confirm(self.entity, source_id, "owner-review", ts=TS)
        return entry_ids(self.company)[source_id]

    def matches(self, source_id: str, counter: str) -> list[dict]:
        return queue.find_transfer_matches(self.entity, source_id, counter)["matches"]

    def test_staged_counterpart_is_found_and_the_named_pair_posts_once(self):
        stage(self.company,
              ("chk-pay", "feed-chk", "2026-10-05", "-1250.00", "PAYMENT TO VISA ENDING IN 0042"),
              ("visa-pay", "feed-visa", "2026-10-06", "1250.00", "PAYMENT THANK YOU"),
              ("chk-other", "feed-chk", "2026-10-06", "-1250.00", "WIRE TO VENDOR"))
        document = queue.find_transfer_matches(self.entity, "visa-pay", CHECKING)
        self.assertEqual(document["source"], {"source_id": "visa-pay", "date": "2026-10-06", "amount": "1250.00",
                                              "account": VISA, "description": "PAYMENT THANK YOU"})
        # Two checking rows qualify; both are returned, nearest date first, and none is chosen.
        self.assertEqual([(m["match_type"], m["source_id"], m["date_days_apart"], m["action"])
                          for m in document["matches"]],
                         [("staged", "chk-other", 0, "propose_transfer"), ("staged", "chk-pay", 1, "propose_transfer")])
        self.assertEqual(document["matches"][1]["amount"], "-1250.00")
        self.assertIsNone(document["matches"][1]["entry_id"])
        # The person names one pair; the existing transfer workflow records it as one transfer.
        item = queue.propose_transfer(self.entity, ["chk-pay", "visa-pay"], "The owner named this pair.")
        queue.confirm_transfer(self.entity, item["source_id"], "owner-review", ts=TS)
        self.assertEqual(staged_ids(self.company), ["chk-other"])
        cash = [row for row in ledger_rows(self.company) if row[3] == CHECKING]
        self.assertEqual([row[4] for row in cash], ["-1250.00"])
        self.assertEqual(queue.find_transfer_doubles(self.entity)["pairs"], [])

    def test_posted_counterpart_is_matched_with_no_new_entry(self):
        stage(self.company,
              ("chk-pay", "feed-chk", "2026-10-05", "-1250.00", "PAYMENT TO VISA ENDING IN 0042"),
              ("visa-pay", "feed-visa", "2026-10-05", "1250.00", "PAYMENT THANK YOU"))
        entry_id = self.post("chk-pay", VISA)
        [match] = self.matches("visa-pay", CHECKING)
        self.assertEqual(match, {
            "match_type": "posted", "source_id": "chk-pay", "entry_id": entry_id, "entry_source_id": "chk-pay",
            "date": "2026-10-05", "amount": "-1250.00", "account": CHECKING,
            "description": "PAYMENT TO VISA ENDING IN 0042", "date_days_apart": 0,
            "action": "confirm_transfer_match", "actionable": True, "reason": None})
        before = ledger_rows(self.company)
        result = queue.confirm_transfer_match(self.entity, "visa-pay", entry_id, "owner-review", ts=TS)
        self.assertEqual(result, {
            "schema_version": 1, "kind": "transfer_match_recorded", "source_id": "visa-pay",
            "date": "2026-10-05", "amount": "1250.00", "account": VISA, "counter_account": CHECKING,
            "entry_id": entry_id, "entry_source_id": "chk-pay", "counter_source_id": "chk-pay",
            "source_status": "matched_to_entry", "entries_written": 0, "recovered": False,
            "live_entry_id": entry_id})
        self.assertEqual(ledger_rows(self.company), before)
        self.assertEqual(staged_ids(self.company), [])
        with LedgerStore(self.company / "ledger.sqlite").connection() as conn:
            payload, status = conn.execute(
                "SELECT payload_json, status FROM source_transactions WHERE id = 'visa-pay'").fetchone()
        self.assertEqual(status, "matched_to_entry")
        self.assertEqual(json.loads(payload)["transferMatch"]["entry_id"], entry_id)
        # A retry finishes nothing new and reports the recorded match.
        again = queue.confirm_transfer_match(self.entity, "visa-pay", entry_id, "owner-review", ts=TS)
        self.assertTrue(again["recovered"])
        # The entry is taken: a second card row with the same payment does not match it.
        stage(self.company, ("visa-pay-2", "feed-visa", "2026-10-05", "1250.00", "PAYMENT THANK YOU"))
        self.assertEqual(self.matches("visa-pay-2", CHECKING), [])
        with self.assertRaisesRegex(ValueError, "does not match"):
            queue.confirm_transfer_match(self.entity, "visa-pay-2", entry_id, "owner-review", ts=TS)
        self.assertEqual(queue.find_transfer_doubles(self.entity)["pairs"], [])

    def test_matched_card_row_counts_in_the_card_import_check(self):
        si.save_profile(self.company, FIXTURES / "checking_three_months.csv", name="chk", ledger_account=CHECKING)
        si.save_profile(self.company, FIXTURES / "card_printed_period.csv", name="visa", ledger_account=VISA)
        si.import_file(self.company, FIXTURES / "checking_three_months.csv", profile="chk", ts=TS)
        card = si.import_file(self.company, FIXTURES / "card_printed_period.csv", profile="visa", ts=TS)
        statement_id = card["statements"][0]["statement"]["statement_id"]
        rows = json.loads((self.company / "staging" / "pending-categorization.json").read_text())
        bank_pay = next(r["id"] for r in rows if r["description"].startswith("PAYMENT TO VISA"))
        card_pay = next(r["id"] for r in rows if r["description"] == "PAYMENT THANK YOU")
        entry_id = self.post(bank_pay, VISA)
        queue.confirm_transfer_match(self.entity, card_pay, entry_id, "owner-review", ts=TS)
        check = check_import(self.company, statement_id)
        credits = next(c for c in check["checks"] if c["id"] == "credits")
        self.assertEqual((credits["status"], credits["source"], credits["books"]), ("matches", "1262.99", "1262.99"))

    def test_equal_payments_to_two_cards_do_not_cross_match(self):
        stage(self.company,
              ("chk-visa", "feed-chk", "2026-10-05", "-500.00", "PAYMENT TO VISA"),
              ("chk-amex", "feed-chk", "2026-10-05", "-500.00", "PAYMENT TO AMEX"),
              ("visa-pay", "feed-visa", "2026-10-05", "500.00", "PAYMENT THANK YOU"),
              ("amex-pay", "feed-amex", "2026-10-06", "500.00", "PAYMENT RECEIVED"))
        self.assertEqual([m["source_id"] for m in self.matches("chk-visa", VISA)], ["visa-pay"])
        self.assertEqual([m["source_id"] for m in self.matches("chk-amex", AMEX)], ["amex-pay"])
        visa_entry = self.post("chk-visa", VISA)
        amex_entry = self.post("chk-amex", AMEX)
        # Each card row finds only the entry whose other line is on its own card.
        self.assertEqual([m["entry_id"] for m in self.matches("visa-pay", CHECKING)], [visa_entry])
        self.assertEqual([m["entry_id"] for m in self.matches("amex-pay", CHECKING)], [amex_entry])
        with self.assertRaisesRegex(ValueError, "does not match"):
            queue.confirm_transfer_match(self.entity, "amex-pay", visa_entry, "owner-review", ts=TS)
        self.assertEqual(sorted(staged_ids(self.company)), ["amex-pay", "visa-pay"])

    def test_four_day_gap_is_not_a_match(self):
        stage(self.company,
              ("chk-pay", "feed-chk", "2026-10-01", "-300.00", "PAYMENT TO VISA"),
              ("chk-posted", "feed-chk", "2026-10-01", "-300.00", "PAYMENT TO VISA"),
              ("visa-pay", "feed-visa", "2026-10-05", "300.00", "PAYMENT THANK YOU"))
        self.post("chk-posted", VISA)
        document = queue.find_transfer_matches(self.entity, "visa-pay", CHECKING)
        self.assertEqual((document["count"], document["matches"]), (0, []))
        self.assertEqual(queue.find_transfer_matches(self.entity, "visa-pay", CHECKING, 4)["count"], 2)

    def test_same_day_equal_amounts_return_both_and_choose_none(self):
        stage(self.company,
              ("chk-pay-1", "feed-chk", "2026-10-05", "-300.00", "PAYMENT TO VISA"),
              ("chk-pay-2", "feed-chk", "2026-10-05", "-300.00", "PAYMENT TO VISA"),
              ("visa-pay", "feed-visa", "2026-10-05", "300.00", "PAYMENT THANK YOU"))
        first, second = self.post("chk-pay-1", VISA), self.post("chk-pay-2", VISA)
        document = queue.find_transfer_matches(self.entity, "visa-pay", CHECKING)
        self.assertEqual(document["count"], 2)
        self.assertEqual([m["entry_id"] for m in document["matches"]], [first, second])
        self.assertTrue(all(m["date_days_apart"] == 0 for m in document["matches"]))
        self.assertEqual(staged_ids(self.company), ["visa-pay"])

    def test_refusals(self):
        stage(self.company, ("visa-pay", "feed-visa", "2026-10-05", "300.00", "PAYMENT THANK YOU"))
        with self.assertRaisesRegex(ValueError, "another account"):
            queue.find_transfer_matches(self.entity, "visa-pay", VISA)
        with self.assertRaisesRegex(ValueError, "Assets: or Liabilities:"):
            queue.find_transfer_matches(self.entity, "visa-pay", "Expenses:Software")
        with self.assertRaisesRegex(ValueError, "not a staged transaction"):
            queue.find_transfer_matches(self.entity, "missing", CHECKING)
        with self.assertRaisesRegex(ValueError, "not a live two-line entry"):
            queue.confirm_transfer_match(self.entity, "visa-pay", 999, "owner-review", ts=TS)
        stage(self.company, ("chk-pay", "feed-chk", "2026-10-05", "-300.00", "PAYMENT TO VISA"))
        entry_id = self.post("chk-pay", VISA)
        queue.propose(self.entity, "visa-pay", "Expenses:Software", "A suggestion that waits.")
        with self.assertRaisesRegex(ValueError, "open proposal"):
            queue.confirm_transfer_match(self.entity, "visa-pay", entry_id, "owner-review", ts=TS)

    def test_doubled_transfer_is_found_as_a_pair(self):
        stage(self.company,
              ("chk-pay", "feed-chk", "2026-10-05", "-1250.00", "PAYMENT TO VISA"),
              ("visa-pay", "feed-visa", "2026-10-06", "1250.00", "PAYMENT THANK YOU"),
              ("chk-late", "feed-chk", "2026-10-01", "-80.00", "PAYMENT TO VISA"),
              ("visa-late", "feed-visa", "2026-10-06", "80.00", "PAYMENT THANK YOU"),
              ("chk-sub", "feed-chk", "2026-10-07", "-20.00", "SOFTWARE"))
        bank_entry, card_entry = self.post("chk-pay", VISA), self.post("visa-pay", CHECKING)
        self.post("chk-late", VISA)
        self.post("visa-late", CHECKING)  # Five days apart: not a pair.
        self.post("chk-sub", "Expenses:Software")
        document = queue.find_transfer_doubles(self.entity)
        self.assertEqual(document["count"], 1)
        self.assertEqual(document["pairs"], [{
            "amount": "1250.00", "date_days_apart": 1, "accounts": [CHECKING, VISA],
            "entries": [
                {"entry_id": bank_entry, "entry_source_id": "chk-pay", "source_id": "chk-pay", "date": "2026-10-05",
                 "account": CHECKING, "amount": "-1250.00", "counter_account": VISA,
                 "description": "PAYMENT TO VISA"},
                {"entry_id": card_entry, "entry_source_id": "visa-pay", "source_id": "visa-pay", "date": "2026-10-06",
                 "account": VISA, "amount": "1250.00", "counter_account": CHECKING,
                 "description": "PAYMENT THANK YOU"},
            ]}])
        self.assertEqual(queue.find_transfer_doubles(self.entity, 5)["count"], 2)

    def test_cli_json_and_remote_classification(self):
        stage(self.company,
              ("chk-pay", "feed-chk", "2026-10-05", "-75.00", "PAYMENT TO VISA"),
              ("visa-pay", "feed-visa", "2026-10-05", "75.00", "PAYMENT THANK YOU"))
        entry_id = self.post("chk-pay", VISA)
        entity = str(self.company)
        code, out, _ = run_cli("queue", "transfer-matches", "--entity", entity, "--source-id", "visa-pay",
                               "--counter-account", CHECKING)
        self.assertEqual((code, json.loads(out)["matches"][0]["entry_id"]), (0, entry_id))
        code, out, _ = run_cli("queue", "transfer-doubles", "--entity", entity)
        self.assertEqual((code, json.loads(out)["kind"]), (0, "transfer_doubles"))
        code, _, err = run_cli("queue", "confirm-transfer-match", "--entity", entity, "--source-id", "visa-pay",
                               "--entry-id", "999")
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)
        code, out, _ = run_cli("queue", "confirm-transfer-match", "--entity", entity, "--source-id", "visa-pay",
                               "--entry-id", str(entry_id))
        self.assertEqual((code, json.loads(out)["source_status"]), (0, "matched_to_entry"))
        for argv, mutates in ((["queue", "transfer-matches", "--entity", "@entity", "--source-id", "x",
                                "--counter-account", CHECKING], False),
                              (["queue", "transfer-doubles", "--entity", "@entity"], False),
                              (["queue", "confirm-transfer-match", "--entity", "@entity", "--source-id", "x",
                                "--entry-id", "1"], True),
                              (["queue", "propose-transfer", "--entity", "@entity", "--source-id", "a",
                                "--source-id", "b", "--reasoning", "r", "--json"], True)):
            with self.subTest(argv=argv):
                self.assertEqual(command_spec(argv)["mutates"], mutates)

    def test_propose_and_confirm_transfer_print_json_on_request(self):
        stage(self.company,
              ("chk-pay", "feed-chk", "2026-10-05", "-75.00", "PAYMENT TO VISA"),
              ("visa-pay", "feed-visa", "2026-10-05", "75.00", "PAYMENT THANK YOU"))
        entity = str(self.company)
        code, out, _ = run_cli("queue", "propose-transfer", "--entity", entity, "--source-id", "chk-pay",
                               "--source-id", "visa-pay", "--reasoning", "Named pair.", "--json")
        item = json.loads(out)
        self.assertEqual((code, item["proposal_type"], item["status"]), (0, "transfer-pair", "open"))
        code, out, _ = run_cli("queue", "confirm-transfer", "--entity", entity, "--item", item["source_id"], "--json")
        self.assertEqual((code, json.loads(out)["status"]), (0, "confirmed"))


class TransferMatchReviewTests(unittest.TestCase):
    """Reproductions from an independent review of the transfer match."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.company = make_company(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    @property
    def entity(self):
        return load_entity(self.company)

    def matched_transfer(self, day: str = "2026-10-31") -> int:
        stage(self.company, ("chk", "feed-chk", day, "-100.00", "PAYMENT TO VISA"),
              ("visa", "feed-visa", day, "100.00", "PAYMENT THANK YOU"))
        queue.propose(self.entity, "chk", VISA, "The owner chose the card account.")
        queue.confirm(self.entity, "chk", "owner-review", ts=TS)
        entry_id = entry_ids(self.company)["chk"]
        queue.confirm_transfer_match(self.entity, "visa", entry_id, "owner-review", ts=TS)
        return entry_id

    def replacement(self, *, day: date = date(2026, 10, 31), amount: str = "100.00",
                    narration: str = "Corrected memo") -> Entry:
        return Entry(date=day, narration=narration, flag="*", meta=(("source-id", "chk-corrected"),),
                     postings=(Posting(CHECKING, -Decimal(amount)), Posting(VISA, Decimal(amount))))

    def test_claim_follows_a_memo_correction_and_takes_no_second_row(self):
        original = self.matched_transfer()
        reverse_and_correct(self.entity, "chk", self.replacement(), "owner-review", ts=TS)
        successor = entry_ids(self.company)["chk-corrected"]
        stage(self.company, ("visa2", "feed-visa", "2026-10-31", "100.00", "OTHER PAYMENT"))
        # The corrected entry is the same transfer, and a row is matched to it already.
        self.assertEqual(queue.find_transfer_matches(self.entity, "visa2", CHECKING)["matches"], [])
        with self.assertRaisesRegex(ValueError, "does not match"):
            queue.confirm_transfer_match(self.entity, "visa2", successor, "owner-review", ts=TS)
        self.assertEqual(staged_ids(self.company), ["visa2"])
        # The receipt keeps the entry that the person matched; the live claim is the corrected entry.
        replay = queue.confirm_transfer_match(self.entity, "visa", original, "owner-review", ts=TS)
        self.assertEqual((replay["recovered"], replay["entry_id"], replay["live_entry_id"]),
                         (True, original, successor))

    def test_a_correction_that_breaks_the_match_is_refused(self):
        self.matched_transfer()
        before = ledger_rows(self.company)
        for name, entry in (("amount", self.replacement(amount="90.00")),
                            ("date", self.replacement(day=date(2026, 10, 30)))):
            with self.subTest(name), self.assertRaises(queue.TransferMatchLineageError) as caught:
                reverse_and_correct(self.entity, "chk", entry, "owner-review", ts=TS)
            self.assertIn("source row 'visa' is matched to", str(caught.exception))
        with self.assertRaises(queue.TransferMatchLineageError):
            reverse_entry(self.entity, "chk", "owner-review", ts=TS)
        self.assertEqual(ledger_rows(self.company), before)

    def test_a_posted_match_on_another_day_is_not_actionable(self):
        stage(self.company, ("chk", "feed-chk", "2026-10-31", "-100.00", "PAYMENT TO VISA"),
              ("visa", "feed-visa", "2026-11-01", "100.00", "PAYMENT THANK YOU"))
        queue.propose(self.entity, "chk", VISA, "The owner chose the card account.")
        queue.confirm(self.entity, "chk", "owner-review", ts=TS)
        entry_id = entry_ids(self.company)["chk"]
        [match] = queue.find_transfer_matches(self.entity, "visa", CHECKING)["matches"]
        self.assertEqual((match["entry_id"], match["date_days_apart"], match["action"], match["actionable"],
                          match["reason"]), (entry_id, 1, "none", False, "dates_differ"))
        before = ledger_rows(self.company)
        with self.assertRaisesRegex(ValueError, "must have the same date"):
            queue.confirm_transfer_match(self.entity, "visa", entry_id, "owner-review", ts=TS)
        self.assertEqual((ledger_rows(self.company), staged_ids(self.company)), (before, ["visa"]))

    def test_card_balances_need_dates_that_agree_with_the_period(self):
        rows = ParsedCsv([ParsedRow(4, "2026-10-02", "charge", Decimal("-25"))])
        september = [{"kind": "opening", "date": "2026-09-01", "amount": Decimal("100")},
                     {"kind": "closing", "date": "2026-09-30", "amount": Decimal("125")}]
        for name, period in (("period excludes the rows", ("2026-09-01", "2026-09-30")),
                             ("balance lines outside the period", ("2026-10-01", "2026-10-31"))):
            with self.subTest(name):
                meta = _statement_metadata(rows, [dict(m) for m in september], "liability", period)
                self.assertEqual((meta["opening_balance"], meta["closing_balance"], meta["balance_source"]),
                                 (None, None, None))
                self.assertTrue(any("were not recorded" in w for w in meta["_warnings"]))
        october = [{"kind": "opening", "date": "2026-09-30", "amount": Decimal("100")},
                   {"kind": "closing", "date": "2026-10-31", "amount": Decimal("125")}]
        meta = _statement_metadata(rows, october, "liability", ("2026-10-01", "2026-10-31"))
        self.assertEqual((meta["opening_balance"], meta["closing_balance"]), (Decimal("-100"), Decimal("-125")))

    def test_every_candidate_carries_its_evidence(self):
        # A refund on the card and a payment from checking can look like one transfer. The engine cannot
        # tell, so it returns the candidate with its evidence and chooses nothing.
        stage(self.company, ("payment", "feed-chk", "2026-10-05", "-25.00", "ONLINE PAYMENT"),
              ("card-refund", "feed-visa", "2026-10-05", "25.00", "REFUND SOFTWARE"))
        document = queue.find_transfer_matches(self.entity, "card-refund", CHECKING)
        self.assertEqual(document["source"]["description"], "REFUND SOFTWARE")
        [match] = document["matches"]
        for field in ("match_type", "source_id", "date", "amount", "account", "description",
                      "date_days_apart", "action", "actionable", "reason"):
            self.assertIn(field, match)
        self.assertEqual((match["description"], match["amount"], match["actionable"]), ("ONLINE PAYMENT", "-25.00", True))


class StatementScenarioTests(unittest.TestCase):
    """A three-month checking file and a card file with a printed period, as a person reviews them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.company = make_company(Path(self._tmp.name))
        for name, profile, account in (("checking_three_months.csv", "chk", CHECKING),
                                       ("card_printed_period.csv", "visa", VISA)):
            si.save_profile(self.company, FIXTURES / name, name=profile, ledger_account=account)
            si.import_file(self.company, FIXTURES / name, profile=profile, ts=TS)
        rows = json.loads((self.company / "staging" / "pending-categorization.json").read_text())
        self.bank_pay = next(r["id"] for r in rows if r["description"].startswith("PAYMENT TO VISA"))
        self.card_pay = next(r["id"] for r in rows if r["description"] == "PAYMENT THANK YOU")
        entity = load_entity(self.company)
        for row in rows:
            if row["id"] in {self.bank_pay, self.card_pay}:
                continue
            category = "Income:Consulting" if float(row["amount"]) > 0 and row["accountId"] == CHECKING \
                else "Expenses:Software"
            queue.propose(entity, row["id"], category, "Synthetic review.")
            queue.confirm(entity, row["id"], "review", ts=TS)
        queue.propose(entity, self.bank_pay, VISA, "Card payment.")
        queue.confirm(entity, self.bank_pay, "review", ts=TS)

    def tearDown(self):
        self._tmp.cleanup()

    def coverage(self):
        return check_coverage(self.company, date(2026, 8, 1), date(2026, 10, 31))

    def october(self, document, account):
        row = next(row for row in document["accounts"] if row["account"] == account)
        return next(month for month in row["months"] if month["month"] == "2026-10")

    def test_payment_recorded_twice_shows_in_both_tie_outs(self):
        entity = load_entity(self.company)
        queue.propose(entity, self.card_pay, CHECKING, "The owner chose the checking account.")
        queue.confirm(entity, self.card_pay, "review", ts=TS)
        document = self.coverage()
        for account in (CHECKING, VISA):
            tie = self.october(document, account)["tieout"]
            self.assertEqual((tie["status"], tie["difference"]), ("differs", "1250.00"), account)
        blockers, warnings = readiness_items(document)
        self.assertNotIn("statement_tieout_differs", [w["code"] for w in warnings])
        differs = [w for w in blockers if w["code"] == "statement_tieout_differs"]
        self.assertEqual(len(differs), 2)
        self.assertTrue(all("(difference 1250.00)" in w["message"] for w in differs))
        self.assertEqual(queue.find_transfer_doubles(entity)["count"], 1)

    def test_payment_matched_once_ties_out(self):
        [match] = queue.find_transfer_matches(load_entity(self.company), self.card_pay, CHECKING)["matches"]
        queue.confirm_transfer_match(load_entity(self.company), self.card_pay, match["entry_id"], "review", ts=TS)
        document = self.coverage()
        for account in (CHECKING, VISA):
            month = self.october(document, account)
            self.assertEqual((month["status"], month["tieout"]["status"], month["tieout"]["difference"]),
                             ("complete", "matches", "0.00"), account)
        blockers, warnings = readiness_items(document)
        self.assertEqual([w for w in blockers + warnings if w["code"] == "statement_tieout_differs"], [])


if __name__ == "__main__":
    unittest.main()
