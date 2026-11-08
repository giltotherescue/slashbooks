"""A books start policy fences live postings at the ledger write.

The host (for example a hosted service) passes a books start policy to a mutating command with the
environment variable ``SLASHBOOKS_BOOKS_POLICY``: the path of a JSON document

    {"schema_version": 1, "mode": "enforced", "epoch": 1,
     "accounts": {"Assets:Bank:Checking": {"state": "authorized", "start_day": "2026-03-15", "version": 1}}}

Without the variable the engine keeps its earlier behavior. Synthetic company: a QuickBooks migration with a
mid-month cutover S = 2026-03-15. The real opening operation (``qb sync-opening --apply``) records Checking
+100.00 on S. A bank feed then brings five rows from Jan 1, so three rows are dated before S. The account
catalog still opens Checking on 2026-01-01, so the open-date rule alone does not refuse them. The correct
Checking balance is 100.00 - 25.00 - 19.00 = 56.00.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bookkeeping import queue  # noqa: E402
from bookkeeping import quickbooks_sync as qb_sync  # noqa: E402
from bookkeeping.automation_policy import ExecutionContext, PreparationDenied, StandingPolicy  # noqa: E402
from bookkeeping.entity import init_entity, load_entity  # noqa: E402
from bookkeeping.ledger.importer import _atomic_ledger_write, import_transactions, reverse_entry  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.preparation_runtime import evidence_hash, run_preparation_command  # noqa: E402

ENV = "SLASHBOOKS_BOOKS_POLICY"
S = "2026-03-15"
CHECKING = "Assets:Bank:Checking"
FEES = "Expenses:Fees"
PAYEE = "SYNTHETIC SUPPLY CO"
FIXTURE = Path(__file__).parent / "fixtures" / "quickbooks_api" / "snapshot.json"
# The engine fixture snapshot with every date moved so that the cutover is S and the prior-day reports are on S - 1.
SHIFT = {"2025-01-01": "2025-03-15", "2025-12-31": "2026-03-14", "2026-01-01": "2026-03-15",
         "2026-01-15": "2026-03-29", "2026-01-31": "2026-04-14", "2026-02-01T12:00:00Z": "2026-04-15T12:00:00Z"}
ROWS = (  # (id, date, amount)
    ("jan10", "2026-01-10", "-30.00"),
    ("mar01", "2026-03-01", "-40.00"),
    ("mar14", "2026-03-14", "-12.00"),
    ("mar15", "2026-03-15", "-25.00"),
    ("apr02", "2026-04-02", "-19.00"),
)
BEFORE_S = ("jan10", "mar01", "mar14")
ON_OR_AFTER_S = ("mar15", "apr02")

UNSET = "BOOKS_START_UNSET"
OPENING_REQUIRED = "OPENING_REQUIRED"
BEFORE_START = "BOOKS_DATE_BEFORE_AUTHORIZED_START"
POLICY_REQUIRED = "BOOKS_POLICY_REQUIRED"
POLICY_INVALID = "BOOKS_POLICY_INVALID"


def row(sid: str, day: str, amount: str, feed: str = "feed-1") -> dict:
    return {"id": sid, "accountId": feed, "accountName": "Synthetic account", "accountType": "checking",
            "date": day, "amount": amount, "currency": "USD", "description": PAYEE, "pending": False}


def enforced(accounts: dict, epoch: int = 1) -> dict:
    return {"schema_version": 1, "mode": "enforced", "epoch": epoch, "accounts": accounts}


def authorized(start: str = S, version: int = 1) -> dict:
    return {"state": "authorized", "start_day": start, "version": version}


def refusal_code(error: BaseException) -> str | None:
    """The refusal code of an engine error, or ``None`` for a generic exception (which fails the test)."""
    return getattr(error, "code", None) or getattr(error, "reason", None)


class Company(unittest.TestCase):
    """Shared helpers. Each test class builds its own synthetic company."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "books"
        init_entity(self.path, name="Synthetic Mid-Month Books")

    def map_feed(self, feed: str, account: str) -> None:
        config_path = self.path / "entity.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.setdefault("bank_account_mappings", {})[feed] = account
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        self.entity = load_entity(self.path)

    def stage(self, rows) -> None:
        import_transactions(self.entity, list(rows), "intake", intake_only=True)

    @property
    def store(self) -> LedgerStore:
        return LedgerStore(self.path / "ledger.sqlite")

    @contextmanager
    def policy(self, document):
        """Run the block with ``document`` as the books start policy (a dict, or raw text for a bad file)."""
        policy_path = self.root / f"policy-{id(document)}.json"
        policy_path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
        with patch.dict(os.environ, {ENV: str(policy_path)}):
            yield

    def balance(self, account: str = CHECKING) -> Decimal:
        return sum((p.amount for e in self.store.load_entries() for p in e.postings if p.account == account),
                   Decimal("0.00"))

    def booked(self) -> list[str]:
        """Bank row IDs in the books, one item for each entry, so a row booked twice appears twice."""
        ids = [e.source_id for e in self.store.load_entries() if e.source_id]
        return sorted(sid for sid in ids if sid in {r[0] for r in ROWS} or sid.startswith("row-"))

    def pending(self) -> dict[str, dict]:
        path = self.path / "staging" / "pending-categorization.json"
        return {item["id"]: item for item in json.loads(path.read_text(encoding="utf-8"))} if path.exists() else {}

    def snapshot(self) -> tuple[str, int]:
        return self.store.content_digest(), len(self.store.load_entries())

    def person_confirm(self, sid: str, category: str = FEES) -> dict:
        queue.propose(self.entity, sid, category, "Synthetic person choice")
        return queue.confirm(self.entity, sid, "person-review")

    def assert_refused(self, call, code: str, *, account: str = CHECKING, sid: str | None = None):
        """``call`` raises an engine refusal with exactly ``code``, writes nothing, and the row stays in review."""
        before = self.snapshot()
        with self.assertRaises(Exception) as caught:
            call()
        self.assertEqual(refusal_code(caught.exception), code, f"expected {code}, got {caught.exception!r}")
        self.assertEqual(self.snapshot(), before, "a refusal must write nothing")
        if sid is not None:
            self.assertIn(sid, self.pending())
            self.assertNotIn(sid, self.booked())
        return caught.exception


class MidMonthQuickBooksTests(Company):
    """A QuickBooks migration with a cutover on S, through the real ``qb sync-opening --apply``."""

    def setUp(self) -> None:
        super().setUp()
        text = FIXTURE.read_text(encoding="utf-8")
        for old, new in SHIFT.items():
            text = text.replace(f'"{old}"', f'"{new}"')
        self.snapshot_path = self.root / "quickbooks.json"
        self.snapshot_path.write_text(text, encoding="utf-8")

    def open_books(self) -> None:
        """The real opening operation: Checking +100.00 on S. The catalog still opens Checking on 2026-01-01."""
        applied = qb_sync.sync_opening(load_entity(self.path), self.snapshot_path, date.fromisoformat(S), apply=True)
        self.assertTrue(applied["applied"], applied)
        self.map_feed("feed-1", CHECKING)
        self.assertEqual({o.account: o.date for o in self.store.load_opens()}[CHECKING], date(2026, 1, 1))
        self.assertEqual(self.balance(), Decimal("100.00"))
        self.stage(row(*item) for item in ROWS)

    # -- the three refusal codes ---------------------------------------------------

    def test_unset_account_refuses_every_live_posting_with_books_start_unset(self) -> None:
        self.open_books()
        with self.policy(enforced({CHECKING: {"state": "unset"}})):
            for sid, _, _ in ROWS:
                with self.subTest(sid=sid):
                    error = self.assert_refused(lambda: self.person_confirm(sid), UNSET, sid=sid)
                    self.assertEqual(error.account, CHECKING)
        self.assertEqual(self.booked(), [])
        self.assertEqual(self.balance(), Decimal("100.00"))

    def test_rows_before_the_authorized_start_are_refused_with_the_exact_code(self) -> None:
        self.open_books()
        with self.policy(enforced({CHECKING: authorized()})):
            for sid, day, _ in ROWS[:3]:
                with self.subTest(sid=sid):
                    error = self.assert_refused(lambda: self.person_confirm(sid), BEFORE_START, sid=sid)
                    detail = error.as_dict()
                    self.assertEqual((detail["account"], detail["start_day"], detail["entry_date"]), (CHECKING, S, day))
                    self.assertEqual((detail["state"], detail["policy_epoch"], detail["account_version"]),
                                     ("authorized", 1, 1))

    def test_authorized_start_allows_rows_on_and_after_the_start(self) -> None:
        self.open_books()
        with self.policy(enforced({CHECKING: authorized()})):
            for sid in ON_OR_AFTER_S:
                self.person_confirm(sid)
        self.assertEqual(self.booked(), sorted(ON_OR_AFTER_S))

    # -- the mid-month overlap ---------------------------------------------------

    def test_mid_month_overlap_books_each_valid_row_exactly_once_and_checking_is_56(self) -> None:
        self.open_books()
        refused = {}
        with self.policy(enforced({CHECKING: authorized()})):
            for sid, _, _ in ROWS:
                try:
                    self.person_confirm(sid)
                except Exception as error:  # noqa: BLE001 - the code is asserted below
                    refused[sid] = refusal_code(error)
            # A second attempt at a valid row is not a second booking.
            with self.assertRaises(Exception):
                self.person_confirm("mar15")
        self.assertEqual(refused, {sid: BEFORE_START for sid in BEFORE_S})
        self.assertEqual(self.booked(), ["apr02", "mar15"])
        self.assertEqual(self.balance(), Decimal("56.00"))
        for sid in BEFORE_S:
            self.assertIn(sid, self.pending())

    def test_standing_automation_is_held_with_the_same_code(self) -> None:
        self.open_books()
        with self.policy(enforced({CHECKING: authorized()})):
            for sid in BEFORE_S:
                with self.subTest(sid=sid):
                    error = self.assert_refused(lambda: self.automation_confirm(sid), BEFORE_START, sid=sid)
                    self.assertIsInstance(error, PreparationDenied)
            for sid in ON_OR_AFTER_S:
                self.assertEqual(self.automation_confirm(sid), 0)
        self.assertEqual(self.booked(), ["apr02", "mar15"])
        self.assertEqual(self.balance(), Decimal("56.00"))

    def automation_confirm(self, sid: str) -> int:
        policy = StandingPolicy.from_document(dict(
            schema_version=1, policy_id="synthetic-policy", version=1, entity_key="synthetic-company",
            start_period="2026-01", end_period="2026-12", actions=["categorize"], held_source_ids=[],
            held_entry_ids=[]))
        context = ExecutionContext("synthetic-company", "synthetic-execution", policy, 1, 4, 4,
                                   datetime.now(timezone.utc) + timedelta(minutes=5))
        queue.propose(self.entity, sid, FEES, "Synthetic standing policy")
        with redirect_stdout(StringIO()):
            return run_preparation_command(
                ["queue", "confirm", "--entity", str(self.path), "--item", sid], context, entity_path=self.path,
                expected_ledger_digest=self.store.content_digest(),
                source_hashes={sid: evidence_hash(self.pending()[sid])})

    def test_saved_rule_plan_names_the_policy_code(self) -> None:
        from bookkeeping.routine import routine_plan
        self.open_books()
        key = queue._counterparty_key(PAYEE, "")
        rules = dict(version=1, enabled=True, review_categories=[FEES], answer_roles=["client"], rules=[
            dict(id="rule-1", source_account_id="feed-1", counterparty_key=key, category=FEES,
                 approved_ledger_account=CHECKING)])
        with self.policy(enforced({CHECKING: authorized()})):
            plan = routine_plan(self.entity, rules, source_ids=[sid for sid, _, _ in ROWS])
        decisions = {item["source_id"]: item["reason"] for item in plan["unresolved"]}
        decisions.update({item["source_id"]: "recorded" for item in plan["actions"]})
        self.assertEqual(decisions, {"jan10": BEFORE_START, "mar01": BEFORE_START, "mar14": BEFORE_START,
                                     "mar15": "recorded", "apr02": "recorded"})

    def test_learned_categories_hold_rows_before_the_start_at_intake(self) -> None:
        self.open_books()
        learned = [row(f"row-{sid}", day, amount) for sid, day, amount in ROWS]
        with self.policy(enforced({CHECKING: authorized()})):
            result = import_transactions(self.entity, learned, "learned", lambda txn: (FEES, "learned"))
        self.assertEqual((result.new_entries, result.pending_categorization), (2, 3))
        self.assertEqual(self.booked(), ["row-apr02", "row-mar15"])
        for sid in BEFORE_S:
            self.assertEqual(self.pending()[f"row-{sid}"]["review_hold_reason"], BEFORE_START)

    def test_the_cli_prints_the_refusal_as_json(self) -> None:
        from bookkeeping import cli
        self.open_books()
        queue.propose(self.entity, "mar14", FEES, "Synthetic person choice")
        errors = StringIO()
        with self.policy(enforced({CHECKING: authorized()}, epoch=7)), redirect_stdout(StringIO()), \
                redirect_stderr(errors):
            code = cli.main(["queue", "confirm", "--entity", str(self.path), "--item", "mar14"])
        self.assertEqual(code, 1)
        error = json.loads(errors.getvalue())["error"]
        self.assertEqual({k: error[k] for k in ("code", "account", "start_day", "entry_date", "policy_epoch")},
                         {"code": BEFORE_START, "account": CHECKING, "start_day": S, "entry_date": "2026-03-14",
                          "policy_epoch": 7})
        self.assertTrue(error["message"] and error["action"])

    # -- the opening exception ---------------------------------------------------

    def test_pending_opening_refuses_on_and_after_the_start_until_the_real_opening(self) -> None:
        self.map_feed("feed-1", CHECKING)
        self.stage(row(*item) for item in ROWS)
        pending = {"state": "authorized_pending_opening", "start_day": S, "version": 1}
        with self.policy(enforced({CHECKING: pending})):
            for sid in ON_OR_AFTER_S:
                with self.subTest(sid=sid):
                    self.assert_refused(lambda: self.person_confirm(sid), OPENING_REQUIRED, sid=sid)
            # An entry that only claims to be an opening (import-source metadata) gets no exception.
            lookalike = Entry(date=date.fromisoformat(S), narration="Opening balance",
                              meta=(("source-id", "quickbooks-opening-lookalike"), ("import-source", "quickbooks-opening")),
                              postings=(Posting(CHECKING, Decimal("100.00")),
                                        Posting("Equity:Opening-Balances", Decimal("-100.00"))))
            self.assert_refused(lambda: _atomic_ledger_write(
                self.entity, [Open(date(2026, 1, 1), "Equity:Opening-Balances")], [lookalike], "lookalike", None,
                "synthetic look-alike opening"), OPENING_REQUIRED)
            # The real opening operation records the opening on S through its own call site.
            applied = qb_sync.sync_opening(self.entity, self.snapshot_path, date.fromisoformat(S), apply=True)
            self.assertTrue(applied["applied"], applied)
        self.assertEqual(self.balance(), Decimal("100.00"))
        # The host sees the opening and moves the account to authorized (a new policy version and epoch).
        with self.policy(enforced({CHECKING: authorized(version=2)}, epoch=2)):
            for sid in ON_OR_AFTER_S:
                self.person_confirm(sid)
        self.assertEqual(self.booked(), ["apr02", "mar15"])
        self.assertEqual(self.balance(), Decimal("56.00"))

    # -- exact reversal ----------------------------------------------------------

    def test_an_exact_reversal_is_allowed_and_a_forged_one_is_refused(self) -> None:
        self.open_books()
        with self.policy(enforced({CHECKING: authorized()})):
            self.person_confirm("apr02")
        # A later start leaves apr02 before it. Its exact reversal removes activity, so it is allowed.
        with self.policy(enforced({CHECKING: authorized("2026-04-10", version=2)}, epoch=2)):
            forged = Entry(date=date(2026, 4, 2), narration="Reversal of: synthetic",
                           meta=(("reverses", "apr02"),),
                           postings=(Posting(CHECKING, Decimal("10.00")), Posting(FEES, Decimal("-10.00"))))
            self.assert_refused(lambda: _atomic_ledger_write(self.entity, [], [forged], "forged", None,
                                                             "synthetic forged reversal"), BEFORE_START)
            reverse_entry(self.entity, "apr02", "correction")
        self.assertEqual(self.balance(), Decimal("100.00"))

    # -- missing or bad policy ----------------------------------------------------

    def test_enforced_policy_without_the_account_is_refused(self) -> None:
        self.open_books()
        with self.policy(enforced({"Assets:Bank:Other": authorized()})):
            error = self.assert_refused(lambda: self.person_confirm("apr02"), POLICY_REQUIRED, sid="apr02")
            self.assertEqual(error.account, CHECKING)
        with patch.dict(os.environ, {ENV: str(self.root / "missing-policy.json")}):
            self.assert_refused(lambda: self.person_confirm("apr02"), POLICY_REQUIRED, sid="apr02")
        for label, bad in {"not json": "{", "unknown state": json.dumps(enforced({CHECKING: {"state": "maybe"}})),
                           "authorized without a start": json.dumps(enforced({CHECKING: {"state": "authorized",
                                                                                           "version": 1}})),
                           "no epoch": json.dumps({"schema_version": 1, "mode": "enforced", "accounts": {}})}.items():
            with self.subTest(label), self.policy(bad):
                self.assert_refused(lambda: self.person_confirm("apr02"), POLICY_INVALID, sid="apr02")

    # -- legacy ------------------------------------------------------------------

    def test_without_a_policy_the_engine_keeps_its_earlier_behavior(self) -> None:
        self.open_books()
        self.assertNotIn(ENV, os.environ)
        for sid, _, _ in ROWS:
            self.person_confirm(sid)
        # The known legacy double count: every row books, because the catalog opens Checking on Jan 1.
        self.assertEqual(self.booked(), sorted(sid for sid, _, _ in ROWS))
        self.assertEqual(self.balance(), Decimal("-26.00"))

    def test_a_legacy_mode_policy_changes_nothing(self) -> None:
        self.open_books()
        with self.policy({"schema_version": 1, "mode": "legacy", "epoch": 0, "accounts": {}}):
            for sid, _, _ in ROWS:
                self.person_confirm(sid)
        self.assertEqual(self.balance(), Decimal("-26.00"))


class CustomChartTests(Company):
    """Custom chart names: the scope comes from mappings and the policy, never from name prefixes."""

    OPERATING = "Assets:Treasury:Operating-4521"   # a bank account with no "Bank" in its name
    CARD = "Liabilities:Amex-Platinum"              # a card account with no "Card" in its name

    def setUp(self) -> None:
        super().setUp()
        with self.store.transaction() as conn:
            self.store.insert_opens([Open(date(2026, 1, 1), self.OPERATING, currencies=("USD",)),
                                     Open(date(2026, 1, 1), self.CARD, currencies=("USD",))], conn)
        self.map_feed("feed-op", self.OPERATING)
        self.map_feed("feed-card", self.CARD)

    def test_a_mapped_account_with_a_custom_name_is_in_scope(self) -> None:
        self.stage([row("row-op-feb", "2026-02-10", "-30.00", "feed-op"), row("row-op-apr", "2026-04-10", "-5.00", "feed-op")])
        with self.policy(enforced({self.OPERATING: authorized(), self.CARD: authorized()})):
            error = self.assert_refused(lambda: self.person_confirm("row-op-feb"), BEFORE_START, account=self.OPERATING,
                                        sid="row-op-feb")
            self.assertEqual(error.account, self.OPERATING)
            self.person_confirm("row-op-apr")
        self.assertEqual(self.booked(), ["row-op-apr"])

    def test_a_transfer_checks_both_sides(self) -> None:
        # A payment from the operating account to the card on 2026-03-10.
        self.stage([row("row-pay-out", "2026-03-10", "-500.00", "feed-op"),
                    row("row-pay-in", "2026-03-10", "500.00", "feed-card")])
        item = queue.propose_transfer(self.entity, ["row-pay-out", "row-pay-in"], "Synthetic card payment")
        cases = {
            "card side before its start": (enforced({self.OPERATING: authorized("2026-01-01"),
                                                     self.CARD: authorized()}), BEFORE_START, self.CARD),
            "operating side before its start": (enforced({self.OPERATING: authorized(),
                                                          self.CARD: authorized("2026-01-01")}), BEFORE_START, self.OPERATING),
            "card side unset": (enforced({self.OPERATING: authorized("2026-01-01"), self.CARD: {"state": "unset"}}),
                                UNSET, self.CARD),
            "card side missing from the policy": (enforced({self.OPERATING: authorized("2026-01-01")}),
                                                  POLICY_REQUIRED, self.CARD),
        }
        for label, (document, code, account) in cases.items():
            with self.subTest(label), self.policy(document):
                error = self.assert_refused(
                    lambda: queue.confirm_transfer(self.entity, item["source_id"], "person-review"), code)
                self.assertEqual(error.account, account)
        with self.policy(enforced({self.OPERATING: authorized("2026-01-01"), self.CARD: authorized("2026-03-01")})):
            queue.confirm_transfer(self.entity, item["source_id"], "person-review")
        self.assertEqual(self.balance(self.OPERATING), Decimal("-500.00"))
        self.assertEqual(self.balance(self.CARD), Decimal("500.00"))

    def test_an_account_named_only_by_the_policy_is_checked(self) -> None:
        # The category is a policy account here, for example a loan the host scopes from its chart metadata.
        loan = "Liabilities:Owner-Note"
        with self.store.transaction() as conn:
            self.store.insert_opens([Open(date(2026, 1, 1), loan, currencies=("USD",))], conn)
        self.stage([row("row-op-apr", "2026-04-10", "-5.00", "feed-op")])
        with self.policy(enforced({self.OPERATING: authorized(), self.CARD: authorized(),
                                   loan: {"state": "unset"}})):
            error = self.assert_refused(lambda: self.person_confirm("row-op-apr", loan), UNSET, sid="row-op-apr")
            self.assertEqual(error.account, loan)
            # A category that neither a mapping nor the policy names is not in scope.
            self.person_confirm("row-op-apr", FEES)
        self.assertEqual(self.booked(), ["row-op-apr"])


if __name__ == "__main__":
    unittest.main()
