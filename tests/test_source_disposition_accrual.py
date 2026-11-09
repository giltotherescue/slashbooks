"""The expected completion of an ``outside_books`` claim by a fresh accrual opening (the ``accrual`` opening kind).

Synthetic typed accrual company with cutover C = 2026-07-01. The real native preview and apply record one opening
entry on 2026-06-30, the day before the cutover: Checking 1,000.00 and one open receivable 400.00 against opening
equity 1,400.00. Checking and the receivable are both in the books start policy (the reviewed governed set). A
retained Checking row dated before the cutover was left out of the books while the opening was pending. All data is
synthetic.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping import source_disposition as sd  # noqa: E402
from bookkeeping.accrual import opening  # noqa: E402
from bookkeeping.entity import init_entity, load_entity  # noqa: E402
from bookkeeping.ledger.books_policy_check import check_ledger_delta, entry_fingerprint  # noqa: E402
from bookkeeping.ledger.model import Entry, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402

C = "2026-07-01"
CHECKING, RECEIVABLE = "Assets:Bank:Checking", "Assets:Receivable:Accounts-Receivable"
EQUITY = "Equity:Opening-Balances"
ROW = "banksync:pre-start"


def account_rows(state: str, version: int, accounts=(CHECKING, RECEIVABLE)) -> dict:
    return {account: {"state": state, "start_day": C, "version": version} for account in accounts}


def policy(state: str = "authorized_pending_opening", version: int = 1, epoch: int = 4,
           accounts=(CHECKING, RECEIVABLE)) -> dict:
    return {"schema_version": 1, "mode": "enforced", "epoch": epoch, "accounts": account_rows(state, version, accounts)}


AUTHORIZED = policy("authorized", 2, 5)


class AccrualOpeningCompletionTests(unittest.TestCase):
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
            {"id": ROW, "accountId": "checking-feed", "date": "2026-06-15", "amount": "-50.00",
             "description": "SYNTHETIC SUPPLIES", "currency": "USD", "pending": False}]))
        (cls.base / "evidence").mkdir(exist_ok=True)
        (cls.base / "evidence" / "open-items.txt").write_text("Synthetic invoice 400.00 outstanding")
        request = {
            "operation_id": "native-accrual-opening", "cutover": C, "evidence_refs": ["evidence/open-items.txt"],
            "lines": [{"account": CHECKING, "amount": "1000.00"}, {"account": RECEIVABLE, "amount": "400.00"},
                      {"account": EQUITY, "amount": "-1400.00"}],
            "items": [{"id": "inv-1", "kind": "receivable", "account": RECEIVABLE,
                       "counterparty": "Synthetic Customer", "reference": "INV-1", "due_date": "2026-07-10",
                       "open_amount": "400.00"}]}
        cls.plan = opening.preview_opening(load_entity(cls.base), request)
        cls.candidate = root / "candidate"
        shutil.copytree(cls.base, cls.candidate)
        opening.apply_opening(load_entity(cls.candidate), cls.plan)
        (cls.entry,) = LedgerStore(cls.candidate / "ledger.sqlite").load_entries()
        before = sd.load_context(cls.base, policy())
        cls.claim = sd.confirm(before, sd.preview(before, ROW, "outside_books")["claim"])["claim"]
        cls.after = sd.load_context(cls.candidate, AUTHORIZED)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # -- helpers ------------------------------------------------------------------------------------------------

    def row(self, account: str, entry: Entry | None = None, **change) -> dict:
        entry = entry or self.entry
        return {"account": account, "start_day": C, "version": 1, "operation": "accrual_opening_prior_day",
                "operation_id": f"review-{account}", "entry_source_id": entry.source_id,
                "entry_fingerprint": entry_fingerprint(entry), **change}

    def v3(self, rows=None, entry: Entry | None = None) -> dict:
        """The exact version 3 books start check request that the host sent for the publication."""
        rows = rows if rows is not None else [self.row(CHECKING, entry), self.row(RECEIVABLE, entry)]
        return {"schema_version": 3, "policy": policy(accounts=[r["account"] for r in rows]), "scope": [CHECKING],
                "opening_authorizations": rows}

    def with_entry(self, entry: Entry, context: sd.DispositionContext | None = None) -> sd.DispositionContext:
        context = context or self.after
        openings = tuple(sd.Opening(account, entry) for account in dict.fromkeys(
            p.account for p in entry.postings if not p.account.startswith("Equity:")))
        return dataclasses.replace(context, openings=openings)

    def evaluate(self, context=None, transitions=None, authorization=None) -> dict:
        return sd.evaluate(context or self.after, [self.claim], transitions, opening_authorization=authorization)[0]

    def assert_refused(self, result: dict, reason: str) -> None:
        self.assertEqual(result["status"], "invalidated", result)
        self.assertIn(reason, result["triggers"])
        self.assertIn(reason, sd.ACCRUAL_COMPLETION_REASONS)
        self.assertIsNone(result["transition_evidence"])
        self.assertEqual(sd.excluded_source_ids([result]), [])

    # -- the native fixture -------------------------------------------------------------------------------------

    def test_the_fixture_is_a_fresh_prior_day_accrual_opening_with_a_receivable(self) -> None:
        self.assertEqual(self.entry.date, date(2026, 6, 30))
        self.assertEqual(dict(self.entry.meta)["import-source"], "accrual-opening")
        self.assertEqual({p.account: str(p.amount) for p in self.entry.postings},
                         {CHECKING: "1000.00", RECEIVABLE: "400.00", EQUITY: "-1400.00"})
        self.assertEqual(entry_fingerprint(self.entry), self.plan["proposed_entry_fingerprint"])
        self.assertEqual(self.plan["accounting_plan"]["id"], self.entry.source_id)
        self.assertEqual(self.claim["basis"]["opening"], None)
        self.assertEqual(self.claim["conflicts"], ["NO_OPENING", "OPENING_NOT_COMMITTED"])
        # The exact v3 request also passes the publication guard (#130) for this entry.
        result = check_ledger_delta(self.v3(), [], [self.entry], {CHECKING})
        self.assertEqual(result["status"], "ok", result)

    def test_the_accrual_opening_is_an_accrual_kind_opening_and_not_a_booked_line(self) -> None:
        kinds = {item.account: item.kind for item in self.after.openings}
        self.assertEqual(kinds, {CHECKING: "accrual", RECEIVABLE: "accrual"})
        self.assertEqual([b for b in self.after.booked if b.source_id == self.entry.source_id], [])
        self.assertEqual(sd.OPENING_KINDS["accrual-opening"], "accrual")
        self.assertNotIn("accrual-tracking-opening", sd.OPENING_KINDS)

    # -- the completion -----------------------------------------------------------------------------------------

    def test_the_retained_pre_start_row_completes_against_the_prior_day_opening(self) -> None:
        first = self.evaluate(authorization=self.v3())
        self.assertEqual((first["status"], first["triggers"]), ("transition_pending", []), first)
        evidence = first["transition_evidence"]
        self.assertEqual(evidence["kind"], "expected_accrual_opening_recorded")
        self.assertEqual((evidence["account"], evidence["start_day"], evidence["account_version"]),
                         (CHECKING, C, 2))
        self.assertEqual(evidence["opening"]["date"], "2026-06-30")
        self.assertEqual(evidence["open_date"], "2026-06-30")
        self.assertEqual(evidence["authorization"], {
            "schema_version": 3, "operation": "accrual_opening_prior_day", "policy_epoch": 4, "start_day": C,
            "entry_date": "2026-06-30", "entry_source_id": self.entry.source_id,
            "entry_fingerprint": entry_fingerprint(self.entry),
            "accounts": [{"account": CHECKING, "operation_id": f"review-{CHECKING}", "version": 1},
                         {"account": RECEIVABLE, "operation_id": f"review-{RECEIVABLE}", "version": 1}]})
        transitions = {self.claim["preview_digest"]: evidence}
        for _ in range(2):
            done = self.evaluate(transitions=transitions)
            self.assertEqual((done["status"], done["triggers"], done["state"]), ("resolved", [], "outside_books"))
            self.assertEqual(done["notes"], ["EXPECTED_OPENING_RECORDED"])
        self.assertEqual(sd.excluded_source_ids([done]), [ROW])
        # The completion changes no balance and never allows a booking.
        self.assertEqual(LedgerStore(self.candidate / "ledger.sqlite").load_entries(), [self.entry])
        hold = sd.reclassify(self.after, ROW, self.claim["row_digest"])
        self.assertEqual((hold["date_checks_pass"], hold["date_hold_reason"]), (False, "source_catalog_conflict"))

    def test_the_command_reads_the_authorization_from_the_evaluate_request(self) -> None:
        request = {"schema_version": 1, "action": "evaluate", "policy": AUTHORIZED, "claims": [self.claim],
                   "opening_authorization": self.v3()}
        result = sd.run_request(sd._parse_request(request), self.after)
        self.assertEqual(result["results"][0]["status"], "transition_pending")

    def test_a_recorded_completion_still_invalidates_on_a_later_change(self) -> None:
        evidence = self.evaluate(authorization=self.v3())["transition_evidence"]
        transitions = {self.claim["preview_digest"]: evidence}
        replaced = dataclasses.replace(self.entry, narration="Another opening")
        self.assertIn("OPENING_CHANGED", self.evaluate(self.with_entry(replaced), transitions)["triggers"])
        later = dataclasses.replace(self.after, policy=sd.parse_policy(policy("authorized", 3, 6)))
        self.assertIn("POLICY_CHANGED", self.evaluate(later, transitions)["triggers"])
        moved = dataclasses.replace(self.after, open_dates={**self.after.open_dates, CHECKING: date(2026, 6, 1)})
        self.assertIn("OPEN_DATE_CHANGED", self.evaluate(moved, transitions)["triggers"])

    def test_recorded_accrual_evidence_is_checked_field_by_field(self) -> None:
        evidence = self.evaluate(authorization=self.v3())["transition_evidence"]

        def resign(**change):
            body = {k: v for k, v in {**evidence, **change}.items() if k != "evidence_digest"}
            return {**body, "evidence_digest": sd._sha(body)}

        auth = evidence["authorization"]
        cases = {
            "relabeled as a statement completion": resign(kind="expected_opening_recorded"),
            "another fingerprint": resign(authorization={**auth, "entry_fingerprint": "0" * 64}),
            "receivable left out": resign(authorization={**auth, "accounts": auth["accounts"][:1]}),
            "version 2": resign(authorization={**auth, "schema_version": 2}),
            "same-day entry date": resign(authorization={**auth, "entry_date": C}),
            "another open date": resign(open_date=C),
            "no authorization": {k: v for k, v in resign().items() if k != "authorization"},
        }
        for name, bad in cases.items():
            with self.subTest(name):
                result = self.evaluate(transitions={self.claim["preview_digest"]: bad})
                # Evidence that does not fit is not used, so the open date is compared with the confirmed one too.
                self.assertEqual(result["triggers"], ["OPEN_DATE_CHANGED", "TRANSITION_EVIDENCE_INVALID"], result)

    # -- review blockers on 02a58b79 ----------------------------------------------------------------------------

    def stored(self) -> dict:
        return self.evaluate(authorization=self.v3())["transition_evidence"]

    @staticmethod
    def rehash(document: dict) -> dict:
        body = {k: v for k, v in document.items() if k != "evidence_digest"}
        return {**body, "evidence_digest": sd._sha(body)}

    def replay(self, evidence: dict, context=None) -> dict:
        return self.evaluate(context, transitions={self.claim["preview_digest"]: evidence})

    def with_policy(self, document: dict) -> sd.DispositionContext:
        return dataclasses.replace(self.after, policy=sd.parse_policy(document))

    def test_blocker_1_stored_evidence_needs_positive_safe_pending_versions_on_every_row(self) -> None:
        evidence = self.stored()
        for version in (-1, 0, 7, 2 ** 53, True, "1"):
            with self.subTest(version=version):
                changed = json.loads(json.dumps(evidence))
                changed["authorization"]["accounts"][1]["version"] = version
                result = self.replay(self.rehash(changed))
                self.assertEqual(result["status"], "invalidated", result)
                # Version 7 is a safe version, so the current policy (receivable version 2, not 8) refuses it.
                self.assertIn("POLICY_CHANGED" if version == 7 else "TRANSITION_EVIDENCE_INVALID", result["triggers"])
                self.assertEqual(sd.excluded_source_ids([result]), [])

    def test_blocker_1_stored_evidence_needs_every_governed_account_in_the_current_policy(self) -> None:
        evidence = self.stored()
        self.assertEqual(self.replay(evidence)["status"], "resolved")
        removed = self.with_policy(policy("authorized", 2, 6, [CHECKING]))
        drift = self.with_policy({**policy("authorized", 2, 6), "accounts": {
            **account_rows("authorized", 2, [CHECKING]), **account_rows("authorized", 3, [RECEIVABLE])}})
        moved = self.with_policy({**policy("authorized", 2, 6), "accounts": {
            **account_rows("authorized", 2, [CHECKING]),
            RECEIVABLE: {"state": "authorized", "start_day": "2026-08-01", "version": 2}}})
        for name, context in (("receivable removed", removed), ("receivable version advanced", drift),
                              ("receivable start moved", moved)):
            with self.subTest(name):
                result = self.replay(evidence, context)
                self.assertEqual(result["status"], "invalidated", result)
                self.assertIn("POLICY_CHANGED", result["triggers"])
                self.assertEqual(sd.excluded_source_ids([result]), [])

    def test_blocker_2_the_accrual_envelope_needs_an_integer_schema_version(self) -> None:
        evidence = self.stored()
        for value in (True, 1.0, "1"):
            with self.subTest(value=value):
                result = self.replay(self.rehash({**evidence, "schema_version": value}))
                self.assertIn("TRANSITION_EVIDENCE_INVALID", result["triggers"])
                self.assertEqual(sd.excluded_source_ids([result]), [])

    def test_blocker_3_the_request_epoch_is_between_the_captured_and_the_current_epoch(self) -> None:
        self.assertEqual(self.evaluate(authorization=self.v3())["transition_evidence"]["authorization"]
                         ["policy_epoch"], 4)
        for name, epoch in (("stale", 3), ("current", 5), ("future", 999)):
            with self.subTest(name):
                request = self.v3()
                request["policy"]["epoch"] = epoch
                self.assert_refused(self.evaluate(authorization=request), "ACCRUAL_AUTHORIZATION_EPOCH")
        evidence = self.stored()
        for epoch in (3, 5):
            with self.subTest(recorded=epoch):
                changed = json.loads(json.dumps(evidence))
                changed["authorization"]["policy_epoch"] = epoch
                self.assertIn("TRANSITION_EVIDENCE_INVALID", self.replay(self.rehash(changed))["triggers"])

    def test_blocker_4_operation_ids_are_recorded_and_not_authenticated(self) -> None:
        """The engine only checks that each operation ID is non-empty text and unique in the record.

        It cannot authenticate the IDs against the policy. A trusted host must supply the immutable origin.
        """
        evidence = json.loads(json.dumps(self.stored()))
        evidence["authorization"]["accounts"][1]["operation_id"] = "another-receipt"
        self.assertEqual(self.replay(self.rehash(evidence))["status"], "resolved")
        evidence["authorization"]["accounts"][1]["operation_id"] = f"review-{CHECKING}"
        self.assertIn("TRANSITION_EVIDENCE_INVALID", self.replay(self.rehash(evidence))["triggers"])
        self.assertIn("does not authenticate", sd._accrual_completion.__doc__)

    def test_item_d_a_decision_previewed_with_the_opening_live_resolves_unchanged(self) -> None:
        live = sd.confirm(self.after, sd.preview(self.after, ROW, "outside_books")["claim"])["claim"]
        self.assertEqual(live["basis"]["opening"]["kind"], "accrual")
        self.assertEqual(live["conflicts"], ["OPENING_AFTER_ROW"])
        result = sd.evaluate(self.after, [live])[0]
        self.assertEqual((result["status"], result["triggers"], result["transition_evidence"]),
                         ("resolved", [], None))
        # A decision stored by an earlier engine read the accrual opening as booked lines (no opening). It is
        # invalidated, and a person reviews it again.
        facts = [{"source_id": self.entry.source_id, "account": p.account, "date": "2026-06-30",
                  "amount": str(p.amount)} for p in self.entry.postings]
        earlier = dataclasses.replace(self.after, openings=(), booked=tuple(
            sd.Booked(f["source_id"], f["account"], date(2026, 6, 30), f["amount"]) for f in facts))
        old = sd.confirm(earlier, sd.preview(earlier, ROW, "outside_books")["claim"])["claim"]
        self.assertIsNone(old["basis"]["opening"])
        result = sd.evaluate(self.after, [old])[0]
        self.assertEqual((result["status"], result["triggers"]), ("invalidated", ["OPENING_CHANGED"]))

    def opened_with(self, operation_id: str) -> tuple[Path, Entry]:
        """A copy of the base books with the same native opening, applied with another ``operation_id``."""
        folder = Path(self.tmp.name) / f"collision-{len(list(Path(self.tmp.name).iterdir()))}"
        shutil.copytree(self.base, folder)
        plan = opening.preview_opening(load_entity(folder), {**self.plan["request"], "operation_id": operation_id})
        opening.apply_opening(load_entity(folder), plan)
        (entry,) = LedgerStore(folder / "ledger.sqlite").load_entries()
        return folder, entry

    def test_blocker_6_an_opening_source_id_that_names_the_row_is_booked(self) -> None:
        """The accrual opening's source ID records the row, so the row is never left out as outside_books."""
        for operation_id in (ROW, ROW.removeprefix("banksync:")):
            with self.subTest(operation_id=operation_id):
                folder, entry = self.opened_with(operation_id)
                self.assertEqual(entry.source_id, operation_id)
                context = sd.load_context(folder, AUTHORIZED)
                first = self.evaluate(context, authorization=self.v3(entry=entry))
                self.assertEqual(first["status"], "invalidated", first)
                self.assertIn("DISPOSED_ROW_BOOKED", first["triggers"])
                self.assertIsNone(first["transition_evidence"])
                self.assertEqual(sd.excluded_source_ids([first]), [])
                index = sd.ResolvedIdentityIndex(context, sd.recorded_source_ids(folder))
                self.assertIsNone(index.unique_unrecorded_row(sd.source_id_variants(ROW)))
                self.assertIsNone(sd.resolved_identity(index, self.claim, {**first, "status": "resolved"}))
                fresh = sd.preview(context, ROW, "outside_books")
                self.assertEqual((fresh["status"], fresh["reason"]), ("refused", "already_booked"))
                # The opening is still read as the accrual opening of both accounts.
                self.assertEqual({o.account for o in context.openings}, {CHECKING, RECEIVABLE})

    def test_blocker_6_an_unrelated_opening_source_id_still_resolves(self) -> None:
        folder, entry = self.opened_with("unrelated-accrual-opening")
        context = sd.load_context(folder, AUTHORIZED)
        first = self.evaluate(context, authorization=self.v3(entry=entry))
        self.assertEqual((first["status"], first["triggers"]), ("transition_pending", []), first)
        done = self.evaluate(context, {self.claim["preview_digest"]: first["transition_evidence"]})
        self.assertEqual(done["status"], "resolved")
        index = sd.ResolvedIdentityIndex(context, sd.recorded_source_ids(folder))
        self.assertEqual(sd.resolved_identity(index, self.claim, done)["source_id"], ROW)

    # -- refusals -----------------------------------------------------------------------------------------------

    def test_without_an_authorization_it_is_refused_and_visible(self) -> None:
        self.assert_refused(self.evaluate(), "ACCRUAL_AUTHORIZATION_REQUIRED")

    def test_a_same_day_accrual_entry_is_refused(self) -> None:
        same_day = dataclasses.replace(self.entry, date=date(2026, 7, 1))
        result = self.evaluate(self.with_entry(same_day), authorization=self.v3(entry=same_day))
        self.assert_refused(result, "ACCRUAL_OPENING_NOT_PRIOR_DAY")

    def test_a_wrong_fingerprint_or_source_id_is_refused(self) -> None:
        wrong = self.v3([self.row(CHECKING, entry_fingerprint="0" * 64),
                         self.row(RECEIVABLE, entry_fingerprint="0" * 64)])
        self.assert_refused(self.evaluate(authorization=wrong), "ACCRUAL_ENTRY_MISMATCH")
        other = self.v3([self.row(CHECKING, entry_source_id="another-plan"),
                         self.row(RECEIVABLE, entry_source_id="another-plan")])
        self.assert_refused(self.evaluate(authorization=other), "ACCRUAL_ENTRY_MISMATCH")

    def test_a_receivable_left_out_of_the_authorization_is_refused(self) -> None:
        result = self.evaluate(authorization=self.v3([self.row(CHECKING)]))
        self.assert_refused(result, "ACCRUAL_ACCOUNTS_MISMATCH")

    def test_a_receivable_outside_the_governed_set_is_refused(self) -> None:
        ungoverned = dataclasses.replace(self.after, policy=sd.parse_policy(policy("authorized", 2, 5, [CHECKING])))
        result = self.evaluate(ungoverned, authorization=self.v3())
        self.assert_refused(result, "ACCRUAL_ACCOUNT_NOT_GOVERNED")
        pending = dataclasses.replace(self.after, policy=sd.parse_policy({
            "schema_version": 1, "mode": "enforced", "epoch": 5,
            "accounts": {**account_rows("authorized", 2, [CHECKING]),
                         **account_rows("authorized_pending_opening", 1, [RECEIVABLE])}}))
        self.assert_refused(self.evaluate(pending, authorization=self.v3()), "ACCRUAL_ACCOUNT_NOT_GOVERNED")

    def test_a_version_2_same_day_authorization_is_refused(self) -> None:
        rows = [self.row(account, operation="accrual_opening") for account in (CHECKING, RECEIVABLE)]
        v2 = {"schema_version": 2, "policy": policy(), "scope": [CHECKING], "opening_authorizations": rows}
        self.assert_refused(self.evaluate(authorization=v2), "ACCRUAL_AUTHORIZATION_NOT_PRIOR_DAY")
        same_day = [self.row(account, operation="accrual_opening", start_day="2026-06-30")
                    for account in (CHECKING, RECEIVABLE)]
        v2_day = {**v2, "opening_authorizations": same_day}
        self.assert_refused(self.evaluate(authorization=v2_day), "ACCRUAL_AUTHORIZATION_NOT_PRIOR_DAY")

    def test_a_malformed_or_stale_authorization_is_refused(self) -> None:
        self.assert_refused(self.evaluate(authorization={"schema_version": 3}), "ACCRUAL_AUTHORIZATION_INVALID")
        later = self.v3([self.row(a, start_day="2026-08-01") for a in (CHECKING, RECEIVABLE)])
        later["policy"] = {"schema_version": 1, "mode": "enforced", "epoch": 4, "accounts": {
            a: {"state": "authorized_pending_opening", "start_day": "2026-08-01", "version": 1}
            for a in (CHECKING, RECEIVABLE)}}
        self.assert_refused(self.evaluate(authorization=later), "ACCRUAL_AUTHORIZATION_STALE")
        old_version = self.v3([self.row(a, version=7) for a in (CHECKING, RECEIVABLE)])
        old_version["policy"] = policy(version=7)
        self.assert_refused(self.evaluate(authorization=old_version), "ACCRUAL_AUTHORIZATION_STALE")

    def test_an_entry_that_is_not_a_fresh_accrual_opening_is_refused(self) -> None:
        meta = dict(self.entry.meta)
        cases = {
            "tracking key": {**meta, "accrual-tracking-opening": "true"},
            "summary key": {**meta, "summary-from": "2026-01-01"},
            "another cutover": {**meta, "cutover-date": "2026-08-01"},
            "another coverage": {**meta, "coverage-id": "accrual-tracking"},
        }
        for name, changed in cases.items():
            with self.subTest(name):
                entry = dataclasses.replace(self.entry, meta=tuple(changed.items()))
                result = self.evaluate(self.with_entry(entry), authorization=self.v3(entry=entry))
                self.assert_refused(result, "ACCRUAL_OPENING_NOT_FRESH")
        income = dataclasses.replace(self.entry, postings=(*self.entry.postings[:2],
                                                           Posting("Income:Sales", self.entry.postings[2].amount,
                                                                   "USD")))
        result = self.evaluate(self.with_entry(income), authorization=self.v3(entry=income))
        self.assert_refused(result, "ACCRUAL_OPENING_NOT_FRESH")

    def test_a_tracking_opening_is_never_an_opening_kind(self) -> None:
        """A tracking opening adds accrual balances to books that already started. It is not a books opening."""
        meta = {**dict(self.entry.meta), "import-source": "accrual-tracking-opening"}
        tracking = dataclasses.replace(self.entry, meta=tuple(meta.items()))
        with self.assertRaisesRegex(sd.DispositionInputError, "tracking"):
            sd.context_from_document({"openings": [{"account": CHECKING, "entry": sd.entry_document(tracking)}]})
        openings, booked = sd._ledger_facts([tracking])
        self.assertEqual(openings, [])
        self.assertEqual({b["account"] for b in booked}, {CHECKING, RECEIVABLE, EQUITY})
        result = self.evaluate(dataclasses.replace(self.after, openings=()), authorization=self.v3(entry=tracking))
        self.assertEqual(result["status"], "invalidated")
        self.assertIn("POLICY_CHANGED", result["triggers"])

    def test_the_authorization_does_not_change_a_statement_or_quickbooks_completion(self) -> None:
        amount = self.entry.postings[0].amount
        for kind, extra in (("statement-opening", (("statement-id", "s1"),)), ("quickbooks-opening", ())):
            with self.subTest(kind):
                same_day = Entry(date=date(2026, 7, 1), narration="Opening balance",
                                 meta=(("source-id", ROW if kind == "statement-opening" else f"{kind}-one"),
                                       ("import-source", kind), *extra),
                                 postings=(Posting(CHECKING, amount, "USD"), Posting(EQUITY, -amount, "USD")))
                context = dataclasses.replace(self.with_entry(same_day),
                                              open_dates={**self.after.open_dates, CHECKING: date(2026, 7, 1)})
                plain = self.evaluate(context)
                self.assertEqual((plain["status"], plain["transition_evidence"]["kind"]),
                                 ("transition_pending", "expected_opening_recorded"))
                self.assertEqual(self.evaluate(context, authorization=self.v3()), plain)
                done = self.evaluate(context, {self.claim["preview_digest"]: plain["transition_evidence"]})
                self.assertEqual(done["status"], "resolved")

if __name__ == "__main__":
    unittest.main()
