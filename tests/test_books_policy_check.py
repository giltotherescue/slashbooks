"""The trusted books start check refuses a proposed ledger change that a session got past the in-command check.

A session that runs the engine controls ``SLASHBOOKS_BOOKS_POLICY``. It can remove the variable or the file,
drop accounts from the document, or set ``mode`` to ``legacy``. The host's policy epoch stays the same, so an
epoch check at publication does not see it. The host therefore runs ``ledger.books_policy_check`` on the
proposed workspace with its own immutable policy, and publishes nothing when the check refuses.

Synthetic company: a QuickBooks cutover on S = 2026-03-15 through the real ``qb sync-opening --apply``
(Checking +100.00 on S), then five bank rows from Jan 1. The correct Checking balance is 56.00.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_books_start_policy as shared  # noqa: E402
from bookkeeping import quickbooks_sync as qb_sync  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.ledger.importer import _atomic_ledger_write, reverse_entry  # noqa: E402
from bookkeeping.ledger.model import Entry, Open, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src"
S, CHECKING, FEES, ENV = shared.S, shared.CHECKING, shared.FEES, shared.ENV
ROWS, BEFORE_S = shared.ROWS, shared.BEFORE_S
EQUITY = "Equity:Opening-Balances"
BEFORE_START, OPENING_REQUIRED = shared.BEFORE_START, shared.OPENING_REQUIRED
POLICY_REQUIRED, HISTORY_CHANGED = shared.POLICY_REQUIRED, "BOOKS_LEDGER_HISTORY_CHANGED"


def checker():
    """The trusted check module. Imported here, so each test fails on its own before the module exists."""
    from bookkeeping.ledger import books_policy_check
    return books_policy_check


class TrustedCheckTests(shared.Company):
    def setUp(self) -> None:
        super().setUp()
        text = shared.FIXTURE.read_text(encoding="utf-8")
        for old, new in shared.SHIFT.items():
            text = text.replace(f'"{old}"', f'"{new}"')
        self.snapshot_path = self.root / "quickbooks.json"
        self.snapshot_path.write_text(text, encoding="utf-8")
        self.published = self.root / "published"

    # -- helpers ---------------------------------------------------------------

    def open_books(self) -> None:
        applied = qb_sync.sync_opening(load_entity(self.path), self.snapshot_path, date.fromisoformat(S), apply=True)
        self.assertTrue(applied["applied"], applied)
        self.map_feed("feed-1", CHECKING)
        self.stage(shared.row(*item) for item in ROWS)
        self.freeze_published()

    def freeze_published(self) -> None:
        """The workspace that the host published before the job. The job works on ``self.path``."""
        if self.published.exists():
            shutil.rmtree(self.published)
        shutil.copytree(self.path, self.published)

    def host_request(self, accounts: dict | None = None, openings=(), epoch: int = 1) -> dict:
        """The host's immutable job snapshot: epoch 1, Checking authorized from S, scope from its chart."""
        return {"schema_version": 1, "policy": shared.enforced(accounts or {CHECKING: shared.authorized()}, epoch),
                "scope": [CHECKING], "opening_authorizations": list(openings)}

    def published_digest(self) -> str:
        return LedgerStore(self.published / "ledger.sqlite").content_digest()

    def publish(self, request: dict) -> dict:
        """The host's publication: the trusted check first, then the copy only when the check says ok."""
        result = checker().check_workspaces(request, self.published, self.path)
        if result["status"] == "ok":
            shutil.rmtree(self.published)
            shutil.copytree(self.path, self.published)
        return result

    def assert_refused_at_publish(self, codes: dict[str, str]) -> dict:
        before = self.published_digest()
        result = self.publish(self.host_request())
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual({r["source_id"]: r["code"] for r in result["refusals"]}, codes)
        self.assertTrue(all(r["policy_epoch"] == 1 for r in result["refusals"]))
        self.assertEqual(self.published_digest(), before, "nothing may be published")
        return result

    def direct_write(self, sid: str, day: str, amount: str) -> None:
        _atomic_ledger_write(self.entity, [], [Entry(
            date=date.fromisoformat(day), narration="Synthetic row", meta=(("source-id", sid),),
            postings=(Posting(CHECKING, Decimal(amount)), Posting(FEES, -Decimal(amount))))], "agent", None, "agent write")

    def pre_s_booked(self) -> list[str]:
        return sorted(set(BEFORE_S) & set(self.booked()))

    # -- a valid change publishes ----------------------------------------------

    def test_a_valid_change_passes_and_publishes(self) -> None:
        self.open_books()
        with self.policy(shared.enforced({CHECKING: shared.authorized()})):
            for sid in ("mar15", "apr02"):
                self.person_confirm(sid)
        result = self.publish(self.host_request())
        self.assertEqual((result["status"], result["checked_entries"], result["refusals"]), ("ok", 2, []))
        published = LedgerStore(self.published / "ledger.sqlite")
        self.assertEqual(sum(p.amount for e in published.load_entries() for p in e.postings if p.account == CHECKING),
                         Decimal("56.00"))

    # -- the four bypasses, each with the host epoch unchanged ---------------------

    def test_bypass_variable_removed_is_refused_at_publish(self) -> None:
        self.open_books()
        self.assertNotIn(ENV, os.environ)
        for sid, _, _ in ROWS:
            self.person_confirm(sid)
        self.assertEqual(self.pre_s_booked(), sorted(BEFORE_S), "the bypass books rows before S in the session")
        self.assert_refused_at_publish({sid: BEFORE_START for sid in BEFORE_S})

    def test_bypass_file_removed_is_refused_at_publish(self) -> None:
        self.open_books()
        with self.policy(shared.enforced({CHECKING: shared.authorized()})):
            self.person_confirm("mar15")
            Path(os.environ[ENV]).unlink()
            # While the variable names a missing file, the in-command check refuses...
            self.assert_refused(lambda: self.person_confirm("apr02"), POLICY_REQUIRED, sid="apr02")
        # ...so the session also removes the variable and books the rest.
        for sid in ("jan10", "mar01", "mar14", "apr02"):
            self.person_confirm(sid)
        self.assertEqual(self.pre_s_booked(), sorted(BEFORE_S))
        self.assert_refused_at_publish({sid: BEFORE_START for sid in BEFORE_S})

    def test_bypass_accounts_dropped_is_refused_at_publish(self) -> None:
        self.open_books()
        config_path = self.path / "entity.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.pop("bank_account_mappings")
        config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
        self.entity = load_entity(self.path)
        with self.policy(shared.enforced({})):  # no accounts, and no mapping names Checking any more
            for sid, day, amount in ROWS[:3]:
                self.direct_write(sid, day, amount)
        self.assertEqual(self.pre_s_booked(), sorted(BEFORE_S))
        self.assert_refused_at_publish({sid: BEFORE_START for sid in BEFORE_S})

    def test_bypass_mode_legacy_is_refused_at_publish_and_the_check_ignores_the_variable(self) -> None:
        self.open_books()
        legacy = {"schema_version": 1, "mode": "legacy", "epoch": 1, "accounts": {}}
        with self.policy(legacy):
            for sid, _, _ in ROWS:
                self.person_confirm(sid)
            self.assertEqual(self.pre_s_booked(), sorted(BEFORE_S))
            # The variable still names the legacy document while the host checks. The check does not read it.
            self.assert_refused_at_publish({sid: BEFORE_START for sid in BEFORE_S})

    def test_the_check_runs_as_its_own_isolated_process(self) -> None:
        self.open_books()
        for sid, _, _ in ROWS:
            self.person_confirm(sid)
        request = self.root / "request.json"
        request.write_text(json.dumps(self.host_request()), encoding="utf-8")
        legacy = self.root / "legacy.json"
        legacy.write_text(json.dumps({"schema_version": 1, "mode": "legacy", "epoch": 1, "accounts": {}}))
        code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "from bookkeeping.ledger.books_policy_check import main; raise SystemExit(main(sys.argv[2:]))")
        done = subprocess.run(
            [sys.executable, "-I", "-c", code, str(SRC), "check", "--request", str(request), "--base",
             str(self.published), "--candidate", str(self.path)],
            capture_output=True, text=True, env={ENV: str(legacy), "PATH": os.environ.get("PATH", "")}, timeout=120)
        self.assertEqual(done.returncode, 1, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual({r["source_id"]: r["code"] for r in result["refusals"]}, {sid: BEFORE_START for sid in BEFORE_S})

    # -- scope from the many-to-one mapping ------------------------------------------

    def test_a_new_mapping_to_an_account_without_a_policy_state_is_refused(self) -> None:
        self.open_books()
        shadow = "Assets:Reserve-Funds"
        with self.store.transaction() as conn:
            self.store.insert_opens([Open(date(2026, 1, 1), shadow, currencies=("USD",))], conn)
        self.map_feed("feed-2", shadow)  # a second feed; feed-1 still maps to Checking
        self.stage([shared.row("row-shadow", "2026-03-01", "-40.00", "feed-2")])
        self.person_confirm("row-shadow")  # no variable in this session
        result = self.publish(self.host_request())
        self.assertEqual([(r["source_id"], r["code"], r["account"]) for r in result["refusals"]],
                         [("row-shadow", POLICY_REQUIRED, shadow)])

    # -- history and reversal ---------------------------------------------------

    def test_a_change_that_removes_a_published_entry_is_refused(self) -> None:
        self.open_books()
        conn = sqlite3.connect(self.path / "ledger.sqlite")
        try:
            with conn:
                conn.execute("DELETE FROM postings")
                conn.execute("DELETE FROM entries")
        finally:
            conn.close()
        result = self.publish(self.host_request())
        self.assertEqual([r["code"] for r in result["refusals"]], [HISTORY_CHANGED])

    def test_an_exact_reversal_passes_and_a_forged_one_is_refused(self) -> None:
        self.open_books()
        with self.policy(shared.enforced({CHECKING: shared.authorized()})):
            self.person_confirm("apr02")
        self.freeze_published()
        later = {CHECKING: shared.authorized("2026-04-10", version=2)}
        reverse_entry(self.entity, "apr02", "correction")
        self.assertEqual(self.publish(self.host_request(later, epoch=2))["status"], "ok")
        forged = Entry(date=date(2026, 4, 2), narration="Reversal of: synthetic", meta=(("reverses", "apr02"),),
                       postings=(Posting(CHECKING, Decimal("19.00")), Posting(FEES, Decimal("-19.00"))))
        _atomic_ledger_write(self.entity, [], [forged], "forged", None, "a second reversal of the same entry")
        result = self.publish(self.host_request(later, epoch=2))
        self.assertEqual([r["code"] for r in result["refusals"]], [BEFORE_START])

    # -- opening authorizations ------------------------------------------------

    def pending_policy(self, version: int = 1) -> dict:
        return {CHECKING: {"state": "authorized_pending_opening", "start_day": S, "version": version}}

    def statement_opening(self, on: str = S) -> dict:
        from bookkeeping.statement_opening import record_opening
        self.map_feed("feed-1", CHECKING)
        self.freeze_published()
        recorded = record_opening(self.path, account=CHECKING, on=on, amount="100.00")
        self.assertEqual(recorded["status"], "recorded", recorded)
        return recorded

    def authorization(self, source_id: str, operation: str, postings, **change) -> dict:
        return {"account": CHECKING, "start_day": S, "version": 1, "operation": operation,
                "operation_id": "host-receipt-1", "entry_source_id": source_id,
                "postings": [[a, str(v)] for a, v in postings], **change}

    def test_a_typed_starting_balance_needs_a_matching_host_authorization(self) -> None:
        recorded = self.statement_opening()
        postings = [(p["account"], p["amount"]) for p in recorded["postings"]]
        good = self.authorization(recorded["source_id"], "starting_balance", postings)
        cases = {
            "no authorization": [],
            "older policy version": [dict(good, version=2)],
            "another start day": [dict(good, start_day="2026-03-01")],
            "another amount": [dict(good, postings=[[CHECKING, "90.00"], [EQUITY, "-90.00"]])],
            "another entry": [dict(good, entry_source_id="statement-opening-other")],
        }
        for label, openings in cases.items():
            with self.subTest(label):
                result = checker().check_workspaces(self.host_request(self.pending_policy(), openings), self.published, self.path)
                self.assertEqual([(r["code"], r["account"]) for r in result["refusals"]], [(OPENING_REQUIRED, CHECKING)])
        result = self.publish(self.host_request(self.pending_policy(), [good]))
        self.assertEqual((result["status"], result["openings_matched"]), ("ok", ["host-receipt-1"]))

    def test_a_quickbooks_opening_needs_a_matching_host_authorization(self) -> None:
        self.map_feed("feed-1", CHECKING)
        self.freeze_published()
        preview = qb_sync.sync_opening(self.entity, self.snapshot_path, date.fromisoformat(S))  # the reviewed preview
        applied = qb_sync.sync_opening(self.entity, self.snapshot_path, date.fromisoformat(S), apply=True)
        self.assertTrue(applied["applied"])
        committed = next(e for e in self.store.load_entries() if dict(e.meta).get("import-source") == "quickbooks-opening")
        good = self.authorization(committed.source_id, "quickbooks_sync_opening",
                                  [(p["account"], p["amount"]) for p in preview["opening"]["proposed_postings"]])
        refused = checker().check_workspaces(self.host_request(self.pending_policy()), self.published, self.path)
        self.assertEqual([r["code"] for r in refused["refusals"]], [OPENING_REQUIRED])
        self.assertEqual(self.publish(self.host_request(self.pending_policy(), [good]))["status"], "ok")

    def test_an_opening_before_an_authorized_start_is_refused(self) -> None:
        recorded = self.statement_opening(on="2026-03-01")
        result = self.publish(self.host_request())
        self.assertEqual([(r["code"], r["source_id"]) for r in result["refusals"]],
                         [(BEFORE_START, recorded["source_id"])])

    def test_an_opening_on_an_unset_account_is_refused(self) -> None:
        self.statement_opening()
        result = self.publish(self.host_request({CHECKING: {"state": "unset"}}))
        self.assertEqual([r["code"] for r in result["refusals"]], [shared.UNSET])

    # -- the request ---------------------------------------------------------------

    def test_a_bad_request_is_refused(self) -> None:
        from bookkeeping.ledger.books_policy import BooksPolicyError
        self.open_books()
        bad = {"no scope": {"schema_version": 1, "policy": shared.enforced({})},
               "unknown operation": self.host_request(openings=[self.authorization("x", "manual", [(CHECKING, "1"),
                                                                                                  (EQUITY, "-1")])])}
        for label, request in bad.items():
            with self.subTest(label), self.assertRaises(BooksPolicyError) as caught:
                checker().check_workspaces(request, self.published, self.path)
            self.assertEqual(caught.exception.code, "BOOKS_POLICY_INVALID")


if __name__ == "__main__":
    unittest.main()
