"""Tests for ``opening_presentation``: the presentation amount of each account of an opening entry.

All companies, statements and QuickBooks reports here are synthetic.
"""

from __future__ import annotations

import builtins
import copy
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping import statement_import as si  # noqa: E402
from bookkeeping.entity import init_entity, load_entity  # noqa: E402
from bookkeeping.ledger.model import Entry, Posting  # noqa: E402
from bookkeeping.ledger.store import FINANCIAL_KINDS, LedgerStore, default_store_path  # noqa: E402
from bookkeeping.opening_presentation import (  # noqa: E402
    REFUSAL_REASONS,
    OpeningPresentationError,
    main,
    opening_presentation,
)
from bookkeeping.source_disposition import entry_document  # noqa: E402
from bookkeeping.statement_opening import record_opening  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "statements"
CHECKING = "Assets:Bank:Checking"
CARD = "Liabilities:CreditCard:Visa"
EQUITY = "Equity:Opening-Balances"

QB_CHART = """Account name,Account type,Detail type,Lock
Checking (4512) - 1,Bank,Checking,No
Savings (9873) - 1,Bank,Savings,No
Accounts Receivable (A/R),Accounts receivable (A/R),Accounts Receivable (A/R),No
Accounts Payable (A/P),Accounts payable (A/P),Accounts Payable (A/P),No
Visa Card (3301) - 2,Credit Card,Credit Card,No
Equipment Loan,Long-term liabilities,Notes Payable,No
Opening Balance Equity,Equity,Opening Balance Equity,No
Retained Earnings,Equity,Retained Earnings,No
"""
# A/P has a debit balance (the company paid a supplier too much), so its presentation amount is negative.
QB_TRIAL_BALANCE = """"Synthetic Co",,
Trial Balance,,
"As of Dec 31, 2025",,

Full name,Debit,Credit
Checking (4512) - 1,"20,000.00",
Savings (9873) - 1,"5,000.10",
Accounts Receivable (A/R),"3,250.50",
Accounts Payable (A/P),125.00,
Visa Card (3301) - 2,,"2,000.25"
Equipment Loan,,"15,000.00"
Retained Earnings,,"11,375.35"
TOTAL,"$28,375.60","$28,375.60"



"Cash Basis Thursday, June 12, 2025 10:30 AM GMT-05:00",,
"""
QB_CHECKING = "Assets:Bank:Checking-4512-1"
QB_SAVINGS = "Assets:Bank:Savings-9873-1"
QB_RECEIVABLE = "Assets:Receivable:Accounts-Receivable-A-R"
QB_PAYABLE = "Liabilities:Payable:Accounts-Payable-A-P"
QB_CARD = "Liabilities:CreditCard:Visa-Card-3301-2"
QB_LOAN = "Liabilities:Other:Equipment-Loan"
QB_KINDS = {QB_CHECKING: "bank", QB_SAVINGS: "bank", QB_RECEIVABLE: "other", QB_PAYABLE: "other",
            QB_CARD: "credit_card", QB_LOAN: "other"}


def _utf8_sorted(accounts):
    return sorted(accounts, key=lambda name: name.encode("utf-8"))


def _statement_entity(root: Path) -> Path:
    entity_dir = root / "co"
    (entity_dir / "staging").mkdir(parents=True)
    (entity_dir / "entity.json").write_text(
        json.dumps({"name": "Synthetic Co", "business_type": "consulting"}) + "\n", encoding="utf-8")
    (entity_dir / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n", encoding="utf-8")
    return entity_dir


def _snapshot(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def _opening(postings, meta=(("source-id", "quickbooks-opening-x"), ("import-source", "quickbooks-opening"))):
    return Entry(date=date(2026, 1, 1), narration="Opening balances", meta=tuple(meta),
                 postings=tuple(Posting(account, Decimal(amount), currency) for account, amount, currency in postings))


class StatementOpeningTests(unittest.TestCase):
    """For a statement opening, the result equals the ``opening-balance`` metadata of the entry exactly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.entity = _statement_entity(Path(self._tmp.name))

    def imp(self, name: str, profile: str, account: str, **kwargs) -> dict:
        si.save_profile(self.entity, FIXTURES / name, name=profile, ledger_account=account, overrides={})
        result = si.import_file(self.entity, FIXTURES / name, profile=profile, ts="2026-02-01T00:00:00Z", **kwargs)
        return result["statements"][0]["statement"]

    def committed(self, source_id: str) -> Entry:
        found = [e for e in LedgerStore(default_store_path(self.entity)).load_entries() if e.source_id == source_id]
        self.assertEqual(len(found), 1)
        return found[0]

    def check(self, preview: dict, account: str, kind: str, expected: str) -> None:
        self.assertEqual(preview["status"], "dry_run")
        document = preview["proposed_entry"]
        meta = dict(map(tuple, document["meta"]))
        self.assertEqual(meta["opening-balance"], expected)
        rows = opening_presentation(document, [account], {account: kind})
        self.assertEqual(rows, [{"account": account, "status": "presented", "financial_kind": kind,
                                 "amount": meta["opening-balance"], "currency": "USD"}])

    def test_printed_bank_and_card_openings_equal_the_opening_balance_metadata(self):
        checking = self.imp("chase_like_checking.csv", "checking", CHECKING,
                            period_start="2026-01-01", period_end="2026-01-31")
        card = self.imp("card_debit_credit.csv", "card", CARD, period_start="2026-01-01", period_end="2026-01-31",
                        opening_balance="1000.00", closing_balance="769.99")
        for record, account, kind in ((checking, CHECKING, "bank"), (card, CARD, "credit_card")):
            with self.subTest(account):
                preview = record_opening(self.entity, record["statement_id"], dry_run=True)
                self.assertEqual(preview["opening_source"], "printed")
                self.check(preview, account, kind, preview["opening_balance"])
                done = record_opening(self.entity, record["statement_id"])
                self.assertEqual(done["status"], "recorded")
                entry = self.committed(done["source_id"])
                rows = opening_presentation(entry, [account], {account: kind})
                self.assertEqual(rows[0]["amount"], dict(entry.meta)["opening-balance"])
        self.assertEqual(opening_presentation(self.committed(f"statement-opening-{card['statement_id']}"),
                                              [CARD], {CARD: "credit_card"})[0]["amount"], "1000.00")

    def test_typed_openings_of_each_sign_equal_the_opening_balance_metadata(self):
        from bookkeeping.ledger.model import Open

        cash = "Assets:Cash:Petty"
        store = LedgerStore(default_store_path(self.entity))
        store.initialize()
        with store.transaction() as conn:
            store.insert_opens([Open(date(2026, 1, 1), account) for account in (CHECKING, CARD, cash)], conn)
        for account, kind, amount in ((CHECKING, "bank", "7907.82"), (CHECKING, "bank", "-50.25"),
                                      (CARD, "credit_card", "1234.56"), (CARD, "credit_card", "-12.40"),
                                      (cash, "cash", "300.00")):
            with self.subTest(account=account, amount=amount):
                preview = record_opening(self.entity, account=account, on="2026-01-01", amount=amount, dry_run=True)
                self.check(preview, account, kind, amount)

    def test_a_statement_opening_whose_metadata_differs_is_refused(self):
        entry = _opening([(CARD, "-250.00", "USD"), (EQUITY, "250.00", "USD")],
                         (("source-id", "statement-opening-x"), ("import-source", "statement-opening"),
                          ("opening-source", "person"), ("opening-balance", "-250.00")))
        row = opening_presentation(entry, [CARD], {CARD: "credit_card"})[0]
        self.assertEqual((row["status"], row["reason"], row["financial_kind"]),
                         ("refused", "opening_balance_differs", "credit_card"))
        self.assertNotIn("amount", row)


class QuickBooksOpeningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.folder = root / "qb"
        self.folder.mkdir()
        (self.folder / "chart_of_accounts.csv").write_text(QB_CHART, encoding="utf-8")
        (self.folder / "trial_balance_cash_basis.csv").write_text(QB_TRIAL_BALANCE, encoding="utf-8")
        init_entity(root / "entity", name="Synthetic Co", business_type="consulting")
        self.entity = load_entity(root / "entity")

    def test_many_accounts_have_exact_amounts_and_signs(self):
        from bookkeeping.quickbooks import import_opening, preview_opening

        preview = preview_opening(self.entity, self.folder, date(2026, 1, 1))
        self.assertTrue(preview["success"], preview["errors"])
        document = preview["proposed_entry"]
        accounts = _utf8_sorted(QB_KINDS)
        rows = opening_presentation(document, accounts, QB_KINDS)
        self.assertEqual([row["account"] for row in rows], accounts)
        self.assertEqual({row["account"]: (row["financial_kind"], row["amount"], row["currency"]) for row in rows}, {
            QB_CHECKING: ("bank", "20000.00", "USD"),
            QB_SAVINGS: ("bank", "5000.10", "USD"),
            QB_RECEIVABLE: ("other", "3250.50", "USD"),
            QB_PAYABLE: ("other", "-125.00", "USD"),
            QB_CARD: ("credit_card", "2000.25", "USD"),
            QB_LOAN: ("other", "15000.00", "USD"),
        })
        self.assertTrue(all(row["status"] == "presented" for row in rows))
        # The committed entry gives the same answer as the reviewed preview.
        result = import_opening(self.entity, self.folder, date(2026, 1, 1))
        self.assertTrue(result.success, result.errors)
        committed = [e for e in LedgerStore(default_store_path(self.entity.path)).load_entries()
                     if e.source_id == preview["proposed_source_id"]]
        self.assertEqual(opening_presentation(committed[0], accounts, QB_KINDS), rows)
        self.assertEqual(opening_presentation(entry_document(committed[0]), accounts, QB_KINDS), rows)


class AccrualOpeningTests(unittest.TestCase):
    def test_fresh_accrual_opening(self):
        from bookkeeping.accrual import opening

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "accrual-co"
            init_entity(root, name="Synthetic Accrual", basis="accrual", cutover_date="2026-01-01")
            entity = load_entity(root)
            (root / "evidence").mkdir(exist_ok=True)
            (root / "evidence/ar-ap-list.pdf").write_bytes(b"%PDF synthetic open items")
            receivable, payable = "Assets:Receivable:Accounts-Receivable", "Liabilities:Payable:Accounts-Payable"
            request = {
                "operation_id": "synthetic-opening", "cutover": "2026-01-01",
                "evidence_refs": ["evidence/ar-ap-list.pdf"],
                "lines": [{"account": CHECKING, "amount": "30000.00"}, {"account": receivable, "amount": "300.00"},
                          {"account": payable, "amount": "-100.00"}, {"account": EQUITY, "amount": "-30200.00"}],
                "items": [{"id": "inv-1", "kind": "receivable", "account": receivable,
                           "counterparty": "Synthetic Customer", "reference": "INV-1", "due_date": "2025-12-15",
                           "open_amount": "300.00"},
                          {"id": "bill-1", "kind": "payable", "account": payable,
                           "counterparty": "Synthetic Supplier", "reference": "BILL-9", "due_date": "2026-01-10",
                           "open_amount": "100.00"}]}
            plan = opening.preview_opening(entity, request)
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(dict(map(tuple, plan["proposed_entry"]["meta"]))["import-source"], "accrual-opening")
            kinds = {CHECKING: "bank", receivable: "other", payable: "other"}
            rows = opening_presentation(plan["proposed_entry"], _utf8_sorted(kinds), kinds)
            self.assertEqual([(r["account"], r["financial_kind"], r["amount"]) for r in rows],
                             [(CHECKING, "bank", "30000.00"), (receivable, "other", "300.00"),
                              (payable, "other", "100.00")])


class ContractTests(unittest.TestCase):
    """Kinds, refusals, the account list and the input checks."""

    def setUp(self):
        self.entry = _opening([(QB_CHECKING, "100.00", "USD"), (QB_CARD, "-40.00", "USD"),
                               ("Assets:Cash", "5.00", "USD"), (QB_LOAN, "-65.00", "USD")])
        self.accounts = _utf8_sorted(["Assets:Cash", QB_CHECKING, QB_CARD, QB_LOAN])

    def test_every_supported_kind_on_every_allowed_root_is_presented(self):
        self.assertEqual(FINANCIAL_KINDS, ("bank", "credit_card", "cash", "other"))
        cases = [("Assets:Bank:Op", "bank", "10.00", "10.00"), ("Assets:Cash:Till", "cash", "-3.00", "-3.00"),
                 ("Assets:Receivable:AR", "other", "7.50", "7.50"),
                 ("Liabilities:CreditCard:Amex", "credit_card", "-20.00", "20.00"),
                 ("Liabilities:Payable:AP", "other", "-4.00", "4.00"),
                 ("Liabilities:Other:Loan", "other", "9.00", "-9.00")]
        total = sum(Decimal(ledger) for _, _, ledger, _ in cases)
        entry = _opening([(a, ledger, "USD") for a, _, ledger, _ in cases] + [(EQUITY, str(-total), "USD")])
        kinds = {a: kind for a, kind, _, _ in cases}
        rows = opening_presentation(entry, _utf8_sorted(kinds), kinds)
        self.assertEqual({r["account"]: (r["status"], r["financial_kind"], r["amount"]) for r in rows},
                         {a: ("presented", kind, shown) for a, kind, _, shown in cases})

    def test_unknown_or_unsupported_kinds_are_refused_per_account_and_never_dropped(self):
        kinds = {QB_CHECKING: "bank", QB_CARD: "loan", "Assets:Cash": "credit_card", QB_LOAN: None}
        rows = opening_presentation(self.entry, self.accounts, kinds)
        self.assertEqual([r["account"] for r in rows], self.accounts)
        by_account = {r["account"]: r for r in rows}
        self.assertEqual(by_account[QB_CHECKING]["status"], "presented")
        expected = {QB_CARD: ("financial_kind_unsupported", "loan"),
                    "Assets:Cash": ("financial_kind_mismatch", "credit_card"),
                    QB_LOAN: ("financial_kind_missing", None)}
        for account, (reason, kind) in expected.items():
            row = by_account[account]
            self.assertEqual((row["status"], row["reason"], row["financial_kind"]), ("refused", reason, kind))
            self.assertIn(reason, REFUSAL_REASONS)
            self.assertIsInstance(row["message"], str)
            self.assertEqual(set(row), {"account", "status", "reason", "financial_kind", "message"})
        # An account with no entry in the kinds is unclassified.
        rows = opening_presentation(self.entry, self.accounts, {QB_CHECKING: "bank"})
        self.assertEqual([r.get("reason") for r in rows],
                         [None if a == QB_CHECKING else "financial_kind_missing" for a in self.accounts])
        rows = opening_presentation(self.entry, self.accounts, {**kinds, QB_LOAN: 7})
        loan = next(r for r in rows if r["account"] == QB_LOAN)
        self.assertEqual((loan["reason"], loan["financial_kind"]), ("financial_kind_unsupported", None))

    def test_an_account_in_more_than_one_currency_is_refused(self):
        entry = _opening([(QB_CHECKING, "100.00", "USD"), (QB_CHECKING, "50.00", "EUR"),
                          (EQUITY, "-100.00", "USD"), (EQUITY, "-50.00", "EUR")])
        row = opening_presentation(entry, [QB_CHECKING], {QB_CHECKING: "bank"})[0]
        self.assertEqual((row["status"], row["reason"]), ("refused", "mixed_currency"))

    def test_lines_of_one_account_add_up_and_zero_has_no_sign(self):
        entry = _opening([(QB_CARD, "-40.00", "EUR"), (QB_CARD, "40.00", "EUR"), (QB_CHECKING, "0.10", "EUR"),
                          (QB_CHECKING, "0.20", "EUR"), (EQUITY, "-0.30", "EUR")])
        rows = opening_presentation(entry, _utf8_sorted([QB_CHECKING, QB_CARD]),
                                    {QB_CHECKING: "bank", QB_CARD: "credit_card"})
        self.assertEqual([(r["account"], r["amount"], r["currency"]) for r in rows],
                         [(QB_CHECKING, "0.30", "EUR"), (QB_CARD, "0.00", "EUR")])

    def test_the_account_list_must_match_the_balance_accounts_of_the_entry_exactly(self):
        kinds = {a: "other" for a in self.accounts}
        for accounts, not_posted, not_listed in (
                (self.accounts[:-1], [], [self.accounts[-1]]),
                (self.accounts + ["Liabilities:Zzz"], ["Liabilities:Zzz"], []),
                (_utf8_sorted(self.accounts + [EQUITY]), [EQUITY], []),
                ([], [], self.accounts)):
            with self.subTest(accounts=accounts):
                with self.assertRaises(OpeningPresentationError) as caught:
                    opening_presentation(self.entry, accounts, kinds)
                error = caught.exception
                self.assertEqual(error.code, "accounts_mismatch")
                self.assertEqual((error.detail["not_posted"], error.detail["not_listed"]), (not_posted, not_listed))
                self.assertEqual(error.as_dict()["code"], "accounts_mismatch")

    def test_the_account_list_must_be_unique_text_sorted_by_utf8_bytes(self):
        for accounts in (list(reversed(self.accounts)), self.accounts + [self.accounts[-1]], "Assets:Cash",
                         [*self.accounts[:-1], 3], (a for a in self.accounts)):
            with self.subTest(accounts=accounts):
                with self.assertRaises(OpeningPresentationError) as caught:
                    opening_presentation(self.entry, accounts, {})
                self.assertEqual(caught.exception.code, "invalid_accounts")

    def test_entry_input_checks(self):
        good = entry_document(self.entry)
        bad_documents = [{**good, "extra": 1}, {k: v for k, v in good.items() if k != "flag"},
                         {**good, "postings": [{**good["postings"][0], "amount": "NaN"}, *good["postings"][1:]]},
                         [], "entry"]
        for document in bad_documents:
            with self.subTest(document=str(document)[:60]):
                with self.assertRaises(OpeningPresentationError) as caught:
                    opening_presentation(document, self.accounts, {})
                self.assertEqual(caught.exception.code, "invalid_entry")
        not_openings = [
            _opening([(QB_CHECKING, "1.00", "USD"), ("Income:Sales", "-1.00", "USD")]),
            _opening([(QB_CHECKING, "1.00", "USD"), (EQUITY, "-1.00", "USD")],
                     (("source-id", "t-1"), ("import-source", "bank-csv"))),
            _opening([(QB_CHECKING, "1.00", "USD"), (EQUITY, "-1.00", "USD")], (("source-id", "t-1"),)),
            _opening([(QB_CHECKING, "-1.00", "USD"), (EQUITY, "1.00", "USD")],
                     (("source-id", "r-1"), ("import-source", "quickbooks-opening"), ("reverses", "q-1"))),
        ]
        for entry in not_openings:
            with self.subTest(entry=entry.meta):
                with self.assertRaises(OpeningPresentationError) as caught:
                    opening_presentation(entry, [QB_CHECKING], {QB_CHECKING: "bank"})
                self.assertEqual(caught.exception.code, "not_an_opening")
        with self.assertRaises(OpeningPresentationError) as caught:
            opening_presentation(self.entry, self.accounts, [("Assets:Cash", "cash")])
        self.assertEqual(caught.exception.code, "invalid_financial_kinds")

    def test_every_opening_import_source_is_accepted(self):
        for meta in ((("import-source", "statement-opening"),), (("import-source", "quickbooks-opening"),),
                     (("import-source", "quickbooks-opening"), ("accrual-opening", "true")),
                     (("import-source", "accrual-opening"), ("accrual-opening", "true")),
                     (("import-source", "accrual-tracking-opening"), ("accrual-tracking-opening", "true"))):
            entry = _opening([(QB_CHECKING, "1.00", "USD"), (EQUITY, "-1.00", "USD")], (("source-id", "o-1"), *meta))
            self.assertEqual(opening_presentation(entry, [QB_CHECKING], {QB_CHECKING: "bank"})[0]["amount"], "1.00")


class ReadOnlyTests(unittest.TestCase):
    def test_the_function_writes_nothing_reads_no_file_and_does_not_change_its_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "entity"
            init_entity(root, name="Synthetic Co", business_type="consulting")
            entry = _opening([(QB_CHECKING, "100.00", "USD"), (QB_CARD, "-40.00", "USD"), (EQUITY, "-60.00", "USD")])
            document = entry_document(entry)
            accounts = _utf8_sorted([QB_CHECKING, QB_CARD])
            kinds = {QB_CHECKING: "bank", QB_CARD: "credit_card"}
            expected = opening_presentation(document, accounts, kinds)
            inputs = copy.deepcopy((document, accounts, kinds))
            before = _snapshot(root)
            cwd_before = sorted(os.listdir(os.getcwd()))

            def refuse(*args, **kwargs):
                raise AssertionError("opening_presentation must not do I/O")

            with mock.patch.object(builtins, "open", refuse), mock.patch.object(io, "open", refuse), \
                    mock.patch.object(os, "open", refuse), mock.patch.object(sqlite3, "connect", refuse), \
                    mock.patch.object(Path, "write_text", refuse), mock.patch.object(Path, "write_bytes", refuse), \
                    mock.patch.object(Path, "read_text", refuse), mock.patch.object(Path, "read_bytes", refuse), \
                    mock.patch.object(Path, "mkdir", refuse), mock.patch.object(Path, "touch", refuse):
                for given in ((document, accounts, kinds),
                              (entry, tuple(accounts), MappingProxyType(dict(kinds)))):
                    self.assertEqual(opening_presentation(*given), expected)
            self.assertEqual((document, accounts, kinds), inputs)
            self.assertEqual(_snapshot(root), before)
            self.assertEqual(sorted(os.listdir(os.getcwd())), cwd_before)


class CommandTests(unittest.TestCase):
    def run_main(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(argv))
        return code, json.loads(out.getvalue())

    def test_command_reads_kinds_read_only_and_prints_the_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "entity"
            init_entity(root, name="Synthetic Co", business_type="consulting")
            store = LedgerStore(default_store_path(root))
            with store.transaction() as conn:
                from bookkeeping.ledger.model import Open
                store.insert_opens([Open(date(2026, 1, 1), QB_CHECKING), Open(date(2026, 1, 1), QB_CARD)], conn)
                store.assign_financial_kinds({QB_CHECKING: "bank", QB_CARD: "credit_card"}, conn)
            entry = _opening([(QB_CHECKING, "100.00", "USD"), (QB_CARD, "-40.00", "USD"), (EQUITY, "-60.00", "USD")])
            request = Path(tmp) / "request.json"
            request.write_text(json.dumps({"schema_version": 1, "entry": entry_document(entry),
                                           "accounts": _utf8_sorted([QB_CHECKING, QB_CARD])}), encoding="utf-8")
            before = _snapshot(root)
            code, result = self.run_main("show", "--request", str(request), "--entity", str(root))
            self.assertEqual(code, 0)
            self.assertEqual(result, {"status": "ok", "accounts": [
                {"account": QB_CHECKING, "status": "presented", "financial_kind": "bank", "amount": "100.00",
                 "currency": "USD"},
                {"account": QB_CARD, "status": "presented", "financial_kind": "credit_card", "amount": "40.00",
                 "currency": "USD"}]})
            self.assertEqual(_snapshot(root), before)

            empty = Path(tmp) / "no-ledger"
            empty.mkdir()
            code, result = self.run_main("show", "--request", str(request), "--entity", str(empty))
            self.assertEqual((code, result["status"]), (1, "refused"))
            self.assertEqual([r["reason"] for r in result["accounts"]], ["financial_kind_missing"] * 2)
            self.assertEqual(list(empty.iterdir()), [])

            request.write_text(json.dumps({"schema_version": 1, "entry": entry_document(entry),
                                           "accounts": [QB_CHECKING]}), encoding="utf-8")
            code, result = self.run_main("show", "--request", str(request), "--entity", str(root))
            self.assertEqual((code, result["status"], result["error"]["code"]), (2, "error", "accounts_mismatch"))
            self.assertEqual(result["error"]["not_listed"], [QB_CARD])
            for bad in ({"schema_version": 2, "entry": entry_document(entry), "accounts": []},
                        {"schema_version": 1, "entry": entry_document(entry)}):
                request.write_text(json.dumps(bad), encoding="utf-8")
                code, result = self.run_main("show", "--request", str(request), "--entity", str(root))
                self.assertEqual((code, result["error"]["code"]), (2, "invalid_request"))
            request.write_text('{"schema_version": 1, "entry": {}, "accounts": [], "x": NaN}', encoding="utf-8")
            code, result = self.run_main("show", "--request", str(request), "--entity", str(root))
            self.assertEqual((code, result["error"]["code"]), (2, "invalid_request"))


if __name__ == "__main__":
    unittest.main()
