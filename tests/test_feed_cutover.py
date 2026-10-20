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

    def test_rows_on_or_before_the_cutover_are_skipped_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            code, out = _ingest(root, self.ROWS)
            self.assertEqual(code, 0)
            self.assertEqual(_queued(root), ["plaid:new1", "plaid:sav1"])
            self.assertIn("before cutover:      2 skipped", out)
            self.assertIn(f"{CHECKING}: 2 on or before 2026-01-31", out)

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
            kept, skipped = split_at_cutover(entity, rows)
            self.assertEqual((len(kept), skipped), (2, {}))

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

    def test_a_prepared_snapshot_with_early_rows_is_refused_not_trimmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            path = root.parent / "feed.json"
            path.write_text(json.dumps({"transactions": self.ROWS}), encoding="utf-8")
            err = io.StringIO()
            with mock.patch("bookkeeping.preparation_runtime.prepared_ingest_transactions",
                            return_value=(self.ROWS, False)), redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = main(["ingest", str(path), "--entity", str(root), "--source", "plaid", "--no-feed-window"])
            self.assertEqual(code, 1)
            self.assertIn("cutover", err.getvalue())
            self.assertEqual(_queued(root), [])


if __name__ == "__main__":
    unittest.main()
