"""Tests for `books import check`. All data is synthetic."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bookkeeping import import_check  # noqa: E402
from bookkeeping import queue as queue_module  # noqa: E402
from bookkeeping import statement_import as si  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "statements"
CHECKING = "Assets:Bank:Checking"
CARD = "Liabilities:CreditCard:Visa"
TS = "2026-02-01T00:00:00Z"


def make_company(root: Path) -> Path:
    company = root / "co"
    (company / "staging").mkdir(parents=True)
    (company / "entity.json").write_text(json.dumps({
        "name": "Synthetic Co", "business_type": "consulting", "bank_account_mappings": {}}) + "\n")
    (company / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n")
    return company


def run_cli(*argv: str):
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(list(argv))
    return code, json.loads(out.getvalue())


class ImportCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entity = make_company(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def map(self, name, profile, account=CHECKING, **kw):
        extra = {k: kw.pop(k) for k in ("boundary_date", "side") if k in kw}
        return si.save_profile(self.entity, FIXTURES / name, name=profile, ledger_account=account,
                               overrides=kw, **extra)

    def imp(self, name, profile=None, **kw):
        return si.import_file(self.entity, FIXTURES / name, profile=profile, ts=TS, **kw)

    def csv(self, name, *rows):
        path = Path(self._tmp.name) / name
        path.write_text("Date,Description,Amount\n" + "".join(f"{r}\n" for r in rows))
        return path

    def import_csv(self, path, profile, account=CHECKING, **kw):
        si.save_profile(self.entity, path, name=profile, ledger_account=account,
                         overrides={"sign": "standard", "date_format": "%m/%d/%Y"})
        return si.import_file(self.entity, path, profile=profile, ts=TS, **kw)["statements"][0]

    def import_chase(self, **kw):
        self.map("chase_like_checking.csv", "chase")
        kw = {"period_start": "2026-01-01", "period_end": "2026-01-31", "opening_balance": "1000.00",
              "closing_balance": "3142.26", **kw}
        return self.imp("chase_like_checking.csv", "chase", **kw)["statements"][0]

    def checks(self, result):
        return {c["id"]: c for c in result["checks"]}

    def queue_path(self):
        return self.entity / "staging" / "pending-categorization.json"

    def record_rows(self, source_ids, account=CHECKING):
        """Move queued rows into the ledger, as an approval would."""
        queue = json.loads(self.queue_path().read_text())
        store = LedgerStore(self.entity / "ledger.sqlite")
        store.initialize()
        keep = []
        with store.transaction() as conn:
            store.insert_opens([Open(date(2026, 1, 1), n, ("USD",)) for n in (account, "Expenses:Other")], conn)
            for txn in queue:
                if txn["id"] in source_ids:
                    amount = Decimal(txn["amount"])
                    store.insert_entries([Entry(
                        date=date.fromisoformat(txn["date"]), narration="Synthetic",
                        meta=(("source-id", txn["id"]),),
                        postings=(Posting(account, amount), Posting("Expenses:Other", -amount)))], conn)
                else:
                    keep.append(txn)
        self.queue_path().write_text(json.dumps(keep))

    def test_import_stores_a_matching_result(self):
        entry = self.import_chase()
        result = entry["import_check"]
        self.assertEqual(result["status"], "matches")
        self.assertEqual((result["schema_version"], result["kind"], result["account"]), (1, "statement", CHECKING))
        self.assertEqual(result["import_id"], entry["statement"]["statement_id"])
        self.assertEqual(result["counts"], {"in_source": 6, "recorded": 0, "queued": 6, "duplicate": 0,
                                            "pending_skipped": 0})
        self.assertEqual(result["waiting_rows"], 6)
        self.assertEqual(result["computed_at"], TS)
        self.assertTrue(result["books_revision"].startswith("sha256:"))
        c = self.checks(result)
        self.assertEqual((c["count"]["status"], c["count"]["source"], c["count"]["books"], c["count"]["difference"]),
                         ("matches", "6", "6", "0"))
        self.assertEqual((c["debits"]["source"], c["debits"]["books"]), ("358.99", "358.99"))
        self.assertEqual((c["credits"]["source"], c["credits"]["books"]), ("2501.25", "2501.25"))
        self.assertEqual((c["closing"]["status"], c["closing"]["detail_code"], c["closing"]["books"]),
                         ("matches", "queued_rows_included", "3142.26"))
        self.assertEqual((c["opening"]["status"], c["opening"]["detail_code"]), ("not_checkable", "first_statement"))
        self.assertEqual(import_check.load_stored(self.entity, result["import_id"]), result)

    def test_recompute_from_the_archived_file_and_cli(self):
        entry = self.import_chase()
        sid = entry["import_check"]["import_id"]
        before = self.queue_path().read_text()
        code, result = run_cli("import", "check", sid, "--entity", str(self.entity))
        self.assertEqual(code, 0)
        self.assertEqual({k: v for k, v in result.items() if k != "computed_at"},
                         {k: v for k, v in entry["import_check"].items() if k != "computed_at"})
        code, stored = run_cli("import", "check", sid, "--entity", str(self.entity), "--stored")
        self.assertEqual((code, stored), (0, entry["import_check"]))
        self.assertEqual(self.queue_path().read_text(), before)
        code, missing = run_cli("import", "check", "st-nope", "--entity", str(self.entity))
        self.assertEqual((code, missing["error"]["code"]), (1, "import_not_found"))

    def test_posted_rows_use_ledger_postings_and_a_changed_row_differs(self):
        entry = self.import_chase()
        sid = entry["import_check"]["import_id"]
        queue = json.loads(self.queue_path().read_text())
        self.record_rows({t["id"] for t in queue if Decimal(t["amount"]) < 0})
        result = import_check.check_import(self.entity, sid, ts=TS)
        self.assertEqual(result["status"], "matches")
        self.assertEqual((result["counts"]["recorded"], result["counts"]["queued"]), (4, 2))
        self.assertEqual(result["waiting_rows"], 2)
        # Change one queued amount after the import: totals and closing now differ.
        queue = json.loads(self.queue_path().read_text())
        queue[0]["amount"] = "1.00"
        self.queue_path().write_text(json.dumps(queue))
        result = import_check.check_import(self.entity, sid, ts=TS)
        c = self.checks(result)
        self.assertEqual(result["status"], "differs")
        self.assertEqual(c["count"]["status"], "matches")
        self.assertEqual((c["credits"]["status"], c["credits"]["detail_code"]), ("differs", "totals_differ"))
        self.assertEqual((c["closing"]["status"], c["closing"]["detail_code"]), ("differs", "books_differ"))

    def test_row_on_the_period_start_day_is_in_the_books_movement(self):
        path = self.csv("jan.csv", "01/01/2026,TEST A,-10.00", "01/15/2026,TEST B,100.00")
        entry = self.import_csv(path, "jan", period_start="2026-01-01", period_end="2026-01-31",
                                opening_balance="500.00", closing_balance="590.00")
        c = self.checks(entry["import_check"])
        self.assertEqual((c["closing"]["status"], c["closing"]["books"]), ("matches", "590.00"))
        self.assertEqual(entry["import_check"]["status"], "matches")

    def test_unaccounted_row_differs_on_count(self):
        entry = self.import_chase()
        queue = json.loads(self.queue_path().read_text())
        self.queue_path().write_text(json.dumps(queue[1:]))
        result = import_check.check_import(self.entity, entry["import_check"]["import_id"], ts=TS)
        count = self.checks(result)["count"]
        self.assertEqual((result["status"], count["status"], count["detail_code"], count["difference"]),
                         ("differs", "differs", "rows_unaccounted", "1"))
        self.assertEqual(result["counts"]["queued"], 5)

    def test_pending_rows_are_counted_and_skipped(self):
        self.map("pending_rows.csv", "pending")
        [entry] = self.imp("pending_rows.csv", "pending")["statements"]
        result = entry["import_check"]
        self.assertEqual(result["counts"], {"in_source": 4, "recorded": 0, "queued": 3, "duplicate": 0,
                                            "pending_skipped": 1})
        c = self.checks(result)
        self.assertEqual(c["count"]["status"], "matches")
        self.assertEqual((c["debits"]["source"], c["credits"]["source"]), ("4.50", "110.00"))
        self.assertEqual((c["closing"]["status"], c["closing"]["detail_code"]), ("not_checkable", "no_balances"))
        self.assertEqual(result["status"], "partial")

    def test_boundary_rows_count_as_duplicates_at_file_amount(self):
        self.map("pending_rows.csv", "pending", boundary_date="2026-01-07", side="before")
        [entry] = self.imp("pending_rows.csv", "pending")["statements"]
        result = entry["import_check"]
        self.assertEqual(result["counts"]["duplicate"], 1)
        c = self.checks(result)
        self.assertEqual(c["count"]["status"], "matches")
        self.assertEqual((c["credits"]["status"], c["credits"]["detail_code"]),
                         ("matches", "duplicates_at_file_amount"))

    def test_held_duplicate_is_counted_and_waits(self):
        config = json.loads((self.entity / "entity.json").read_text())
        config["bank_account_mappings"] = {"feed-chk": CHECKING}
        (self.entity / "entity.json").write_text(json.dumps(config))
        self.queue_path().write_text(json.dumps([{
            "id": "feed:1", "date": "2026-01-05", "description": "NORTHWIND COFFEE", "amount": "-4.50",
            "accountId": "feed-chk", "pending": False}]))
        entry = self.import_chase()
        self.assertEqual(len(entry["held"]), 1)
        result = entry["import_check"]
        self.assertEqual(result["counts"], {"in_source": 6, "recorded": 0, "queued": 5, "duplicate": 1,
                                            "pending_skipped": 0})
        self.assertEqual(result["waiting_rows"], 6)
        c = self.checks(result)
        self.assertEqual((result["status"], c["count"]["status"]), ("matches", "matches"))
        self.assertEqual(c["debits"]["detail_code"], "duplicates_at_file_amount")
        # The person decides the held row is a duplicate: it still counts, and no longer waits.
        queue_module.resolve_duplicate_candidate(load_entity(self.entity), entry["held"][0]["source_id"], "duplicate")
        after = import_check.check_import(self.entity, result["import_id"], ts=TS)
        self.assertEqual((after["counts"]["duplicate"], after["waiting_rows"]), (1, 5))
        self.assertEqual(after["status"], "matches")

    def test_card_liability_sign(self):
        self.map("card_debit_credit.csv", "card", account=CARD)
        [entry] = self.imp("card_debit_credit.csv", "card", period_start="2026-01-01", period_end="2026-01-31",
                           opening_balance="1000.00", closing_balance="769.99")["statements"]
        result = entry["import_check"]
        c = self.checks(result)
        self.assertEqual(result["status"], "matches")
        self.assertEqual((c["closing"]["source"], c["closing"]["books"]), ("769.99", "769.99"))
        self.assertEqual((c["debits"]["source"], c["credits"]["source"]), ("169.99", "400.00"))
        wrong = self.imp("card_debit_credit.csv", "card", period_start="2026-01-01", period_end="2026-01-31",
                         opening_balance="1000.00", closing_balance="900.00")["statements"][0]["import_check"]
        c = self.checks(wrong)
        self.assertEqual((wrong["status"], c["closing"]["detail_code"], c["closing"]["difference"]),
                         ("differs", "books_differ", "130.01"))

    def test_file_without_balances_still_checks_count(self):
        self.map("amex_like_card.csv", "amex", account=CARD)
        [entry] = self.imp("amex_like_card.csv", "amex")["statements"]
        result = entry["import_check"]
        c = self.checks(result)
        self.assertEqual(c["count"]["status"], "matches")
        self.assertEqual([c["closing"]["detail_code"], c["opening"]["detail_code"]], ["no_balances"] * 2)
        self.assertEqual(result["status"], "partial")

    def test_unconfirmed_period_uses_file_rows_only(self):
        self.map("card_debit_credit.csv", "card", account=CARD)
        [entry] = self.imp("card_debit_credit.csv", "card", opening_balance="1000.00",
                           closing_balance="900.00")["statements"]
        c = self.checks(entry["import_check"])
        self.assertEqual((c["closing"]["status"], c["closing"]["detail_code"]), ("differs", "file_rows_differ"))
        ok = self.imp("card_debit_credit.csv", "card", opening_balance="1000.00",
                      closing_balance="769.99")["statements"][0]["import_check"]
        self.assertEqual(self.checks(ok)["closing"]["detail_code"], "period_unconfirmed")
        self.assertEqual(ok["status"], "partial")

    def test_overlapping_statement_is_not_aligned(self):
        self.import_chase()
        partial = self.csv("partial.csv", "01/16/2026,TEST A,-10.00")
        entry = self.import_csv(partial, "partial", period_start="2026-01-16", period_end="2026-01-20",
                                opening_balance="500.00", closing_balance="490.00")
        c = self.checks(entry["import_check"])
        self.assertEqual((c["closing"]["status"], c["closing"]["detail_code"]), ("not_checkable", "not_aligned"))
        self.assertEqual((entry["import_check"]["period"], entry["import_check"]["status"]),
                         ({"from": "2026-01-16", "to": "2026-01-20"}, "partial"))

    def test_opening_matches_and_differs_against_the_prior_statement(self):
        self.import_chase()
        february = self.csv("feb.csv", "02/05/2026,TEST A,-10.00")
        entry = self.import_csv(february, "feb", period_start="2026-02-01", period_end="2026-02-28",
                                opening_balance="3142.26", closing_balance="3132.26")
        result = entry["import_check"]
        c = self.checks(result)
        self.assertEqual((c["opening"]["status"], c["opening"]["detail_code"], c["opening"]["books"]),
                         ("matches", "prior_statement_closing", "3142.26"))
        self.assertEqual((c["closing"]["status"], result["status"]), ("matches", "matches"))
        wrong = self.import_csv(february, "feb", period_start="2026-02-01", period_end="2026-02-28",
                                opening_balance="3100.00", closing_balance="3090.00")["import_check"]
        c = self.checks(wrong)
        self.assertEqual((c["opening"]["status"], c["opening"]["difference"]), ("differs", "-42.26"))
        # A statement two months on has an earlier statement but none that ends the day before.
        april = self.csv("apr.csv", "04/05/2026,TEST A,-10.00")
        gap = self.import_csv(april, "apr", period_start="2026-04-01", period_end="2026-04-30",
                              opening_balance="3000.00", closing_balance="2990.00")["import_check"]
        self.assertEqual(self.checks(gap)["opening"]["detail_code"], "no_prior_statement")

    def test_foreign_currency_is_not_checkable_but_count_checks(self):
        entity = load_entity(self.entity)
        record = {"statement_id": "st-eur", "account": CHECKING, "period_start": "2026-01-01",
                  "period_end": "2026-01-31", "period_confirmed": True, "opening_balance": "0.00",
                  "closing_balance": "-4.50", "currency": "EUR"}
        rows = [{"id": "x1", "date": "2026-01-05", "amount": "-4.50", "currency": "EUR"}]
        result = import_check.check_statement(entity, record, rows, ts=TS)
        c = self.checks(result)
        self.assertEqual(c["count"]["status"], "differs")
        self.assertEqual({c[k]["detail_code"] for k in ("debits", "credits", "closing", "opening")},
                         {"foreign_currency"})
        self.assertTrue(all(c[k]["status"] == "not_checkable" for k in ("debits", "credits", "closing", "opening")))

    def test_dry_run_stores_nothing_and_check_is_read_only(self):
        self.map("chase_like_checking.csv", "chase")
        result = self.imp("chase_like_checking.csv", "chase", dry_run=True)
        self.assertNotIn("import_check", result["statements"][0])
        self.assertFalse((self.entity / "ingestion" / "statements" / "checks").exists())

    def test_remote_contract_classifies_check_as_read(self):
        self.assertFalse(command_spec(["import", "check", "st-1", "--entity", "e"])["mutates"])


if __name__ == "__main__":
    unittest.main()
