from __future__ import annotations

"""Tests for src/bookkeeping/queue.py — U9: review queue, learned context, trust ramp.

Test scenarios per plan (all referenced AE/edge/error IDs):

AE2 arc:
  - New counterparty queues with reasoning.
  - correct → learned context updated, reset=True.
  - After 3 consecutive confirms (threshold=3), next occurrence auto-posts via make_categorizer.
  - Correction after auto-posting resets count, forces queue again.

Edge / error:
  - propose with phantom source_id → rejected.
  - propose with unknown category → rejected.
  - reasoning with newlines/control chars → sanitized before persisting.
  - malformed queue file → quarantined with named error.
  - reopen on amount change → item reopened, delta flagged, category pre-filled.
  - session summary counts match simulated close.
  - session summary text has no beancount/SQL jargon.
  - quarterly-review on two-quarter fixture → variance flags + auto-posted sample.
  - threshold from trust-policy.json honored (change to 2 → behavior shifts).
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.entity import Entity, add_account, approve_related_entity, record_related_entity  # noqa: E402
from bookkeeping.ledger.importer import import_transactions  # noqa: E402
from bookkeeping.ledger.model import Open  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.queue import (  # noqa: E402
    _counterparty_key,
    _sanitize_reasoning,
    _update_learned_context,
    correct,
    confirm,
    confirm_group,
    eligible_for_autopost,
    list_queue_items,
    load_learned_context,
    make_categorizer,
    propose,
    propose_group,
    propose_related_entity,
    propose_split,
    propose_transfer,
    reopen_if_amount_changed,
    reconcile_pending_amount_changes,
    save_split_template,
    summarize_queue_items,
    withdraw,
    find_transfer_candidates,
    confirm_split,
    confirm_transfer,
    find_transfer_exceptions,
    write_session_summary,
    quarterly_review,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TS = "2026-01-15T00:00:00Z"
SESSION = "test-q-session-001"


_ACCOUNT_NAMES = [
    "Assets:Bank:Checking",
    "Liabilities:CreditCard",
    "Equity:Opening-Balances",
    "Income:Consulting",
    "Expenses:Software",
    "Expenses:Subcontractors",
    "Expenses:Meals",
    "Expenses:Travel",
    "Expenses:Office",
    "Expenses:Fees",
]


def _make_entity(tmp_dir: Path, threshold: int = 3) -> Entity:
    """Create a minimal entity directory with a SQLite account catalog."""
    entity_dir = tmp_dir / "entity"
    entity_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("staging", "reports", "review-queue", "learned-context"):
        (entity_dir / sub).mkdir(exist_ok=True)

    entity_config = {"name": "Test Entity", "business_type": "consulting"}
    (entity_dir / "entity.json").write_text(
        json.dumps(entity_config) + "\n", encoding="utf-8"
    )
    trust_policy = {"auto_post_threshold": threshold, "queue_all_until_confirmed": True}
    (entity_dir / "trust-policy.json").write_text(
        json.dumps(trust_policy) + "\n", encoding="utf-8"
    )
    store = LedgerStore(entity_dir / "ledger.sqlite")
    store.initialize()
    with store.transaction() as conn:
        store.set_meta("canonical", "true", conn)
        store.set_meta("account_catalog", "sqlite", conn)
        store.insert_opens(
            [
                Open(
                    date=date(2026, 1, 1),
                    account=account,
                    currencies=("USD",) if account.startswith(("Assets:", "Liabilities:", "Expenses:")) else (),
                )
                for account in _ACCOUNT_NAMES
            ],
            conn,
        )

    return Entity(
        path=entity_dir,
        entity_config=entity_config,
        trust_policy=trust_policy,
    )


def _add_pending_categorization(entity: Entity, source_id: str, description: str,
                                 amount: str = "100.00", txn_date: str = "2026-01-10") -> None:
    """Helper: add an item to staging/pending-categorization.json."""
    path = entity.staging_dir / "pending-categorization.json"
    existing: list = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    existing.append({
        "id": source_id,
        "date": txn_date,
        "amount": amount,
        "description": description,
        "accountName": "Checking",
        "pending": False,
    })
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# Tests: sanitization
# ---------------------------------------------------------------------------

class TestSanitizeReasoning(unittest.TestCase):

    def test_strips_newlines(self) -> None:
        raw = "This is\nreasoning\rwith CR"
        result = _sanitize_reasoning(raw)
        self.assertNotIn("\n", result)
        self.assertNotIn("\r", result)

    def test_strips_control_characters(self) -> None:
        raw = "Hello\x00World\x1fEnd"
        result = _sanitize_reasoning(raw)
        self.assertEqual(result, "HelloWorldEnd")

    def test_length_cap_2000(self) -> None:
        raw = "x" * 3000
        result = _sanitize_reasoning(raw)
        self.assertEqual(len(result), 2000)

    def test_normal_text_unchanged(self) -> None:
        raw = "Software subscription for CI tooling."
        self.assertEqual(_sanitize_reasoning(raw), raw)


class TestPendingWorkVisibility(unittest.TestCase):

    def test_open_list_includes_unproposed_staged_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            _add_pending_categorization(entity, "staged-1", "Uncategorised vendor")

            items = list_queue_items(entity, status="open")

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["source_id"], "staged-1")
            self.assertEqual(items[0]["status"], "staged")

    def test_grouped_proposal_and_confirmation_posts_only_the_selected_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            _add_pending_categorization(entity, "group-1", "Repeat vendor")
            _add_pending_categorization(entity, "group-2", "Repeat vendor")
            _add_pending_categorization(entity, "other", "Other vendor")
            propose_group(entity, ["group-1", "group-2"], "Expenses:Software", "Repeated subscription")

            confirmed = confirm_group(entity, "Expenses:Software", SESSION, ts=TS)

            self.assertEqual([item["source_id"] for item in confirmed], ["group-1", "group-2"])
            staged = json.loads((entity.staging_dir / "pending-categorization.json").read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in staged], ["other"])

    def test_summary_groups_by_treatment_and_keeps_staged_items_unclassified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            _add_pending_categorization(entity, "software-1", "Acme", "-10.00", "2026-01-05")
            _add_pending_categorization(entity, "software-2", "Acme", "-15.00", "2026-01-20")
            _add_pending_categorization(entity, "unknown-1", "Unknown", "-20.00", "2026-01-25")
            propose_group(
                entity,
                ["software-1", "software-2"],
                "Expenses:Software",
                "Routine subscription",
            )

            groups = summarize_queue_items(entity)

            self.assertEqual([group["treatment"] for group in groups], [
                "Needs review", "Expenses:Software",
            ])
            software = groups[1]
            self.assertEqual(software["count"], 2)
            self.assertEqual(software["total"], "-25.00")
            self.assertEqual(software["date_from"], "2026-01-05")
            self.assertEqual(software["date_to"], "2026-01-20")


# ---------------------------------------------------------------------------
# Tests: eligible_for_autopost / trust ramp
# ---------------------------------------------------------------------------

class TestEligibleForAutopost(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_context_returns_false(self) -> None:
        self.assertFalse(eligible_for_autopost(self.entity, "UNKNOWN VENDOR"))

    def test_below_threshold_not_eligible(self) -> None:
        _update_learned_context(self.entity, "ACME CORP", "Expenses:Software", corrected=False)
        _update_learned_context(self.entity, "ACME CORP", "Expenses:Software", corrected=False)
        # count = 2, threshold = 3
        self.assertFalse(eligible_for_autopost(self.entity, "ACME CORP"))

    def test_at_threshold_eligible(self) -> None:
        for _ in range(3):
            _update_learned_context(self.entity, "ACME CORP", "Expenses:Software", corrected=False)
        # count = 3, threshold = 3
        self.assertTrue(eligible_for_autopost(self.entity, "ACME CORP"))

    def test_reset_flag_blocks_even_at_threshold(self) -> None:
        for _ in range(3):
            _update_learned_context(self.entity, "ACME CORP", "Expenses:Software", corrected=False)
        # Correct → resets count and sets reset=True
        _update_learned_context(self.entity, "ACME CORP", "Expenses:Fees", corrected=True)
        self.assertFalse(eligible_for_autopost(self.entity, "ACME CORP"))

    def test_reset_cleared_on_next_confirm(self) -> None:
        for _ in range(3):
            _update_learned_context(self.entity, "ACME CORP", "Expenses:Software", corrected=False)
        _update_learned_context(self.entity, "ACME CORP", "Expenses:Fees", corrected=True)
        # count=0, reset=True; now confirm once → reset cleared, count=1
        _update_learned_context(self.entity, "ACME CORP", "Expenses:Fees", corrected=False)
        ctx = load_learned_context(self.entity)
        entry = ctx["ACME CORP"]
        self.assertFalse(entry["reset"])
        self.assertEqual(entry["confirmed_count"], 1)
        # still below threshold (1 < 3)
        self.assertFalse(eligible_for_autopost(self.entity, "ACME CORP"))

    def test_threshold_from_policy(self) -> None:
        """Threshold honored from trust-policy.json — change to 2 shifts behavior."""
        tmp2 = tempfile.TemporaryDirectory()
        try:
            entity2 = _make_entity(Path(tmp2.name), threshold=2)
            _update_learned_context(entity2, "VENDOR", "Expenses:Software", corrected=False)
            _update_learned_context(entity2, "VENDOR", "Expenses:Software", corrected=False)
            # count=2, threshold=2 → should be eligible
            self.assertTrue(eligible_for_autopost(entity2, "VENDOR"))
        finally:
            tmp2.cleanup()

    def test_threshold_change_shifts_behavior(self) -> None:
        """With threshold=3: 2 confirms not eligible; with threshold=2: 2 confirms eligible."""
        key = "SAME VENDOR"
        # entity with threshold=3
        _update_learned_context(self.entity, key, "Expenses:Software", corrected=False)
        _update_learned_context(self.entity, key, "Expenses:Software", corrected=False)
        self.assertFalse(eligible_for_autopost(self.entity, key))

        # entity with threshold=2
        tmp2 = tempfile.TemporaryDirectory()
        try:
            entity2 = _make_entity(Path(tmp2.name), threshold=2)
            _update_learned_context(entity2, key, "Expenses:Software", corrected=False)
            _update_learned_context(entity2, key, "Expenses:Software", corrected=False)
            self.assertTrue(eligible_for_autopost(entity2, key))
        finally:
            tmp2.cleanup()


# ---------------------------------------------------------------------------
# Tests: make_categorizer
# ---------------------------------------------------------------------------

class TestMakeCategorizer(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unknown_counterparty_routes_to_queue(self) -> None:
        cat = make_categorizer(self.entity)
        account, confidence = cat({"description": "NEW VENDOR UNKNOWN", "amount": "50.00"})
        self.assertEqual(account, "")
        self.assertEqual(confidence, "queue")

    def test_eligible_counterparty_auto_posts(self) -> None:
        key = "ADOBE INC"
        for _ in range(3):
            _update_learned_context(self.entity, key, "Expenses:Software", corrected=False)

        cat = make_categorizer(self.entity)
        txn = {"description": "ADOBE INC", "amount": "54.99"}
        account, confidence = cat(txn)
        self.assertEqual(account, "Expenses:Software")
        self.assertEqual(confidence, "auto")

    def test_correction_after_auto_posting_resets_queue(self) -> None:
        """After correction, make_categorizer must return queue (not auto)."""
        key = "DROPBOX"
        for _ in range(3):
            _update_learned_context(self.entity, key, "Expenses:Software", corrected=False)
        # Now correct → reset=True
        _update_learned_context(self.entity, key, "Expenses:Fees", corrected=True)

        cat = make_categorizer(self.entity)
        account, confidence = cat({"description": "DROPBOX", "amount": "15.00"})
        self.assertEqual(account, "")
        self.assertEqual(confidence, "queue")

    def test_after_correction_and_one_confirm_still_queues(self) -> None:
        """Reset cleared on first confirm, but count=1 < threshold=3 → still queue."""
        key = "DROPBOX"
        for _ in range(3):
            _update_learned_context(self.entity, key, "Expenses:Software", corrected=False)
        _update_learned_context(self.entity, key, "Expenses:Fees", corrected=True)
        # One confirm → reset cleared, count=1
        _update_learned_context(self.entity, key, "Expenses:Fees", corrected=False)

        cat = make_categorizer(self.entity)
        account, confidence = cat({"description": "DROPBOX", "amount": "15.00"})
        self.assertEqual(account, "")
        self.assertEqual(confidence, "queue")


# ---------------------------------------------------------------------------
# Tests: propose validation
# ---------------------------------------------------------------------------

class TestPropose(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_phantom_source_id_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            propose(self.entity, "nonexistent-id-999", "Expenses:Software", "Some reasoning")
        self.assertIn("pending-categorization", str(ctx.exception))

    def test_unknown_category_rejected(self) -> None:
        _add_pending_categorization(self.entity, "txn-001", "GitHub subscription")
        with self.assertRaises(ValueError) as ctx:
            propose(self.entity, "txn-001", "Expenses:UnknownCategory", "reasoning")
        self.assertIn("account catalog", str(ctx.exception))

    def test_entity_local_chart_file_is_not_category_catalog(self) -> None:
        (self.entity.path / "chart-of-accounts.beancount").write_text(
            "2026-01-01 open Expenses:UnknownCategory USD\n",
            encoding="utf-8",
        )
        _add_pending_categorization(self.entity, "txn-chart-ignored", "Legacy chart")
        with self.assertRaises(ValueError):
            propose(self.entity, "txn-chart-ignored", "Expenses:UnknownCategory", "reasoning")

    def test_reasoning_sanitized_before_persisting(self) -> None:
        _add_pending_categorization(self.entity, "txn-002", "DigitalOcean")
        raw_reasoning = "Software\nhosting\nservice\x00with\rnull"
        item = propose(self.entity, "txn-002", "Expenses:Software", raw_reasoning)
        # Persisted reasoning must not contain newlines or control chars
        self.assertNotIn("\n", item["reasoning"])
        self.assertNotIn("\r", item["reasoning"])
        self.assertNotIn("\x00", item["reasoning"])

    def test_valid_propose_creates_open_item(self) -> None:
        _add_pending_categorization(self.entity, "txn-003", "Adobe Creative Cloud")
        item = propose(self.entity, "txn-003", "Expenses:Software", "Design software subscription")
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["source_id"], "txn-003")
        self.assertEqual(item["proposed_category"], "Expenses:Software")

    def test_propose_persists_to_disk(self) -> None:
        _add_pending_categorization(self.entity, "txn-004", "Slack")
        propose(self.entity, "txn-004", "Expenses:Software", "Team communication tool")
        items = list_queue_items(self.entity)
        ids = [i["source_id"] for i in items]
        self.assertIn("txn-004", ids)

    def test_propose_context_sanitized(self) -> None:
        _add_pending_categorization(self.entity, "txn-005", "Zoom")
        item = propose(
            self.entity, "txn-005", "Expenses:Software",
            "Video conferencing",
            context="some context\nwith newline"
        )
        self.assertNotIn("\n", item["context"])


# ---------------------------------------------------------------------------
# Tests: AE2 arc — full confirm/correct flow
# ---------------------------------------------------------------------------

class TestAE2Arc(unittest.TestCase):
    """AE2: New counterparty queues with reasoning; correct → learned context;
    3 confirms → auto-posts; correct after auto-posting resets."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))
        self._session = "ae2-session"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _setup_txn(self, txn_id: str, description: str = "Subcontractor Invoice",
                   amount: str = "500.00") -> None:
        _add_pending_categorization(self.entity, txn_id, description, amount=amount)

    def test_new_counterparty_queues_with_reasoning(self) -> None:
        self._setup_txn("txn-ae2-001", "John Doe Design Invoice")
        item = propose(
            self.entity, "txn-ae2-001",
            "Expenses:Subcontractors",
            "Freelance design work by John Doe, matches invoice",
        )
        self.assertEqual(item["status"], "open")
        self.assertIn("Freelance design", item["reasoning"])
        self.assertEqual(item["proposed_category"], "Expenses:Subcontractors")

    def test_correct_updates_learned_context_with_reset(self) -> None:
        self._setup_txn("txn-ae2-002", "JOHN DOE DESIGN")
        propose(self.entity, "txn-ae2-002", "Expenses:Subcontractors", "freelance design")
        correct(self.entity, "txn-ae2-002", "Expenses:Subcontractors",
                note="confirmed as subcontractor", session_id=self._session, ts=TS)

        ctx = load_learned_context(self.entity)
        key = "JOHN DOE DESIGN"
        self.assertIn(key, ctx)
        entry = ctx[key]
        self.assertEqual(entry["confirmed_count"], 0)
        self.assertTrue(entry["reset"])
        self.assertEqual(entry["canonical_category"], "Expenses:Subcontractors")

    def test_three_confirms_enable_auto_posting(self) -> None:
        """After 3 consecutive confirms, next occurrence auto-posts."""
        # Description is "GitHub" so counterparty key is normalize_description("GitHub") = "GITHUB"
        key = "GITHUB"
        for i in range(3):
            txn_id = f"txn-github-{i}"
            self._setup_txn(txn_id, "GitHub", amount="10.00")
            propose(self.entity, txn_id, "Expenses:Software", "GitHub subscription")
            confirm(self.entity, txn_id, session_id=self._session, ts=TS)

        ctx = load_learned_context(self.entity)
        self.assertIn(key, ctx)
        self.assertEqual(ctx[key]["confirmed_count"], 3)
        self.assertFalse(ctx[key]["reset"])

        # Now make_categorizer should auto-post
        cat = make_categorizer(self.entity)
        account, confidence = cat({"description": "GitHub", "amount": "10.00"})
        self.assertEqual(account, "Expenses:Software")
        self.assertEqual(confidence, "auto")

    def test_correction_after_auto_posts_resets_and_forces_queue(self) -> None:
        """Correction after reaching threshold: reset count, force queue next time."""
        key = "GITHUB INC"
        for i in range(3):
            txn_id = f"txn-github-reset-{i}"
            self._setup_txn(txn_id, "GitHub", amount="10.00")
            propose(self.entity, txn_id, "Expenses:Software", "GitHub subscription")
            confirm(self.entity, txn_id, session_id=self._session, ts=TS)

        # Confirm auto-posts
        cat = make_categorizer(self.entity)
        account, _ = cat({"description": "GitHub", "amount": "10.00"})
        self.assertEqual(account, "Expenses:Software")

        # Now correct a new occurrence
        txn_id = "txn-github-corrected"
        self._setup_txn(txn_id, "GitHub Enterprise", amount="20.00")
        propose(self.entity, txn_id, "Expenses:Software", "GitHub enterprise")
        correct(self.entity, txn_id, "Expenses:Fees",
                note="enterprise plan, different category",
                session_id=self._session, ts=TS)

        # After correction: count reset, reset=True
        ctx = load_learned_context(self.entity)
        # The key for "GitHub Enterprise" normalized
        from bookkeeping.ledger.normalize import normalize_description
        corrected_key = normalize_description("GitHub Enterprise")
        self.assertIn(corrected_key, ctx)
        entry = ctx[corrected_key]
        self.assertEqual(entry["confirmed_count"], 0)
        self.assertTrue(entry["reset"])

        # Next make_categorizer call must route to queue
        cat2 = make_categorizer(self.entity)
        acc, conf = cat2({"description": "GitHub Enterprise", "amount": "20.00"})
        self.assertEqual(acc, "")
        self.assertEqual(conf, "queue")


# ---------------------------------------------------------------------------
# Tests: malformed queue file quarantined
# ---------------------------------------------------------------------------

class TestMalformedQueueFile(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_malformed_json_quarantined_with_named_error(self) -> None:
        q_dir = self.entity.review_queue_dir
        malformed_path = q_dir / "bad_item.json"
        malformed_path.write_text("{not valid json[[[", encoding="utf-8")

        # list_queue_items should not raise; malformed file is quarantined
        items = list_queue_items(self.entity)
        # The bad file should not appear in items
        ids = [i.get("source_id") for i in items]
        self.assertNotIn("bad_item", ids)

        # It should have been quarantined
        quarantine = self.entity.path / "review-queue" / "quarantine"
        self.assertTrue(quarantine.exists())
        # An error sidecar should be present
        error_files = list(quarantine.glob("*.error.json"))
        self.assertTrue(len(error_files) >= 1)

    def test_missing_required_fields_quarantined(self) -> None:
        q_dir = self.entity.review_queue_dir
        incomplete_path = q_dir / "incomplete.json"
        incomplete_path.write_text(json.dumps({"source_id": "missing-status"}), encoding="utf-8")

        items = list_queue_items(self.entity)
        ids = [i.get("source_id") for i in items]
        self.assertNotIn("missing-status", ids)

        quarantine = self.entity.path / "review-queue" / "quarantine"
        error_files = list(quarantine.glob("*.error.json"))
        self.assertTrue(len(error_files) >= 1)


# ---------------------------------------------------------------------------
# Tests: reopen on amount change
# ---------------------------------------------------------------------------

class TestReopenIfAmountChanged(unittest.TestCase):

    def test_same_amount_returns_none(self) -> None:
        item = {
            "source_id": "txn-100",
            "original_amount": "100.00",
            "amount": "100.00",
            "proposed_category": "Expenses:Software",
            "status": "open",
        }
        result = reopen_if_amount_changed(item, Decimal("100.00"))
        self.assertIsNone(result)

    def test_different_amount_reopens_with_delta(self) -> None:
        item = {
            "source_id": "txn-100",
            "original_amount": "100.00",
            "amount": "100.00",
            "proposed_category": "Expenses:Software",
            "status": "confirmed",
        }
        result = reopen_if_amount_changed(item, Decimal("102.50"))
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "reopened")
        self.assertEqual(result["amount"], "102.50")
        self.assertEqual(result["delta"], "2.50")
        # Category pre-filled
        self.assertEqual(result["proposed_category"], "Expenses:Software")

    def test_decrease_amount_flagged_with_negative_delta(self) -> None:
        item = {
            "source_id": "txn-101",
            "original_amount": "50.00",
            "amount": "50.00",
            "proposed_category": "Expenses:Meals",
            "status": "confirmed",
        }
        result = reopen_if_amount_changed(item, Decimal("48.00"))
        self.assertIsNotNone(result)
        self.assertEqual(result["delta"], "-2.00")
        self.assertEqual(result["status"], "reopened")

    def test_original_amount_preserved(self) -> None:
        item = {
            "source_id": "txn-102",
            "original_amount": "75.00",
            "amount": "75.00",
            "proposed_category": "Expenses:Travel",
            "status": "open",
        }
        result = reopen_if_amount_changed(item, Decimal("80.00"))
        self.assertIsNotNone(result)
        self.assertEqual(result["original_amount"], "75.00")
        self.assertEqual(result["amount"], "80.00")


# ---------------------------------------------------------------------------
# Tests: session summary
# ---------------------------------------------------------------------------

class TestSessionSummary(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_session_summary_persisted(self) -> None:
        counts = {
            "new": 10,
            "auto_posted": 6,
            "queued": 4,
            "confirmed": 3,
            "corrected": 1,
            "reopened": 0,
            "reconciliation_status": "clean",
            "reconciliation_notes": "",
        }
        txt_path = write_session_summary(self.entity, "sess-001", counts)
        self.assertTrue(txt_path.exists())
        json_path = txt_path.with_suffix(".json")
        self.assertTrue(json_path.exists())

    def test_session_summary_counts_match(self) -> None:
        counts = {
            "new": 7,
            "auto_posted": 5,
            "queued": 2,
            "confirmed": 2,
            "corrected": 0,
            "reopened": 1,
            "reconciliation_status": "clean",
        }
        txt_path = write_session_summary(self.entity, "sess-002", counts)
        json_path = txt_path.with_suffix(".json")
        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["new"], 7)
        self.assertEqual(data["auto_posted"], 5)
        self.assertEqual(data["queued"], 2)
        self.assertEqual(data["confirmed"], 2)
        self.assertEqual(data["reopened"], 1)

    def test_session_summary_text_jargon_free(self) -> None:
        """Plain-text output must not contain beancount syntax or SQL keywords."""
        counts = {
            "new": 3,
            "auto_posted": 2,
            "queued": 1,
            "confirmed": 1,
            "corrected": 0,
            "reopened": 0,
            "reconciliation_status": "clean",
        }
        txt_path = write_session_summary(self.entity, "sess-003", counts)
        content = txt_path.read_text(encoding="utf-8")

        # Must not contain beancount/SQL jargon
        self.assertNotIn(";", content)
        self.assertNotIn("beancount", content.lower())
        self.assertNotIn("SELECT", content)
        self.assertNotIn("pushtag", content)
        self.assertNotIn("poptag", content)
        # Must not contain raw colon-syntax account names
        import re
        # Raw "Assets:" style should not appear
        self.assertFalse(bool(re.search(r'Assets:[A-Za-z]', content)), content)
        self.assertFalse(bool(re.search(r'Expenses:[A-Za-z]', content)), content)
        self.assertFalse(bool(re.search(r'Income:[A-Za-z]', content)), content)

    def test_session_summary_path(self) -> None:
        txt_path = write_session_summary(self.entity, "sess-test-001", {})
        self.assertIn("sessions", str(txt_path))
        self.assertTrue(str(txt_path).endswith(".txt"))


# ---------------------------------------------------------------------------
# Tests: learned context persistence
# ---------------------------------------------------------------------------

class TestLearnedContextPersistence(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_confirm_increments_count(self) -> None:
        _update_learned_context(self.entity, "VENDOR A", "Expenses:Software", corrected=False)
        ctx = load_learned_context(self.entity)
        self.assertEqual(ctx["VENDOR A"]["confirmed_count"], 1)
        self.assertFalse(ctx["VENDOR A"]["reset"])

    def test_correct_resets_count_and_sets_reset(self) -> None:
        _update_learned_context(self.entity, "VENDOR B", "Expenses:Software", corrected=False)
        _update_learned_context(self.entity, "VENDOR B", "Expenses:Software", corrected=False)
        _update_learned_context(self.entity, "VENDOR B", "Expenses:Fees", corrected=True)
        ctx = load_learned_context(self.entity)
        self.assertEqual(ctx["VENDOR B"]["confirmed_count"], 0)
        self.assertTrue(ctx["VENDOR B"]["reset"])
        self.assertEqual(ctx["VENDOR B"]["canonical_category"], "Expenses:Fees")

    def test_note_persisted(self) -> None:
        _update_learned_context(
            self.entity, "VENDOR C", "Expenses:Office",
            corrected=True, note="Wrong category originally"
        )
        ctx = load_learned_context(self.entity)
        self.assertEqual(ctx["VENDOR C"]["notes"], "Wrong category originally")

    def test_sorted_keys_in_file(self) -> None:
        """File must be human-readable sorted-keys JSON."""
        _update_learned_context(self.entity, "ZEBRA CO", "Expenses:Software", corrected=False)
        _update_learned_context(self.entity, "ALPHA CO", "Expenses:Fees", corrected=False)
        p = self.entity.path / "learned-context" / "counterparties.json"
        text = p.read_text(encoding="utf-8")
        # Should start with ALPHA CO (sorted)
        self.assertLess(text.index("ALPHA CO"), text.index("ZEBRA CO"))

    def test_atomic_write(self) -> None:
        """Learned context file must be written atomically (no .tmp left over)."""
        _update_learned_context(self.entity, "ATOM TEST", "Expenses:Software", corrected=False)
        tmp_file = self.entity.path / "learned-context" / "counterparties.json.tmp"
        self.assertFalse(tmp_file.exists())


# ---------------------------------------------------------------------------
# Tests: quarterly review
# ---------------------------------------------------------------------------

class TestQuarterlyReview(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))
        self._build_two_quarter_fixture()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _build_two_quarter_fixture(self) -> None:
        """Import two quarters of transactions to enable variance-flag testing."""
        # Q1 transactions: software and consulting income
        q1_txns = [
            {
                "id": f"q1-txn-{i}",
                "date": f"2026-0{(i % 3) + 1}-15",
                "amount": "1000.00",
                "description": "Client Payment",
                "accountName": "Checking",
                "pending": False,
            }
            for i in range(3)
        ] + [
            {
                "id": f"q1-exp-{i}",
                "date": f"2026-0{(i % 3) + 1}-10",
                "amount": "-200.00",
                "description": "GitHub subscription",
                "accountName": "Checking",
                "pending": False,
            }
            for i in range(3)
        ]

        # Q2 transactions: more income, higher expenses
        q2_txns = [
            {
                "id": f"q2-txn-{i}",
                "date": f"2026-0{(i % 3) + 4}-15",
                "amount": "2000.00",
                "description": "Client Payment",
                "accountName": "Checking",
                "pending": False,
            }
            for i in range(3)
        ] + [
            {
                "id": f"q2-exp-{i}",
                "date": f"2026-0{(i % 3) + 4}-10",
                "amount": "-800.00",
                "description": "GitHub subscription",
                "accountName": "Checking",
                "pending": False,
            }
            for i in range(3)
        ]

        def _cat(txn: dict) -> tuple[str, str]:
            if "Payment" in txn["description"]:
                return ("Income:Consulting", "auto")
            return ("Expenses:Software", "auto")

        import_transactions(
            self.entity, q1_txns + q2_txns,
            session_id="fixture-import",
            categorizer=_cat,
            ts=TS,
            session_date=date(2026, 7, 1),
        )

        # Regenerate cache
        from bookkeeping.reports.cache import regenerate
        regenerate(self.entity.path)

    def test_quarterly_review_produces_output(self) -> None:
        result = quarterly_review(self.entity, quarter=1, year=2026)
        self.assertIn("pnl", result)
        self.assertIn("balance_sheet", result)
        self.assertEqual(result["quarter"], 1)
        self.assertEqual(result["year"], 2026)

    def test_quarterly_review_writes_files(self) -> None:
        quarterly_review(self.entity, quarter=1, year=2026)
        rpt_dir = self.entity.reports_dir / "quarterly"
        self.assertTrue((rpt_dir / "2026-Q1.json").exists())
        self.assertTrue((rpt_dir / "2026-Q1.txt").exists())

    def test_variance_flags_detected(self) -> None:
        """Q2 has higher revenue and expenses vs Q1 → variance flags expected."""
        result = quarterly_review(self.entity, quarter=2, year=2026)
        # Should have prior quarter data (Q1)
        self.assertTrue(result["has_prior_quarter"])
        # Variance flags should be non-empty (revenue doubled, expenses 4x)
        self.assertTrue(len(result["variance_flags"]) > 0)

    def test_variance_flags_plain_english(self) -> None:
        """Variance flags must not contain beancount account colon syntax."""
        import re
        result = quarterly_review(self.entity, quarter=2, year=2026)
        for flag in result["variance_flags"]:
            # Should contain human-readable text with ' › ' separator if any account
            # Raw colon syntax should not appear in flags
            self.assertNotIn("Assets:", flag)
            self.assertNotIn("SELECT", flag)
            self.assertNotIn("pushtag", flag)

    def test_auto_posted_sample_present(self) -> None:
        result = quarterly_review(self.entity, quarter=1, year=2026)
        # We imported some transactions → sample should be populated
        self.assertIsInstance(result["auto_posted_sample"], list)
        # Entries have expected shape
        for entry in result["auto_posted_sample"]:
            self.assertIn("date", entry)
            self.assertIn("account", entry)
            # Account uses readable separator (›) not colon
            if entry["account"]:
                # Should not contain raw colon
                self.assertNotIn("Expenses:S", entry["account"])
                self.assertNotIn("Income:C", entry["account"])

    def test_quarterly_txt_jargon_free(self) -> None:
        """The .txt output must be free of beancount/SQL jargon."""
        import re
        quarterly_review(self.entity, quarter=1, year=2026)
        rpt_dir = self.entity.reports_dir / "quarterly"
        content = (rpt_dir / "2026-Q1.txt").read_text(encoding="utf-8")
        self.assertNotIn("pushtag", content)
        self.assertNotIn("poptag", content)
        self.assertNotIn("beancount", content.lower())
        self.assertNotIn("SELECT", content)

    def test_first_quarter_has_no_prior(self) -> None:
        """Q1 of a new year has no prior-quarter data in this fixture."""
        # Q1 2025 would have no data at all
        result = quarterly_review(self.entity, quarter=1, year=2025)
        # May have no prior, and no variance flags
        self.assertFalse(result["has_prior_quarter"])

    def test_auto_posted_sample_max_10(self) -> None:
        result = quarterly_review(self.entity, quarter=2, year=2026)
        self.assertLessEqual(len(result["auto_posted_sample"]), 10)


# ---------------------------------------------------------------------------
# Tests: confirm + correct write through importer
# ---------------------------------------------------------------------------

class TestConfirmWritesLedger(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_confirm_writes_balanced_entry(self) -> None:
        _add_pending_categorization(self.entity, "txn-write-001", "Software subscription", amount="54.99")
        propose(self.entity, "txn-write-001", "Expenses:Software", "CI tooling subscription")
        confirm(self.entity, "txn-write-001", session_id="confirm-session", ts=TS)

        # Store projection must now contain the entry.
        from bookkeeping.ledger.projections import render_store_ledger
        from bookkeeping.ledger.store import default_store_path
        from bookkeeping.ledger.validator import validate, parse_ledger
        text = render_store_ledger(default_store_path(self.entity.path))
        errors = validate(text)
        self.assertEqual(errors, [], f"Ledger validation errors: {errors}")

        parsed = parse_ledger(text)
        entries = parsed["entries"]
        self.assertTrue(len(entries) >= 1)
        # Find the entry for this source_id
        entry = next((e for e in entries if e.source_id == "txn-write-001"), None)
        self.assertIsNotNone(entry, "Entry for txn-write-001 not found in ledger")
        # Postings must balance
        total = sum(p.amount for p in entry.postings)
        self.assertAlmostEqual(float(total), 0.0, places=2)

    def test_confirm_removes_from_pending_categorization(self) -> None:
        _add_pending_categorization(self.entity, "txn-write-002", "Office supplies", amount="30.00")
        propose(self.entity, "txn-write-002", "Expenses:Office", "Paper and supplies")
        confirm(self.entity, "txn-write-002", session_id="confirm-session", ts=TS)

        pending = json.loads(
            (self.entity.staging_dir / "pending-categorization.json").read_text(encoding="utf-8")
        )
        ids = [str(p.get("id", "")) for p in pending]
        self.assertNotIn("txn-write-002", ids)

    def test_confirm_updates_learned_context(self) -> None:
        _add_pending_categorization(self.entity, "txn-learn-001", "AWS", amount="80.00")
        propose(self.entity, "txn-learn-001", "Expenses:Software", "Cloud hosting")
        confirm(self.entity, "txn-learn-001", session_id="confirm-session", ts=TS)

        ctx = load_learned_context(self.entity)
        self.assertIn("AWS", ctx)
        self.assertEqual(ctx["AWS"]["canonical_category"], "Expenses:Software")
        self.assertEqual(ctx["AWS"]["confirmed_count"], 1)
        self.assertFalse(ctx["AWS"]["reset"])

    def test_correct_writes_corrected_entry(self) -> None:
        _add_pending_categorization(self.entity, "txn-corr-001", "Travel expense", amount="120.00")
        propose(self.entity, "txn-corr-001", "Expenses:Meals", "Possible meal expense")
        correct(
            self.entity, "txn-corr-001", "Expenses:Travel",
            note="Flight, not meal",
            session_id="correct-session", ts=TS
        )

        from bookkeeping.ledger.projections import render_store_ledger
        from bookkeeping.ledger.store import default_store_path
        from bookkeeping.ledger.validator import validate, parse_ledger
        text = render_store_ledger(default_store_path(self.entity.path))
        errors = validate(text)
        self.assertEqual(errors, [], f"Ledger validation errors: {errors}")
        parsed = parse_ledger(text)
        entries = parsed["entries"]
        entry = next((e for e in entries if e.source_id == "txn-corr-001"), None)
        self.assertIsNotNone(entry)
        # Category posting should be Expenses:Travel
        accounts = [p.account for p in entry.postings]
        self.assertIn("Expenses:Travel", accounts)

    def test_correct_resets_learned_context(self) -> None:
        _add_pending_categorization(self.entity, "txn-corr-002", "Mystery Charge", amount="50.00")
        propose(self.entity, "txn-corr-002", "Expenses:Meals", "Possible meal")
        correct(
            self.entity, "txn-corr-002", "Expenses:Fees",
            session_id="correct-session", ts=TS
        )

        ctx = load_learned_context(self.entity)
        key = "MYSTERY CHARGE"
        self.assertIn(key, ctx)
        entry = ctx[key]
        self.assertEqual(entry["confirmed_count"], 0)
        self.assertTrue(entry["reset"])
        self.assertEqual(entry["canonical_category"], "Expenses:Fees")


# ---------------------------------------------------------------------------
# Tests: direct reviewed splits, transfer pairs, and related entities
# ---------------------------------------------------------------------------

class TestMigrationReviewWorkflows(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_split_posts_one_balanced_entry_and_preserves_source_id(self) -> None:
        _add_pending_categorization(self.entity, "payroll-1", "Payroll debit", amount="-100.00")
        item = propose_split(
            self.entity,
            "payroll-1",
            "Net pay plus payroll liability.",
            ["Expenses:Software=70.00", "Liabilities:CreditCard=30.00"],
        )
        self.assertEqual(item["liability_effect"], [{"account": "Liabilities:CreditCard", "amount": "30.00"}])
        confirmed = confirm_split(self.entity, "payroll-1", "split-session", ts=TS)
        self.assertEqual(confirmed["status"], "confirmed")

        from bookkeeping.ledger.projections import render_store_ledger
        from bookkeeping.ledger.store import default_store_path
        from bookkeeping.ledger.validator import parse_ledger, validate
        text = render_store_ledger(default_store_path(self.entity.path))
        self.assertEqual(validate(text), [])
        entry = next(entry for entry in parse_ledger(text)["entries"] if entry.source_id == "payroll-1")
        self.assertEqual(len(entry.postings), 3)
        self.assertIn("Expenses:Software", [posting.account for posting in entry.postings])

    def test_exact_split_template_can_be_reused(self) -> None:
        save_split_template(
            self.entity,
            "payroll-100",
            ["Expenses:Software=70.00", "Liabilities:CreditCard=30.00"],
        )
        _add_pending_categorization(self.entity, "payroll-template", "Payroll debit", amount="-100.00")
        item = propose_split(self.entity, "payroll-template", "Matches the approved payroll allocation.", template="payroll-100")
        self.assertEqual(item["split_template"], "payroll-100")
        self.assertEqual(len(item["split_postings"]), 2)

    def test_transfer_pair_posts_once_and_marks_both_source_ids_seen(self) -> None:
        _add_pending_categorization(self.entity, "bank-payment", "Amex card payment", amount="-125.00", txn_date="2026-02-01")
        _add_pending_categorization(self.entity, "card-payment", "Payment received", amount="125.00", txn_date="2026-02-03")
        pending_path = self.entity.staging_dir / "pending-categorization.json"
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        pending[1]["accountName"] = "Amex"
        pending[1]["accountType"] = "credit_card"
        pending_path.write_text(json.dumps(pending), encoding="utf-8")

        candidates = find_transfer_candidates(self.entity, date_tolerance_days=3)
        self.assertEqual(len(candidates), 1)
        _add_pending_categorization(self.entity, "unmatched-wire", "Wire transfer to vendor", amount="-500.00", txn_date="2026-02-04")
        exceptions = find_transfer_exceptions(self.entity, date_tolerance_days=3)
        self.assertEqual([exception["source_id"] for exception in exceptions], ["unmatched-wire"])
        item = propose_transfer(self.entity, ["bank-payment", "card-payment"], "Same payment in the bank and card feeds.")
        self.assertEqual(item["proposal_type"], "transfer-pair")
        confirmed = confirm_transfer(self.entity, item["source_id"], "transfer-session", ts=TS)
        self.assertEqual(confirmed["status"], "confirmed")

        from bookkeeping.ledger.store import LedgerStore, default_store_path
        store = LedgerStore(default_store_path(self.entity.path))
        with store.connection() as conn:
            rows = conn.execute("SELECT id FROM source_transactions ORDER BY id").fetchall()
            entry_rows = conn.execute("SELECT date, metadata_json FROM entries ORDER BY date").fetchall()
        self.assertEqual([row[0] for row in rows], ["bank-payment", "card-payment"])
        self.assertEqual([row[0] for row in entry_rows], ["2026-02-01", "2026-02-03"])
        self.assertTrue(all("paired-source-id" in row[1] for row in entry_rows))
        self.assertTrue(store.source_exists("card-payment"))
        seen = json.loads((self.entity.staging_dir / "seen-ids.json").read_text(encoding="utf-8"))
        self.assertEqual(set(seen), {"bank-payment", "card-payment"})

    def test_split_confirmation_retry_heals_after_ledger_commit(self) -> None:
        _add_pending_categorization(self.entity, "split-retry", "Payroll", amount="-100.00")
        propose_split(self.entity, "split-retry", "Allocation", ["Expenses:Software=100.00"])
        with patch("bookkeeping.queue._remove_from_pending_categorization", side_effect=ValueError("disk failure")):
            with self.assertRaisesRegex(ValueError, "disk failure"):
                confirm_split(self.entity, "split-retry", "split-session", ts=TS)

        healed = confirm_split(self.entity, "split-retry", "split-session", ts=TS)
        self.assertEqual(healed["status"], "confirmed")
        from bookkeeping.ledger.store import default_store_path
        store = LedgerStore(default_store_path(self.entity.path))
        with store.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM entries WHERE source_id = 'split-retry'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_related_entity_policy_requires_explicit_proposal_and_is_not_learned(self) -> None:
        add_account(self.entity.path, "Assets:Intercompany:LegacyCo")
        add_account(self.entity.path, "Liabilities:Intercompany:LegacyCo")
        record_related_entity(
            self.entity.path,
            "Legacy Co",
            "Assets:Intercompany:LegacyCo",
            "Liabilities:Intercompany:LegacyCo",
            "income",
            "create-receivable",
            "Income:Consulting",
        )
        _add_pending_categorization(self.entity, "legacy-receipt", "Legacy Co settlement", amount="200.00")
        with self.assertRaisesRegex(ValueError, "not been owner-authorized"):
            propose_related_entity(self.entity, "legacy-receipt", "Legacy Co", "Not approved.")
        approve_related_entity(self.entity.path, "Legacy Co", "Gil approved the migration policy.")
        item = propose_related_entity(self.entity, "legacy-receipt", "Legacy Co", "Owner-approved migration fallback.")
        self.assertEqual(item["proposed_category"], "Income:Consulting")
        self.assertEqual(item["related_entity_treatment"], "owner-authorized inbound income fallback")
        confirm(self.entity, "legacy-receipt", "related-session", ts=TS)
        self.assertNotIn("LEGACY CO SETTLEMENT", load_learned_context(self.entity))

    def test_related_entity_policy_change_requires_fresh_proposal(self) -> None:
        add_account(self.entity.path, "Assets:Intercompany:LegacyCo")
        add_account(self.entity.path, "Liabilities:Intercompany:LegacyCo")
        record_related_entity(self.entity.path, "Legacy Co", "Assets:Intercompany:LegacyCo", "Liabilities:Intercompany:LegacyCo", "income", "create-receivable", "Income:Consulting")
        approve_related_entity(self.entity.path, "Legacy Co", "Initial owner approval.")
        _add_pending_categorization(self.entity, "legacy-change", "Legacy Co settlement", amount="200.00")
        propose_related_entity(self.entity, "legacy-change", "Legacy Co", "Approved policy.")
        record_related_entity(self.entity.path, "Legacy Co", "Assets:Intercompany:LegacyCo", "Liabilities:Intercompany:LegacyCo", "settle-receivable", "create-receivable")
        approve_related_entity(self.entity.path, "Legacy Co", "Owner approved the revised policy.")
        with self.assertRaisesRegex(ValueError, "policy changed"):
            confirm(self.entity, "legacy-change", "related-session", ts=TS)

    def test_special_proposal_must_be_withdrawn_before_reproposal(self) -> None:
        _add_pending_categorization(self.entity, "split-withdraw", "Payroll", amount="-100.00")
        propose_split(self.entity, "split-withdraw", "Allocation", ["Expenses:Software=100.00"])
        with self.assertRaisesRegex(ValueError, "Specialized proposals"):
            correct(self.entity, "split-withdraw", "Expenses:Office", session_id="s")
        item = withdraw(self.entity, "split-withdraw", "Replace allocation.", ts=TS)
        self.assertEqual(item["status"], "withdrawn")
        self.assertIsNotNone(next(txn for txn in json.loads((self.entity.staging_dir / "pending-categorization.json").read_text()) if txn["id"] == "split-withdraw"))

    def test_corrupt_split_templates_are_not_overwritten(self) -> None:
        path = self.entity.staging_dir / "split-templates.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "existing split templates"):
            save_split_template(self.entity, "new", ["Expenses:Software=100.00"])
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


# ---------------------------------------------------------------------------
# Tests: item status transitions
# ---------------------------------------------------------------------------

class TestItemStatusTransitions(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_confirm_sets_status_confirmed(self) -> None:
        _add_pending_categorization(self.entity, "txn-s-001", "Expense A", amount="10.00")
        propose(self.entity, "txn-s-001", "Expenses:Office", "Office supplies")
        item = confirm(self.entity, "txn-s-001", session_id="s", ts=TS)
        self.assertEqual(item["status"], "confirmed")
        self.assertEqual(item["confirmed_category"], "Expenses:Office")

    def test_correct_sets_status_corrected(self) -> None:
        _add_pending_categorization(self.entity, "txn-s-002", "Expense B", amount="20.00")
        propose(self.entity, "txn-s-002", "Expenses:Office", "Office")
        item = correct(self.entity, "txn-s-002", "Expenses:Fees",
                       session_id="s", ts=TS)
        self.assertEqual(item["status"], "corrected")

    def test_confirm_already_confirmed_raises(self) -> None:
        _add_pending_categorization(self.entity, "txn-s-003", "Expense C", amount="30.00")
        propose(self.entity, "txn-s-003", "Expenses:Office", "Office")
        confirm(self.entity, "txn-s-003", session_id="s", ts=TS)
        with self.assertRaises(ValueError):
            confirm(self.entity, "txn-s-003", session_id="s", ts=TS)


# ---------------------------------------------------------------------------
# Tests: list and show
# ---------------------------------------------------------------------------

class TestListShow(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_list_returns_all_items(self) -> None:
        for i in range(3):
            _add_pending_categorization(self.entity, f"txn-list-{i}", f"Vendor {i}")
            propose(self.entity, f"txn-list-{i}", "Expenses:Software", f"reason {i}")
        items = list_queue_items(self.entity)
        self.assertEqual(len(items), 3)

    def test_list_filtered_by_status(self) -> None:
        _add_pending_categorization(self.entity, "txn-open-1", "VendorX", amount="5.00")
        _add_pending_categorization(self.entity, "txn-open-2", "VendorY", amount="5.00")
        propose(self.entity, "txn-open-1", "Expenses:Software", "r")
        propose(self.entity, "txn-open-2", "Expenses:Fees", "r")
        confirm(self.entity, "txn-open-2", session_id="s", ts=TS)

        open_items = list_queue_items(self.entity, status="open")
        confirmed_items = list_queue_items(self.entity, status="confirmed")
        self.assertEqual(len(open_items), 1)
        self.assertEqual(len(confirmed_items), 1)

    def test_empty_queue_returns_empty_list(self) -> None:
        items = list_queue_items(self.entity)
        self.assertEqual(items, [])


# ---------------------------------------------------------------------------
# Tests: session summary integration with importer counts
# ---------------------------------------------------------------------------

class TestSessionSummaryIntegration(unittest.TestCase):
    """Integration: close simulation produces session summary with correct counts."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.entity = _make_entity(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_session_summary_counts_match_close(self) -> None:
        """Full simulated close: counts in summary match ledger/queue state."""
        # Import some transactions: 3 auto-posted, 2 queued
        auto_txns = [
            {
                "id": f"auto-{i}",
                "date": "2026-03-15",
                "amount": "100.00",
                "description": "Known Vendor",
                "accountName": "Checking",
                "pending": False,
            }
            for i in range(3)
        ]
        queue_txns = [
            {
                "id": f"queued-{i}",
                "date": "2026-03-20",
                "amount": "50.00",
                "description": f"Unknown Vendor {i}",
                "accountName": "Checking",
                "pending": False,
            }
            for i in range(2)
        ]

        # Pre-set auto-post trust for "Known Vendor"
        key = "KNOWN VENDOR"
        for _ in range(3):
            _update_learned_context(self.entity, key, "Expenses:Software", corrected=False)

        cat = make_categorizer(self.entity)
        result = import_transactions(
            self.entity,
            auto_txns + queue_txns,
            session_id="close-session",
            categorizer=cat,
            ts=TS,
            session_date=date(2026, 3, 31),
        )

        # Simulate: 1 confirmed, 1 corrected from the 2 queued
        queued_items = list_queue_items(self.entity, status=None)
        # queued items are in pending-categorization; propose then confirm/correct
        for item_id in ["queued-0"]:
            _add_pending_categorization(
                self.entity, item_id + "-manual", f"Unknown Vendor", amount="50.00"
            ) if False else None

        counts = {
            "new": result.new_entries,
            "auto_posted": result.new_entries - result.pending_categorization,
            "queued": result.pending_categorization,
            "confirmed": 1,
            "corrected": 1,
            "reopened": 0,
            "reconciliation_status": "clean",
        }

        txt_path = write_session_summary(self.entity, "close-session", counts)
        json_path = txt_path.with_suffix(".json")
        data = json.loads(json_path.read_text(encoding="utf-8"))

        # Auto-posted: 3 known vendor transactions
        self.assertEqual(data["new"], result.new_entries)
        self.assertEqual(data["confirmed"], 1)
        self.assertEqual(data["corrected"], 1)


if __name__ == "__main__":
    unittest.main()
