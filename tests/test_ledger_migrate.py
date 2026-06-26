from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.ledger.migrate import migrate_beancount_to_store
from bookkeeping.ledger.projections import render_store_ledger
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.ledger.validator import parse_ledger, validate
from bookkeeping.cli import main


def _make_entity(tmp_path: Path, ledger_text: str | None = None) -> Path:
    entity = tmp_path / "entity"
    entity.mkdir()
    (entity / "entity.json").write_text('{"name": "Migration Test"}\n', encoding="utf-8")
    if ledger_text is None:
        ledger_text = (ROOT / "tests" / "fixtures" / "ledger" / "golden.beancount").read_text(encoding="utf-8")
    (entity / "books.beancount").write_text(ledger_text, encoding="utf-8")
    return entity


class LedgerMigrationTests(unittest.TestCase):
    def test_dry_run_reports_counts_without_writing_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            result = migrate_beancount_to_store(entity, dry_run=True)

            self.assertTrue(result.success, result.error_message)
            self.assertTrue(result.dry_run)
            self.assertEqual(result.counts.entries, 3)
            self.assertEqual(result.counts.postings, 6)
            self.assertFalse((entity / "ledger.sqlite").exists())

    def test_migration_preserves_fixture_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            result = migrate_beancount_to_store(entity)

            self.assertTrue(result.success, result.error_message)
            store = LedgerStore(entity / "ledger.sqlite")
            entries = store.load_entries()
            self.assertEqual(len(entries), 3)
            self.assertEqual(entries[1].source_id, "txn_001")
            self.assertEqual(entries[1].postings[0].account, "Assets:Mercury")
            self.assertEqual(str(entries[1].postings[0].amount), "2500.00")
            self.assertEqual(store.verify_audit_chain(), [])

    def test_migration_preserves_reversal_and_posting_metadata(self) -> None:
        ledger_text = """\
option "title" "Trace Test"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Bank:Checking USD
2026-01-01 open Expenses:Software USD

2026-01-02 * "Original"
  source-id: "orig-1"
  Assets:Bank:Checking  -10.00 USD
    qb-name: "Checking"
  Expenses:Software      10.00 USD

2026-01-03 * "Reversal"
  reverses: "orig-1"
  Assets:Bank:Checking   10.00 USD
  Expenses:Software     -10.00 USD

2026-01-03 * "Corrected"
  source-id: "orig-1-corrected"
  correction-of: "orig-1"
  Assets:Bank:Checking  -12.00 USD
  Expenses:Software      12.00 USD
"""
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp), ledger_text)
            result = migrate_beancount_to_store(entity)

            self.assertTrue(result.success, result.error_message)
            store = LedgerStore(entity / "ledger.sqlite")
            entries = store.load_entries()
            self.assertEqual(entries[0].postings[0].meta, (("qb-name", "Checking"),))
            self.assertIn(("reverses", "orig-1"), entries[1].meta)
            self.assertIn(("correction-of", "orig-1"), entries[2].meta)

            projected = parse_ledger(render_store_ledger(entity / "ledger.sqlite"))
            self.assertEqual(projected["entries"][0].postings[0].meta, (("qb-name", "Checking"),))
            self.assertIn(("reverses", "orig-1"), projected["entries"][1].meta)

    def test_existing_unreadable_store_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            (entity / "ledger.sqlite").write_text("not sqlite", encoding="utf-8")

            result = migrate_beancount_to_store(entity)

            self.assertFalse(result.success)
            self.assertIn("--force", result.error_message)
            self.assertEqual((entity / "ledger.sqlite").read_text(encoding="utf-8"), "not sqlite")

    def test_migration_is_idempotent_for_same_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            first = migrate_beancount_to_store(entity)
            second = migrate_beancount_to_store(entity)

            self.assertTrue(first.success)
            self.assertTrue(second.success)
            self.assertTrue(second.skipped)
            self.assertEqual(first.counts.entries, second.counts.entries)

    def test_invalid_ledger_leaves_existing_store_intact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            result = migrate_beancount_to_store(entity)
            self.assertTrue(result.success)
            before = (entity / "ledger.sqlite").read_bytes()

            (entity / "books.beancount").write_text(
                '2026-01-01 * "Bad"\n  Assets:Bank:Checking  1.00 USD\n',
                encoding="utf-8",
            )
            failed = migrate_beancount_to_store(entity, force=True)

            self.assertFalse(failed.success)
            self.assertEqual((entity / "ledger.sqlite").read_bytes(), before)

    def test_store_projection_validates_and_matches_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            migrate_beancount_to_store(entity)

            rendered = render_store_ledger(entity / "ledger.sqlite")
            self.assertEqual(validate(rendered), [])

            original = parse_ledger((entity / "books.beancount").read_text(encoding="utf-8"))
            projected = parse_ledger(rendered)
            self.assertEqual(len(projected["opens"]), len(original["opens"]))
            self.assertEqual(len(projected["entries"]), len(original["entries"]))
            self.assertEqual(
                [(p.account, p.amount) for p in projected["entries"][1].postings],
                [(p.account, p.amount) for p in original["entries"][1].postings],
            )

    def test_cli_migrate_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            snapshot = Path(tmp) / "snapshot.beancount"
            out = StringIO()

            with redirect_stdout(out):
                migrate_rc = main(["ledger", "migrate", "--entity", str(entity)])
                snapshot_rc = main([
                    "ledger",
                    "snapshot",
                    "--entity",
                    str(entity),
                    "--output",
                    str(snapshot),
                ])

            self.assertEqual(migrate_rc, 0)
            self.assertEqual(snapshot_rc, 0)
            self.assertTrue((entity / "ledger.sqlite").exists())
            self.assertEqual(validate(snapshot.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
