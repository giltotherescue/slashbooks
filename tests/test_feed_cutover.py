"""Cutover date per mapped account: a feed import skips earlier rows unless --backfill."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.bank_accounts import clip_windows_to_cutover, split_at_cutover  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.entity import add_account, init_entity, load_entity, map_bank_account  # noqa: E402
from bookkeeping.ledger.importer import _load_pending_categorization  # noqa: E402
from datetime import date  # noqa: E402

CHECKING = "Assets:Bank:Operating-Checking"
SAVINGS = "Assets:Bank:Reserve-Savings"


def _company(tmp: str, *, cutover: str | None = "2026-01-31") -> Path:
    root = Path(tmp).resolve() / "company"
    init_entity(root, name="Harbor Studio", business_type="consulting", legal_structure="llc")
    for account in (CHECKING, SAVINGS):
        add_account(root, account, open_date="2025-01-01")
    map_bank_account(root, "acct_1", CHECKING, provider="plaid", cutover_date=cutover)
    map_bank_account(root, "acct_2", SAVINGS)  # flat: no cutover
    return root


def _row(source_id: str, day: str, account: str = "acct_1", **changes) -> dict:
    row = {"id": source_id, "date": day, "description": f"Vendor {source_id}", "amount": "-25.00",
           "accountId": account, "accountName": "Checking", "accountType": "checking", "pending": False}
    row.update(changes)
    return row


def _ingest(root: Path, rows: list[dict], *extra: str) -> tuple[int, str]:
    path = root.parent / "feed.json"
    path.write_text(json.dumps({"transactions": rows}), encoding="utf-8")
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = main(["ingest", str(path), "--entity", str(root), "--source", "plaid", "--no-feed-window", *extra])
    return code, out.getvalue()


def _queued(root: Path) -> list[str]:
    return sorted(item["id"] for item in _load_pending_categorization(load_entity(root)))


class CutoverTests(unittest.TestCase):
    ROWS = [_row("old1", "2026-01-30"), _row("edge", "2026-01-31"), _row("new1", "2026-02-01"),
            _row("sav1", "2026-01-05", account="acct_2")]

    def test_rows_on_or_before_the_cutover_go_to_review_and_are_never_posted(self) -> None:
        from bookkeeping.queue import list_duplicate_candidates
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            code, out = _ingest(root, self.ROWS)
            self.assertEqual(code, 0)
            self.assertEqual(_queued(root), ["plaid:new1", "plaid:sav1"])
            held = {c["source_id"]: c["match_rule"] for c in list_duplicate_candidates(load_entity(root))}
            self.assertEqual(held, {"plaid:old1": "before_cutover_not_in_books",
                                    "plaid:edge": "before_cutover_not_in_books"})
            self.assertIn(f"{CHECKING}: 2 row(s) on or before 2026-01-31 go to review", out)

    def test_a_late_row_the_old_provider_never_sent_is_not_lost(self) -> None:
        """A row dated on the cutover day that the old source never delivered reaches a person."""
        from bookkeeping.ledger.importer import import_transactions
        from bookkeeping.queue import list_duplicate_candidates, resolve_duplicate_candidate
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            import_transactions(load_entity(root), [_row("old-a", "2026-01-31", description="KNOWN")], "old",
                                lambda t: ("Expenses:Services", "high"))
            add_account(root, "Expenses:Services", open_date="2025-01-01")
            _ingest(root, [_row("late", "2026-01-31", description="NEVER SENT BY OLD PROVIDER", amount="-81.00")])
            self.assertEqual([c["source_id"] for c in list_duplicate_candidates(load_entity(root))], ["plaid:late"])
            self.assertEqual(_queued(root), [])
            _ingest(root, [_row("late", "2026-01-31", description="NEVER SENT BY OLD PROVIDER", amount="-81.00")])
            self.assertEqual(_queued(root), [])  # a resync does not release it
            resolve_duplicate_candidate(load_entity(root), "plaid:late", "distinct")
            self.assertEqual(_queued(root), ["plaid:late"])

    def test_a_generic_ingest_never_posts_a_pre_cutover_row_even_with_a_learned_rule(self) -> None:
        from bookkeeping.ledger.importer import import_transactions
        from bookkeeping.ledger.store import LedgerStore, default_store_path
        from bookkeeping.queue import list_duplicate_candidates, set_taught
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            add_account(root, "Expenses:Services", open_date="2025-01-01")
            set_taught(load_entity(root), "GENERIC VENDOR", "Expenses:Services", by="owner", by_name="Owner",
                       ts="2026-01-01T00:00:00Z")
            path = root.parent / "feed.json"
            rows = [_row("g1", "2026-01-30", description="GENERIC VENDOR"),
                    _row("g2", "2026-02-02", description="GENERIC VENDOR", amount="-31.00")]
            path.write_text(json.dumps({"transactions": rows}), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = main(["ingest", str(path), "--entity", str(root), "--source", "ingest", "--no-feed-window"])
            self.assertEqual(code, 0)
            posted = sorted(e.source_id for e in LedgerStore(default_store_path(root)).load_entries())
            self.assertNotIn("g1", posted)               # the early row is never posted
            self.assertNotIn("g1", _queued(root))
            self.assertEqual(_queued(root) + [x for x in posted if x == "g2"], ["g2"])  # the later row follows the normal path
            self.assertEqual([c["source_id"] for c in list_duplicate_candidates(load_entity(root))], ["g1"])
            self.assertIn("go to review, none are posted", out.getvalue())  # and the message is true
            # The importer itself, with the guard off.
            result = import_transactions(
                load_entity(root), [_row("g3", "2026-01-29", description="GENERIC VENDOR")], "s",
                lambda t: ("Expenses:Services", "high"), cutover_hold_ids=frozenset({"g3"}))
            self.assertEqual((result.new_entries, result.pending_categorization, result.duplicate_candidates), (0, 0, 1))

    def test_cutover_date_can_be_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self.assertEqual(main(["entity", "bank-account-map", str(root), "--feed-account-id", "acct_1",
                                   "--account", CHECKING, "--clear-cutover-date"]), 0)
            entry = json.loads((root / "entity.json").read_text())["bank_account_mappings"]["acct_1"]
            self.assertNotIn("cutover_date", entry)
            self.assertEqual(entry["provider"], "plaid")
            _ingest(root, self.ROWS)
            self.assertEqual(len(_queued(root)), 4)

    def test_backfill_imports_the_earlier_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            code, out = _ingest(root, self.ROWS, "--backfill")
            self.assertEqual(code, 0)
            self.assertEqual(_queued(root), ["plaid:edge", "plaid:new1", "plaid:old1", "plaid:sav1"])
            self.assertNotIn("before cutover", out)

    def test_no_cutover_means_nothing_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp, cutover=None)
            _ingest(root, self.ROWS)
            self.assertEqual(len(_queued(root)), 4)

    def test_pending_rows_before_the_cutover_are_skipped_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            _ingest(root, [_row("pend", "2026-01-20", pending=True)])
            staged = root / "staging" / "pending.json"
            self.assertEqual(json.loads(staged.read_text()) if staged.exists() else [], [])

    def test_split_at_cutover_ignores_unreadable_dates_and_unmapped_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = load_entity(_company(tmp))
            rows = [_row("bad-date", "not-a-date"), _row("none", "2026-01-01", account="other")]
            kept, hold_ids, report = split_at_cutover(entity, rows)
            self.assertEqual((len(kept), hold_ids, report), (2, set(), {}))

    def test_coverage_windows_start_after_the_cutover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = load_entity(_company(tmp))
            windows = [
                {"account": CHECKING, "from": date(2026, 1, 10), "to": date(2026, 2, 28), "source": "x"},
                {"account": CHECKING, "from": date(2026, 1, 1), "to": date(2026, 1, 20), "source": "y"},
                {"account": CHECKING, "from": date(2026, 3, 1), "to": date(2026, 3, 31), "source": "z"},
                {"account": SAVINGS, "from": date(2026, 1, 1), "to": date(2026, 1, 20), "source": "s"},
            ]
            clipped = clip_windows_to_cutover(entity, windows)
            self.assertEqual([(w["account"], w["from"], w["to"]) for w in clipped], [
                (CHECKING, date(2026, 2, 1), date(2026, 2, 28)),
                (CHECKING, date(2026, 3, 1), date(2026, 3, 31)),
                (SAVINGS, date(2026, 1, 1), date(2026, 1, 20))])

    def test_a_prepared_snapshot_keeps_every_row_and_early_posted_rows_are_held(self) -> None:
        from bookkeeping.queue import list_duplicate_candidates
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            path = root.parent / "feed.json"
            path.write_text(json.dumps({"transactions": self.ROWS}), encoding="utf-8")
            snapshot = [dict(r, id="plaid:" + r["id"]) for r in self.ROWS]
            with mock.patch("bookkeeping.preparation_runtime.prepared_ingest_transactions",
                            return_value=(snapshot, False)), redirect_stdout(io.StringIO()):
                code = main(["ingest", str(path), "--entity", str(root), "--source", "plaid", "--no-feed-window"])
            self.assertEqual(code, 0)
            self.assertEqual(_queued(root), ["plaid:new1", "plaid:sav1"])
            self.assertEqual(len(list_duplicate_candidates(load_entity(root))), 2)


if __name__ == "__main__":
    unittest.main()
