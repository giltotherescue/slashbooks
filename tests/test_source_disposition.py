"""Tests for ``bookkeeping.source_disposition``: pure evaluation of a person's decision to close a staged row.

The module stores nothing and trusts no host. All data is synthetic.
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping import source_disposition as sd  # noqa: E402

CHECKING = "Assets:Bank:Checking"
SAVINGS = "Assets:Bank:Savings"
SHA_A = "a" * 64
SHA_B = "b" * 64


def feed_row(source_id: str = "banksync:r1", day: str = "2026-02-10", amount: str = "-25.00", **extra) -> dict:
    return {"id": source_id, "date": day, "amount": amount, "description": "NORTHWIND COFFEE", "currency": "USD",
            "accountId": "feed-chk", "pending": False, **extra}


def statement_row(source_id: str = "stmt:s1:1", day: str = "2026-02-10", statement_id: str = "s1", **extra) -> dict:
    return {"id": source_id, "date": day, "amount": "-40.00", "description": "CONTOSO", "currency": "USD",
            "accountId": CHECKING, "csvMappingKey": "statement:chk", "statementId": statement_id, "pending": False,
            **extra}


def policy(state: str = "authorized", start: str = "2026-03-01", version: int = 2, epoch: int = 4,
           mode: str = "enforced", extra: dict | None = None) -> dict:
    account = {"state": state}
    if state != "unset":
        account.update(start_day=start, version=version)
    return {"schema_version": 1, "mode": mode, "epoch": epoch, "accounts": {CHECKING: account, **(extra or {})}}


def opening(source_id: str = "statement-opening-s2", day: str = "2026-03-01", amount: str = "1000.00", *,
            account: str = CHECKING, equity: str = "Equity:Opening-Balances", currency: str = "USD",
            statement_id: str = "s2") -> dict:
    """A complete opening entry in the context JSON format (every Entry and Posting field)."""
    negative = amount[1:] if amount.startswith("-") else "-" + amount
    entry = {"date": day, "narration": "Opening balance", "payee": None, "flag": "*", "tags": [], "links": [],
             "meta": [["source-id", source_id], ["import-source", "statement-opening"], ["statement-id", statement_id]],
             "postings": [{"account": account, "amount": amount, "currency": currency, "meta": []},
                          {"account": equity, "amount": negative, "currency": currency, "meta": []}]}
    return {"account": account, "entry": entry}


def context(**overrides) -> dict:
    doc = {
        "rows": [feed_row()],
        "bank_account_mappings": {"feed-chk": CHECKING, "feed-sav": SAVINGS},
        "csv_account_mappings": {"statement:chk": {"ledger_account": CHECKING, "confirmed": True}},
        "open_dates": {CHECKING: "2026-01-01", SAVINGS: "2026-01-01"},
        "openings": [opening()],
        "statements": [
            {"statement_id": "s1", "account": CHECKING, "period_start": "2026-02-01", "period_end": "2026-02-28",
             "evidence_sha256": SHA_A},
            {"statement_id": "s2", "account": CHECKING, "period_start": "2026-03-01", "period_end": "2026-03-31",
             "evidence_sha256": SHA_B},
        ],
        "booked": [],
        "policy": policy(),
    }
    doc.update(overrides)
    return doc


def ctx(**overrides):
    return sd.context_from_document(context(**overrides))


class RowIdentityTests(unittest.TestCase):
    def test_identity_has_both_spellings_and_a_digest_of_the_source_fields(self):
        identity = sd.row_identity(feed_row())
        self.assertEqual(identity["source_id"], "banksync:r1")
        self.assertEqual(identity["spellings"], ["banksync:r1", "r1"])
        self.assertRegex(identity["row_digest"], r"^[0-9a-f]{64}$")

    def test_derived_labels_do_not_change_the_digest_and_source_fields_do(self):
        base = sd.row_identity(feed_row())["row_digest"]
        labelled = feed_row(review_hold_reason="source_catalog_conflict", accrual_review_required=True,
                            accrual_review_action="Link a settlement")
        self.assertEqual(sd.row_identity(labelled)["row_digest"], base)
        self.assertNotEqual(sd.row_identity(feed_row(amount="-25.01"))["row_digest"], base)
        self.assertNotEqual(sd.row_identity(feed_row(description="OTHER"))["row_digest"], base)

    def test_every_label_that_the_importer_adds_is_a_derived_field(self):
        text = (SRC / "bookkeeping" / "ledger" / "importer.py").read_text(encoding="utf-8")
        added = set(re.findall(r'held\["([A-Za-z_]+)"\]\s*=', text))
        self.assertTrue(added)
        self.assertLessEqual(added, set(sd.DERIVED_FIELDS))


class PreviewTests(unittest.TestCase):
    def test_preview_echoes_the_date_account_reason_and_current_policy_version(self):
        result = sd.preview(ctx(), "r1", "outside_books")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["source_id"], "banksync:r1")
        self.assertEqual((result["row"]["date"], result["row"]["amount"], result["basis"]["account"]),
                         ("2026-02-10", "-25.00", CHECKING))
        self.assertEqual(result["reason_code"], "before_authorized_start")
        self.assertEqual(result["basis"]["policy"], {"mode": "enforced", "epoch": 4, "account_state": "authorized",
                                                     "account_version": 2, "start_day": "2026-03-01"})
        self.assertEqual(result["basis"]["catalog"], {"open_date": "2026-01-01"})
        self.assertRegex(result["preview_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(result["claim"]["preview_digest"], result["preview_digest"])

    def test_legacy_preview_uses_the_open_date_and_records_it(self):
        result = sd.preview(ctx(policy=None, open_dates={CHECKING: "2026-03-01"}), "r1", "outside_books")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["reason_code"], "before_open_date")
        self.assertEqual(result["basis"]["policy"], {"mode": "legacy"})
        self.assertEqual(result["basis"]["catalog"], {"open_date": "2026-03-01"})

    def test_preview_is_deterministic_and_reads_no_environment(self):
        first = sd.preview(ctx(), "r1", "outside_books")
        with mock.patch.dict(os.environ, {"SLASHBOOKS_BOOKS_POLICY": "/nonexistent.json",
                                          "SLASHBOOKS_INTAKE_ONLY": "1"}):
            second = sd.preview(ctx(), "r1", "outside_books")
        self.assertEqual(first, second)

    def test_reason_text_is_optional_short_plain_text(self):
        self.assertEqual(sd.preview(ctx(), "r1", "outside_books", reason_text="Paid before we started.")["status"],
                         "ok")
        for bad in ("x" * 281, "line\nbreak", "tab\there", 12):
            with self.subTest(bad=bad):
                result = sd.preview(ctx(), "r1", "outside_books", reason_text=bad)
                self.assertEqual(result["code"], "DISPOSITION_INPUT_INVALID")

    def test_unknown_state_and_unknown_row_are_refused(self):
        self.assertEqual(sd.preview(ctx(), "r1", "ignored")["code"], "DISPOSITION_INPUT_INVALID")
        self.assertEqual(sd.preview(ctx(), "nope", "outside_books")["code"], "DISPOSITION_ROW_NOT_FOUND")


class ConflictTests(unittest.TestCase):
    def codes(self, **overrides) -> list[str]:
        result = sd.preview(ctx(**overrides), "r1", "outside_books")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["state"], "outside_books")
        return result["conflicts"]

    def test_an_opening_after_the_row_is_shown(self):
        self.assertEqual(self.codes(), ["OPENING_AFTER_ROW"])

    def test_opening_and_open_dates_that_differ_from_the_start_are_shown(self):
        openings = [opening("o", "2026-03-05", "1.00")]
        self.assertEqual(self.codes(openings=openings, open_dates={CHECKING: "2026-03-03"}),
                         ["OPENING_AFTER_ROW", "OPENING_DATE_NOT_START", "OPEN_DATE_AFTER_START"])
        # An open date before the start is normal: the catalog lifespan is not the books start.
        self.assertEqual(self.codes(open_dates={CHECKING: "2025-06-01"}), ["OPENING_AFTER_ROW"])

    def test_no_opening_pending_opening_and_a_similar_booked_row_are_shown(self):
        booked = [{"source_id": "b1", "account": CHECKING, "date": "2026-02-10", "amount": "-25.00"}]
        self.assertEqual(self.codes(openings=[], policy=policy("authorized_pending_opening"), booked=booked,
                                    open_dates={CHECKING: "2026-01-01"}),
                         ["NO_OPENING", "OPENING_NOT_COMMITTED", "SIMILAR_ROW_BOOKED"])

    def test_conflicts_change_the_digest_and_never_the_state(self):
        plain = sd.preview(ctx(), "r1", "outside_books")
        other = sd.preview(ctx(openings=[]), "r1", "outside_books")
        self.assertNotEqual(plain["conflicts_digest"], other["conflicts_digest"])
        self.assertEqual((plain["state"], other["state"]), ("outside_books", "outside_books"))


class EligibilityTests(unittest.TestCase):
    def refusal(self, *, row=None, **overrides) -> str:
        doc = context(**overrides)
        if row is not None:
            doc["rows"] = [row]
        result = sd.preview(sd.context_from_document(doc), str((row or feed_row())["id"]), "outside_books")
        self.assertEqual((result["status"], result["code"]), ("refused", "DISPOSITION_NOT_ELIGIBLE"), result)
        return result["reason"]

    def test_rows_on_or_after_the_start_are_refused(self):
        self.assertEqual(self.refusal(row=feed_row(day="2026-03-01")), "on_or_after_start")

    def test_unset_and_missing_policy_accounts_are_refused(self):
        self.assertEqual(self.refusal(policy=policy("unset")), "start_unset")
        doc = policy()
        doc["accounts"] = {SAVINGS: doc["accounts"][CHECKING]}
        self.assertEqual(self.refusal(policy=doc), "policy_required")

    def test_legacy_rows_on_or_after_the_open_date_or_with_no_open_date_are_refused(self):
        self.assertEqual(self.refusal(policy=None), "on_or_after_open_date")
        self.assertEqual(self.refusal(policy=None, open_dates={}), "no_open_date")

    def test_pending_booked_badly_dated_and_unmapped_rows_are_refused(self):
        self.assertEqual(self.refusal(row=feed_row(pending=True)), "pending_row")
        self.assertEqual(self.refusal(booked=[{"source_id": "r1", "account": CHECKING, "date": "2026-02-10",
                                               "amount": "-25.00"}]), "already_booked")
        for bad in ("02/10/2026", "", None):
            with self.subTest(date=bad):
                row = feed_row()
                row["date"] = bad
                self.assertEqual(self.refusal(row=row), "invalid_date")
        self.assertEqual(self.refusal(row=feed_row(accountId="unknown-feed")), "account_not_mapped")


class PendingOpeningTests(unittest.TestCase):
    def test_outside_books_is_allowed_before_the_start_while_the_opening_is_pending(self):
        result = sd.preview(ctx(policy=policy("authorized_pending_opening"), openings=[]), "r1", "outside_books")
        self.assertEqual(result["status"], "ok", result)
        self.assertIn("OPENING_NOT_COMMITTED", result["conflicts"])
        self.assertEqual(result["basis"]["policy"]["account_state"], "authorized_pending_opening")
        self.assertIsNone(result["basis"]["opening"])

    def test_covered_by_opening_is_refused_while_the_opening_is_pending(self):
        result = sd.preview(ctx(policy=policy("authorized_pending_opening"), rows=[statement_row()]), "stmt:s1:1",
                            "covered_by_opening",
                            evidence={"kind": "statement_line", "statement_id": "s1"})
        self.assertEqual((result["code"], result["reason"]), ("DISPOSITION_NOT_ELIGIBLE", "opening_not_committed"))

    def test_a_disposition_never_unlocks_booking(self):
        context_ = ctx(policy=policy("authorized_pending_opening"), openings=[])
        claim = sd.preview(context_, "r1", "outside_books")["claim"]
        self.assertEqual(sd.evaluate(context_, [claim])[0]["status"], "resolved")
        hold = sd.reclassify(context_, "r1", claim["row_digest"])
        self.assertEqual(hold["date_hold_reason"], "OPENING_REQUIRED")
        self.assertFalse(hold["date_checks_pass"])


class CoverageProvenanceTests(unittest.TestCase):
    """Each case must be refused for ``covered_by_opening``.

    These six tests preserve the fail-before cases: the evaluator at ``da55876`` accepted every one of them. The
    engine keeps no immutable list of statement lines, so a row's ``statementId`` and fields cannot be checked
    against the line that the statement really had. The tests pass now only because every ``covered_by_opening``
    claim is refused on purpose. That refusal is deliberate unsupported behavior, not proof of line validation.
    """

    EVIDENCE = {"kind": "statement_line", "statement_id": "s1"}

    def assert_refused(self, rows, **overrides):
        doc = context(rows=rows, **overrides)
        result = sd.preview(sd.context_from_document(doc), str(rows[0]["id"]), "covered_by_opening",
                            evidence=self.EVIDENCE)
        self.assertEqual(result["status"], "refused", result)
        self.assertIn(result["code"], {"DISPOSITION_LINE_EVIDENCE_UNAVAILABLE", "DISPOSITION_EVIDENCE_MISMATCH"})

    def test_row_amount_changed_after_import(self):
        self.assert_refused([statement_row(amount="-41.00")])

    def test_row_date_changed_after_import(self):
        self.assert_refused([statement_row(day="2026-02-11")])

    def test_row_source_changed_after_import(self):
        self.assert_refused([statement_row(source_id="banksync:other-source")])

    def test_fabricated_statement_id_on_a_row_that_is_not_a_line(self):
        self.assert_refused([feed_row(statementId="s1", csvMappingKey="statement:chk", accountId=CHECKING)])

    def test_statement_file_replaced_or_its_lines_edited(self):
        statements = copy.deepcopy(context()["statements"])
        statements[0]["evidence_sha256"] = "d" * 64
        self.assert_refused([statement_row()], statements=statements)

    def test_a_duplicate_line_claimed_by_two_rows(self):
        rows = [statement_row(), statement_row(source_id="stmt:s1:dup")]
        self.assert_refused(rows)
        doc = context(rows=rows)
        second = sd.preview(sd.context_from_document(doc), "stmt:s1:dup", "covered_by_opening",
                            evidence=self.EVIDENCE)
        self.assertEqual(second["status"], "refused", second)


class CoverageTests(unittest.TestCase):
    EVIDENCE = {"kind": "statement_line", "statement_id": "s1"}

    def run_preview(self, row=None, evidence=EVIDENCE, **overrides):
        doc = context(**overrides)
        doc["rows"] = [row or statement_row()]
        return sd.preview(sd.context_from_document(doc), str((row or statement_row())["id"]),
                          "covered_by_opening", evidence=evidence)

    def assert_refused(self, code: str, reason: str, **kwargs):
        result = self.run_preview(**kwargs)
        self.assertEqual((result["status"], result["code"], result["reason"]), ("refused", code, reason), result)
        self.assertNotIn("claim", result)

    def test_no_evidence_and_a_date_alone_are_refused(self):
        self.assert_refused("DISPOSITION_EVIDENCE_REQUIRED", "evidence_required", evidence=None)
        self.assert_refused("DISPOSITION_EVIDENCE_REQUIRED", "evidence_required",
                            evidence={"kind": "date", "date": "2026-02-10"})

    def test_a_source_history_handoff_is_not_available_yet(self):
        self.assert_refused("DISPOSITION_HANDOFF_UNAVAILABLE", "handoff_unavailable",
                            evidence={"kind": "source_history_handoff", "source_ids": ["stmt:s1:1"]})

    def test_every_statement_line_claim_is_refused_until_retained_lines_exist(self):
        """The former "accepted" case (statementId equal, period inside, gapless chain) is refused too."""
        statements = context()["statements"]
        chained = [{"statement_id": "s0", "account": CHECKING, "period_start": "2026-01-01",
                    "period_end": "2026-01-31", "evidence_sha256": SHA_A}, *statements]
        cases = {
            "statementId, period and chain all match": {},
            "longer chain": {"row": statement_row(source_id="stmt:s0:1", day="2026-01-12", statement_id="s0"),
                             "evidence": {"kind": "statement_line", "statement_id": "s0"}, "statements": chained},
            "feed row": {"row": feed_row()},
            "unknown statement": {"statements": statements[1:]},
            "no opening": {"openings": []},
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                self.assert_refused("DISPOSITION_LINE_EVIDENCE_UNAVAILABLE", "no_retained_statement_lines", **kwargs)


class EvaluateTests(unittest.TestCase):
    def claim(self, context_doc=None, source_id="r1", state="outside_books", evidence=None):
        result = sd.preview(sd.context_from_document(context_doc or context()), source_id, state, evidence=evidence)
        self.assertEqual(result["status"], "ok", result)
        return result["claim"]

    def status(self, claim, **overrides):
        return sd.evaluate(sd.context_from_document(context(**overrides)), [claim])[0]

    def test_an_unchanged_claim_is_resolved(self):
        self.assertEqual(self.status(self.claim()), {"source_id": "banksync:r1", "state": "outside_books",
                                                     "status": "resolved", "triggers": [], "notes": [],
                                                     "transition_evidence": None})

    def test_a_policy_change_for_the_account_invalidates_and_an_epoch_change_alone_does_not(self):
        claim = self.claim()
        self.assertEqual(self.status(claim, policy=policy(epoch=9))["status"], "resolved")
        self.assertEqual(self.status(claim, policy=policy(epoch=9, extra={SAVINGS: {"state": "unset"}}))["status"],
                         "resolved")
        self.assertEqual(self.status(claim, policy=policy(version=3))["triggers"], ["POLICY_CHANGED"])
        moved = self.status(claim, policy=policy(start="2026-02-01", version=3))
        self.assertEqual(moved["triggers"], ["NOT_ELIGIBLE_NOW", "POLICY_CHANGED"])

    def test_mapping_open_date_and_opening_changes_invalidate(self):
        claim = self.claim()
        self.assertIn("MAPPING_CHANGED", self.status(
            claim, bank_account_mappings={"feed-chk": SAVINGS}, policy=policy(extra={SAVINGS: {
                "state": "authorized", "start_day": "2026-03-01", "version": 1}}))["triggers"])
        self.assertEqual(self.status(claim, open_dates={CHECKING: "2025-12-01"})["triggers"], ["OPEN_DATE_CHANGED"])
        self.assertIn("OPENING_CHANGED", self.status(claim, openings=[])["triggers"])

    def test_a_stored_coverage_claim_never_resolves_and_is_not_confirmed(self):
        claim = dict(self.claim(), state="covered_by_opening")
        result = self.status(claim)
        self.assertEqual((result["status"], result["triggers"]), ("invalidated", ["LINE_EVIDENCE_UNAVAILABLE"]))
        self.assertEqual(sd.confirm(ctx(), claim)["status"], "refused")

    def test_row_changes_invalidate(self):
        claim = self.claim()
        self.assertEqual(self.status(claim, rows=[feed_row(amount="-26.00")])["triggers"], ["SOURCE_ROW_CHANGED"])
        self.assertEqual(self.status(claim, rows=[])["triggers"], ["SOURCE_ROW_CHANGED"])
        booked = [{"source_id": "banksync:r1", "account": CHECKING, "date": "2026-02-10", "amount": "-25.00"}]
        self.assertIn("DISPOSED_ROW_BOOKED", self.status(claim, rows=[], booked=booked)["triggers"])

    def test_a_derived_label_change_does_not_invalidate(self):
        claim = self.claim()
        self.assertEqual(self.status(claim, rows=[feed_row(review_hold_reason="OPENING_REQUIRED")])["status"],
                         "resolved")

    def test_a_claim_without_its_preview_digest_is_not_confirmed(self):
        claim = self.claim()
        unconfirmed = {k: v for k, v in claim.items() if k != "preview_digest"}
        self.assertEqual(sd.confirm(ctx(), unconfirmed)["code"], "DISPOSITION_CONFIRMATION_REQUIRED")
        self.assertEqual(sd.confirm(ctx(), claim)["status"], "ok")
        changed = sd.confirm(ctx(policy=policy(version=3)), claim)
        self.assertEqual(changed["code"], "DISPOSITION_PREVIEW_CHANGED")
        self.assertEqual(changed["preview"]["basis"]["policy"]["account_version"], 3)

    def test_only_resolved_rows_are_excluded(self):
        claim = self.claim()
        results = sd.evaluate(ctx(policy=policy(version=3)), [claim])
        self.assertEqual(sd.excluded_source_ids(results), [])
        self.assertEqual(sd.excluded_source_ids(sd.evaluate(ctx(), [claim])), ["banksync:r1"])


class ResolvedIdentityTests(unittest.TestCase):
    """The one rule for leaving a staged row out: a resolved claim, one exact row, not booked or recorded."""

    def setUp(self):
        self.claim = sd.preview(ctx(), "r1", "outside_books")["claim"]

    def identity(self, context_=None, claim=None, result=None, recorded=()):
        context_ = context_ or ctx()
        claim = claim or self.claim
        result = result or sd.evaluate(context_, [claim])[0]
        return sd.resolved_identity(sd.ResolvedIdentityIndex(context_, recorded), claim, result)

    def test_a_resolved_claim_names_its_exact_row(self):
        identity = sd.row_identity(feed_row())
        self.assertEqual(self.identity(), {**identity, "preview_digest": self.claim["preview_digest"]})

    def test_an_unresolved_claim_names_nothing(self):
        self.assertIsNone(self.identity(ctx(policy=policy(version=3))))
        self.assertIsNone(self.identity(ctx(rows=[feed_row(amount="-26.00")])))

    def test_a_result_for_another_claim_or_a_made_up_result_names_nothing(self):
        resolved = {"source_id": "banksync:r1", "state": "outside_books", "status": "resolved"}
        unknown = {**self.claim, "source_id": "banksync:nope"}
        self.assertIsNone(self.identity(claim=unknown, result={**resolved, "source_id": "banksync:nope"}))
        other = {**self.claim, "source_id": "banksync:r2"}
        rows = [feed_row(), feed_row("banksync:r2", "2026-02-11")]
        self.assertIsNone(self.identity(ctx(rows=rows), claim=other, result={**resolved, "source_id": "banksync:r2"}))
        self.assertIsNone(self.identity(result={**resolved, "source_id": "banksync:r2"}))
        self.assertIsNone(self.identity(claim={**self.claim, "state": "covered_by_opening"}, result=resolved))
        unconfirmed = {k: v for k, v in self.claim.items() if k != "preview_digest"}
        self.assertEqual(sd.evaluate(ctx(), [unconfirmed])[0]["status"], "resolved")
        self.assertIsNone(self.identity(claim=unconfirmed))

    def test_two_rows_with_one_spelling_are_ambiguous(self):
        both = ctx(rows=[feed_row(), feed_row("r1")])
        self.assertEqual(sd.evaluate(both, [self.claim])[0]["status"], "resolved")
        self.assertIsNone(self.identity(both))

    def test_repeated_row_position_is_ambiguous_even_for_the_same_object(self):
        row = feed_row()
        self.assertIsNone(self.identity(ctx(rows=[row, row])))

    def test_alias_positions_are_unioned_without_transitive_expansion(self):
        rows = [feed_row(), feed_row("banksync:banksync:r1")]
        self.assertIsNone(self.identity(ctx(rows=rows)))
        self.assertIsNotNone(self.identity(ctx(rows=[feed_row(), feed_row("plaid:r1")])))

    def test_new_context_rebuilds_the_index_and_never_reuses_prior_ledger_membership(self):
        native = ctx()
        result = sd.evaluate(native, [self.claim])[0]
        index = sd.ResolvedIdentityIndex(native, [])
        before = sd.resolved_identity(index, self.claim, result)
        self.assertIsNotNone(before)
        changed = ctx(rows=[feed_row(amount="-26.00")])
        self.assertIsNone(sd.resolved_identity(sd.ResolvedIdentityIndex(changed, []), self.claim, result))
        self.assertIsNone(sd.resolved_identity(sd.ResolvedIdentityIndex(native, ["r1"]), self.claim, result))
        self.assertEqual(sd.resolved_identity(index, self.claim, result), before)

    def test_a_booked_or_recorded_spelling_names_nothing(self):
        booked = [{"source_id": "r1", "account": SAVINGS, "date": "2026-02-10", "amount": "-25.00"}]
        self.assertIsNone(self.identity(ctx(booked=booked),
                                        result=sd.evaluate(ctx(), [self.claim])[0]))
        # A sealed ``source_transactions`` row (posted, or no financial effect) under the legacy spelling.
        self.assertIsNone(self.identity(recorded=["r1"]))
        self.assertIsNone(self.identity(recorded=["banksync:r1"]))
        self.assertIsNotNone(self.identity(recorded=["plaid:r1"]))

    def test_recorded_source_ids_reads_every_status_and_spelling(self):
        from bookkeeping.ledger.store import LedgerStore, default_store_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(sd.recorded_source_ids(root), frozenset())
            store = LedgerStore(default_store_path(root))
            store.initialize()
            with store.transaction() as conn:
                store.insert_source_transactions([{"id": "r1"}], conn, status="no_financial_effect")
                store.insert_source_transactions([{"id": "plaid:p2"}], conn)
            self.assertEqual(sd.recorded_source_ids(root), {"r1", "banksync:r1", "plaid:p2"})


class EpochRuleTests(unittest.TestCase):
    """Two separate rules. Rule 1: a stored decision survives an epoch-only change when its basis is unchanged.
    Rule 2: the confirmation fences the full epoch it captured; a host publication asserts that same epoch."""

    def test_rule_1_a_stored_decision_survives_an_epoch_only_change(self):
        claim = sd.preview(ctx(), "r1", "outside_books")["claim"]
        other = policy(epoch=9, extra={SAVINGS: {"state": "authorized", "start_day": "2026-05-01", "version": 1}})
        self.assertEqual(sd.evaluate(ctx(policy=other), [claim])[0]["status"], "resolved")

    def test_rule_2_confirmation_fences_the_full_captured_epoch(self):
        claim = sd.preview(ctx(), "r1", "outside_books")["claim"]
        self.assertEqual(claim["basis"]["policy"]["epoch"], 4)
        self.assertEqual(sd.captured_epoch(claim), 4)
        refused = sd.confirm(ctx(policy=policy(epoch=5)), claim)
        self.assertEqual(refused["code"], "DISPOSITION_PREVIEW_CHANGED")
        legacy = sd.preview(ctx(policy=None, open_dates={CHECKING: "2026-03-01"}), "r1", "outside_books")["claim"]
        self.assertIsNone(sd.captured_epoch(legacy))


class ExpectedOpeningTransitionTests(unittest.TestCase):
    """Pending opening -> one trusted opening completion under the same start, account and mapping.

    The person's decision never changes. The first evaluation after the opening is recorded proposes transition
    evidence (the opening that completed it). The host records that evidence one time and passes it to every later
    evaluation, so a later reversal or replacement of that opening is a material change.
    """

    PENDING = dict(policy=policy("authorized_pending_opening", version=2), openings=[])
    OPENING_A = opening("statement-opening-a")
    OPENING_B = opening("statement-opening-b", amount="1200.00")

    def claim(self):
        result = sd.preview(ctx(**self.PENDING), "r1", "outside_books")
        self.assertEqual(result["status"], "ok", result)
        return result["claim"]

    def evaluate(self, claim, transitions=None, **overrides):
        doc = dict(policy=policy("authorized", version=3, epoch=5), openings=[self.OPENING_A])
        doc.update(overrides)
        return sd.evaluate(ctx(**doc), [claim], transitions=transitions)[0]

    def completed(self, claim):
        """The first evaluation after completion, and the evidence that the host records one time."""
        first = self.evaluate(claim)
        self.assertEqual((first["status"], first["triggers"]), ("transition_pending", []), first)
        evidence = first["transition_evidence"]
        self.assertEqual({k: evidence[k] for k in ("kind", "source_id", "claim_preview_digest", "account",
                                                   "start_day", "account_version")},
                         {"kind": "expected_opening_recorded", "source_id": "banksync:r1",
                          "claim_preview_digest": claim["preview_digest"], "account": CHECKING,
                          "start_day": "2026-03-01", "account_version": 3})
        self.assertEqual(evidence["opening"]["source_id"], "statement-opening-a")
        return {claim["preview_digest"]: evidence}

    def test_the_expected_opening_keeps_the_exclusion_without_a_second_prompt(self):
        claim = self.claim()
        self.assertEqual(sd.evaluate(ctx(**self.PENDING), [claim])[0]["status"], "resolved")
        transitions = self.completed(claim)
        for _ in range(3):
            result = self.evaluate(claim, transitions)
            self.assertEqual((result["status"], result["triggers"], result["state"]), ("resolved", [], "outside_books"))
            self.assertEqual(result["notes"], ["EXPECTED_OPENING_RECORDED"])
        # It never becomes coverage, and it never allows a booking.
        hold = sd.reclassify(ctx(policy=policy("authorized", version=3, epoch=5), openings=[self.OPENING_A]), "r1",
                             claim["row_digest"])
        self.assertEqual((hold["date_checks_pass"], hold["date_hold_reason"]),
                         (False, "BOOKS_DATE_BEFORE_AUTHORIZED_START"))

    def test_a_replacement_opening_on_the_same_day_and_version_is_material(self):
        claim = self.claim()
        transitions = self.completed(claim)
        self.assertEqual(self.evaluate(claim, transitions)["status"], "resolved")
        result = self.evaluate(claim, transitions, openings=[self.OPENING_B])
        self.assertEqual((result["status"], result["triggers"]), ("invalidated", ["OPENING_CHANGED"]))

    def test_the_completed_opening_disappearing_invalidates(self):
        claim = self.claim()
        result = self.evaluate(claim, self.completed(claim), openings=[])
        self.assertEqual(result["status"], "invalidated")
        self.assertIn("OPENING_CHANGED", result["triggers"])

    def test_a_version_change_after_completion_invalidates(self):
        claim = self.claim()
        result = self.evaluate(claim, self.completed(claim), policy=policy("authorized", version=4))
        self.assertEqual((result["status"], result["triggers"]), ("invalidated", ["POLICY_CHANGED"]))

    def test_without_transition_evidence_after_completion_it_is_not_resolved(self):
        """It returns ``transition_pending`` and proposes evidence for the opening in force now (here B). A host that
        already recorded A must pass A; it must never record a second completion."""
        claim = self.claim()
        self.completed(claim)
        result = self.evaluate(claim, None, openings=[self.OPENING_B])
        self.assertEqual(result["status"], "transition_pending")
        self.assertEqual(result["transition_evidence"]["opening"]["source_id"], "statement-opening-b")
        self.assertEqual(sd.excluded_source_ids([result]), [])

    def test_evidence_for_another_claim_or_a_changed_record_is_refused(self):
        claim = self.claim()
        evidence = self.completed(claim)[claim["preview_digest"]]
        other = {claim["preview_digest"]: dict(evidence, claim_preview_digest="0" * 64)}
        self.assertEqual(self.evaluate(claim, other)["triggers"], ["TRANSITION_EVIDENCE_INVALID"])
        edited = {claim["preview_digest"]: dict(evidence, account_version=4)}
        self.assertEqual(self.evaluate(claim, edited)["triggers"], ["TRANSITION_EVIDENCE_INVALID"])

    def test_the_opening_digest_covers_every_posting(self):
        claim = self.claim()
        transitions = self.completed(claim)
        changed = opening("statement-opening-a", equity="Equity:Owner")
        self.assertEqual(self.evaluate(claim, transitions, openings=[changed])["triggers"], ["OPENING_CHANGED"])

    @staticmethod
    def resign(evidence):
        body = {k: v for k, v in evidence.items() if k != "evidence_digest"}
        return {**body, "evidence_digest": sd._sha(body)}

    def test_malformed_evidence_is_refused_even_with_a_recomputed_digest(self):
        claim = self.claim()
        evidence = self.completed(claim)[claim["preview_digest"]]

        def nested(**changes):
            return self.resign({**evidence, "opening": {**evidence["opening"], **changes}})

        def posting(**changes):
            entry = copy.deepcopy(evidence["opening"]["entry"])
            entry["postings"][0].update(changes)
            return nested(entry=entry)

        missing = {k: v for k, v in evidence["opening"].items() if k != "digest"}
        cases = {
            "schema_version": self.resign({**evidence, "schema_version": 2}),
            "missing nested key": self.resign({**evidence, "opening": missing}),
            "extra nested key": nested(extra=True),
            "wrong type": nested(date=20260301),
            "non-finite amount": posting(amount="NaN"),
            "infinite amount": posting(amount="Infinity"),
            "number instead of text": posting(amount=1000),
            "extra posting field": posting(note="x"),
            "entry without a field": nested(entry={k: v for k, v in evidence["opening"]["entry"].items()
                                                   if k != "payee"}),
            "digest of another entry": nested(entry=self.OPENING_B["entry"]),
            "version as text": self.resign({**evidence, "account_version": "3"}),
        }
        for name, bad in cases.items():
            with self.subTest(name):
                result = self.evaluate(claim, {claim["preview_digest"]: bad})
                self.assertEqual(result["triggers"], ["TRANSITION_EVIDENCE_INVALID"], result)

    def test_material_changes_still_invalidate(self):
        claim = self.claim()
        moved_opening = opening("statement-opening-a", "2026-03-05")
        savings = {SAVINGS: {"state": "authorized", "start_day": "2026-03-01", "version": 1}}
        cases = {
            "opening on another day": ({"openings": [moved_opening]}, "OPENING_CHANGED"),
            "another start": ({"policy": policy("authorized", start="2026-04-01", version=3)}, "POLICY_CHANGED"),
            "version skipped": ({"policy": policy("authorized", version=4)}, "POLICY_CHANGED"),
            "mapping changed": ({"bank_account_mappings": {"feed-chk": SAVINGS},
                                 "policy": policy("authorized", version=3, extra=savings)}, "MAPPING_CHANGED"),
            "new conflict": ({"booked": [{"source_id": "b9", "account": CHECKING, "date": "2026-02-10",
                                          "amount": "-25.00"}]}, "CONFLICTS_CHANGED"),
            "no opening yet": ({"openings": []}, "POLICY_CHANGED"),
            "open date changed": ({"open_dates": {CHECKING: "2025-12-01"}}, "OPEN_DATE_CHANGED"),
        }
        for name, (overrides, trigger) in cases.items():
            with self.subTest(name):
                result = self.evaluate(claim, None, **overrides)
                self.assertEqual(result["status"], "invalidated", result)
                self.assertIn(trigger, result["triggers"])
                self.assertEqual(result["notes"], [])

    def test_a_claim_from_an_authorized_account_does_not_use_the_transition(self):
        claim = sd.preview(ctx(), "r1", "outside_books")["claim"]
        self.assertEqual(sd.evaluate(ctx(policy=policy(version=3)), [claim])[0]["triggers"], ["POLICY_CHANGED"])


class ReclassifyTests(unittest.TestCase):
    def test_date_checks_pass_does_not_mean_bookable(self):
        """Only two date checks run. An ask-first or accrual-held row stays protected, and its labels are not cleared."""
        rows = [feed_row(review_hold_reason="above_ask_first_amount", accrual_review_required=True,
                         accrual_review_action="Link a settlement")]
        digest = sd.row_identity(rows[0])["row_digest"]
        result = sd.reclassify(ctx(rows=rows, policy=None, open_dates={CHECKING: "2026-01-01"}), "r1", digest)
        self.assertEqual((result["status"], result["date_checks_pass"], result["date_hold_reason"]), ("ok", True, None))
        self.assertNotIn("may_book", result)
        self.assertNotIn("review_hold_reason", result, "a host must not overwrite the row's other review labels")
        self.assertEqual(result["not_checked"], ["accrual_review", "ask_first_amount", "ordinary_review_and_booking"])

    def test_restore_uses_the_current_guards_not_the_stored_label(self):
        row = feed_row(review_hold_reason="source_catalog_conflict")
        digest = sd.row_identity(row)["row_digest"]
        legacy = ctx(rows=[row], policy=None, open_dates={CHECKING: "2026-03-01"})
        self.assertEqual(sd.reclassify(legacy, "r1", digest)["date_hold_reason"], "source_catalog_conflict")
        opened_earlier = ctx(rows=[row], policy=None, open_dates={CHECKING: "2026-01-01"})
        result = sd.reclassify(opened_earlier, "r1", digest)
        self.assertEqual((result["date_hold_reason"], result["date_checks_pass"]), (None, True))
        enforced = sd.reclassify(ctx(rows=[row]), "r1", digest)
        self.assertEqual(enforced["date_hold_reason"], "BOOKS_DATE_BEFORE_AUTHORIZED_START")

    def test_a_changed_row_is_not_restored(self):
        result = sd.reclassify(ctx(), "r1", "0" * 64)
        self.assertEqual((result["status"], result["code"]), ("refused", "SOURCE_ROW_CHANGED"))


class OpeningFingerprintWorkspaceTests(unittest.TestCase):
    """The completing opening is fingerprinted as a complete ledger entry, read from a real company ledger.

    Each variant is a separate synthetic company whose opening differs from opening A in one way only.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.count = 0

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def opening_entry(*, currency="USD", posting_meta=(), payee=None, narration="Opening balance",
                      entry_meta=(), amount="1000.00"):
        from decimal import Decimal
        from bookkeeping.ledger.model import Entry, Posting

        return Entry(date=date(2026, 3, 1), narration=narration, payee=payee,
                     meta=(("source-id", "statement-opening-a"), ("import-source", "statement-opening"),
                           *entry_meta),
                     postings=(Posting(CHECKING, Decimal(amount), currency, tuple(posting_meta)),
                               Posting("Equity:Opening-Balances", -Decimal(amount), currency)))

    def company(self, opening=None) -> Path:
        from bookkeeping.entity import load_entity
        from bookkeeping.ledger.importer import import_transactions
        from bookkeeping.ledger.model import Open
        from bookkeeping.ledger.store import LedgerStore, default_store_path

        self.count += 1
        entity = self.root / f"co{self.count}"
        (entity / "staging").mkdir(parents=True)
        (entity / "entity.json").write_text(json.dumps({
            "name": "Synthetic Co", "business_type": "consulting",
            "bank_account_mappings": {"feed-chk": CHECKING}}) + "\n", encoding="utf-8")
        (entity / "trust-policy.json").write_text(json.dumps({"auto_post_threshold": 3}) + "\n", encoding="utf-8")
        store = LedgerStore(default_store_path(entity))
        store.initialize()
        with store.transaction() as conn:
            store.insert_opens([Open(date=date(2026, 1, 1), account=CHECKING),
                                Open(date=date(2026, 1, 1), account="Equity:Opening-Balances")], conn)
        import_transactions(load_entity(entity), [feed_row()], session_id="t")
        if opening is not None:
            with store.transaction() as conn:
                store.insert_entries([opening], conn)
        return entity

    def run_case(self, variant):
        pending = policy("authorized_pending_opening", version=2)
        authorized = policy("authorized", version=3, epoch=5)
        claim = sd.preview(sd.load_context(self.company(), pending), "r1", "outside_books")["claim"]
        first = sd.evaluate(sd.load_context(self.company(self.opening_entry()), authorized), [claim])[0]
        self.assertEqual(first["status"], "transition_pending", first)
        transitions = {claim["preview_digest"]: first["transition_evidence"]}
        return sd.evaluate(sd.load_context(self.company(variant), authorized), [claim], transitions)[0]

    def test_an_unchanged_opening_stays_resolved(self):
        result = self.run_case(self.opening_entry())
        self.assertEqual((result["status"], result["triggers"]), ("resolved", []))

    def test_a_currency_only_change_is_material(self):
        self.assertEqual(self.run_case(self.opening_entry(currency="EUR"))["triggers"], ["OPENING_CHANGED"])

    def test_a_posting_metadata_only_change_is_material(self):
        result = self.run_case(self.opening_entry(posting_meta=(("qb-name", "Checking"),)))
        self.assertEqual(result["triggers"], ["OPENING_CHANGED"])

    def test_an_entry_metadata_or_payee_change_is_material(self):
        self.assertEqual(self.run_case(self.opening_entry(payee="First Bank"))["triggers"], ["OPENING_CHANGED"])
        result = self.run_case(self.opening_entry(entry_meta=(("note", "corrected"),)))
        self.assertEqual(result["triggers"], ["OPENING_CHANGED"])

    def test_a_replacement_with_the_same_source_id_date_and_amount_is_material(self):
        result = self.run_case(self.opening_entry(narration="Opening balance, corrected"))
        self.assertEqual(result["triggers"], ["OPENING_CHANGED"])


class WorkspaceAndCommandTests(unittest.TestCase):
    def setUp(self):
        from bookkeeping.entity import load_entity
        from bookkeeping.ledger.importer import import_transactions
        from bookkeeping.ledger.model import Open
        from bookkeeping.ledger.store import LedgerStore, default_store_path

        self._tmp = tempfile.TemporaryDirectory()
        self.entity = Path(self._tmp.name) / "co"
        (self.entity / "staging").mkdir(parents=True)
        (self.entity / "entity.json").write_text(json.dumps({
            "name": "Synthetic Co", "business_type": "consulting",
            "bank_account_mappings": {"feed-chk": CHECKING}}) + "\n", encoding="utf-8")
        (self.entity / "trust-policy.json").write_text(json.dumps({"auto_post_threshold": 3}) + "\n",
                                                       encoding="utf-8")
        store = LedgerStore(default_store_path(self.entity))
        store.initialize()
        with store.transaction() as conn:
            store.insert_opens([Open(date=date(2026, 3, 1), account=CHECKING)], conn)
        result = import_transactions(load_entity(self.entity), [feed_row(), feed_row("banksync:r2", "2026-03-04")],
                                     session_id="t")
        self.assertEqual(result.pending_categorization, 2)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_context_reads_a_workspace_and_changes_nothing(self):
        from bookkeeping.ledger.books_policy_check import workspace_digest

        before = workspace_digest(self.entity)
        context_ = sd.load_context(self.entity)
        result = sd.preview(context_, "r1", "outside_books")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual((result["reason_code"], result["conflicts"]), ("before_open_date", ["NO_OPENING"]))
        self.assertEqual(sd.preview(context_, "r2", "outside_books")["reason"], "on_or_after_open_date")
        self.assertEqual(workspace_digest(self.entity), before)

    def test_intake_keeps_a_statement_id_that_a_connector_supplies(self):
        """A break point: ``statementId`` on a staged row is a plain field, not proof that a statement had the line."""
        from bookkeeping.entity import load_entity
        from bookkeeping.ledger.importer import import_transactions

        forged = feed_row("banksync:r3", "2026-02-12", statementId="st-0123456789abcdef")
        import_transactions(load_entity(self.entity), [forged], session_id="t2")
        staged = json.loads((self.entity / "staging" / "pending-categorization.json").read_text(encoding="utf-8"))
        self.assertEqual([row.get("statementId") for row in staged if row["id"] == "banksync:r3"],
                         ["st-0123456789abcdef"])
        result = sd.preview(sd.load_context(self.entity), "r3", "covered_by_opening",
                            evidence={"kind": "statement_line", "statement_id": "st-0123456789abcdef"})
        self.assertEqual(result["code"], "DISPOSITION_LINE_EVIDENCE_UNAVAILABLE")

    def request(self, document: dict) -> Path:
        path = Path(self._tmp.name) / "request.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_check_command_exit_codes(self):
        preview = self.request({"schema_version": 1, "action": "preview", "source_id": "r1",
                                "state": "outside_books", "policy": None})
        out = io.StringIO()
        with redirect_stdout(out):
            code = sd.main(["check", "--request", str(preview), "--entity", str(self.entity)])
        self.assertEqual(code, 0, out.getvalue())
        claim = json.loads(out.getvalue())["claim"]
        claim["row_digest"] = "0" * 64
        evaluate = self.request({"schema_version": 1, "action": "evaluate", "claims": [claim], "policy": None})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(sd.main(["check", "--request", str(evaluate), "--entity", str(self.entity)]), 1)
        self.assertEqual(json.loads(out.getvalue())["results"][0]["triggers"], ["SOURCE_ROW_CHANGED"])
        bad = self.request({"schema_version": 1, "action": "preview", "source_id": "r1", "state": "outside_books",
                            "unknown": True, "policy": None})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(sd.main(["check", "--request", str(bad), "--entity", str(self.entity)]), 2)

    def test_check_runs_as_an_isolated_process(self):
        request = self.request({"schema_version": 1, "action": "preview", "source_id": "banksync:r1",
                                "state": "outside_books", "policy": None})
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from bookkeeping.source_disposition import main; "
                "raise SystemExit(main(sys.argv[2:]))")
        done = subprocess.run([sys.executable, "-I", "-c", code, str(SRC), "check", "--request", str(request),
                               "--entity", str(self.entity)], capture_output=True, text=True, check=False,
                              env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["reason_code"], "before_open_date")


if __name__ == "__main__":
    unittest.main()
