"""Tests for historical statement import (`books statement`).

All fixtures under tests/fixtures/statements/ are synthetic.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping import queue as queue_module  # noqa: E402
from bookkeeping import statement_import as si  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.connectors import bankcsv, ofx  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.ledger.importer import import_transactions  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "statements"
CHECKING = "Assets:Bank:Checking"
CARD = "Liabilities:CreditCard:Visa"


def fixture(name: str) -> Path:
    return FIXTURES / name


def _make_entity(root: Path, extra: dict | None = None) -> Path:
    entity_dir = root / "co"
    (entity_dir / "staging").mkdir(parents=True)
    config = {"name": "Synthetic Co", "business_type": "consulting", **(extra or {})}
    (entity_dir / "entity.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
    (entity_dir / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n",
        encoding="utf-8")
    return entity_dir


def _queue_ids(entity_dir: Path) -> list[str]:
    path = entity_dir / "staging" / "pending-categorization.json"
    if not path.exists():
        return []
    return [item["id"] for item in json.loads(path.read_text(encoding="utf-8"))]


def _candidates(entity_dir: Path) -> list[dict]:
    path = entity_dir / "staging" / "duplicate-candidates.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


class DetectionTests(unittest.TestCase):
    def inspect(self, name: str, **kwargs):
        return si.inspect_file(fixture(name), **kwargs)

    def test_chase_like_checking_is_fully_detected(self):
        result = self.inspect("chase_like_checking.csv")
        self.assertEqual(result["status"], "ready_to_confirm")
        layout = result["layout"]
        self.assertEqual(layout["column_names"]["date"], "Posting Date")
        self.assertEqual(layout["column_names"]["reference"], "Check or Slip #")
        self.assertEqual(layout["column_names"]["direction"], "Details")
        self.assertEqual(layout["sign"], "standard")
        self.assertEqual(layout["date_format"], "%m/%d/%Y")

    def test_inspect_reports_the_detected_row_count(self):
        # Data rows: file lines minus the header line where the file has one.
        for name, rows in (("chase_like_checking.csv", 6), ("wells_like_headerless.csv", 4),
                           ("amex_like_card.csv", 4)):
            self.assertEqual(self.inspect(name)["row_count"], rows, name)

    def test_row_count_excludes_the_header_line_in_every_detection_state(self):
        from bookkeeping.connectors import bankcsv
        body = "2026-01-01,A,-5.00\n2026-01-02,B,6.00\n"
        for label, text in (
                ("header detected", "Date,Description,Amount\n" + body),
                ("header present but not detected", "When,What,Howmuch\n" + body),
                ("no header", body)):
            with self.subTest(label):
                self.assertEqual(bankcsv.detect_layout(text)["row_count"], 2)

    def test_chase_like_card_prefers_posting_date(self):
        result = self.inspect("chase_like_card.csv")
        self.assertEqual(result["status"], "ready_to_confirm")
        self.assertEqual(result["layout"]["column_names"]["date"], "Post Date")
        self.assertIn("posting date", result["evidence"]["date_column"])
        self.assertEqual(result["preview"][0], {"date": "2026-01-21", "description": "FABRIKAM SUPPLY",
                                                "amount": "-180.00", "balance": None})

    def test_bofa_like_sign_needs_account_kind_then_uses_running_balance(self):
        without = self.inspect("bofa_like_checking.csv")
        self.assertEqual(without["status"], "needs_mapping")
        self.assertEqual(without["uncertain"], ["sign"])
        self.assertEqual(without["options_for_uncertain"], {"sign": ["--sign"]})
        with_kind = self.inspect("bofa_like_checking.csv", ledger_account=CHECKING)
        self.assertEqual(with_kind["status"], "ready_to_confirm")
        self.assertEqual(with_kind["evidence"]["sign_votes"], {"running_balance": "standard"})

    def test_wells_like_headerless_columns_are_inferred_with_warning(self):
        result = self.inspect("wells_like_headerless.csv")
        self.assertEqual(result["status"], "ready_to_confirm")
        self.assertEqual(result["layout"]["columns"], {"date": 0, "amount": 1, "description": 4})
        self.assertTrue(any("no header row" in w for w in result["warnings"]))

    def test_amex_like_card_sign_is_inverted_for_a_card(self):
        self.assertEqual(self.inspect("amex_like_card.csv")["uncertain"], ["sign"])
        result = self.inspect("amex_like_card.csv", ledger_account=CARD)
        self.assertEqual(result["layout"]["sign"], "inverted")
        self.assertEqual(result["preview"][0]["amount"], "-49.99")
        self.assertEqual(result["preview"][1]["amount"], "500.00")

    def test_card_with_separate_debit_and_credit_columns(self):
        result = self.inspect("card_debit_credit.csv")
        self.assertEqual(result["layout"]["sign"], "debit_credit")
        self.assertEqual([p["amount"] for p in result["preview"]], ["-49.99", "400.00", "-120.00"])

    def test_semicolon_and_comma_decimal(self):
        result = self.inspect("comma_decimal_semicolon.csv", ledger_account=CHECKING)
        self.assertEqual(result["layout"]["delimiter"], ";")
        self.assertEqual(result["layout"]["decimal"], ",")
        self.assertEqual(result["layout"]["date_format"], "%d.%m.%Y")
        self.assertEqual(result["preview"][1]["amount"], "-1234.56")

    def test_ambiguous_dates_and_sign_are_never_guessed(self):
        result = self.inspect("ambiguous.csv")
        self.assertEqual(result["status"], "needs_mapping")
        self.assertEqual(result["uncertain"], ["date_format", "sign"])
        self.assertIn("%m/%d/%Y", result["evidence"]["date_format"])
        self.assertEqual(result["preview"], [])

    def test_mixed_currency_file_is_rejected(self):
        # One file, two currencies (USD and EUR): Slashbooks does not convert, so it refuses.
        result = self.inspect("foreign_currency.csv")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "unsupported_currency")
        self.assertIn("more than one currency", result["error"]["message"])

    def test_single_foreign_currency_file_names_its_currency(self):
        # A file in one other currency is read in that currency; the import decides against the company.
        result = self.inspect("foreign_header.csv")
        self.assertNotEqual(result["status"], "error")
        self.assertEqual(result["layout"]["currency"], "EUR")
        self.assertEqual(result["currency"]["file"], "EUR")
        refused = self.inspect("foreign_header.csv", entity_config={"currency": "USD", "currency_source": "set"})
        self.assertEqual(refused["status"], "error")
        self.assertEqual(refused["error"]["code"], "unsupported_currency")
        self.assertIn("EUR", refused["error"]["message"])
        self.assertIn("USD", refused["error"]["message"])

    def test_malformed_files_have_clear_errors(self):
        self.assertEqual(self.inspect("not_a_statement.csv")["error"]["code"], "not_delimited")
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.csv"
            empty.write_text("\n\n", encoding="utf-8")
            self.assertEqual(si.inspect_file(empty)["error"]["code"], "empty_file")
            binary = Path(tmp) / "statement.csv"
            binary.write_bytes(b"%PDF-1.4\x00\x01\x02")
            self.assertEqual(si.inspect_file(binary)["error"]["code"], "binary_file")
            missing = si.inspect_file(Path(tmp) / "missing.csv")
            self.assertEqual(missing["error"]["code"], "file_not_found")

    def test_money_parsing_variants(self):
        self.assertEqual(str(bankcsv.parse_money("(1,234.50)")), "-1234.50")
        self.assertEqual(str(bankcsv.parse_money("$12.00-")), "-12.00")
        self.assertEqual(str(bankcsv.parse_money("1.234,50", ",")), "1234.50")
        self.assertIsNone(bankcsv.parse_money(""))
        with self.assertRaises(bankcsv.ForeignCurrencyError):
            bankcsv.parse_money("€12.00")
        with self.assertRaises(ValueError):
            bankcsv.parse_money("12.345")


class OfxTests(unittest.TestCase):
    def test_sgml_checking_statement(self):
        [stmt] = ofx.parse_ofx(fixture("checking.ofx").read_text(encoding="utf-8"))
        self.assertEqual(stmt.kind, "bank")
        self.assertEqual(stmt.account_last4, "0017")
        self.assertNotIn("SYNTH", stmt.account_identity)
        self.assertEqual((stmt.period_start, stmt.period_end), ("2026-01-01", "2026-01-31"))
        self.assertEqual(str(stmt.ledger_balance), "3142.26")
        self.assertEqual(len(stmt.transactions), 6)
        self.assertEqual(stmt.in_file_duplicates, 1)
        first = stmt.transactions[0]
        self.assertEqual((first.date, str(first.amount), first.memo), ("2026-01-02", "2500.00", "PPD"))
        self.assertEqual(stmt.transactions[1].memo, "")
        self.assertEqual(stmt.transactions[3].reference, "1042")

    def test_xml_qfx_card_statement(self):
        [stmt] = ofx.parse_ofx(fixture("card.qfx").read_text(encoding="utf-8"))
        self.assertEqual(stmt.kind, "creditcard")
        self.assertEqual(str(stmt.ledger_balance), "-1270.00")
        self.assertEqual(stmt.transactions[2].name, "FABRIKAM & SONS")

    def _bad(self, text: str) -> str:
        with self.assertRaises(bankcsv.StatementFileError) as ctx:
            ofx.parse_ofx(text)
        return ctx.exception.code

    def test_malformed_ofx(self):
        base = fixture("card.qfx").read_text(encoding="utf-8")
        self.assertEqual(self._bad(base.replace("</OFX>", "")), "malformed_ofx")
        self.assertEqual(self._bad(base.replace("<FITID>C2</FITID>", "")), "malformed_ofx")
        self.assertEqual(self._bad(base.replace("<FITID>C2</FITID>", "<FITID>C1</FITID>")), "conflicting_fitid")
        # A statement in another currency is parsed and names its currency; the import checks it against the company.
        [cad] = ofx.parse_ofx(base.replace("<CURDEF>USD", "<CURDEF>CAD"))
        self.assertEqual(ofx.statement_currencies(cad), {"CAD"})
        self.assertEqual(self._bad(base.replace("<TRNAMT>-49.99", "<TRNAMT>abc")), "malformed_ofx")
        self.assertEqual(self._bad("<OFX></OFX>"), "malformed_ofx")

    def test_empty_available_balance_and_investment_amounts_keep_importing(self):
        base = fixture("card.qfx").read_text(encoding="utf-8")
        # An empty AVAILBAL amount is skipped, as it was before the amount check.
        text = base.replace("</LEDGERBAL>", "</LEDGERBAL><AVAILBAL><BALAMT></BALAMT><DTASOF>20260131</DTASOF></AVAILBAL>", 1) \
            if "</LEDGERBAL>" in base else base
        self.assertIn("AVAILBAL", text)
        [stmt] = ofx.parse_ofx(text)
        self.assertEqual(stmt.kind, "creditcard")
        # A filled AVAILBAL amount still has to be a clean amount.
        bad = text.replace("<AVAILBAL><BALAMT></BALAMT>", "<AVAILBAL><BALAMT>12.345</BALAMT>")
        self.assertEqual(self._bad(bad), "malformed_ofx")

    def test_investment_file_with_a_subcent_value_is_unsupported_not_malformed(self):
        text = ("<OFX><INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS><CURDEF>USD</CURDEF>"
                "<INVPOSLIST><POSMF><UNITS>1.5</UNITS><UNITPRICE>10.123456</UNITPRICE></POSMF></INVPOSLIST>"
                "<INVBAL><AVAILCASH>1.0</AVAILCASH><BALLIST><BAL><NAME>x</NAME><BALAMT>1.2345</BALAMT></BAL></BALLIST></INVBAL>"
                "</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1></OFX>")
        self.assertEqual(self._bad(text), "unsupported_statement")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.entity = _make_entity(self.root, {"bank_account_mappings": {"feed-chk": CHECKING}})

    def tearDown(self):
        self._tmp.cleanup()

    def map(self, name: str, profile: str, account: str = CHECKING, **overrides):
        kwargs = {k: overrides.pop(k) for k in ("ofx_account", "boundary_date", "side") if k in overrides}
        return si.save_profile(self.entity, fixture(name), name=profile, ledger_account=account,
                               overrides=overrides, **kwargs)

    def imp(self, name: str, profile: str | None = None, **kwargs):
        return si.import_file(self.entity, fixture(name), profile=profile, ts="2026-02-01T00:00:00Z", **kwargs)

    def test_csv_import_records_metadata_and_is_idempotent(self):
        saved = self.map("chase_like_checking.csv", "chase-checking")
        self.assertEqual(saved["mapping_key"], "statement:chase-checking")
        config = json.loads((self.entity / "entity.json").read_text())
        self.assertTrue(config["csv_account_mappings"]["statement:chase-checking"]["confirmed"])

        first = self.imp("chase_like_checking.csv", "chase-checking")
        self.assertEqual(first["status"], "imported")
        [stmt] = first["statements"]
        self.assertEqual(stmt["counts"]["queued_for_review"], 6)
        self.assertTrue(any("identical rows" in w for w in stmt["warnings"]))
        record = stmt["statement"]
        self.assertEqual(record["opening_balance"], "1000.00")
        self.assertEqual(record["closing_balance"], "3142.26")
        self.assertEqual(record["balance_source"], "running_balance")
        self.assertEqual(record["period_source"], "transactions")
        self.assertEqual(record["transactions_total"], "2142.26")
        self.assertTrue((self.entity / record["evidence_ref"]).exists())
        # A CSV export without printed statement dates adds no statement coverage
        # until a person confirms the period.
        self.assertFalse(record["period_confirmed"])
        self.assertEqual(stmt["coverage"], "needs_period")
        self.assertIn("--period-start", stmt["next_step"])
        self.assertFalse((self.entity / "ingestion" / "statements" / f"{record['statement_id']}.json").exists())
        self.assertEqual(len(set(_queue_ids(self.entity))), 6)

        again = self.imp("chase_like_checking.csv", "chase-checking")
        counts = again["statements"][0]["counts"]
        self.assertEqual(counts["already_imported"], 6)
        self.assertEqual(counts["queued_for_review"], 0)
        self.assertEqual(len(si.load_statement_records(self.entity)), 1)

        overlap = self.imp("chase_like_checking_later.csv", "chase-checking")
        counts = overlap["statements"][0]["counts"]
        self.assertEqual((counts["already_imported"], counts["queued_for_review"]), (2, 1))
        self.assertEqual(len(si.load_statement_records(self.entity)), 2)

    def test_ofx_overlapping_csv_is_held_never_double_recorded(self):
        self.map("chase_like_checking.csv", "chase-checking")
        self.imp("chase_like_checking.csv", "chase-checking")
        self.map("checking.ofx", "checking-ofx")
        result = self.imp("checking.ofx")
        [stmt] = result["statements"]
        self.assertEqual(stmt["counts"]["held_for_review"], 6)
        self.assertEqual(stmt["counts"]["in_file_duplicates_skipped"], 1)
        self.assertEqual(stmt["counts"].get("queued_for_review"), 0)
        self.assertEqual({h["match_rule"] for h in stmt["held"]}, {"same_date_amount_description"})
        self.assertEqual(len(_queue_ids(self.entity)), 6)
        entity = load_entity(self.entity)
        listed = queue_module.list_queue_items(entity)
        self.assertEqual(sum(1 for i in listed if i.get("status") == "duplicate-candidate"), 6)
        self.assertEqual(stmt["statement"]["closing_balance"], "3142.26")
        self.assertEqual(stmt["statement"]["period_source"], "statement")

        again = self.imp("checking.ofx")
        self.assertEqual(again["statements"][0]["counts"]["already_held_for_review"], 6)

        held = stmt["held"][0]["source_id"]
        queue_module.resolve_duplicate_candidate(entity, held, "duplicate")
        third = self.imp("checking.ofx")
        counts = third["statements"][0]["counts"]
        self.assertEqual((counts["confirmed_duplicate"], counts["already_held_for_review"]), (1, 5))

    def test_ofx_import_dedupes_by_fitid(self):
        self.map("checking.ofx", "checking-ofx")
        first = self.imp("checking.ofx")["statements"][0]["counts"]
        self.assertEqual(first["queued_for_review"], 6)
        second = self.imp("checking.ofx")["statements"][0]["counts"]
        self.assertEqual(second["already_imported"], 6)
        ids = _queue_ids(self.entity)
        self.assertTrue(all(i.startswith("ofx:") for i in ids))

    def test_bank_feed_overlap_uses_near_date_rule(self):
        entity = load_entity(self.entity)
        feed = {"id": "feed-txn-1", "date": "2026-01-06", "description": "NORTHWIND CAFE #12",
                "amount": "-4.50", "accountId": "feed-chk", "pending": False}
        posted = import_transactions(entity, [feed], session_id="feed",
                                     categorizer=lambda txn: ("Expenses:Meals", "auto"),
                                     ts="2026-02-01T00:00:00Z")
        self.assertEqual(posted.new_entries, 1)
        self.map("chase_like_checking.csv", "chase-checking")
        stmt = self.imp("chase_like_checking.csv", "chase-checking")["statements"][0]
        self.assertEqual(stmt["counts"]["held_for_review"], 1)
        self.assertEqual(stmt["counts"]["queued_for_review"], 5)
        held = stmt["held"][0]
        self.assertEqual(held["match_rule"], "same_amount_within_3_days_other_source")
        self.assertEqual(held["existing_source_id"], "feed-txn-1")
        released = queue_module.resolve_duplicate_candidate(load_entity(self.entity), held["source_id"], "distinct")
        self.assertEqual(released["status"], "confirmed-distinct")
        self.assertIn(held["source_id"], _queue_ids(self.entity))

    def test_qfx_card_and_bofa_summary_metadata(self):
        self.map("card.qfx", "visa", account=CARD)
        card = self.imp("card.qfx")["statements"][0]["statement"]
        # Statement presentation: money owed on a card is positive.
        self.assertEqual(card["closing_balance"], "1270.00")
        self.assertEqual(card["opening_balance"], "1540.01")
        self.assertEqual(card["institution_account"]["account_last4"], "0042")
        self.map("bofa_like_checking.csv", "bofa-checking", account="Assets:Bank:Savings")
        bofa = self.imp("bofa_like_checking.csv", "bofa-checking")["statements"][0]
        record = bofa["statement"]
        self.assertEqual((record["period_start"], record["period_end"]), ("2026-01-01", "2026-01-31"))
        self.assertEqual((record["opening_balance"], record["closing_balance"]), ("5000.00", "5354.75"))
        self.assertEqual(record["period_source"], "file_summary")
        self.assertEqual(bofa["counts"]["queued_for_review"], 3)
        listed = [r["profile"] for r in si.load_statement_records(self.entity)]
        self.assertEqual(sorted(listed), ["bofa-checking", "visa"])

    def test_ofx_import_writes_a_coverage_contract_record(self):
        from bookkeeping import coverage

        self.map("checking.ofx", "checking-ofx")
        result = self.imp("checking.ofx")
        [stmt] = result["statements"]
        self.assertEqual(stmt["coverage"], "recorded")
        records, findings = coverage.load_statements(self.entity)
        self.assertEqual(findings, [])
        [record] = records
        self.assertEqual((record.account, record.kind), (CHECKING, "statement"))
        self.assertEqual((record.period_start.isoformat(), record.period_end.isoformat()),
                         ("2026-01-01", "2026-01-31"))
        # LEDGERBAL dated on the last statement day is the closing balance; the
        # opening balance is that balance less the file's transactions.
        self.assertEqual((str(record.opening_balance), str(record.closing_balance)), ("1000.00", "3142.26"))
        self.assertEqual(record.evidence_sha256, stmt["statement"]["evidence_sha256"])
        self.assertTrue((self.entity / record.evidence_ref).is_file())
        [document] = si.load_statement_records(self.entity)
        self.assertEqual(document["writer"], "books statement import")
        self.assertEqual(document["ledger_balance_date"], "2026-01-31")

    def test_ofx_ledger_balance_after_statement_end_is_not_a_closing_balance(self):
        text = fixture("checking.ofx").read_text(encoding="utf-8").replace(
            "<LEDGERBAL><BALAMT>3142.26<DTASOF>20260131", "<LEDGERBAL><BALAMT>3142.26<DTASOF>20260203")
        path = self.root / "later-balance.ofx"
        path.write_text(text, encoding="utf-8")
        si.save_profile(self.entity, path, name="checking-ofx", ledger_account=CHECKING)
        [stmt] = si.import_file(self.entity, path, ts="2026-02-04T00:00:00Z")["statements"]
        record = stmt["statement"]
        self.assertEqual((record["opening_balance"], record["closing_balance"]), (None, None))
        self.assertEqual((record["ledger_balance"], record["ledger_balance_date"]), ("3142.26", "2026-02-03"))
        self.assertTrue(any("not on the last statement day" in w for w in stmt["warnings"]))
        self.assertEqual(stmt["coverage"], "recorded")

    def test_csv_period_confirmed_later_promotes_the_record(self):
        from bookkeeping import coverage

        self.map("chase_like_checking.csv", "chase-checking")
        first = self.imp("chase_like_checking.csv", "chase-checking")["statements"][0]
        self.assertEqual(coverage.load_statements(self.entity), ([], []))
        self.assertEqual([r["period_confirmed"] for r in si.load_statement_records(self.entity)], [False])

        with self.assertRaises(si.StatementImportError) as ctx:
            self.imp("chase_like_checking.csv", "chase-checking",
                     period_start="2026-01-10", period_end="2026-01-31")
        self.assertEqual(ctx.exception.code, "period_mismatch")
        with self.assertRaises(si.StatementImportError) as ctx:
            self.imp("chase_like_checking.csv", "chase-checking", period_start="2026-01-01")
        self.assertEqual(ctx.exception.code, "invalid_period")

        again = self.imp("chase_like_checking.csv", "chase-checking",
                         period_start="2026-01-01", period_end="2026-01-31")["statements"][0]
        self.assertEqual(again["counts"]["already_imported"], 6)
        self.assertEqual(again["coverage"], "recorded")
        [record], findings = coverage.load_statements(self.entity)
        self.assertEqual(findings, [])
        self.assertEqual(record.statement_id, first["statement"]["statement_id"])
        self.assertEqual((record.period_start.isoformat(), record.period_end.isoformat()),
                         ("2026-01-01", "2026-01-31"))
        # Running-balance balances stay valid inside a confirmed period.
        self.assertEqual((str(record.opening_balance), str(record.closing_balance)), ("1000.00", "3142.26"))
        [listed] = si.load_statement_records(self.entity)
        self.assertEqual((listed["period_confirmed"], listed["period_source"]), (True, "person"))

        # A later import of the same file without dates keeps the confirmed period.
        repeat = self.imp("chase_like_checking.csv", "chase-checking")["statements"][0]
        self.assertEqual(repeat["coverage"], "recorded")
        [listed] = si.load_statement_records(self.entity)
        self.assertEqual((listed["period_start"], listed["period_source"]), ("2026-01-01", "person"))

    def test_person_supplied_card_balances_use_statement_presentation(self):
        from bookkeeping import coverage

        self.map("card_debit_credit.csv", "card", account=CARD)
        stmt = self.imp("card_debit_credit.csv", "card", period_start="2026-01-01",
                        period_end="2026-01-31", opening_balance="1000.00",
                        closing_balance="769.99")["statements"][0]
        self.assertEqual(stmt["statement"]["balance_source"], "person")
        self.assertEqual(stmt["warnings"], [])
        [record], _ = coverage.load_statements(self.entity)
        self.assertEqual((str(record.opening_balance), str(record.closing_balance)), ("1000.00", "769.99"))
        wrong = self.imp("card_debit_credit.csv", "card", period_start="2026-01-01",
                         period_end="2026-01-31", opening_balance="1000.00",
                         closing_balance="900.00")["statements"][0]
        self.assertTrue(any("check for missing or extra rows" in w for w in wrong["warnings"]))

    def test_pending_rows_and_boundary_are_counted(self):
        self.map("pending_rows.csv", "pending", boundary_date="2026-01-07", side="before")
        counts = self.imp("pending_rows.csv", "pending")["statements"][0]["counts"]
        self.assertEqual(counts["pending_rows_skipped"], 1)
        self.assertEqual(counts["outside_boundary"], 1)
        self.assertEqual(counts["queued_for_review"], 2)

    def test_dry_run_changes_nothing(self):
        self.map("chase_like_checking.csv", "chase-checking")
        before = (self.entity / "entity.json").read_text()
        result = self.imp("chase_like_checking.csv", "chase-checking", dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["statements"][0]["counts"]["to_import"], 6)
        self.assertEqual(_queue_ids(self.entity), [])
        self.assertEqual(si.load_statement_records(self.entity), [])
        self.assertEqual((self.entity / "entity.json").read_text(), before)

    def test_mapping_rules(self):
        with self.assertRaises(si.StatementImportError) as ctx:
            self.map("ambiguous.csv", "ambiguous")
        self.assertEqual(ctx.exception.code, "needs_mapping")
        self.assertEqual(ctx.exception.detail["uncertain"], ["date_format", "sign"])
        saved = self.map("ambiguous.csv", "ambiguous", date_format="%d/%m/%Y", sign="standard")
        self.assertEqual(saved["statement_profile"]["date_format"], "%d/%m/%Y")
        with self.assertRaises(si.StatementImportError) as ctx:
            self.map("ambiguous.csv", "ambiguous", account="Assets:Bank:Other",
                     date_format="%d/%m/%Y", sign="standard")
        self.assertEqual(ctx.exception.code, "profile_account_changed")
        with self.assertRaises(si.StatementImportError) as ctx:
            self.map("chase_like_checking.csv", "chk", account="Expenses:Meals")
        self.assertEqual(ctx.exception.code, "invalid_ledger_account")
        with self.assertRaises(si.StatementImportError) as ctx:
            self.map("malformed_amount.csv", "bad", sign="standard")
        self.assertEqual(ctx.exception.code, "unparseable_rows")
        self.assertEqual(ctx.exception.detail["rows"], [3])
        with self.assertRaises(si.StatementImportError) as ctx:
            self.map("chase_like_checking.csv", "Bad Name")
        self.assertEqual(ctx.exception.code, "invalid_profile_name")

    def test_explicit_columns_override_detection(self):
        saved = self.map("card_debit_credit.csv", "card", account=CARD,
                         date_column="Transaction Date", reference_column="Card No.")
        self.assertEqual(saved["statement_profile"]["columns"]["date"], 0)
        txns = si._csv_statement(fixture("card_debit_credit.csv"),
                                 fixture("card_debit_credit.csv").read_text(), "0" * 64, "card",
                                 si._profiles(load_entity(self.entity).entity_config)["card"])["transactions"]
        self.assertEqual(txns[0]["date"], "2026-01-03")

    def test_changed_layout_and_missing_profile(self):
        self.map("chase_like_checking.csv", "chase-checking")
        result = self.imp("chase_like_card.csv", "chase-checking")
        self.assertEqual(result["status"], "needs_mapping")
        self.assertEqual(result["error"]["code"], "layout_changed")
        checked = si.inspect_file(fixture("chase_like_card.csv"),
                                  entity_config=load_entity(self.entity).entity_config,
                                  profile="chase-checking")
        self.assertEqual(checked["status"], "needs_mapping")
        no_profile = self.imp("chase_like_checking.csv")
        self.assertEqual(no_profile["status"], "needs_mapping")
        unmapped = self.imp("card.qfx")
        self.assertEqual(unmapped["status"], "needs_mapping")
        self.assertEqual(unmapped["unmapped_accounts"][0]["account_last4"], "0042")

    def test_cli_round_trip_and_exit_codes(self):
        def run(*argv):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(list(argv))
            return code, json.loads(out.getvalue())

        path = str(fixture("amex_like_card.csv"))
        code, result = run("statement", "inspect", path)
        self.assertEqual((code, result["status"]), (0, "needs_mapping"))
        code, result = run("statement", "map", path, "--entity", str(self.entity), "--profile", "amex",
                           "--ledger-account", "Liabilities:CreditCard:Amex")
        self.assertEqual((code, result["status"]), (0, "saved"))
        self.assertEqual(result["statement_profile"]["sign"], "inverted")
        code, result = run("statement", "import", path, "--entity", str(self.entity))
        self.assertEqual((code, result["status"]), (3, "needs_mapping"))
        code, result = run("statement", "import", path, "--entity", str(self.entity), "--profile", "amex")
        self.assertEqual((code, result["statements"][0]["counts"]["queued_for_review"]), (0, 4))
        code, result = run("statement", "list", "--entity", str(self.entity))
        self.assertEqual(result["statements"][0]["account"], "Liabilities:CreditCard:Amex")
        code, result = run("statement", "inspect", str(fixture("foreign_currency.csv")))
        self.assertEqual(code, 1)

    def test_remote_contract_classification(self):
        self.assertFalse(command_spec(["statement", "inspect", "a.csv"])["mutates"])
        self.assertFalse(command_spec(["statement", "list", "--entity", "e"])["mutates"])
        self.assertTrue(command_spec(["statement", "import", "a.csv", "--entity", "e"])["mutates"])
        self.assertFalse(command_spec(["statement", "import", "a.csv", "--entity", "e", "--dry-run"])["mutates"])
        self.assertTrue(command_spec(["statement", "map", "a.csv", "--entity", "e", "--profile", "p",
                                      "--ledger-account", CHECKING])["mutates"])
        spec = command_spec(["statement", "import", "a.csv", "--entity", "e", "--output", "o.json"])
        self.assertEqual((spec["input_fields"], spec["output_fields"]), (["file"], ["output"]))
        self.assertFalse(spec["network"])


if __name__ == "__main__":
    unittest.main()
