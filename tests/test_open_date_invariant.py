"""No ledger write posts to an account before its open date.

Synthetic company: Assets:Bank:Checking opens on S (2026-01-01) with an opening balance of
100.00 on S that already includes the activity before S. Bank rows dated before S, on S and
after S wait in review. Every recording path must make the same decision as the saved-rule
path (``routine``): rows before S stay in review with ``source_catalog_conflict``; rows on
and after S are recorded. The correct Checking balance is 100.00 - 25.00 - 19.00 = 56.00.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bookkeeping import queue  # noqa: E402
from bookkeeping.automation_policy import ExecutionContext, PreparationDenied, StandingPolicy  # noqa: E402
from bookkeeping.entity import add_account, load_entity  # noqa: E402
from bookkeeping.ledger import importer  # noqa: E402
from bookkeeping.ledger.importer import (  # noqa: E402
    _atomic_ledger_write, import_transactions, reverse_and_correct, reverse_entry,
)
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.open_dates import AccountNotOpenError  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.preparation_runtime import evidence_hash, run_preparation_command  # noqa: E402
from bookkeeping.routine import routine_plan  # noqa: E402

S = date(2026, 1, 1)
BANK = "Assets:Bank:Checking"
EQUITY = "Equity:Opening-Balances"
FEES = "Expenses:Fees"
SUPPLIES = "Expenses:Supplies"
LATE = "Expenses:Late-Category"  # a category that opens after S
PAYEE = "SYNTHETIC SUPPLY CO"
ROWS = (  # (id, date, amount)
    ("pre-dec15", "2025-12-15", "-40.00"),
    ("pre-dec31", "2025-12-31", "-12.00"),
    ("on-s", "2026-01-01", "-25.00"),
    ("post-jan10", "2026-01-10", "-19.00"),
)
BEFORE = ("pre-dec15", "pre-dec31")
ON_OR_AFTER = ("on-s", "post-jan10")


def row(sid: str, day: str, amount: str) -> dict:
    return {"id": sid, "accountId": "feed-1", "accountName": "Synthetic checking", "accountType": "checking",
            "date": day, "amount": amount, "currency": "USD", "description": PAYEE, "pending": False}


class OpenDateInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "company"
        for sub in ("staging", "reports", "review-queue", "learned-context"):
            (self.path / sub).mkdir(parents=True)
        config = {"name": "Synthetic Company", "business_type": "consulting",
                  "bank_account_mappings": {"feed-1": BANK}}
        (self.path / "entity.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        (self.path / "trust-policy.json").write_text(
            json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n", encoding="utf-8")
        self.store = LedgerStore(self.path / "ledger.sqlite")
        self.store.initialize()
        with self.store.transaction() as conn:
            self.store.set_meta("canonical", "true", conn)
            self.store.insert_opens([
                Open(date(2000, 1, 1), FEES, currencies=("USD",)),
                Open(date(2000, 1, 1), SUPPLIES, currencies=("USD",)),
                Open(date(2026, 2, 1), LATE, currencies=("USD",)),
            ], conn)
        self.entity = load_entity(self.path)
        # The opening on S already contains every row before S.
        _atomic_ledger_write(self.entity, [Open(S, BANK, currencies=("USD",)), Open(S, EQUITY)], [Entry(
            date=S, narration="Opening balance", meta=(("source-id", "synthetic-opening"),
                                                       ("import-source", "quickbooks-opening")),
            postings=(Posting(BANK, Decimal("100.00")), Posting(EQUITY, Decimal("-100.00"))))],
            "opening", None, "synthetic opening")
        import_transactions(self.entity, [row(*item) for item in ROWS], "intake", intake_only=True)

    # -- helpers ---------------------------------------------------------------

    def balance(self, account: str = BANK) -> Decimal:
        return sum((p.amount for e in self.store.load_entries() for p in e.postings if p.account == account),
                   Decimal("0.00"))

    def posted(self) -> set[str]:
        return {e.source_id for e in self.store.load_entries() if e.source_id} - {"synthetic-opening"}

    def pending(self) -> dict[str, dict]:
        path = self.path / "staging" / "pending-categorization.json"
        return {item["id"]: item for item in json.loads(path.read_text(encoding="utf-8"))}

    def snapshot(self) -> tuple[str, int, dict]:
        return self.store.content_digest(), len(self.store.load_audit_events()), self.pending()

    def person_confirm(self, sid: str, category: str = FEES) -> dict:
        queue.propose(self.entity, sid, category, "Synthetic person choice")
        return queue.confirm(self.entity, sid, "person-review")

    def authority(self, start_period: str) -> ExecutionContext:
        policy = StandingPolicy.from_document(dict(
            schema_version=1, policy_id="synthetic-policy", version=1, entity_key="synthetic-company",
            start_period=start_period, end_period="2026-12", actions=["categorize"],
            held_source_ids=[], held_entry_ids=[]))
        return ExecutionContext("synthetic-company", "synthetic-execution", policy, 1, 4, 4,
                                datetime.now(timezone.utc) + timedelta(minutes=5))

    def automation_confirm(self, sid: str, start_period: str = "2025-12") -> int:
        queue.propose(self.entity, sid, FEES, "Synthetic standing policy")
        source = self.pending()[sid]
        with redirect_stdout(StringIO()):
            return run_preparation_command(
                ["queue", "confirm", "--entity", str(self.path), "--item", sid], self.authority(start_period),
                entity_path=self.path, expected_ledger_digest=self.store.content_digest(),
                source_hashes={sid: evidence_hash(source)})

    def assert_held(self, sid: str) -> None:
        self.assertNotIn(sid, self.posted())
        self.assertIn(sid, self.pending())
        self.assertIn(queue.read_queue_item(self.entity, sid)["status"], {"open", "reopened"})

    # -- person confirmation -----------------------------------------------------

    def test_person_confirmation_refuses_rows_before_the_open_date_and_keeps_them_in_review(self) -> None:
        for sid in BEFORE:
            with self.subTest(sid=sid):
                before = self.snapshot()
                with self.assertRaises(AccountNotOpenError) as caught:
                    self.person_confirm(sid)
                self.assertEqual(caught.exception.code, "source_catalog_conflict")
                self.assertEqual(caught.exception.account, BANK)
                self.assertEqual(caught.exception.open_date, S)
                self.assertIn("stays in review", str(caught.exception))
                self.assertIn("books entity account-add", caught.exception.action)
                self.assertEqual(self.snapshot()[:2], before[:2])
                self.assert_held(sid)
        for sid in ON_OR_AFTER:
            self.assertEqual(self.person_confirm(sid)["status"], "confirmed")
        self.assertEqual(self.posted(), set(ON_OR_AFTER))
        self.assertEqual(self.balance(), Decimal("56.00"))

    def test_replay_of_a_refused_confirmation_refuses_again_and_writes_nothing(self) -> None:
        queue.propose(self.entity, "pre-dec15", FEES, "Synthetic person choice")
        before = self.snapshot()
        messages = []
        for _ in range(3):
            with self.assertRaises(AccountNotOpenError) as caught:
                queue.confirm(self.entity, "pre-dec15", "person-review")
            messages.append((caught.exception.code, str(caught.exception)))
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(set(messages)), 1)
        self.assert_held("pre-dec15")

    def test_the_cli_names_the_reason_code(self) -> None:
        from contextlib import redirect_stderr
        from bookkeeping import cli
        queue.propose(self.entity, "pre-dec31", FEES, "Synthetic person choice")
        errors = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(errors):
            code = cli.main(["queue", "confirm", "--entity", str(self.path), "--item", "pre-dec31"])
        self.assertEqual(code, 1)
        error = json.loads(errors.getvalue())["error"]
        self.assertEqual(error["code"], "source_catalog_conflict")
        self.assertIn("--open-date", error["action"])
        self.assert_held("pre-dec31")

    def test_rows_queued_before_the_opening_was_recorded_are_refused_at_confirmation(self) -> None:
        # A second company: the rows are staged and proposed first, then the account opens on S.
        other = Path(self.tmp.name) / "late-opening"
        for sub in ("staging", "reports", "review-queue", "learned-context"):
            (other / sub).mkdir(parents=True)
        (other / "entity.json").write_text(json.dumps({"name": "Synthetic Company", "business_type": "consulting",
                                                       "bank_account_mappings": {"feed-1": BANK}}), encoding="utf-8")
        store = LedgerStore(other / "ledger.sqlite")
        store.initialize()
        with store.transaction() as conn:
            store.set_meta("canonical", "true", conn)
            store.insert_opens([Open(date(2000, 1, 1), FEES, currencies=("USD",))], conn)
        entity = load_entity(other)
        import_transactions(entity, [row(*item) for item in ROWS], "intake", intake_only=True)
        for sid, _, _ in ROWS:
            queue.propose(entity, sid, FEES, "Queued before the opening")
        _atomic_ledger_write(entity, [Open(S, BANK, currencies=("USD",)), Open(S, EQUITY)], [Entry(
            date=S, narration="Opening balance", meta=(("source-id", "synthetic-opening"),),
            postings=(Posting(BANK, Decimal("100.00")), Posting(EQUITY, Decimal("-100.00"))))],
            "opening", None, "synthetic opening")
        for sid in BEFORE:
            with self.subTest(sid=sid), self.assertRaises(AccountNotOpenError):
                queue.confirm(entity, sid, "person-review")
        for sid in ON_OR_AFTER:
            queue.confirm(entity, sid, "person-review")
        self.assertEqual({e.source_id for e in store.load_entries()} - {"synthetic-opening"}, set(ON_OR_AFTER))

    def test_a_legitimate_open_date_change_is_a_separate_command(self) -> None:
        with self.assertRaises(AccountNotOpenError):
            self.person_confirm("pre-dec15")
        add_account(self.path, BANK, open_date="2025-12-01")  # the person moves the open date first
        self.assertEqual(queue.confirm(self.entity, "pre-dec15", "person-review")["status"], "confirmed")
        self.assertIn("pre-dec15", self.posted())

    # -- group and automation confirmation --------------------------------------

    def test_group_confirmation_refuses_the_whole_group_before_any_write(self) -> None:
        queue.propose_group(self.entity, [sid for sid, _, _ in ROWS], FEES, "Synthetic group")
        before = self.snapshot()
        with self.assertRaises(AccountNotOpenError) as caught:
            queue.confirm_group(self.entity, FEES, "person-review")
        self.assertEqual(caught.exception.code, "source_catalog_conflict")
        self.assertEqual(self.snapshot(), before)
        for sid, _, _ in ROWS:
            self.assert_held(sid)

    def test_automation_confirmation_holds_rows_before_the_open_date(self) -> None:
        for sid in BEFORE:
            with self.subTest(sid=sid):
                before = self.store.content_digest()
                with self.assertRaises(PreparationDenied) as caught:
                    self.automation_confirm(sid)
                self.assertEqual(caught.exception.reason, "source_catalog_conflict")
                self.assertEqual(self.store.content_digest(), before)
                self.assert_held(sid)
        for sid in ON_OR_AFTER:
            self.assertEqual(self.automation_confirm(sid), 0)
        self.assertEqual(self.posted(), set(ON_OR_AFTER))
        self.assertEqual(self.balance(), Decimal("56.00"))

    # -- multi-posting entries ---------------------------------------------------

    def test_a_split_with_one_posting_before_its_account_opens_is_refused_whole(self) -> None:
        queue.propose_split(self.entity, "on-s", "Synthetic split", [f"{FEES}=15.00", f"{LATE}=10.00"])
        before = self.snapshot()
        with self.assertRaises(AccountNotOpenError) as caught:
            queue.confirm_split(self.entity, "on-s", "person-review")
        self.assertEqual((caught.exception.code, caught.exception.account), ("category_catalog_conflict", LATE))
        self.assertEqual(self.snapshot(), before)
        self.assert_held("on-s")

    def test_a_direct_ledger_write_with_one_early_posting_writes_nothing(self) -> None:
        before = self.snapshot()
        entry = Entry(date=date(2026, 1, 15), narration="Synthetic", meta=(("source-id", "direct-1"),),
                      postings=(Posting(BANK, Decimal("-30.00")), Posting(FEES, Decimal("20.00")),
                                Posting(LATE, Decimal("10.00"))))
        with self.assertRaises(AccountNotOpenError) as caught:
            _atomic_ledger_write(self.entity, [], [entry], "direct", None, "synthetic direct write")
        self.assertEqual(caught.exception.code, "category_catalog_conflict")
        self.assertEqual(self.snapshot(), before)

    # -- corrections -------------------------------------------------------------

    def test_correction_cannot_move_a_bank_row_before_the_open_date(self) -> None:
        self.person_confirm("post-jan10")
        before = self.snapshot()
        early = Entry(date=date(2025, 12, 20), narration=PAYEE, meta=(("source-id", "post-jan10-fix"),),
                      postings=(Posting(BANK, Decimal("-19.00")), Posting(SUPPLIES, Decimal("19.00"))))
        split = Entry(date=date(2026, 1, 10), narration=PAYEE, meta=(("source-id", "post-jan10-fix"),),
                      postings=(Posting(BANK, Decimal("-19.00")), Posting(SUPPLIES, Decimal("9.00")),
                                Posting(LATE, Decimal("10.00"))))
        for corrected, code in ((early, "source_catalog_conflict"), (split, "category_catalog_conflict")):
            with self.subTest(code=code):
                with self.assertRaises(AccountNotOpenError) as caught:
                    reverse_and_correct(self.entity, "post-jan10", corrected, "correction")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.snapshot(), before)  # no reversal without its replacement
        valid = Entry(date=date(2026, 1, 10), narration=PAYEE, meta=(("source-id", "post-jan10-fix"),),
                      postings=(Posting(BANK, Decimal("-19.00")), Posting(SUPPLIES, Decimal("19.00"))))
        reverse_and_correct(self.entity, "post-jan10", valid, "correction")
        self.assertEqual(self.balance(), Decimal("81.00"))
        self.assertEqual(self.balance(SUPPLIES), Decimal("19.00"))

    def test_a_reversal_removes_an_early_posting_recorded_before_this_rule(self) -> None:
        queue.propose(self.entity, "pre-dec15", FEES, "Synthetic person choice")
        with patch.object(importer, "_refuse_postings_before_open"):  # the books as an older version wrote them
            queue.confirm(self.entity, "pre-dec15", "person-review")
        self.assertEqual(self.balance(), Decimal("60.00"))
        reverse_entry(self.entity, "pre-dec15", "correction")
        self.assertEqual(self.balance(), Decimal("100.00"))

    def record_early_posting(self) -> None:
        queue.propose(self.entity, "pre-dec15", FEES, "Synthetic person choice")
        with patch.object(importer, "_refuse_postings_before_open"):  # the books as an older version wrote them
            queue.confirm(self.entity, "pre-dec15", "person-review")

    def test_an_entry_that_only_claims_to_be_a_reversal_is_refused(self) -> None:
        self.record_early_posting()
        day = date(2025, 12, 15)
        forged = {
            "wrong accounts": (day, ((BANK, "40.00"), (SUPPLIES, "-40.00"))),
            "partial amount": (day, ((BANK, "20.00"), (FEES, "-20.00"))),
            "wrong date": (date(2025, 12, 20), ((BANK, "40.00"), (FEES, "-40.00"))),
            "same direction": (day, ((BANK, "-40.00"), (FEES, "40.00"))),
        }
        for label, (when, postings) in forged.items():
            with self.subTest(label):
                before = self.snapshot()
                entry = Entry(date=when, narration="Reversal of: synthetic", meta=(("reverses", "pre-dec15"),),
                              postings=tuple(Posting(account, Decimal(amount)) for account, amount in postings))
                with self.assertRaises(AccountNotOpenError):
                    _atomic_ledger_write(self.entity, [], [entry], "forged", None, "synthetic forged reversal")
                self.assertEqual(self.snapshot(), before)
        # A second reversal of the same entry does not remove anything, so it is not a reversal either.
        exact = Entry(date=day, narration="Reversal of: synthetic", meta=(("reverses", "pre-dec15"),),
                      postings=(Posting(BANK, Decimal("40.00")), Posting(FEES, Decimal("-40.00"))))
        _atomic_ledger_write(self.entity, [], [exact], "reversal", None, "synthetic exact reversal")
        self.assertEqual(self.balance(), Decimal("100.00"))
        with self.assertRaises(AccountNotOpenError):
            _atomic_ledger_write(self.entity, [], [exact], "reversal-again", None, "synthetic second reversal")
        self.assertEqual(self.balance(), Decimal("100.00"))

    def test_a_write_cannot_move_an_open_date_earlier(self) -> None:
        early = Entry(date=date(2025, 12, 15), narration=PAYEE, meta=(("source-id", "backdated-1"),),
                      postings=(Posting(BANK, Decimal("-40.00")), Posting(FEES, Decimal("40.00"))))
        on_s = Entry(date=S, narration=PAYEE, meta=(("source-id", "backdated-2"),),
                     postings=(Posting(BANK, Decimal("-5.00")), Posting(FEES, Decimal("5.00"))))
        for entries in ([early], [on_s]):
            with self.subTest(day=entries[0].date.isoformat()):
                before = self.snapshot()
                with self.assertRaises(ValueError) as caught:
                    _atomic_ledger_write(self.entity, [Open(date(2025, 1, 1), BANK, currencies=("USD",))], entries,
                                         "backdated", None, "synthetic backdated open")
                self.assertEqual(getattr(caught.exception, "code", None), "open_date_change_not_authorized")
                self.assertEqual(self.snapshot(), before)
        self.assertEqual({o.account: o.date for o in self.store.load_opens()}[BANK], S)
        # Ordinary confirmations and corrections still work, and the explicit command still moves the date.
        self.person_confirm("on-s")
        add_account(self.path, BANK, open_date="2025-12-01")
        self.assertEqual({o.account: o.date for o in self.store.load_opens()}[BANK], date(2025, 12, 1))

    def test_the_reason_states_the_date_rule_without_claiming_a_duplicate(self) -> None:
        with self.assertRaises(AccountNotOpenError) as caught:
            self.person_confirm("pre-dec15")
        message = str(caught.exception)
        self.assertIn("opens on 2026-01-01", message)
        self.assertIn("may already include", message)
        for claim in ("count twice", "already includes"):
            self.assertNotIn(claim, message)

    def test_group_confirmation_refuses_a_member_that_changes_after_the_check(self) -> None:
        # The check and the writes run under one company lock. The writes are separate ledger transactions:
        # a member that is refused at its write stays in review, and the members before it stay recorded.
        savings = "Assets:Bank:Savings"
        add_account(self.path, savings, open_date="2026-01-01")
        config = json.loads((self.path / "entity.json").read_text(encoding="utf-8"))
        config["bank_account_mappings"]["feed-2"] = savings
        (self.path / "entity.json").write_text(json.dumps(config), encoding="utf-8")
        self.entity = load_entity(self.path)
        import_transactions(self.entity, [dict(row("savings-jan10", "2026-01-10", "-3.00"), accountId="feed-2")],
                            "intake", intake_only=True)
        queue.propose_group(self.entity, ["on-s", "savings-jan10"], FEES, "Synthetic group")
        real_check = queue._refuse_item_before_open
        checked = []

        def check_then_change(entity, item):
            real_check(entity, item)
            checked.append(item["source_id"])
            if len(checked) == 2:  # a change that does not go through the company lock
                with self.store.transaction() as conn:
                    conn.execute("UPDATE accounts SET open_date='2026-02-01' WHERE name=?", (savings,))

        with patch.object(queue, "_refuse_item_before_open", check_then_change), \
                self.assertRaises(AccountNotOpenError) as caught:
            queue.confirm_group(self.entity, FEES, "person-review")
        self.assertEqual((caught.exception.code, caught.exception.account), ("source_catalog_conflict", savings))
        self.assertEqual(sorted(checked), ["on-s", "savings-jan10"])
        self.assertEqual(self.posted(), {"on-s"})
        self.assert_held("savings-jan10")

    # -- intake and saved-rule parity ---------------------------------------------

    def test_learned_categories_do_not_post_rows_before_the_open_date_at_intake(self) -> None:
        other = [row(f"learned-{sid}", day, amount) for sid, day, amount in ROWS]
        result = import_transactions(self.entity, other, "learned", lambda txn: (FEES, "learned"))
        self.assertEqual((result.new_entries, result.pending_categorization), (2, 2))
        self.assertEqual(self.posted(), {f"learned-{sid}" for sid in ON_OR_AFTER})
        for sid in BEFORE:
            self.assertEqual(self.pending()[f"learned-{sid}"]["review_hold_reason"], "source_catalog_conflict")

    def test_confirmation_makes_the_saved_rule_decision(self) -> None:
        late_row = row("late-category", "2026-01-12", "-7.00")
        import_transactions(self.entity, [late_row], "intake", intake_only=True)
        cases = [(sid, FEES) for sid, _, _ in ROWS] + [("late-category", LATE)]
        key = queue._counterparty_key(PAYEE, "")
        decisions = {}
        for sid, category in cases:
            policy = dict(version=1, enabled=True, review_categories=[FEES, LATE], answer_roles=["client"], rules=[
                dict(id="rule-1", source_account_id="feed-1", counterparty_key=key, category=category,
                     approved_ledger_account=BANK)])
            plan = routine_plan(self.entity, policy, source_ids=[sid])
            decisions[sid] = plan["unresolved"][0]["reason"] if plan["unresolved"] else "recorded"
        self.assertEqual(decisions, {"pre-dec15": "source_catalog_conflict", "pre-dec31": "source_catalog_conflict",
                                     "on-s": "recorded", "post-jan10": "recorded",
                                     "late-category": "category_catalog_conflict"})
        for sid, category in cases:
            with self.subTest(sid=sid):
                try:
                    self.person_confirm(sid, category)
                    outcome = "recorded"
                except AccountNotOpenError as exc:
                    outcome = exc.code
                self.assertEqual(outcome, decisions[sid])


if __name__ == "__main__":
    unittest.main()
