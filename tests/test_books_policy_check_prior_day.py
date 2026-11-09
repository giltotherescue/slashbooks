"""Request version 3: a fresh accrual opening on the day before the cutover (``accrual_opening_prior_day``).

Synthetic typed accrual company with cutover C = 2026-07-01. The real native preview and apply record one opening
entry on 2026-06-30 that posts to Checking (bank, mapped), CreditCard (credit card, typed only), an open receivable
and opening equity. Checking and CreditCard are pending in the books start policy. All data is synthetic.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.accrual import opening  # noqa: E402
from bookkeeping.entity import init_entity, load_entity  # noqa: E402
from bookkeeping.ledger.books_policy import BooksPolicyError  # noqa: E402
from bookkeeping.ledger.model import Entry, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402

C = "2026-07-01"
CHECKING, CARD = "Assets:Bank:Checking", "Liabilities:CreditCard"
RECEIVABLE, EQUITY = "Assets:Receivable:Accounts-Receivable", "Equity:Opening-Balances"
SAVINGS = "Assets:Bank:Savings"
MISMATCH, OPENING_REQUIRED = "BOOKS_OPENING_AUTHORIZATION_MISMATCH", "OPENING_REQUIRED"


def checker():
    from bookkeeping.ledger import books_policy_check
    return books_policy_check


def pending(accounts=(CHECKING, CARD), version: int = 2, start: str = C) -> dict:
    return {account: {"state": "authorized_pending_opening", "start_day": start, "version": version}
            for account in accounts}


def policy(accounts: dict | None = None) -> dict:
    return {"schema_version": 1, "mode": "enforced", "epoch": 4, "accounts": accounts or pending()}


class PriorDayOpeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.base = root / "base"
        init_entity(cls.base, name="Synthetic Accrual", basis="accrual", cutover_date=C,
                    classify_starter_accounts=True)
        config = json.loads((cls.base / "entity.json").read_bytes())
        config["bank_account_mappings"] = {"checking-feed": CHECKING}
        (cls.base / "entity.json").write_text(json.dumps(config))
        (cls.base / "staging" / "pending-categorization.json").write_text(json.dumps([
            {"id": "banksync:prior", "accountId": "checking-feed", "date": "2026-06-29", "amount": "1.00"},
            {"id": "banksync:opening-day", "accountId": "checking-feed", "date": "2026-06-30", "amount": "2.00"}]))
        (cls.base / "evidence").mkdir(exist_ok=True)
        (cls.base / "evidence" / "open-items.txt").write_text("Synthetic invoice 50.00 outstanding")
        cls.request = {
            "operation_id": "native-opening", "cutover": C, "evidence_refs": ["evidence/open-items.txt"],
            "lines": [{"account": CHECKING, "amount": "100.00"}, {"account": CARD, "amount": "-25.00"},
                      {"account": RECEIVABLE, "amount": "50.00"}, {"account": EQUITY, "amount": "-125.00"}],
            "items": [{"id": "inv-1", "kind": "receivable", "account": RECEIVABLE,
                       "counterparty": "Synthetic Customer", "reference": "INV-1", "due_date": "2026-07-10",
                       "open_amount": "50.00"}]}
        cls.plan = opening.preview_opening(load_entity(cls.base), cls.request)
        cls.candidate = root / "candidate"
        shutil.copytree(cls.base, cls.candidate)
        cls.applied = opening.apply_opening(load_entity(cls.candidate), cls.plan)
        cls.store = LedgerStore(cls.candidate / "ledger.sqlite")
        (cls.entry,) = cls.store.load_entries()
        base_entries, base_scope = checker()._workspace(cls.base)
        assert base_entries == []
        cls.scope = base_scope | checker()._workspace(cls.candidate)[1]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # -- helpers ------------------------------------------------------------------------------------------------

    def row(self, account: str, entry: Entry | None = None, **change) -> dict:
        entry = entry or self.entry
        return {"account": account, "start_day": C, "version": 2, "operation": "accrual_opening_prior_day",
                "operation_id": f"review-{account}", "entry_source_id": entry.source_id,
                "entry_fingerprint": checker().entry_fingerprint(entry), **change}

    def v3(self, rows=None, accounts: dict | None = None, entry: Entry | None = None) -> dict:
        rows = rows if rows is not None else [self.row(CHECKING, entry), self.row(CARD, entry)]
        return {"schema_version": 3, "policy": policy(accounts), "scope": [CHECKING], "opening_authorizations": rows}

    def check(self, request: dict, added=None, base=()) -> dict:
        added = [self.entry] if added is None else added
        return checker().check_ledger_delta(request, list(base), [*base, *added], self.scope)

    def assert_refused(self, result: dict, code: str = MISMATCH) -> None:
        self.assertEqual(result["status"], "refused", result)
        self.assertIn(code, [r["code"] for r in result["refusals"]])
        self.assertEqual(result["openings_matched"], [])

    def assert_invalid(self, request: dict, words: str = "") -> None:
        with self.assertRaises(BooksPolicyError) as caught:
            self.check(request)
        self.assertEqual(caught.exception.code, "BOOKS_POLICY_INVALID")
        self.assertIn(words, str(caught.exception))

    def changed(self, **change) -> Entry:
        return dataclasses.replace(self.entry, **change)

    def with_meta(self, **change) -> Entry:
        meta = dict(self.entry.meta)
        for key, value in change.items():
            key = key.replace("_", "-")
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value
        return self.changed(meta=tuple(meta.items()))

    # -- the native candidate -----------------------------------------------------------------------------------

    def test_the_native_candidate_is_a_fresh_prior_day_opening(self) -> None:
        self.assertEqual(self.plan["status"], "ready")
        self.assertEqual(self.applied["entries_written"], 1)
        self.assertEqual(self.entry.date, date(2026, 6, 30))
        self.assertEqual(dict(self.entry.meta), {"source-id": "native-opening", "import-source": "accrual-opening",
                                                 "accrual-opening": "true", "coverage-id": "opening",
                                                 "cutover-date": C})
        self.assertEqual(checker().entry_fingerprint(self.entry), self.plan["proposed_entry_fingerprint"])
        self.assertEqual(self.plan["accounting_plan"]["id"], self.entry.source_id)
        self.assertEqual(self.store.load_financial_kinds()[CARD], "credit_card")
        self.assertIn(CARD, self.scope)  # typed, not mapped
        self.assertIn(CHECKING, self.scope)  # typed and mapped

    def test_version_2_refuses_the_prior_day_opening_with_its_real_fingerprint(self) -> None:
        rows = [dict(self.row(account), operation="accrual_opening") for account in (CHECKING, CARD)]
        result = checker().check_workspaces(dict(self.v3(rows), schema_version=2), self.base, self.candidate)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(sorted((r["account"], r["code"], r["entry_date"]) for r in result["refusals"]),
                         [(CHECKING, OPENING_REQUIRED, "2026-06-30"), (CARD, OPENING_REQUIRED, "2026-06-30")])
        self.assertEqual(result["openings_matched"], [])

    def test_version_3_accepts_the_exact_group_with_one_id_per_account(self) -> None:
        result = checker().check_workspaces(self.v3(), self.base, self.candidate)
        self.assertEqual((result["status"], result["refusals"]), ("ok", []))
        self.assertEqual(result["openings_matched"], [f"review-{CHECKING}", f"review-{CARD}"])
        self.assertEqual(self.check(self.v3(list(reversed(self.v3()["opening_authorizations"]))))["status"], "ok")

    def test_the_native_replay_adds_nothing(self) -> None:
        replay = opening.apply_opening(load_entity(self.candidate), self.plan)
        self.assertEqual((replay["entries_written"], replay["replayed"]), (0, True))
        self.assertEqual(self.store.load_entries(), [self.entry])

    def test_outside_books_decisions_without_the_version_3_request_invalidate(self) -> None:
        """The prior-day opening completes a decision only with its version 3 request (``source_disposition``).

        Here no request is given and the receivable is outside the policy, so each decision is invalidated with the
        explicit reasons. ``tests/test_source_disposition_accrual.py`` has the completion itself.
        """
        from bookkeeping.source_disposition import confirm, evaluate, load_context, preview
        before = load_context(self.base, policy())
        claims = [confirm(before, preview(before, row["id"], "outside_books")["claim"])["claim"]
                  for row in before.rows]
        after = policy({account: {"state": "authorized", "start_day": C, "version": 3} for account in (CHECKING, CARD)})
        after["epoch"] = 5
        results = evaluate(load_context(self.candidate, after), claims)
        self.assertEqual([(r["status"], r["triggers"]) for r in results],
                         [("invalidated", ["ACCRUAL_ACCOUNT_NOT_GOVERNED", "ACCRUAL_AUTHORIZATION_REQUIRED",
                                           "OPENING_CHANGED", "OPEN_DATE_CHANGED", "POLICY_CHANGED"])] * 2)

    # -- operation IDs and the account set ----------------------------------------------------------------------

    def test_a_shared_operation_id_is_refused(self) -> None:
        rows = [self.row(CHECKING, operation_id="one-review"), self.row(CARD, operation_id="one-review")]
        self.assert_invalid(self.v3(rows), "its own operation_id")

    def test_a_missing_or_extra_account_is_refused(self) -> None:
        self.assert_refused(self.check(self.v3([self.row(CHECKING)])))
        self.assert_refused(self.check(self.v3([self.row(CARD)])))
        extra = self.v3([self.row(CHECKING), self.row(CARD), self.row(SAVINGS)],
                        accounts=pending((CHECKING, CARD, SAVINGS)))
        self.assert_refused(self.check(extra))
        self.assert_invalid(self.v3([self.row(CHECKING), self.row(CHECKING, operation_id="again"), self.row(CARD)]),
                            "more than one authorization")

    def test_the_account_set_is_the_protected_scope_that_the_entry_posts_to(self) -> None:
        only_checking = self.v3([self.row(CHECKING)], accounts=pending((CHECKING,)))
        both = self.v3(accounts=pending((CHECKING,)))
        for label, mapped in {"typed or mapped card": {CHECKING, CARD}, "unprotected card": {CHECKING}}.items():
            with self.subTest(label):
                one = checker().check_ledger_delta(only_checking, [], [self.entry], mapped)
                two = checker().check_ledger_delta(both, [], [self.entry], mapped)
                if CARD in mapped:
                    # The card is protected, has no pending start and no authorization: refused both ways.
                    self.assert_refused(one)
                    self.assert_refused(two)
                else:
                    self.assertEqual((one["status"], one["openings_matched"]), ("ok", [f"review-{CHECKING}"]))
                    self.assert_refused(two)  # an authorization for an account that is not protected

    def test_the_policy_row_must_be_the_authorized_pending_opening(self) -> None:
        cases = {
            "stale version": pending(version=3),
            "other start": pending(start="2026-07-02"),
            "authorized": {**pending(), CARD: {"state": "authorized", "start_day": C, "version": 2}},
            "unset": {**pending(), CARD: {"state": "unset"}},
            "absent": pending((CHECKING,)),
        }
        for label, accounts in cases.items():
            with self.subTest(label):
                result = self.check(self.v3(accounts=accounts))
                self.assert_refused(result)

    # -- the entry ----------------------------------------------------------------------------------------------

    def test_any_other_entry_date_is_refused(self) -> None:
        for label, day in {"cutover day": date(2026, 7, 1), "two days before": date(2026, 6, 29)}.items():
            with self.subTest(label):
                entry = self.changed(date=day)
                result = self.check(self.v3(entry=entry), [entry])
                self.assert_refused(result)
                self.assertIn("2026-06-30", json.dumps(result["refusals"]))

    def test_another_entry_or_a_changed_entry_is_refused(self) -> None:
        moved = {CHECKING: Decimal("1.00"), EQUITY: Decimal("-1.00")}
        postings = tuple(dataclasses.replace(p, amount=p.amount + moved.get(p.account, 0)) for p in self.entry.postings)
        cases = {
            "posting amounts": self.changed(postings=postings),
            "narration": self.changed(narration="Opening balances (edited)"),
        }
        for label, entry in cases.items():
            with self.subTest(label):
                self.assert_refused(self.check(self.v3(), [entry]))  # authorized for the original entry
        # The rows name the native plan ID; an entry with another source ID is refused even with its own fingerprint.
        source = self.with_meta(source_id="other-source")
        rows = [self.row(account, source, entry_source_id=self.entry.source_id) for account in (CHECKING, CARD)]
        self.assert_refused(self.check(self.v3(rows), [source]))

    def test_only_fresh_accrual_opening_metadata_passes(self) -> None:
        cases = {
            "tracking opening": self.with_meta(import_source="accrual-tracking-opening", accrual_opening=None,
                                               accrual_tracking_opening="true", coverage_id="tracking"),
            "QuickBooks opening": self.with_meta(import_source="quickbooks-opening"),
            "other coverage": self.with_meta(coverage_id="tracking"),
            "other cutover": self.with_meta(cutover_date="2026-07-02"),
            "summary keys": self.with_meta(summary_from="2026-01-01"),
            "no marker": self.with_meta(accrual_opening=None),
        }
        for label, entry in cases.items():
            with self.subTest(label):
                self.assert_refused(self.check(self.v3(entry=entry), [entry]))

    def test_a_zero_line_on_a_protected_account_is_refused(self) -> None:
        entry = self.changed(postings=(*self.entry.postings, Posting(CHECKING, Decimal("0.00"), "USD")))
        self.assert_refused(self.check(self.v3(entry=entry), [entry]))

    def test_history_or_a_second_entry_is_refused(self) -> None:
        other = Entry(date=date(2026, 6, 1), narration="Synthetic earlier", meta=(("source-id", "earlier"),),
                      postings=(Posting("Expenses:Software", Decimal("5.00"), "USD"),
                                Posting(EQUITY, Decimal("-5.00"), "USD")))
        self.assert_refused(self.check(self.v3(), base=[other]))
        self.assert_refused(self.check(self.v3(), [self.entry, other]))  # the second entry is outside the scope
        self.assert_refused(self.check(self.v3(), []))

    def test_an_ordinary_row_before_the_start_keeps_its_refusal(self) -> None:
        row = Entry(date=date(2026, 6, 29), narration="Synthetic", meta=(("source-id", "banksync:prior"),),
                    postings=(Posting(CHECKING, Decimal("1.00"), "USD"), Posting(EQUITY, Decimal("-1.00"), "USD")))
        result = self.check(self.v3(), [self.entry, row])
        self.assert_refused(result)
        self.assertIn((OPENING_REQUIRED, "banksync:prior"),
                      [(r["code"], r["source_id"]) for r in result["refusals"]])

    # -- request shape ------------------------------------------------------------------------------------------

    def test_a_bad_version_3_request_is_invalid(self) -> None:
        good = self.row(CHECKING)
        self.assert_invalid(self.v3([]), "needs its opening authorizations")
        self.assert_invalid(dict(self.v3(), policy=dict(policy(), mode="legacy")), "enforced policy")
        self.assert_invalid(self.v3([good, self.row(CARD, start_day="2026-07-02")]), "same start_day")
        self.assert_invalid(self.v3([good, self.row(CARD, entry_source_id="other")]), "same start_day")
        self.assert_invalid(self.v3([good, self.row(CARD, entry_fingerprint="0" * 64)]), "same start_day")
        for released in ("statement_opening", "starting_balance", "quickbooks_sync_opening", "accrual_opening"):
            with self.subTest(released):
                self.assert_invalid(self.v3([dict(good, operation=released)]), "schema_version")
        for day in ("0001-01-01", "20260701", "2026-W27-3", "2026-7-01"):
            with self.subTest(day):
                self.assert_invalid(self.v3([dict(good, start_day=day)]), "start_day")
        self.assert_invalid(self.v3([dict(good, postings=[])]), "exactly the fields")
        self.assert_invalid(self.v3([dict(good, entry_date="2026-06-30")]), "exactly the fields")
        self.assert_invalid(dict(self.v3(), scope=[CHECKING, float("nan")]), "non-finite")
        for version in (0, 4, "3", True, 3.0, None):
            with self.subTest(version=version):
                self.assert_invalid(dict(self.v3(), schema_version=version),
                                    "must be 2 (or 1, the weaker released version), or 3 for the "
                                    "accrual_opening_prior_day operation only")

    def test_versions_1_and_2_do_not_read_the_prior_day_operation(self) -> None:
        row = self.row(CHECKING)
        self.assert_invalid(dict(self.v3([row]), schema_version=2), "operation must be one of")
        v1 = {k: v for k, v in row.items() if k != "entry_fingerprint"}
        v1["postings"] = [[p.account, str(p.amount), p.currency] for p in self.entry.postings]
        self.assert_invalid(dict(self.v3([v1]), schema_version=1), "operation must be one of")
        self.assertEqual(checker().REQUEST_SCHEMA_VERSION, 1)
        self.assertEqual(checker().FULL_ENTRY_REQUEST_SCHEMA_VERSION, 2)
        self.assertEqual(checker().PRIOR_DAY_REQUEST_SCHEMA_VERSION, 3)
        self.assertEqual(date.fromisoformat(C) - timedelta(days=1), self.entry.date)


if __name__ == "__main__":
    unittest.main()
