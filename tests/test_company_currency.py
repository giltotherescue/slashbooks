"""Company currency: one base currency per company, never converted.

Every company, amount and file here is synthetic.
"""
from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping import currency  # noqa: E402
from bookkeeping import statement_import as si  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.entity import configure_basis, init_entity, load_entity  # noqa: E402
from bookkeeping.ledger.importer import import_transactions  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

STATEMENTS = ROOT / "tests" / "fixtures" / "statements"
QB_SNAPSHOT = ROOT / "tests" / "fixtures" / "quickbooks_api" / "snapshot.json"
CHECKING = "Assets:Bank:Checking"


def _ofx(currency_code: str | None, *, txn_currency: str | None = None) -> str:
    text = (STATEMENTS / "checking.ofx").read_text(encoding="utf-8")
    if currency_code is None:
        text = text.replace("<CURDEF>USD</CURDEF>", "").replace("<CURDEF>USD", "")
    else:
        text = text.replace("<CURDEF>USD", f"<CURDEF>{currency_code}")
    if txn_currency:
        text = text.replace("<FITID>", f"<CURRENCY><CURRATE>1.0<CURSYM>{txn_currency}</CURRENCY><FITID>", 1)
    return text


def _rows(code: str | None, count: int = 2, prefix: str = "r") -> list[dict]:
    rows = []
    for index in range(count):
        row = {"id": f"{prefix}{index}", "date": f"2026-01-0{index + 2}", "description": f"Synthetic row {index}",
               "amount": "-12.50", "accountId": "feed-chk", "pending": False}
        if code:
            row["currency"] = code
        rows.append(row)
    return rows


def _post_everything(_txn: dict) -> tuple[str, str]:
    return "Expenses:Software", "high"


class CompanyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.path = self.root / "company"
        init_entity(self.path, name="Synthetic Currency Co")
        config = json.loads((self.path / "entity.json").read_text())
        config["bank_account_mappings"] = {"feed-chk": CHECKING}
        (self.path / "entity.json").write_text(json.dumps(config, indent=2) + "\n")

    def entity(self):
        return load_entity(self.path)

    def config(self) -> dict:
        return json.loads((self.path / "entity.json").read_text())

    def snapshot(self) -> tuple:
        return (self.config(), (self.path / "ledger.sqlite").read_bytes())

    def audit_types(self) -> list[str]:
        return [event["type"] for event in LedgerStore(self.path / "ledger.sqlite").load_audit_events()]

    def account_currencies(self) -> set[str]:
        with LedgerStore(self.path / "ledger.sqlite").connection() as conn:
            return {row[0] for row in conn.execute("SELECT DISTINCT currency FROM accounts")}

    def posting_currencies(self) -> set[str]:
        with LedgerStore(self.path / "ledger.sqlite").connection() as conn:
            return {row[0] for row in conn.execute("SELECT DISTINCT currency FROM postings")}

    def set_currency(self, code: str) -> dict:
        return currency.apply_set_currency(self.entity(), code)

    def write(self, name: str, text: str) -> Path:
        target = self.root / name
        target.write_text(text, encoding="utf-8")
        return target

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()


class MoneyFormatTests(unittest.TestCase):
    def test_usd_format_is_the_format_the_engine_always_used(self):
        for amount in (Decimal("1234.5"), Decimal("-12"), Decimal("0"), Decimal("1000000.00")):
            self.assertEqual(currency.format_money(amount, "USD"), f"${amount:,.2f}")

    def test_symbols_and_code_fallback(self):
        self.assertEqual(currency.format_money(Decimal("1234.5"), "EUR"), "€1,234.50")
        self.assertEqual(currency.format_money(Decimal("1234.5"), "GBP"), "£1,234.50")
        self.assertEqual(currency.format_money(Decimal("1234.5"), "CAD"), "CA$1,234.50")
        self.assertEqual(currency.format_money(Decimal("1234.5"), "CHF"), "CHF 1,234.50")
        self.assertEqual(currency.format_money("7", "eur"), "€7.00")

    def test_minor_units_follow_the_currency(self):
        self.assertEqual(currency.minor_units("USD"), 2)
        self.assertEqual(currency.minor_units("JPY"), 0)
        self.assertEqual(currency.minor_units("KWD"), 3)
        self.assertEqual(currency.format_money(Decimal("1234"), "JPY"), "¥1,234")
        self.assertEqual(currency.format_money(Decimal("1.5"), "KWD"), "KWD 1.500")

    def test_zero_and_three_decimal_currencies_are_refused_for_books(self):
        for code, digits in (("JPY", 0), ("KWD", 3)):
            with self.assertRaises(currency.CurrencyError) as ctx:
                currency.require_supported(code)
            self.assertEqual(ctx.exception.code, "unsupported_currency_decimals")
            self.assertIn(f"{digits} decimal places", str(ctx.exception))
        self.assertEqual(currency.require_supported("eur"), "EUR")

    def test_codes_are_validated(self):
        for bad in ("US", "EURO", "XYZ", "", None, "12a"):
            with self.assertRaises(currency.CurrencyError):
                currency.normalize_code(bad)

    def test_company_currency_defaults_to_usd(self):
        self.assertEqual(currency.company_currency({}), "USD")
        self.assertEqual(currency.company_currency({"currency": "EUR"}), "EUR")
        with self.assertRaises(ValueError):
            currency.company_currency({"currency": "JPY"})


class SetCurrencyTests(CompanyTestCase):
    def test_default_company_has_no_currency_keys(self):
        config = self.config()
        self.assertNotIn("currency", config)
        self.assertNotIn("currency_source", config)
        self.assertEqual(self.entity().currency, "USD")

    def test_preview_writes_nothing_and_apply_is_audited_and_idempotent(self):
        before = (self.path / "ledger.sqlite").read_bytes(), self.config()
        plan = currency.preview_set_currency(self.entity(), "eur")
        self.assertEqual(plan["request"], {"currency": "EUR"})
        self.assertTrue(plan["changes"])
        self.assertEqual(plan["effects"]["from"], {"currency": "USD", "currency_source": "default"})
        self.assertEqual(((self.path / "ledger.sqlite").read_bytes(), self.config()), before)
        result = currency.apply_set_currency(self.entity(), "EUR", plan["plan_hash"])
        self.assertEqual(result["status"], "applied")
        self.assertEqual((self.config()["currency"], self.config()["currency_source"]), ("EUR", "set"))
        self.assertIn("currency-set", self.audit_types())
        self.assertEqual(self.account_currencies(), {"EUR"})
        self.assertEqual(LedgerStore(self.path / "ledger.sqlite").get_meta("currency"), "EUR")
        again = currency.apply_set_currency(self.entity(), "EUR")
        self.assertEqual(again["status"], "already_applied")
        self.assertEqual(self.audit_types().count("currency-set"), 1)
        from bookkeeping.ledger.importer import check_integrity
        self.assertEqual(check_integrity(self.entity()).status, "ok")

    def test_stale_plan_hash_is_refused(self):
        plan = currency.preview_set_currency(self.entity(), "GBP")
        currency.apply_set_currency(self.entity(), "EUR")
        from bookkeeping.accrual.journals import JournalError
        with self.assertRaises(JournalError):
            currency.apply_set_currency(self.entity(), "GBP", plan["plan_hash"])

    def test_cli_previews_then_applies(self):
        code, out = self.cli("entity", "set-currency", str(self.path), "--currency", "CAD")
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertNotIn("currency", self.config())
        code, out = self.cli("entity", "set-currency", str(self.path), "--currency", "CAD", "--apply",
                             "--plan-hash", plan["plan_hash"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.config()["currency"], "CAD")

    def test_zero_and_three_decimal_currencies_are_refused(self):
        for code in ("JPY", "KWD"):
            status, out = self.cli("entity", "set-currency", str(self.path), "--currency", code)
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(out)["errors"][0]["code"], "unsupported_currency_decimals")
        self.assertNotIn("currency", self.config())

    def test_refused_when_the_company_has_entries(self):
        import_transactions(self.entity(), _rows(None), "s1", categorizer=_post_everything)
        with self.assertRaises(currency.CurrencyError) as ctx:
            currency.preview_set_currency(self.entity(), "EUR")
        self.assertEqual(ctx.exception.code, "company_has_entries")
        self.assertIn("USD", str(ctx.exception))
        # USD by default is already USD: a no-op that writes nothing.
        before = self.snapshot()
        self.assertFalse(currency.preview_set_currency(self.entity(), "USD")["changes"])
        self.assertEqual(currency.apply_set_currency(self.entity(), "USD")["status"], "already_applied")
        self.assertEqual(self.snapshot(), before)

    def test_set_usd_on_a_default_company_writes_nothing(self):
        before = self.snapshot()
        plan = currency.preview_set_currency(self.entity(), "USD")
        self.assertFalse(plan["changes"])
        self.assertEqual(currency.apply_set_currency(self.entity(), "USD", plan["plan_hash"])["status"], "already_applied")
        self.assertEqual(self.snapshot(), before)

    def test_a_set_currency_cannot_change_once_there_are_entries(self):
        self.set_currency("EUR")
        import_transactions(self.entity(), _rows(None), "s1", categorizer=_post_everything)
        for code in ("USD", "GBP"):
            with self.assertRaises(currency.CurrencyError) as ctx:
                currency.preview_set_currency(self.entity(), code)
            self.assertEqual(ctx.exception.code, "company_has_entries")
        self.assertEqual(currency.apply_set_currency(self.entity(), "EUR")["status"], "already_applied")

    def test_entity_json_is_repaired_from_the_ledger_after_a_stop(self):
        # The ledger commits first. A stop before the entity.json write leaves entity.json behind; loading repairs it.
        from unittest.mock import patch
        with patch.object(currency, "write_config", side_effect=OSError("stopped")):
            with self.assertRaises(OSError):
                currency.apply_set_currency(self.entity(), "EUR")
        self.assertNotIn("currency", self.config())
        self.assertEqual(self.entity().currency, "EUR")
        self.assertEqual((self.config()["currency"], self.config()["currency_source"]), ("EUR", "set"))

    def test_refused_when_the_review_queue_has_items(self):
        import_transactions(self.entity(), _rows(None), "s1")  # Queued for review, not posted.
        with self.assertRaises(currency.CurrencyError):
            currency.preview_set_currency(self.entity(), "EUR")

    def test_person_only(self):
        from bookkeeping import preparation_runtime
        from bookkeeping.accrual.journals import JournalError
        original = preparation_runtime.is_preparation
        preparation_runtime.is_preparation = lambda: True
        try:
            with self.assertRaises(JournalError) as ctx:
                currency.apply_set_currency(self.entity(), "EUR")
            self.assertEqual(ctx.exception.code, "currency_person_only")
        finally:
            preparation_runtime.is_preparation = original
        with self.assertRaises(preparation_runtime.PreparationDenied):
            preparation_runtime.supported_preparation_command(
                ["entity", "set-currency", str(self.path), "--currency", "EUR", "--apply"])

    def test_remote_contract(self):
        preview = command_spec(["entity", "set-currency", "dir", "--currency", "EUR"])
        self.assertFalse(preview["mutates"])
        self.assertTrue(command_spec(["entity", "set-currency", "dir", "--currency", "EUR", "--apply"])["mutates"])
        infer = command_spec(["entity", "infer-currency", "--input", "a.ofx", "--input", "b.csv"])
        self.assertFalse(infer["mutates"])
        self.assertEqual(infer["input_fields"], ["input"])
        self.assertEqual(infer["entity_fields"], [])


class InferenceTests(CompanyTestCase):
    def map_and_import(self, file: Path, profile: str, **overrides) -> dict:
        si.save_profile(self.path, file, name=profile, ledger_account=CHECKING, overrides=overrides)
        return si.import_file(self.path, file, profile=None if file.suffix == ".ofx" else profile,
                              ts="2026-02-01T00:00:00Z")

    def assert_inferred(self, code: str) -> None:
        config = self.config()
        self.assertEqual((config["currency"], config["currency_source"]), (code, "inferred"))
        self.assertIn("currency-inferred", self.audit_types())
        self.assertEqual(self.account_currencies(), {code})

    def test_ofx_statement_currency(self):
        result = self.map_and_import(self.write("eur.ofx", _ofx("EUR")), "eur-checking")
        self.assertEqual(result["status"], "imported", result)
        self.assert_inferred("EUR")
        queued = json.loads((self.path / "staging" / "pending-categorization.json").read_text())
        self.assertEqual({row["currency"] for row in queued}, {"EUR"})
        record = result["statements"][0]["statement"]
        self.assertEqual(record["currency"], "EUR")

    def test_ofx_without_a_currency_keeps_the_default(self):
        result = self.map_and_import(self.write("plain.ofx", _ofx(None)), "plain-checking")
        self.assertEqual(result["status"], "imported", result)
        self.assertNotIn("currency", self.config())
        self.assertNotIn("currency-inferred", self.audit_types())

    def test_bank_csv_currency_symbol(self):
        text = "Date,Description,Amount,Balance\n01/15/2026,NORTHWIND COFFEE,-€4.50,€995.50\n01/16/2026,CONTOSO,€100.00,€1095.50\n"
        result = self.map_and_import(self.write("eur.csv", text), "eur-csv", sign="standard")
        self.assertEqual(result["status"], "imported", result)
        self.assert_inferred("EUR")
        self.assertEqual(result["statements"][0]["counts"]["queued_for_review"], 2)

    def test_bank_csv_currency_column(self):
        text = "Date,Description,Amount,Currency\n01/15/2026,NORTHWIND,-4.50,GBP\n01/16/2026,CONTOSO,100.00,GBP\n"
        result = self.map_and_import(self.write("gbp.csv", text), "gbp-csv", sign="standard")
        self.assertEqual(result["status"], "imported", result)
        self.assert_inferred("GBP")

    def test_banksync_rows(self):
        result = import_transactions(self.entity(), _rows("EUR"), "feed", categorizer=_post_everything)
        self.assertEqual(result.new_entries, 2)
        self.assert_inferred("EUR")
        self.assertEqual(self.posting_currencies(), {"EUR"})

    def test_stripe_balance_currency(self):
        from bookkeeping.stripe import stripe_stage
        evidence = {"source": "stripe", "account": {"id": "acct_test", "livemode": False}, "coverage": {"complete": True},
                    "balanceTransactions": [{"id": "txn_sale", "created": 1768435200, "amount": 10000, "fee": 300,
                                             "net": 9700, "currency": "eur", "status": "available", "type": "charge",
                                             "source": "ch_sale"}],
                    "charges": [{"id": "ch_sale", "livemode": False, "amount": 10000, "currency": "eur",
                                 "status": "succeeded", "paid": True, "captured": True,
                                 "balance_transaction": "txn_sale", "metadata": {"stream": "service"}}]}
        for account in ("Assets:Processors:Stripe", "Assets:Transfers:Stripe-Payouts", "Income:Refunds"):
            self.cli("entity", "account-add", str(self.path), "--account", account)
        config = self.config()
        config["bank_account_mappings"]["acct_test"] = "Assets:Processors:Stripe"
        (self.path / "entity.json").write_text(json.dumps(config))
        policy = dict(version=1, enabled=True, stripe_account_id="acct_test", livemode=False, currency="EUR",
                      clearing_account="Assets:Processors:Stripe", payout_transit_account="Assets:Transfers:Stripe-Payouts",
                      fee_account="Expenses:Fees", refund_account="Income:Refunds",
                      revenue_rules=[dict(id="services", match={"metadata": {"object": "charge", "key": "stream", "value": "service"}},
                                          category="Income:Consulting")])
        # A USD policy for EUR evidence is refused before anything is adopted.
        before = self.snapshot()
        with self.assertRaises(ValueError):
            stripe_stage(self.entity(), evidence, {**policy, "currency": "USD"})
        self.assertEqual(self.snapshot(), before)
        # Two balance currencies refuse adoption.
        mixed = copy.deepcopy(evidence)
        mixed["balanceTransactions"].append(dict(evidence["balanceTransactions"][0], id="txn_usd", currency="usd"))
        with self.assertRaises(currency.CurrencyError) as ctx:
            stripe_stage(self.entity(), mixed, policy)
        self.assertEqual(ctx.exception.detail["reason"], "mixed_currency")
        self.assertEqual(self.snapshot(), before)
        staged = stripe_stage(self.entity(), evidence, policy)
        self.assertEqual(staged["staged"], 1)
        self.assert_inferred("EUR")
        # The company now has entries: a row in another currency stays unresolved; the rest still stages.
        other = copy.deepcopy(evidence)
        other["balanceTransactions"] = [dict(evidence["balanceTransactions"][0], id="txn_usd", currency="usd"),
                                        dict(evidence["balanceTransactions"][0], id="txn_eur2")]
        result = stripe_stage(self.entity(), other, policy)
        self.assertEqual(result["staged"], 1)
        self.assertIn(("stripe:acct_test:txn_usd", "unsupported_currency"),
                      {(row["source_id"], row["reason"]) for row in result["plan"]["unresolved"]})

    def test_quickbooks_home_currency(self):
        from bookkeeping import quickbooks_sync as sync
        snapshot = json.loads(QB_SNAPSHOT.read_text())
        snapshot["preferences"]["CurrencyPrefs"]["HomeCurrency"] = {"value": "CAD"}
        snapshot["company_info"]["CurrencyRef"] = {"value": "CAD"}
        text = json.dumps(snapshot).replace('"Currency": "USD"', '"Currency": "CAD"')
        source = self.write("qb.json", text)
        before = self.config(), self.audit_types(), LedgerStore(self.path / "ledger.sqlite").get_meta("currency")
        result = sync.sync_import(self.entity(), source)
        self.assertEqual(result["adopts_currency"], "CAD")
        self.assertTrue(result["opening_supported"], result["warnings"])
        # Neither the import of the evidence nor the opening preview changes the company currency.
        preview = sync.sync_opening(self.entity(), source, date(2026, 1, 1))
        self.assertFalse(preview["applied"])
        self.assertEqual({p["currency"] for p in preview["opening"]["proposed_postings"]}, {"CAD"})
        self.assertEqual((self.config(), self.audit_types(),
                          LedgerStore(self.path / "ledger.sqlite").get_meta("currency")), before)
        applied = sync.sync_opening(self.entity(), source, date(2026, 1, 1), apply=True)
        self.assertTrue(applied["applied"], applied)
        self.assert_inferred("CAD")
        self.assertEqual(self.posting_currencies(), {"CAD"})

    def test_quickbooks_home_currency_mismatch_is_evidence_only(self):
        from bookkeeping import quickbooks_sync as sync
        self.set_currency("EUR")
        snapshot = json.loads(QB_SNAPSHOT.read_text())
        snapshot["preferences"]["CurrencyPrefs"]["HomeCurrency"] = {"value": "CAD"}
        snapshot["company_info"]["CurrencyRef"] = {"value": "CAD"}
        result = sync.sync_import(self.entity(), self.write("qb.json", json.dumps(snapshot)))
        self.assertFalse(result["opening_supported"])
        self.assertTrue(any("CAD" in w and "EUR" in w for w in result["warnings"]))
        self.assertEqual(self.config()["currency"], "EUR")

    def test_mixed_currency_import_is_refused(self):
        rows = _rows("USD", 1, "a") + _rows("EUR", 1, "b")
        with self.assertRaises(currency.CurrencyError) as ctx:
            import_transactions(self.entity(), rows, "feed", categorizer=_post_everything)
        self.assertEqual(ctx.exception.code, "unsupported_currency")
        self.assertIn("EUR", str(ctx.exception))
        self.assertIn("USD", str(ctx.exception))
        self.assertNotIn("currency", self.config())
        self.assertEqual(LedgerStore(self.path / "ledger.sqlite").counts().entries, 0)
        bad = self.write("mixed.ofx", _ofx("EUR", txn_currency="USD"))
        with self.assertRaises(si.StatementImportError) as mapped:
            si.save_profile(self.path, bad, name="mixed", ledger_account=CHECKING)
        self.assertEqual(mapped.exception.code, "unsupported_currency")
        self.assertIn("more than one currency", str(mapped.exception))

    def test_mismatch_with_existing_entries_names_both_currencies(self):
        import_transactions(self.entity(), _rows(None), "s1", categorizer=_post_everything)
        with self.assertRaises(currency.CurrencyError) as ctx:
            import_transactions(self.entity(), _rows("EUR", 1, "x"), "s2", categorizer=_post_everything)
        message = str(ctx.exception)
        self.assertIn("EUR", message)
        self.assertIn("USD", message)
        self.assertIn("set-currency", ctx.exception.action)
        self.assertNotIn("currency", self.config())
        result = self.map_and_import(self.write("eur.ofx", _ofx("EUR")), "eur-checking")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["file_currency"], "EUR")
        self.assertEqual(result["error"]["company_currency"], "USD")

    def test_mismatch_with_a_person_set_currency(self):
        self.set_currency("EUR")
        with self.assertRaises(currency.CurrencyError) as ctx:
            import_transactions(self.entity(), _rows("GBP"), "feed")
        self.assertIn("a person set the company currency", str(ctx.exception))

    def test_zero_decimal_file_currency_is_refused(self):
        with self.assertRaises(currency.CurrencyError) as ctx:
            import_transactions(self.entity(), _rows("JPY"), "feed")
        self.assertEqual(ctx.exception.code, "unsupported_currency_decimals")
        self.assertNotIn("currency", self.config())

    def test_dry_run_does_not_adopt(self):
        file = self.write("eur.ofx", _ofx("EUR"))
        si.save_profile(self.path, file, name="eur-checking", ledger_account=CHECKING)
        result = si.import_file(self.path, file, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertNotIn("currency", self.config())


class InferCurrencyCommandTests(CompanyTestCase):
    def test_infer_reads_files_and_writes_nothing(self):
        files = [self.write("a.ofx", _ofx("EUR")),
                 self.write("b.csv", "Date,Description,Amount (EUR)\n01/05/2026,NORTHWIND,-4.50\n")]
        before = self.config()
        code, out = self.cli("entity", "infer-currency", "--input", str(files[0]), "--input", str(files[1]))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual((result["currency"], result["source"]), ("EUR", "inferred"))
        self.assertEqual([item["kind"] for item in result["evidence"]], ["OFX statement", "bank CSV"])
        self.assertEqual(self.config(), before)

    def test_infer_from_json_exports(self):
        stripe = self.write("stripe.json", json.dumps({"account": {"defaultCurrency": "gbp"},
                                                       "balanceTransactions": [{"currency": "gbp"}]}))
        qb = self.write("qb.json", json.dumps({"preferences": {"CurrencyPrefs": {"HomeCurrency": {"value": "GBP"}}}}))
        self.assertEqual(currency.infer_from_files([stripe, qb])["currency"], "GBP")

    def test_infer_mixed_none_and_unsupported(self):
        mixed = currency.infer_from_files([self.write("a.ofx", _ofx("EUR")), self.write("b.ofx", _ofx("CAD"))])
        self.assertEqual((mixed["currency"], mixed["source"], mixed["currencies"]), (None, "mixed", ["CAD", "EUR"]))
        plain = currency.infer_from_files([STATEMENTS / "chase_like_checking.csv"])
        self.assertEqual((plain["currency"], plain["source"]), (None, "none"))
        yen = currency.infer_from_files([self.write("y.ofx", _ofx("JPY"))])
        self.assertEqual((yen["currency"], yen["source"]), (None, "unsupported"))


class EuroCompanyTests(CompanyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.set_currency("EUR")

    def test_import_report_and_ask(self):
        result = import_transactions(self.entity(), _rows(None, 3), "feed", categorizer=_post_everything)
        self.assertEqual(result.new_entries, 3)
        self.assertEqual(self.posting_currencies(), {"EUR"})
        from bookkeeping.reports.statements import StatementResult, ask, profit_and_loss
        pnl = profit_and_loss(self.path, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(pnl.meta["currency"], "EUR")
        self.assertNotIn("$", pnl.to_text())
        answer = ask(self.path, "How much did we spend in January 2026?", today=date(2026, 2, 1))
        self.assertNotIn("$", answer.answer_text)
        noted = StatementResult("pnl", date(2026, 1, 1), date(2026, 1, 31),
                                meta={"currency": "EUR", "noncash_entries_total": "12.00"}).to_text()
        self.assertIn("Includes €12.00 of entries that may not be cash", noted)
        from bookkeeping.ledger.projections import render_store_ledger
        self.assertIn('option "operating_currency" "EUR"', render_store_ledger(self.path / "ledger.sqlite"))
        from bookkeeping.reports.workbook import run_sanity_checks
        check = next(c for c in run_sanity_checks(self.path, date(2026, 1, 1), date(2026, 1, 31)).checks
                     if c.check == "currency_scope")
        self.assertEqual(check.status, "pass")
        self.assertIn("EUR", check.detail)

    def test_new_accounts_open_in_the_company_currency(self):
        code, _ = self.cli("entity", "account-add", str(self.path), "--account", "Expenses:Research")
        self.assertEqual(code, 0)
        self.assertEqual(self.account_currencies(), {"EUR"})

    def test_bank_csv_dollar_amounts_are_refused(self):
        text = "Date,Description,Amount\n01/15/2026,NORTHWIND,-$4.50\n01/16/2026,CONTOSO,$100.00\n"
        file = self.write("usd.csv", text)
        with self.assertRaises(si.StatementImportError):
            si.save_profile(self.path, file, name="usd-csv", ledger_account=CHECKING, overrides={"sign": "standard"})

    def test_accrual_intake_and_journal(self):
        configure_basis(self.path, "accrual")
        from bookkeeping.accrual import journals
        evidence = {"schema_version": 1, "source": {"namespace": "example.vendor", "account_id": "synthetic",
                    "object_id": "invoice-1", "version": "1"}, "currency": "EUR", "dates": {"issued": "2026-01-31"},
                    "amounts": {"gross": "10.00"}}
        stored = journals.intake_evidence(self.entity(), evidence)
        self.assertIn("id", stored)
        with self.assertRaises(Exception) as ctx:
            journals.intake_evidence(self.entity(), {**evidence, "currency": "USD",
                                                     "source": {**evidence["source"], "object_id": "invoice-2"}})
        self.assertIn("EUR", str(getattr(ctx.exception, "message", ctx.exception)) + json.dumps(
            getattr(ctx.exception, "as_dict", lambda: {})(), default=str))
        request = {"id": "journal-1", "date": "2026-01-31", "description": "Synthetic accrual",
                   "estimate_basis": "Synthetic estimate", "postings": [
                       {"account": "Expenses:Software", "amount": "10.00", "currency": "EUR"},
                       {"account": "Assets:Bank:Checking", "amount": "-10.00"}]}
        plan = journals.preview_journal(self.entity(), request)
        self.assertEqual({p["currency"] for p in plan["request"]["postings"]}, {"EUR"})
        bad = copy.deepcopy(request)
        bad["postings"][0]["currency"] = "USD"
        with self.assertRaises(journals.JournalError) as refused:
            journals.preview_journal(self.entity(), bad)
        self.assertEqual(refused.exception.code, "unsupported_currency")
        code, out = self.cli("accrual", "capabilities", "--entity", str(self.path))
        self.assertEqual(json.loads(out)["currency"], "EUR")
        code, out = self.cli("accrual", "schema", "--entity", str(self.path))
        self.assertNotIn('"const": "USD"', out)

    def test_tax_outputs_stamp_currency_and_flag(self):
        import_transactions(self.entity(), _rows(None, 2), "feed", categorizer=_post_everything)
        from bookkeeping import taxmap
        report = taxmap.mapping_report(self.path, form="schc", from_date=date(2026, 1, 1), to_date=date(2026, 12, 31))
        self.assertEqual(report["currency"], "EUR")
        [flag] = report["flags"]
        self.assertEqual(flag["code"], "tax_mapping_is_us")
        self.assertEqual(flag["message"], "The tax mapping follows US forms. The company currency is EUR. "
                                          "The preparer decides what applies.")
        out = self.root / "tax"
        manifest = taxmap.export_handoff(self.path, date(2026, 1, 1), date(2026, 12, 31), form="schc",
                                         output_dir=out, formats=("csv",))
        self.assertEqual(manifest["currency"], "EUR")
        self.assertEqual(manifest["flags"][0]["code"], "tax_mapping_is_us")
        self.assertIn("The company currency is EUR", (out / "README.txt").read_text())

    def test_usd_company_tax_outputs_have_no_stamp(self):
        other = self.root / "usd"
        init_entity(other, name="Synthetic USD Co")
        from bookkeeping import taxmap
        report = taxmap.mapping_report(other, form="schc")
        self.assertNotIn("currency", report)
        self.assertNotIn("flags", report)


class ReviewFixTests(CompanyTestCase):
    def stripe_setup(self, policy_currency: str = "USD") -> dict:
        for account in ("Assets:Processors:Stripe", "Assets:Transfers:Stripe-Payouts", "Income:Refunds"):
            self.cli("entity", "account-add", str(self.path), "--account", account)
        config = self.config()
        config["bank_account_mappings"]["acct_test"] = "Assets:Processors:Stripe"
        (self.path / "entity.json").write_text(json.dumps(config))
        return dict(version=1, enabled=True, stripe_account_id="acct_test", livemode=False, currency=policy_currency,
                    clearing_account="Assets:Processors:Stripe", payout_transit_account="Assets:Transfers:Stripe-Payouts",
                    fee_account="Expenses:Fees", refund_account="Income:Refunds",
                    revenue_rules=[dict(id="services", match={"metadata": {"object": "charge", "key": "stream", "value": "service"}},
                                        category="Income:Consulting")])

    @staticmethod
    def stripe_evidence(*rows: tuple[str, str]) -> dict:
        bts, charges = [], []
        for index, (bt_id, code) in enumerate(rows):
            bts.append({"id": bt_id, "created": 1768435200 + index, "amount": 10000, "fee": 300, "net": 9700,
                        "currency": code, "status": "available", "type": "charge", "source": f"ch_{bt_id}"})
            charges.append({"id": f"ch_{bt_id}", "livemode": False, "amount": 10000, "currency": code,
                            "status": "succeeded", "paid": True, "captured": True, "balance_transaction": bt_id,
                            "metadata": {"stream": "service"}})
        return {"source": "stripe", "account": {"id": "acct_test", "livemode": False}, "coverage": {"complete": True},
                "balanceTransactions": bts, "charges": charges}

    def test_usd_company_with_entries_stages_usd_rows_and_holds_a_foreign_row(self):
        from bookkeeping.stripe import stripe_plan, stripe_stage
        import_transactions(self.entity(), _rows(None), "s1", categorizer=_post_everything)
        policy = self.stripe_setup("USD")
        evidence = self.stripe_evidence(("txn_a", "usd"), ("txn_b", "eur"))
        result = stripe_stage(self.entity(), evidence, policy)
        self.assertEqual(result["staged"], 1)
        unresolved = {(row["source_id"], row["reason"]) for row in result["plan"]["unresolved"]}
        self.assertIn(("stripe:acct_test:txn_b", "unsupported_currency"), unresolved)
        self.assertEqual(unresolved, {(row["source_id"], row["reason"])
                                      for row in stripe_plan(self.entity(), evidence, policy)["unresolved"]})
        self.assertNotIn("currency", self.config())

    def test_eur_manual_accrual_opening(self):
        from bookkeeping.accrual import opening
        root = self.root / "accrual"
        init_entity(root, name="Synthetic Accrual", basis="accrual", cutover_date="2026-01-01")
        currency.apply_set_currency(load_entity(root), "EUR")
        (root / "evidence").mkdir(exist_ok=True)
        (root / "evidence/list.pdf").write_bytes(b"%PDF synthetic")
        request = {"operation_id": "op", "cutover": "2026-01-01", "evidence_refs": ["evidence/list.pdf"],
                   "lines": [{"account": "Assets:Bank:Checking", "amount": "1000.00"},
                             {"account": "Assets:Receivable:Accounts-Receivable", "amount": "300.00"},
                             {"account": "Equity:Opening-Balances", "amount": "-1300.00"}],
                   "items": [{"id": "inv-1", "kind": "receivable", "account": "Assets:Receivable:Accounts-Receivable",
                              "counterparty": "Synthetic Customer", "due_date": "2025-12-15", "open_amount": "300.00"}]}
        entity = load_entity(root)
        plan = opening.preview_opening(entity, request)
        self.assertEqual(plan["status"], "ready", plan.get("support_differences"))
        self.assertEqual(opening.apply_opening(entity, plan)["entries_written"], 1)
        with LedgerStore(root / "ledger.sqlite").connection() as conn:
            self.assertEqual({row[0] for row in conn.execute("SELECT DISTINCT currency FROM postings")}, {"EUR"})

    def test_eur_quickbooks_accrual_opening(self):
        import shutil
        from bookkeeping.accrual import migration
        root = self.root / "qb-accrual"
        init_entity(root, name="Synthetic Services", basis="accrual", cutover_date="2026-07-01")
        currency.apply_set_currency(load_entity(root), "EUR")
        shutil.copytree(ROOT / "tests/fixtures/accrual/quickbooks", root / "qb-exports")
        (root / "evidence").mkdir(exist_ok=True)
        (root / "evidence/opening.pdf").write_bytes(b"%PDF synthetic opening support")
        support = json.loads((root / "qb-exports/support.json").read_text().replace('"USD"', '"EUR"'))
        entity = load_entity(root)
        plan = migration.preview(entity, root / "qb-exports", "2026-07-01", fiscal_start="2026-01-01", supporting=support)
        self.assertEqual(plan["status"], "ready", plan.get("support_differences"))
        self.assertEqual(migration.apply(entity, plan)["entries_written"], 1)
        with LedgerStore(root / "ledger.sqlite").connection() as conn:
            self.assertEqual({row[0] for row in conn.execute("SELECT DISTINCT currency FROM postings")}, {"EUR"})

    def test_report_amounts_strip_only_the_company_currency(self):
        from bookkeeping import quickbooks as qb
        self.assertEqual(qb.parse_amount("$1,234.00"), Decimal("1234.00"))
        for text in ("€1,234.00", "£5.00"):
            with self.assertRaises(currency.CurrencyError) as ctx:
                qb.parse_amount(text)
            self.assertEqual(ctx.exception.code, "unsupported_currency")
        with currency.report_currency("EUR"):
            self.assertEqual(qb.parse_amount("€1,234.00"), Decimal("1234.00"))
            self.assertEqual(qb.parse_amount("(€5.00)"), Decimal("-5.00"))
            for text in ("$1.00", "£1.00", "1.00 USD"):
                with self.assertRaises(currency.CurrencyError):
                    qb.parse_amount(text)
        from bookkeeping.accrual import migration
        with self.assertRaises(currency.CurrencyError):
            migration._source_amount("€10.00")

    def test_explicit_usd_is_identical_to_an_absent_key(self):
        from bookkeeping import taxmap
        from bookkeeping.reports.workbook import run_sanity_checks
        outputs = []
        for name, extra in (("absent", {}), ("explicit", {"currency": "USD", "currency_source": "set"})):
            root = self.root / name
            init_entity(root, name="Synthetic Same Co")
            config = json.loads((root / "entity.json").read_text())
            config.update({"bank_account_mappings": {"feed-chk": CHECKING}, **extra})
            (root / "entity.json").write_text(json.dumps(config, indent=2) + "\n")
            import_transactions(load_entity(root), _rows(None, 2), "feed", categorizer=_post_everything,
                                ts="2026-02-01T00:00:00Z")
            mapping = taxmap.mapping_report(root, form="schc", from_date=date(2026, 1, 1), to_date=date(2026, 12, 31))
            out = self.root / f"tax-{name}"
            manifest = taxmap.export_handoff(root, date(2026, 1, 1), date(2026, 12, 31), form="schc",
                                             output_dir=out, formats=("csv",))
            manifest = {k: v for k, v in manifest.items() if k != "output_dir"}
            files = {path.name: path.read_bytes() for path in sorted(out.iterdir()) if path.name != "manifest.json"}
            checks = [(c.check, c.status, c.detail) for c in run_sanity_checks(root, date(2026, 1, 1), date(2026, 1, 31)).checks]
            outputs.append((mapping, manifest, files, checks))
        self.assertEqual(outputs[0], outputs[1])

    def test_cash_eur_company_report_meta_and_noncash_notes(self):
        from bookkeeping.reports.statements import ask, profit_and_loss
        self.set_currency("EUR")
        import_transactions(self.entity(), _rows(None, 2), "feed", categorizer=_post_everything)
        cash = profit_and_loss(self.path, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(cash.meta["currency"], "EUR")
        # The noncash notes come from the cash view of accrual books.
        from bookkeeping.accrual import journals
        root = self.root / "accrual-eur"
        init_entity(root, name="Synthetic", basis="accrual", cutover_date="2026-01-01")
        currency.apply_set_currency(load_entity(root), "EUR")
        evidence = {"schema_version": 1, "source": {"namespace": "ex.v", "account_id": "a", "object_id": "i1", "version": "1"},
                    "currency": "EUR", "dates": {"issued": "2026-01-20"}, "amounts": {"gross": "40.00"}}
        stored = journals.intake_evidence(load_entity(root), evidence)["id"]
        request = {"id": "j1", "date": "2026-01-20", "description": "Accrued software", "evidence_ids": [stored],
                   "postings": [{"account": "Expenses:Software", "amount": "40.00"},
                                {"account": "Liabilities:Other:Accrued-Liabilities", "amount": "-40.00"}]}
        entity = load_entity(root)
        journals.apply_journal(entity, journals.preview_journal(entity, request))
        report = profit_and_loss(root, date(2026, 1, 1), date(2026, 1, 31), basis="cash")
        self.assertIn("Includes €40.00 of entries that may not be cash", report.to_text())
        answer = ask(root, "What was our cash basis profit in January 2026?", today=date(2026, 2, 1))
        self.assertIn("Note: this includes €40.00 of entries that may not be cash", answer.answer_text)

    def test_hosted_capture_names_both_currencies_when_entity_json_was_not_saved(self):
        from bookkeeping import hosted_migration
        self.set_currency("EUR")
        import_transactions(self.entity(), _rows(None, 1), "feed", categorizer=_post_everything)
        stale = self.config()
        del stale["currency"], stale["currency_source"]
        (self.path / "entity.json").write_text(json.dumps(stale))  # The host saved the ledger but not entity.json.
        report = hosted_migration.assess(self.path)
        self.assertIn("company_settings_out_of_date", report["blockers"])
        message = report["currency_settings"]["message"]
        self.assertIn("EUR", message)
        self.assertIn("USD", message)
        self.assertIn("the saved company settings are out of date", message)

    def test_ledger_writer_refuses_a_posting_in_another_currency(self):
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry, Posting
        self.set_currency("EUR")
        entry = Entry(date=date(2026, 1, 2), narration="x", meta=(("source-id", "x1"),),
                      postings=(Posting(CHECKING, Decimal("1.00"), "USD"), Posting("Expenses:Software", Decimal("-1.00"), "USD")))
        with self.assertRaises(ValueError) as ctx:
            _atomic_ledger_write(self.entity(), [], [entry], "s", None, "x")
        self.assertIn("EUR", str(ctx.exception))
        self.assertEqual(LedgerStore(self.path / "ledger.sqlite").counts().entries, 0)

    def test_activity_counts_statement_records_duplicates_and_quickbooks_evidence(self):
        for relative in ("ingestion/statements/st-1.json", "sources/quickbooks/abc/snapshot.json"):
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}")
            self.assertTrue(currency.books_activity(self.path))
            self.assertFalse(currency.books_activity(self.path, exclude=("sources/quickbooks/abc",))
                             if relative.startswith("sources") else False)
            target.unlink()
        (self.path / "staging" / "duplicate-candidates.json").write_text('[{"source_id": "x"}]')
        self.assertIn("duplicate candidates", currency.books_activity(self.path))

    def test_infer_reads_stripe_balance_transactions_only(self):
        envelope = self.stripe_evidence(("txn_a", "usd"))
        envelope["charges"][0]["currency"] = "eur"
        result = currency.infer_from_files([self.write("stripe.json", json.dumps(envelope))])
        self.assertEqual(result["currency"], "USD")

    def test_dollar_file_in_a_non_dollar_company_says_so(self):
        self.set_currency("EUR")
        text = "Date,Description,Amount\n01/15/2026,NORTHWIND,-$4.50\n01/16/2026,CONTOSO,$100.00\n"
        result = si.inspect_file(self.write("usd.csv", text), entity_config=self.config())
        self.assertEqual(result["error"]["code"], "unsupported_currency")
        self.assertIn("dollar sign", result["error"]["message"])
        self.assertIn("EUR", result["error"]["message"])

    def test_bundle_currency_error_names_both_currencies(self):
        from bookkeeping import migration_bundle
        with self.assertRaises(migration_bundle.BundleError) as ctx:
            migration_bundle.require_target_currency({"source_entity": {"currency": "EUR"}}, {"currency": "GBP"})
        self.assertIn("EUR", ctx.exception.message)
        self.assertIn("GBP", ctx.exception.message)


class MigrationBundleTests(unittest.TestCase):
    def test_bundle_currency_must_match_the_target(self):
        from bookkeeping import migration_bundle
        manifest = {"source_entity": {"currency": "EUR"}}
        self.assertEqual(migration_bundle.require_target_currency(manifest, {"currency": "EUR"}), "EUR")
        with self.assertRaises(migration_bundle.BundleError) as ctx:
            migration_bundle.require_target_currency(manifest, {})
        self.assertEqual(ctx.exception.code, "unsupported_currency")
        self.assertEqual(migration_bundle.require_target_currency({"source_entity": {"currency": "USD"}}, {}), "USD")


if __name__ == "__main__":
    unittest.main()
