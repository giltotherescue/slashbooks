"""Readiness with confirmed outside-books decisions (``disposition_context``). All data is synthetic."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import json
import re
import shutil
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping import source_disposition as sd
from bookkeeping.coverage import record_feed_window, record_statement
from bookkeeping.ledger.books_policy_check import workspace_digest
from bookkeeping.ledger.model import Entry, Open, Posting
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.readiness import DependencyVersions, ReadinessDispositionContext, assess_review_readiness
from bookkeeping.reconcile import reconcile
from test_queue import _make_entity

CHECKING = "Assets:Bank:Checking"
CARD = "Liabilities:CreditCard"
LEGACY_KEYS = {"schema_version", "basis", "state", "period", "period_status", "books_revision", "source_revision",
               "reply_revision", "policy_version", "evidence_fingerprint", "blockers", "warnings", "sources",
               "coverage", "reconciliations", "reports", "package_id"}


def feed_row(source_id: str = "banksync:r1", day: str = "2026-09-10", amount: str = "-25.00") -> dict:
    return {"id": source_id, "date": day, "amount": amount, "description": "NORTHWIND COFFEE", "currency": "USD",
            "accountId": "feed-chk", "pending": False}


def policy(state: str = "authorized", version: int = 2, epoch: int = 4, start_day: str = "2026-09-12") -> dict:
    # The receipt on September 15 is after the start; the staged rows on September 10 and 11 are before it.
    return {"schema_version": 1, "mode": "enforced", "epoch": epoch,
            "accounts": {CHECKING: {"state": state, "start_day": start_day, "version": version}}}


class DispositionFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.entity = _make_entity(Path(self.temp.name)).path
        config_path = self.entity / "entity.json"
        config = json.loads(config_path.read_text())
        config["declared_sources"] = [{"name": "Synthetic checking", "account": CHECKING,
                                       "coverage_from": "2026-09-01", "coverage_to": "2026-09-30"}]
        config["bank_account_mappings"] = {"feed-chk": CHECKING}
        config_path.write_text(json.dumps(config, sort_keys=True) + "\n")
        self.store = LedgerStore(self.entity / "ledger.sqlite")
        with self.store.transaction() as conn:
            self.store.insert_entries([Entry(
                date=date(2026, 9, 15), narration="Synthetic receipt", flag="*",
                meta=(("source-id", "synthetic-receipt"),),
                postings=(Posting(CHECKING, Decimal("100.00"), "USD"),
                          Posting("Income:Consulting", Decimal("-100.00"), "USD")))], conn)
        reconcile(self.entity, CHECKING, Decimal("100.00"), date(2026, 9, 30),
                  source_opening_balance=Decimal("0.00"), source_transaction_total=Decimal("100.00"),
                  source_snapshot_at="2026-10-01T12:00:00Z", source_through="2026-09-30",
                  source_evidence_refs=["provider-record:statement-2026-09"],
                  source_evidence_hashes={"provider-record:statement-2026-09": "a" * 64})
        self.stage([feed_row()])
        self.policy = policy()
        self.claim = self.preview("banksync:r1")

    # -- helpers ----------------------------------------------------------------------------------------------

    def stage(self, rows):
        (self.entity / "staging" / "pending-categorization.json").write_text(json.dumps(rows) + "\n")

    def preview(self, source_id, policy_document=None):
        result = sd.preview(sd.load_context(self.entity, policy_document or self.policy), source_id, "outside_books")
        self.assertEqual(result["status"], "ok", result)
        return result["claim"]

    def context(self, claims=None, *, policy_document=None, transitions=None, generation=1):
        document = policy_document or self.policy
        return ReadinessDispositionContext(
            document, tuple(claims if claims is not None else [self.claim]), transitions or {},
            {"base_sha256": "b" * 64, "policy_epoch": document["epoch"], "generation": generation,
             "captured_disposition_sha256": "c" * 64})

    def assess(self, disposition_context=None, **kwargs):
        if disposition_context is not None:
            kwargs["disposition_context"] = disposition_context
        return assess_review_readiness(self.entity, "2026-09", DependencyVersions(17, 9, 4, 3),
                                       as_of=date(2026, 10, 13), **kwargs).to_document()

    def staged_refs(self, document):
        work = next((b for b in document["blockers"] if b["code"] == "unresolved_period_work"), None)
        return [] if work is None else work["refs"]

    def book_entry(self, source_id, day=date(2026, 9, 10), account=CHECKING, import_source=None):
        meta = (("source-id", source_id),) + ((("import-source", import_source),) if import_source else ())
        with self.store.transaction() as conn:
            self.store.insert_entries([Entry(
                date=day, narration="Synthetic", flag="*", meta=meta,
                postings=(Posting(account, Decimal("-25.00"), "USD"),
                          Posting("Expenses:Meals" if import_source is None else "Equity:Opening-Balances",
                                  Decimal("25.00"), "USD")))], conn)


class ReadinessDispositionTests(DispositionFixture):
    # -- legacy parity ----------------------------------------------------------------------------------------

    def test_no_context_keeps_the_legacy_document(self):
        legacy = self.assess()
        explicit = self.assess(disposition_context=None, )
        self.assertEqual(json.dumps(legacy, sort_keys=True), json.dumps(explicit, sort_keys=True))
        self.assertEqual(set(legacy), LEGACY_KEYS)
        self.assertEqual(self.staged_refs(legacy), ["banksync:r1"])
        self.assertEqual(legacy["state"], "blocked")

    def test_portable_actions_keep_the_complete_package_stable_across_roots(self):
        self.stage([feed_row(), feed_row("banksync:r2", "2026-09-11")])
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "same-books"
            shutil.copytree(self.entity, copied)
            self.assertEqual(workspace_digest(self.entity), workspace_digest(copied))
            context = self.context()
            local = self.assess(context)
            copied_local = assess_review_readiness(copied, "2026-09", DependencyVersions(17, 9, 4, 3),
                                                   as_of=date(2026, 10, 13), disposition_context=context).to_document()
            self.assertNotEqual(local["package_id"], copied_local["package_id"])
            original = self.assess(context, portable_actions=True)
            relocated = assess_review_readiness(copied, "2026-09", DependencyVersions(17, 9, 4, 3),
                                                as_of=date(2026, 10, 13), disposition_context=context,
                                                portable_actions=True).to_document()
            self.assertEqual(relocated, original)
        blocker = next(b for b in original["blockers"] if b["code"] == "unresolved_period_work")
        self.assertEqual(blocker["action"], "Run: books queue list --entity @entity and confirm, correct, or withdraw each item.")
        self.assertEqual(self.staged_refs(original), ["banksync:r2"])
        self.assertNotIn(str(self.entity.resolve()), json.dumps(original))

        legacy = self.assess(context)
        self.assertEqual(legacy, self.assess(context, portable_actions=False))
        self.assertNotEqual(legacy["package_id"], original["package_id"])
        self.assertEqual(next(b["action"] for b in legacy["blockers"] if b["code"] == "unresolved_period_work"),
                         blocker["action"].replace("@entity", str(self.entity.resolve())))

    def test_a_context_that_resolves_nothing_still_binds_the_package(self):
        # A start on the first day of the period does not bound it, so only the decisions could change it.
        stale = self.context(policy_document=policy(version=3, start_day="2026-09-01"))
        document = self.assess(stale)
        legacy = self.assess()
        self.assertEqual(self.staged_refs(document), ["banksync:r1"])
        self.assertEqual(document["disposition_basis"]["excluded"], [])
        self.assertEqual({k: v for k, v in document.items() if k not in ("disposition_basis", "package_id")},
                         {k: v for k, v in legacy.items() if k != "package_id"})
        self.assertNotEqual(document["package_id"], legacy["package_id"])

    # -- suppression ------------------------------------------------------------------------------------------

    def test_a_current_resolved_decision_leaves_out_only_its_row(self):
        self.stage([feed_row(), feed_row("banksync:r2", "2026-09-11")])
        before = workspace_digest(self.entity)
        document = self.assess(self.context())
        self.assertEqual(self.staged_refs(document), ["banksync:r2"])
        identity = sd.row_identity(feed_row())
        self.assertEqual(document["disposition_basis"], {
            "base_sha256": "b" * 64, "policy_epoch": 4, "generation": 1, "captured_disposition_sha256": "c" * 64,
            "excluded": [{"source_id": "banksync:r1", "row_digest": identity["row_digest"],
                          "preview_digest": self.claim["preview_digest"]}]})
        self.assertEqual(workspace_digest(self.entity), before)

    def test_the_only_open_row_resolved_makes_the_period_ready(self):
        self.assertEqual(self.assess()["state"], "blocked")
        document = self.assess(self.context())
        self.assertEqual((document["state"], document["blockers"]), ("ready_for_review", []))

    def test_invalidated_stale_and_ambiguous_decisions_stay_visible(self):
        cases = {
            "row changed": ([feed_row(amount="-26.00")], self.context()),
            "policy changed": ([feed_row()], self.context(policy_document=policy(version=3))),
            "two spellings": ([feed_row(), feed_row("r1")], self.context()),
            "two decisions": ([feed_row()], self.context([self.claim, dict(self.claim)])),
        }
        for name, (rows, context) in cases.items():
            with self.subTest(name):
                self.stage(rows)
                document = self.assess(context)
                self.assertIn("banksync:r1", self.staged_refs(document))
                self.assertEqual(document["disposition_basis"]["excluded"], [])

    def test_a_pending_transition_stays_visible_until_its_evidence_is_recorded(self):
        pending = policy("authorized_pending_opening", version=2)
        claim = self.preview("banksync:r1", pending)
        self.book_entry("statement-opening-s2", day=date(2026, 9, 12), import_source="statement-opening")
        authorized = policy("authorized", version=3, epoch=5)
        result = sd.evaluate(sd.load_context(self.entity, authorized), [claim])[0]
        self.assertEqual(result["status"], "transition_pending")
        document = self.assess(self.context([claim], policy_document=authorized))
        self.assertEqual(self.staged_refs(document), ["banksync:r1"])
        transitions = {claim["preview_digest"]: result["transition_evidence"]}
        document = self.assess(self.context([claim], policy_document=authorized, transitions=transitions))
        self.assertEqual(self.staged_refs(document), [])

    def test_unknown_or_borrowed_ids_cannot_leave_a_row_out(self):
        self.stage([feed_row(), feed_row("banksync:r2", "2026-09-11")])
        unknown = {**self.claim, "source_id": "banksync:nope"}
        borrowed = {**self.claim, "source_id": "banksync:r2"}
        for claim in (unknown, borrowed):
            with self.subTest(claim["source_id"]):
                document = self.assess(self.context([claim]))
                self.assertEqual(self.staged_refs(document), ["banksync:r1", "banksync:r2"])
        with self.assertRaises(ValueError):
            ReadinessDispositionContext.from_document({
                "policy": self.policy, "claims": [], "transitions": {}, "suppress": ["banksync:r1"],
                "binding": self.context().binding})

    def test_a_booked_or_recorded_row_is_never_left_out(self):
        self.book_entry("r1")
        self.assertEqual(self.staged_refs(self.assess(self.context())), ["banksync:r1"])

    def test_a_row_sealed_with_no_financial_effect_is_never_left_out(self):
        with self.store.transaction() as conn:
            self.store.insert_source_transactions([feed_row("r1")], conn, status="no_financial_effect")
        result = sd.evaluate(sd.load_context(self.entity, self.policy), [self.claim])[0]
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(self.staged_refs(self.assess(self.context())), ["banksync:r1"])

    # -- other blockers ---------------------------------------------------------------------------------------

    def test_proposals_duplicates_and_questions_still_block(self):
        (self.entity / "review-queue" / "p1.json").write_text(json.dumps(
            {"status": "open", "source_id": "proposal-1", "date": "2026-09-12"}))
        (self.entity / "staging" / "duplicate-candidates.json").write_text(json.dumps([
            {"status": "duplicate-candidate", "source_id": "dup-1", "transaction": {"date": "2026-09-13"}}]))
        (self.entity / "reports" / "open-questions.json").write_text(json.dumps([
            {"status": "open", "source_id": "question-1", "date": "2026-09-14"}]))
        document = self.assess(self.context())
        self.assertEqual(self.staged_refs(document), ["dup-1", "proposal-1"])
        self.assertIn("unresolved_questions", {b["code"] for b in document["blockers"]})
        self.assertEqual(len(document["disposition_basis"]["excluded"]), 1)

    # -- binding ----------------------------------------------------------------------------------------------

    def test_another_epoch_or_generation_gives_another_package(self):
        base = self.assess(self.context())
        later_generation = self.assess(self.context(generation=2))
        later_epoch = self.assess(self.context(policy_document=policy(epoch=5)))
        visible = lambda doc: {k: v for k, v in doc.items() if k not in ("disposition_basis", "package_id")}
        for other in (later_generation, later_epoch):
            self.assertEqual(visible(other), visible(base))
            self.assertEqual(other["disposition_basis"]["excluded"], base["disposition_basis"]["excluded"])
            self.assertNotEqual(other["package_id"], base["package_id"])
        self.assertEqual(self.assess(self.context())["package_id"], base["package_id"])

    def test_a_malformed_context_is_refused(self):
        good = self.context().binding
        bad = [
            {**good, "policy_epoch": 5},
            {**good, "generation": -1},
            {**good, "generation": True},
            {**good, "base_sha256": "B" * 64},
            {k: v for k, v in good.items() if k != "captured_disposition_sha256"},
            {**good, "extra": 1},
        ]
        for binding in bad:
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                ReadinessDispositionContext(self.policy, (self.claim,), {}, binding)
        with self.assertRaises(ValueError):
            ReadinessDispositionContext(self.policy, ({"source_id": "banksync:r1"},), {}, good)
        with self.assertRaises(TypeError):
            self.assess({"policy": self.policy})

    def test_the_context_is_a_private_copy(self):
        claim = dict(self.claim)
        context = self.context([claim])
        claim["row_digest"] = hashlib.sha256(b"other").hexdigest()
        self.assertEqual(self.staged_refs(self.assess(context)), [])


def waiting_rows(document):
    """The staged-row count in the one statement tie-out warning (0 when the warning names no staged rows)."""
    [warning] = tieout_warnings(document)
    found = re.search(r" ([0-9]+) row\(s\) are staged for review", warning["message"])
    return int(found.group(1)) if found else 0


def tieout_warnings(document):
    """The statement tie-out item: a blocker when the books differ, a warning when staged rows explain it."""
    return [w for w in document["blockers"] + document["warnings"] if w["code"] == "statement_tieout_differs"]


class ReadinessWaitingCountTests(DispositionFixture):
    """The statement tie-out count of staged rows uses the same resolved-row check as the open work."""

    def setUp(self):
        super().setUp()
        # One statement for exactly the period. The books imply 100.00, so the tie-out differs.
        record_statement(self.entity, {"statement_id": "chk-2026-09", "account": CHECKING, "kind": "statement",
                                       "period_start": "2026-09-01", "period_end": "2026-09-30",
                                       "opening_balance": "0.00", "closing_balance": "25.00"})

    def test_resolved_rows_are_not_staged_and_a_true_staged_row_still_is(self):
        self.stage([feed_row(), feed_row("banksync:r2", "2026-09-11"), feed_row("banksync:r3", "2026-09-12")])
        claims = [self.claim, self.preview("banksync:r2")]
        legacy = self.assess()
        document = self.assess(self.context(claims))
        self.assertEqual(waiting_rows(legacy), 3)
        self.assertEqual(waiting_rows(document), 1)
        self.assertEqual(self.staged_refs(document), ["banksync:r3"])
        [warning] = tieout_warnings(document)
        self.assertIn(" 1 row(s) are staged for review in this period.", warning["message"])
        # The discrepancy comes from the postings only. It does not change.
        [legacy_warning] = tieout_warnings(legacy)
        self.assertEqual(warning["message"].split(").")[0], legacy_warning["message"].split(").")[0])
        self.assertEqual(document["reports"], legacy["reports"])

    def test_all_staged_rows_resolved_gives_no_staged_count(self):
        self.stage([feed_row(), feed_row("banksync:r2", "2026-09-11")])
        document = self.assess(self.context([self.claim, self.preview("banksync:r2")]))
        self.assertEqual(waiting_rows(self.assess()), 2)
        self.assertEqual(waiting_rows(document), 0)
        [warning] = tieout_warnings(document)
        self.assertNotIn("staged", warning["message"])

    def test_an_altered_row_or_a_repeated_alias_is_still_counted(self):
        cases = {
            "altered amount": ([feed_row(amount="-26.00")], 1),
            "repeated alias": ([feed_row(), feed_row("r1")], 2),
        }
        for name, (rows, count) in cases.items():
            with self.subTest(name):
                self.stage(rows)
                self.assertEqual(waiting_rows(self.assess(self.context())), count)

    def test_without_a_context_the_count_is_unchanged(self):
        self.assertEqual(waiting_rows(self.assess()), 1)
        self.assertEqual(waiting_rows(self.assess(self.context(policy_document=policy(version=3)))), 1)


def start_policy(card: dict | None = None, *, mode: str = "enforced") -> dict:
    return {"schema_version": 1, "mode": mode, "epoch": 7,
            "accounts": {CHECKING: {"state": "authorized", "start_day": "2027-01-15", "version": 2},
                         CARD: card or {"state": "unset"}}}


class AccountStartFixture(unittest.TestCase):
    """Checking starts on January 15 (cash). Two earlier rows have confirmed outside-books decisions."""

    checking_entries = ((date(2027, 1, 15), "synthetic-a", Decimal("50.00")),
                        (date(2027, 1, 16), "synthetic-b", Decimal("30.00")))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.entity = _make_entity(Path(self.temp.name)).path
        config_path = self.entity / "entity.json"
        config = json.loads(config_path.read_text())
        config["declared_sources"] = [{"name": "Synthetic checking", "account": CHECKING},
                                      {"name": "Synthetic card", "account": CARD}]
        config["bank_account_mappings"] = {"feed-chk": CHECKING}
        config_path.write_text(json.dumps(config, sort_keys=True) + "\n")
        self.store = LedgerStore(self.entity / "ledger.sqlite")
        for day, source_id, amount in self.checking_entries:
            self.post(CHECKING, day, source_id, amount, other="Income:Consulting")
        # The earlier statement is history before the books start. Its two rows are outside the books.
        record_statement(self.entity, {"statement_id": "chk-early", "account": CHECKING, "kind": "statement",
                                       "period_start": "2027-01-10", "period_end": "2027-01-14",
                                       "opening_balance": "500.00", "closing_balance": "425.00"})
        record_feed_window(self.entity, account=CHECKING, start=date(2027, 1, 17), end=date(2027, 1, 17))
        self.rows = [feed_row("banksync:e1", "2027-01-11", "-25.00"), feed_row("banksync:e2", "2027-01-12", "-50.00")]
        (self.entity / "staging" / "pending-categorization.json").write_text(json.dumps(self.rows) + "\n")

    def context(self, policy_document=None, *, claims_policy=None):
        document = policy_document or start_policy()
        claims = []
        for row in self.rows:
            result = sd.preview(sd.load_context(self.entity, claims_policy or document), row["id"], "outside_books")
            self.assertEqual(result["status"], "ok", result)
            claims.append(result["claim"])
        return ReadinessDispositionContext(
            document, tuple(claims), {},
            {"base_sha256": "b" * 64, "policy_epoch": document["epoch"], "generation": 1,
             "captured_disposition_sha256": "c" * 64})

    def assess(self, disposition_context=None, period="2027-01"):
        kwargs = {} if disposition_context is None else {"disposition_context": disposition_context}
        return assess_review_readiness(self.entity, period, DependencyVersions(1, 1, 1, 1),
                                       as_of=date(2027, 4, 10), **kwargs).to_document()

    @staticmethod
    def month(document, account):
        [row] = next(item for item in document["coverage"] if item["account"] == account)["months"]
        return row

    def post(self, account, day, source_id, amount, *, other="Expenses:Office"):
        with self.store.transaction() as conn:
            self.store.insert_entries([Entry(
                date=day, narration="Synthetic", flag="*", meta=(("source-id", source_id),),
                postings=(Posting(account, Decimal(amount), "USD"),
                          Posting(other, -Decimal(amount), "USD")))], conn)

    def declare(self, *sources):
        """Replace the declared sources with ``(name, account, coverage_from, coverage_to)`` tuples."""
        config_path = self.entity / "entity.json"
        config = json.loads(config_path.read_text())
        config["declared_sources"] = [{"name": name, "account": account,
                                       **({"coverage_from": first, "coverage_to": last} if first else {})}
                                      for name, account, first, last in sources]
        config_path.write_text(json.dumps(config, sort_keys=True) + "\n")

    def reconcile_account(self, account, opening, activity):
        balance = Decimal(opening) + Decimal(activity)
        ref = f"synthetic:statement-{account}"
        reconcile(self.entity, account, balance, date(2027, 1, 31),
                  source_opening_balance=Decimal(opening), source_transaction_total=Decimal(activity),
                  source_snapshot_at="2027-02-01T12:00:00Z", source_through="2027-01-31",
                  source_evidence_refs=[ref], source_evidence_hashes={ref: "a" * 64})

    def make_checking_ready(self):
        """Checking has declared coverage from its start and a clean month-end reconciliation."""
        self.reconcile_account(CHECKING, "0.00", "80.00")

    @staticmethod
    def codes(document, account=None):
        return {b["code"] for b in document["blockers"] if account is None or account in b["refs"]}



class ReadinessAccountStartTests(AccountStartFixture):
    """A cash account whose authorized books start is inside the period needs source evidence from that day."""

    def test_checking_needs_evidence_only_from_its_authorized_start(self):
        document = self.assess(self.context())
        month = self.month(document, CHECKING)
        self.assertEqual((month["month"], month["from"], month["to"], month["days"]),
                         ("2027-01", "2027-01-15", "2027-01-31", 17))
        self.assertEqual(month["gaps"], [{"from": "2027-01-18", "to": "2027-01-31", "days": 14}])
        self.assertEqual((month["status"], month["verified"]), ("partial", False))
        self.assertEqual(month["reasons"], ["Synthetic checking has no statement, feed window, declared coverage or "
                                            "transactions for Jan 18–31."])
        # The weak source on January 15-16 stays visible.
        self.assertEqual(month["notes"], ["Synthetic checking coverage for Jan 15–16 is inferred from transaction dates only; "
                                          "add a statement or feed window to confirm it."])
        source = next(row for row in document["sources"] if row["account"] == CHECKING)
        self.assertEqual((source["requested_from"], source["requested_to"]), ("2027-01-15", "2027-01-31"))
        incomplete = next(b for b in document["blockers"]
                          if b["code"] == "source_coverage_incomplete" and b["refs"] == ["Synthetic checking"])
        self.assertIn("needs 2027-01-15 through 2027-01-31", incomplete["message"])
        self.assertIn("--from 2027-01-15", incomplete["action"])
        checking = [b["message"] for b in document["blockers"] if {CHECKING, "Synthetic checking"} & set(b["refs"])]
        self.assertNotIn("Jan 1–", " ".join(checking))
        self.assertNotIn("2027-01-01", " ".join(checking))

    def test_the_earlier_statement_is_outside_the_in_scope_statements(self):
        document = self.assess(self.context())
        self.assertEqual(tieout_warnings(document), [])
        self.assertNotIn("chk-early", json.dumps(document["warnings"]) + json.dumps(document["blockers"]))
        self.assertIsNone(self.month(document, CHECKING)["tieout"])

    def test_true_findings_remain(self):
        document = self.assess(self.context())
        self.assertIn("source_month_partial", self.codes(document, CHECKING))
        self.assertIn("source_coverage_incomplete", self.codes(document, "Synthetic checking"))
        self.assertIn("reconciliation_missing", self.codes(document, CHECKING))
        self.assertIn("reconciliation_evidence_missing", self.codes(document))
        self.assertEqual(document["state"], "blocked")

    def test_the_card_has_its_own_start(self):
        document = self.assess(self.context(start_policy({"state": "authorized", "start_day": "2027-01-22",
                                                          "version": 1})))
        card = self.month(document, CARD)
        self.assertEqual((card["from"], card["to"], card["days"], card["covered_days"], card["status"]),
                         ("2027-01-22", "2027-01-31", 10, 0, "missing"))
        self.assertEqual(card["gaps"], [{"from": "2027-01-22", "to": "2027-01-31", "days": 10}])
        self.assertEqual(self.month(document, CHECKING)["from"], "2027-01-15")
        self.assertNotIn("books_start_unset", self.codes(document))

    def test_an_unset_or_pending_account_needs_the_whole_month_and_an_explicit_outcome(self):
        cases = {"unset": ({"state": "unset"}, "books_start_unset"),
                 "pending": ({"state": "authorized_pending_opening", "start_day": "2027-01-22", "version": 1},
                             "books_start_opening_required")}
        for name, (card, code) in cases.items():
            with self.subTest(name):
                document = self.assess(self.context(start_policy(card)))
                month = self.month(document, CARD)
                self.assertEqual((month["from"], month["days"], month["status"]), ("2027-01-01", 31, "missing"))
                self.assertIn(code, self.codes(document, CARD))

    def test_a_regular_posting_before_a_mid_month_start_is_a_contradiction(self):
        self.post(CHECKING, date(2027, 1, 10), "synthetic-early", "-20.00")
        self.declare(("Synthetic checking", CHECKING, "2027-01-15", "2027-01-31"))
        self.reconcile_account(CHECKING, "0.00", "60.00")
        document = self.assess(self.context(start_policy({"state": "authorized", "start_day": "2027-01-01",
                                                          "version": 1})))
        self.assertIn("books_start_contradicted", self.codes(document, CHECKING))
        self.assertEqual(document["state"], "blocked")
        # The policy does not bound a contradicted account: it needs the whole month again.
        self.assertEqual(self.month(document, CHECKING)["from"], "2027-01-01")

    def test_no_context_or_a_legacy_policy_keeps_the_whole_month(self):
        legacy = self.assess()
        self.assertEqual(self.month(legacy, CHECKING)["from"], "2027-01-01")
        self.assertEqual(self.month(legacy, CHECKING)["gaps"][0], {"from": "2027-01-01", "to": "2027-01-09", "days": 9})
        [warning] = tieout_warnings(legacy)
        self.assertIn(" 2 row(s) are staged", warning["message"])
        unenforced = self.assess(self.context(start_policy(mode="legacy"), claims_policy=start_policy()))
        self.assertEqual(self.month(unenforced, CHECKING)["from"], "2027-01-01")
        self.assertNotIn("books_start_unset", self.codes(unenforced))

    def test_an_accrual_company_keeps_its_cutover_behavior(self):
        config_path = self.entity / "entity.json"
        config = json.loads(config_path.read_text())
        config["basis"] = "accrual"
        config_path.write_text(json.dumps(config, sort_keys=True) + "\n")
        with self.store.transaction() as conn:
            self.store.set_meta("basis", "accrual", conn)
        without = self.assess()
        document = self.assess(self.context())
        self.assertEqual((document["activity_from"], document["partial_post_cutover"]), ("2027-01-01", False))
        self.assertEqual(self.month(document, CHECKING)["from"], "2027-01-01")
        self.assertEqual(document["coverage"], without["coverage"])
        self.assertEqual(document["sources"], without["sources"])
        self.assertNotIn("books_start_unset", self.codes(document))
        # Accrual stays outside the cash check: a prior-day accrual opening has its own native rules.
        self.post(CARD, date(2027, 1, 10), "synthetic-card", "-25.00")
        future = self.assess(self.context(start_policy({"state": "authorized", "start_day": "2027-02-01",
                                                        "version": 1})))
        self.assertNotIn("books_start_contradicted", self.codes(future))
        self.assertNotIn("no_applicable_period", self.codes(future))

    def test_balances_and_report_totals_do_not_change(self):
        before = workspace_digest(self.entity)
        legacy = self.assess()
        document = self.assess(self.context())
        self.assertEqual(document["reports"], legacy["reports"])
        self.assertEqual(document["reconciliations"], legacy["reconciliations"])
        totals = {report["kind"]: report["totals"] for report in document["reports"]}
        self.assertTrue(totals)
        self.assertEqual(workspace_digest(self.entity), before)
        balance = sum((p.amount for e in self.store.load_entries() for p in e.postings if p.account == CHECKING),
                      Decimal("0"))
        self.assertEqual(balance, Decimal("80.00"))


FUTURE_CARD = {"state": "authorized", "start_day": "2027-02-01", "version": 1}
CARD_2 = "Liabilities:CreditCard:Second"


class ReadinessFutureStartTests(AccountStartFixture):
    """A source account whose authorized start is after the period is outside it only with no earlier posting."""

    def assess_future(self, **cards):
        return self.assess(self.context(start_policy(FUTURE_CARD), **cards))

    def ready_checking_and_card(self):
        self.declare(("Synthetic checking", CHECKING, "2027-01-15", "2027-01-31"),
                     ("Synthetic card", CARD, "2027-01-01", "2027-01-31"))
        self.make_checking_ready()

    def test_a_quiet_future_account_is_outside_the_period(self):
        self.ready_checking_and_card()
        document = self.assess_future()
        card = next(row for row in document["coverage"] if row["account"] == CARD)
        self.assertEqual((card["status"], card["verified"], card["months"], card["books_start"]),
                         ("outside_period", False, [], "2027-02-01"))
        source = next(row for row in document["sources"] if row["account"] == CARD)
        self.assertEqual(source["coverage_status"], "outside_period")
        self.assertEqual(self.codes(document, CARD) | self.codes(document, "Synthetic card"), set())
        self.assertEqual((document["state"], document["blockers"]), ("ready_for_review", []))

    def test_a_quiet_future_account_still_needs_a_supplied_reconciliation_to_be_clean(self):
        self.ready_checking_and_card()
        self.post(CHECKING, date(2027, 1, 20), "synthetic-c", "5.00", other="Income:Consulting")
        document = self.assess_future()
        self.assertEqual(self.codes(document, CARD), set())
        self.assertIn("reconciliation_not_clean", self.codes(document, CHECKING))

    def test_a_staged_row_stays_visible_for_a_quiet_future_account(self):
        self.ready_checking_and_card()
        rows = [*self.rows, feed_row("banksync:open", "2027-01-20", "-5.00")]
        (self.entity / "staging" / "pending-categorization.json").write_text(json.dumps(rows) + "\n")
        document = self.assess_future()
        work = next(b for b in document["blockers"] if b["code"] == "unresolved_period_work")
        self.assertEqual(work["refs"], ["banksync:open"])

    def test_a_posting_before_a_future_start_is_a_contradiction(self):
        self.ready_checking_and_card()
        self.post(CARD, date(2027, 1, 15), "synthetic-card", "-25.00")
        self.reconcile_account(CARD, "0.00", "-25.00")
        document = self.assess_future()
        self.assertIn("books_start_contradicted", self.codes(document, CARD))
        self.assertEqual(document["state"], "blocked")
        self.assertNotEqual(next(row for row in document["coverage"] if row["account"] == CARD)["status"],
                            "outside_period")

    def test_a_net_zero_posting_is_a_contradiction(self):
        self.ready_checking_and_card()
        self.post(CARD, date(2027, 1, 10), "synthetic-charge", "-25.00")
        self.post(CARD, date(2027, 1, 12), "synthetic-refund", "25.00")
        self.reconcile_account(CARD, "0.00", "0.00")
        document = self.assess_future()
        self.assertIn("books_start_contradicted", self.codes(document, CARD))
        self.assertEqual(document["state"], "blocked")

    def test_a_prior_balance_is_a_contradiction(self):
        self.ready_checking_and_card()
        self.post(CARD, date(2026, 12, 20), "synthetic-december", "-25.00")
        self.reconcile_account(CARD, "-25.00", "0.00")
        document = self.assess_future()
        self.assertIn("books_start_contradicted", self.codes(document, CARD))
        self.assertEqual(document["state"], "blocked")

    def test_several_accounts_each_keep_their_own_result(self):
        with self.store.transaction() as conn:
            self.store.insert_opens([Open(date=date(2026, 1, 1), account=CARD_2, currencies=("USD",))], conn)
        self.declare(("Synthetic checking", CHECKING, "2027-01-15", "2027-01-31"),
                     ("Synthetic card", CARD, None, None), ("Second card", CARD_2, "2027-01-01", "2027-01-31"))
        self.make_checking_ready()
        self.post(CARD_2, date(2027, 1, 5), "synthetic-second", "-10.00")
        self.reconcile_account(CARD_2, "0.00", "-10.00")
        policy_document = start_policy(FUTURE_CARD)
        policy_document["accounts"][CARD_2] = {"state": "authorized", "start_day": "2027-03-01", "version": 1}
        document = self.assess(self.context(policy_document))
        statuses = {row["account"]: row["status"] for row in document["coverage"]}
        self.assertEqual(statuses[CARD], "outside_period")
        self.assertNotEqual(statuses[CARD_2], "outside_period")
        self.assertEqual(self.codes(document, CARD), set())
        self.assertIn("books_start_contradicted", self.codes(document, CARD_2))
        self.assertNotIn("books_start_contradicted", self.codes(document, CHECKING))
        self.assertEqual(self.month(document, CHECKING)["from"], "2027-01-15")
        self.assertNotIn("no_applicable_period", self.codes(document))

    def test_no_context_or_a_legacy_policy_is_unchanged(self):
        self.post(CARD, date(2027, 1, 15), "synthetic-card", "-25.00")
        for document in (self.assess(), self.assess(self.context(start_policy(FUTURE_CARD, mode="legacy"),
                                                                 claims_policy=start_policy()))):
            self.assertEqual(self.month(document, CARD)["from"], "2027-01-01")
            self.assertIn("source_month_partial", self.codes(document, CARD))
            self.assertTrue({"books_start_contradicted", "no_applicable_period"}.isdisjoint(self.codes(document)))
            self.assertNotIn("outside_period", json.dumps(document))


class ReadinessNoApplicablePeriodTests(AccountStartFixture):
    """Every source account starts after the period and has no posting: nothing in the period applies."""

    checking_entries = ()

    def test_all_accounts_in_the_future_give_an_explicit_result(self):
        policy_document = start_policy(FUTURE_CARD)
        policy_document["accounts"][CHECKING]["start_day"] = "2027-02-01"
        document = self.assess(self.context(policy_document))
        self.assertEqual(document["state"], "blocked")
        self.assertIn("no_applicable_period", self.codes(document))
        self.assertEqual({row["status"] for row in document["coverage"]}, {"outside_period"})
        self.assertEqual(self.codes(document, CHECKING) | self.codes(document, CARD), set())


class ReadinessEarlierStartTests(AccountStartFixture):
    """The contradiction check uses every authorized start, also one on or before the period's first day."""

    def card_start(self, start_day):
        return self.context(start_policy({"state": "authorized", "start_day": start_day, "version": 1}))

    def test_a_start_on_the_first_day_with_an_earlier_month_posting_is_a_contradiction(self):
        self.post(CARD, date(2026, 12, 20), "synthetic-december", "-25.00")
        document = self.assess(self.card_start("2027-01-01"))
        self.assertIn("books_start_contradicted", self.codes(document, CARD))
        self.assertEqual(self.month(document, CARD)["from"], "2027-01-01")

    def test_a_start_before_the_period_with_an_earlier_posting_is_a_contradiction(self):
        self.post(CARD, date(2027, 1, 5), "synthetic-early", "-25.00")
        document = self.assess(self.card_start("2027-01-10"), period="2027-02")
        self.assertIn("books_start_contradicted", self.codes(document, CARD))
        self.assertEqual(self.month(document, CARD)["from"], "2027-02-01")

    def test_a_posting_before_a_start_on_the_first_day_of_a_later_review_is_a_contradiction(self):
        self.post(CARD, date(2027, 1, 15), "synthetic-card", "-25.00")
        february = self.assess(self.card_start("2027-02-01"), period="2027-02")
        self.assertIn("books_start_contradicted", self.codes(february, CARD))
        self.assertIn("books_start_contradicted", self.codes(self.assess(self.card_start("2027-02-01"),
                                                                         period="2027-03"), CARD))

    def test_a_posting_after_the_period_but_before_a_later_start_is_a_contradiction(self):
        self.post(CARD, date(2027, 2, 10), "synthetic-february", "-25.00")
        document = self.assess(self.card_start("2027-03-01"))
        self.assertIn("books_start_contradicted", self.codes(document, CARD))

    def test_postings_on_or_after_the_start_are_not_a_contradiction(self):
        self.post(CARD, date(2027, 2, 1), "synthetic-start-day", "-25.00")
        self.post(CARD, date(2027, 2, 3), "synthetic-february", "-10.00")
        for start_day, period in (("2027-02-01", "2027-02"), ("2027-02-01", "2027-03"), ("2027-01-01", "2027-02")):
            with self.subTest(start_day=start_day, period=period):
                document = self.assess(self.card_start(start_day), period=period)
                self.assertNotIn("books_start_contradicted", self.codes(document))
        # Checking starts on January 15 with postings from that day: never a contradiction.
        self.assertNotIn("books_start_contradicted", self.codes(self.assess(self.card_start("2027-02-01"),
                                                                           period="2027-02"), CHECKING))

    def test_reports_and_legacy_output_do_not_change(self):
        self.post(CARD, date(2027, 1, 15), "synthetic-card", "-25.00")
        legacy = self.assess(period="2027-02")
        document = self.assess(self.card_start("2027-02-01"), period="2027-02")
        self.assertEqual(document["reports"], legacy["reports"])
        self.assertEqual(self.month(document, CARD), self.month(legacy, CARD))
        self.assertNotIn("books_start_contradicted", self.codes(legacy))


if __name__ == "__main__":
    unittest.main()
