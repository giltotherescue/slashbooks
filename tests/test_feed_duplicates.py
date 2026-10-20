"""Connected feeds share the statement duplicate plan; every connected feed needs a mapping."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.cli import main  # noqa: E402
from bookkeeping.entity import add_account, init_entity, load_entity, map_bank_account  # noqa: E402
from bookkeeping.ledger.importer import _load_pending_categorization, import_transactions  # noqa: E402
from bookkeeping.ledger.store import LedgerStore, default_store_path  # noqa: E402
from bookkeeping.queue import list_duplicate_candidates, resolve_duplicate_candidate  # noqa: E402

CHECKING = "Assets:Bank:Operating-Checking"
POST = lambda t: ("Expenses:Services", "high")  # noqa: E731


def _company(tmp: str) -> Path:
    root = Path(tmp).resolve() / "company"
    init_entity(root, name="Harbor Studio", business_type="consulting", legal_structure="llc")
    add_account(root, CHECKING, open_date="2025-01-01")
    add_account(root, "Expenses:Services", open_date="2025-01-01")
    map_bank_account(root, "bs_1", CHECKING, provider="banksync")
    map_bank_account(root, "plaid_1", CHECKING, provider="plaid")
    return root


def _row(source_id: str, day: str = "2026-01-15", amount: str = "-25.00", description: str = "ACME SUPPLIES",
         account: str = "bs_1") -> dict:
    return {"id": source_id, "date": day, "description": description, "amount": amount,
            "accountId": account, "accountName": "Checking", "accountType": "checking", "pending": False}


def _ingest(root: Path, rows: list[dict], source: str, *extra: str) -> tuple[int, str, str]:
    path = root.parent / "feed.json"
    path.write_text(json.dumps({"transactions": rows}), encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["ingest", str(path), "--entity", str(root), "--source", source, "--no-feed-window", *extra])
    return code, out.getvalue(), err.getvalue()


def _held(root: Path) -> dict[str, str]:
    return {c["source_id"]: c["match_rule"] for c in list_duplicate_candidates(load_entity(root))}


def _queued(root: Path) -> list[str]:
    return sorted(i["id"] for i in _load_pending_categorization(load_entity(root)))


def _posted(root: Path) -> list[str]:
    return sorted(e.source_id for e in LedgerStore(default_store_path(root)).load_entries())


class ProviderSwitchOverlapTests(unittest.TestCase):
    def _old_history(self, root: Path) -> None:
        import_transactions(load_entity(root), [_row("old1"), _row("old2", "2026-01-20", "-40.00", "BETA TOOLS")],
                            "old", POST)

    def test_same_day_same_amount_same_description_from_a_new_provider_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._old_history(root)
            code, out, _ = _ingest(root, [_row("p1", account="plaid_1")], "plaid")
            self.assertEqual(code, 0)
            self.assertEqual(_posted(root), ["old1", "old2"])
            self.assertEqual(_queued(root), [])
            self.assertEqual(_held(root), {"plaid:p1": "legacy_id_same_account_date_amount_description"})
            self.assertIn("duplicate candidates:1", out)

    def test_same_amount_within_three_days_with_another_description_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._old_history(root)
            _ingest(root, [_row("p2", "2026-01-17", "-25.00", "ACME SUPPLIES INC 8841", "plaid_1")], "plaid")
            self.assertEqual(_held(root), {"plaid:p2": "same_amount_within_3_days_other_source"})
            self.assertEqual(_queued(root), [])

    def test_same_amount_four_days_apart_is_not_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._old_history(root)
            _ingest(root, [_row("p3", "2026-01-19", "-25.00", "ACME SUPPLIES INC 8841", "plaid_1")], "plaid")
            self.assertEqual(_held(root), {})
            self.assertEqual(_queued(root), ["plaid:p3"])

    def test_review_items_that_are_still_queued_are_checked_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            import_transactions(load_entity(root), [_row("q1")], "old")  # no categorizer: queued
            _ingest(root, [_row("p4", account="plaid_1")], "plaid")
            self.assertEqual(_queued(root), ["q1"])
            self.assertEqual(list(_held(root)), ["plaid:p4"])

    def test_a_held_row_is_released_as_distinct_or_dropped_as_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._old_history(root)
            _ingest(root, [_row("p5", account="plaid_1"), _row("p6", "2026-01-20", "-40.00", "BETA TOOLS", "plaid_1")],
                    "plaid")
            entity = load_entity(root)
            resolve_duplicate_candidate(entity, "plaid:p5", "duplicate")
            resolve_duplicate_candidate(entity, "plaid:p6", "distinct")
            self.assertEqual(_queued(root), ["plaid:p6"])
            _ingest(root, [_row("p5", account="plaid_1")], "plaid")  # re-sync: stays dropped
            self.assertEqual(_queued(root), ["plaid:p6"])
            self.assertEqual(_posted(root), ["old1", "old2"])

    def test_resync_of_a_held_row_does_not_hold_it_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._old_history(root)
            for _ in range(2):
                _ingest(root, [_row("p7", account="plaid_1")], "plaid")
            self.assertEqual(len(list_duplicate_candidates(load_entity(root))), 1)

    def test_a_row_already_in_the_books_under_the_old_spelling_is_skipped_not_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._old_history(root)
            code, out, _ = _ingest(root, [_row("old1")], "banksync")
            self.assertEqual(code, 0)
            self.assertEqual(_held(root), {})
            self.assertIn("duplicates skipped:  1", out)

    def test_two_real_purchases_from_one_feed_are_not_held_against_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            _ingest(root, [_row("c1", "2026-01-15", "-4.50", "COFFEE")], "banksync")
            _ingest(root, [_row("c2", "2026-01-16", "-4.50", "CAFE ROASTERS")], "banksync")
            self.assertEqual(_held(root), {})
            self.assertEqual(_queued(root), ["banksync:c1", "banksync:c2"])

    def test_banksync_keeps_its_replacement_id_hold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._old_history(root)
            _ingest(root, [_row("recovered")], "banksync")
            self.assertEqual(_held(root), {"banksync:recovered": "legacy_id_same_account_date_amount_description"})

    def test_two_providers_batch_overlap_inside_one_file_is_not_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            _ingest(root, [_row("a", account="plaid_1"), _row("b", account="plaid_1")], "plaid")
            self.assertEqual(_held(root), {})
            self.assertEqual(_queued(root), ["plaid:a", "plaid:b"])


class ResyncAndSpellingTests(unittest.TestCase):
    """A held row stays held; a decision made under one ID spelling is reused under the other."""

    def _history(self, root: Path) -> None:
        import_transactions(load_entity(root), [_row("old1")], "old", POST)

    def _guarded(self, root: Path, row: dict, session: str):
        return import_transactions(load_entity(root), [row], session, POST, legacy_duplicate_guard=True)

    def test_a_held_row_is_not_posted_by_the_next_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._history(root)
            first = self._guarded(root, _row("recovered"), "s0")
            again = self._guarded(root, _row("recovered"), "s1")
            self.assertEqual((first.new_entries, first.duplicate_candidates), (0, 1))
            self.assertEqual((again.new_entries, again.pending_categorization, again.skipped_duplicate), (0, 0, 1))
            self.assertEqual(_posted(root), ["old1"])
            self.assertEqual(_queued(root), [])

    def test_an_open_raw_id_hold_also_holds_the_prefixed_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._history(root)
            self._guarded(root, _row("recovered"), "s0")
            result = self._guarded(root, _row("banksync:recovered"), "s1")
            self.assertEqual((result.new_entries, result.pending_categorization, result.skipped_duplicate), (0, 0, 1))
            self.assertEqual(_posted(root), ["old1"])
            self.assertEqual(len(list_duplicate_candidates(load_entity(root))), 1)

    def test_a_duplicate_decision_under_the_raw_id_is_reused_for_the_prefixed_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._history(root)
            self._guarded(root, _row("recovered"), "s0")
            resolve_duplicate_candidate(load_entity(root), "recovered", "duplicate")
            _ingest(root, [_row("recovered")], "banksync")
            self.assertEqual((_posted(root), _queued(root), _held(root)), (["old1"], [], {}))

    def test_a_distinct_decision_under_the_raw_id_is_reused_for_the_prefixed_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._history(root)
            self._guarded(root, _row("recovered"), "s0")
            resolve_duplicate_candidate(load_entity(root), "recovered", "distinct")
            self.assertEqual(_queued(root), ["recovered"])
            _ingest(root, [_row("recovered")], "banksync")  # the same row again: already queued, not held again
            self.assertEqual((_queued(root), _held(root)), (["recovered"], {}))

    def test_a_distinct_decision_makes_a_plaid_resync_import_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self._history(root)
            _ingest(root, [_row("p1", account="plaid_1")], "plaid")
            resolve_duplicate_candidate(load_entity(root), "plaid:p1", "distinct")
            _ingest(root, [_row("p1", account="plaid_1")], "plaid")
            self.assertEqual((_queued(root), _held(root)), (["plaid:p1"], {}))

    def test_a_row_with_no_description_still_goes_to_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            import_transactions(load_entity(root), [_row("old1", description="")], "old", POST)
            _ingest(root, [_row("p1", description="", account="plaid_1")], "plaid")
            self.assertEqual(list(_held(root)), ["plaid:p1"])
            self.assertEqual(_queued(root), [])

    def test_old_raw_id_rows_count_as_banksync_only_for_a_banksync_feed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            import_transactions(load_entity(root), [_row("raw-old", "2026-01-15", "-4.50", "COFFEE")], "old", POST)
            _ingest(root, [_row("b1", "2026-01-16", "-4.50", "CAFE ROASTERS")], "banksync")
            self.assertEqual(_held(root), {})  # same feed: two real purchases
            _ingest(root, [_row("p9", "2026-01-16", "-4.50", "CAFE ROASTERS", "plaid_1")], "plaid")
            self.assertEqual(list(_held(root)), ["plaid:p9"])  # another provider: held for review


class MappingRequiredTests(unittest.TestCase):
    def test_unmapped_account_from_any_connected_feed_is_refused(self) -> None:
        for source, extra in (("plaid", ()), ("my-sync", ("--feed-provider", "teller"))):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                root = _company(tmp)
                code, _, err = _ingest(root, [_row("u1", account="never_mapped")], source, *extra)
                self.assertEqual(code, 1)
                self.assertIn("account mapping required", err)
                self.assertIn("never_mapped", err)
                self.assertEqual((_queued(root), _posted(root)), ([], []))

    def test_a_feed_row_without_an_account_id_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            row = _row("u2")
            del row["accountId"]
            code, _, err = _ingest(root, [row], "plaid")
            self.assertEqual(code, 1)
            self.assertIn("without an accountId", err)

    def test_banksync_message_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            code, _, err = _ingest(root, [_row("u3", account="nope")], "banksync")
            self.assertEqual(code, 1)
            self.assertIn("BankSync account mapping required before ingesting connected activity for: nope", err)

    def test_a_generic_ingest_still_uses_the_name_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            code, _, _ = _ingest(root, [_row("g1", account="adhoc")], "ingest")
            self.assertEqual(code, 0)
            self.assertEqual(_queued(root), ["g1"])


if __name__ == "__main__":
    unittest.main()
