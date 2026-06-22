from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from src.bookkeeping.entity import load_entity
from src.bookkeeping.ledger.importer import import_transactions
from src.bookkeeping.ledger.migrate import migrate_beancount_to_store
from src.bookkeeping.ledger.store import LedgerStore, default_store_path
from src.bookkeeping.reports.cache import open_cache
from src.bookkeeping.reports.statements import profit_and_loss
from src.bookkeeping.reports.workbook import generate_accountant_package


def _make_entity(root: Path) -> Path:
    entity = root / "acme"
    entity.mkdir()
    (entity / "entity.json").write_text('{"name":"Acme Co"}\n', encoding="utf-8")
    return entity


def _large_ledger(entry_count: int) -> str:
    lines = [
        'option "title" "Scale Entity"',
        'option "operating_currency" "USD"',
        'option "inferred_tolerance_default" "USD:0.005"',
        "",
        "2026-01-01 open Assets:Bank:Checking USD",
        "2026-01-01 open Income:Revenue:Consulting USD",
        "",
    ]
    start = date(2026, 1, 1)
    for idx in range(entry_count):
        day = start + timedelta(days=idx % 120)
        amount = Decimal("10.00") + Decimal(idx % 7)
        lines.extend(
            [
                f'{day.isoformat()} * "Customer {idx % 11}" "Scale revenue {idx}"',
                f'  source-id: "scale_{idx:05d}"',
                f"  Assets:Bank:Checking        {amount:.2f} USD",
                f"  Income:Revenue:Consulting  -{amount:.2f} USD",
                "",
            ]
        )
    return "\n".join(lines)


class ScalableLedgerRegressionTests(unittest.TestCase):
    def test_store_backed_reports_and_selected_export_scale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            (entity / "books.beancount").write_text(_large_ledger(1500), encoding="utf-8")

            migration = migrate_beancount_to_store(entity, force=True)
            self.assertTrue(migration, migration.error_message)
            self.assertEqual(migration.counts.entries, 1500)
            self.assertEqual(migration.counts.postings, 3000)

            store_path = default_store_path(entity)
            with open_cache(entity) as conn:
                store_entry_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            self.assertEqual(store_entry_count, 1500)

            pnl = profit_and_loss(entity, date(2026, 1, 1), date(2026, 4, 30))
            self.assertEqual(pnl.totals["net_income"], Decimal("19495.00"))

            export = generate_accountant_package(
                entity,
                date(2026, 1, 1),
                date(2026, 4, 30),
                override=True,
                sheets="pnl,trial-balance",
            )
            self.assertTrue(export.success, export.error)
            self.assertEqual(sorted(path.name for path in export.csv_files), ["P-and-L.csv", "Trial-Balance.csv"])
            self.assertTrue(store_path.exists())

    def test_import_writes_store_audit_source_payloads_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_path = _make_entity(Path(tmp))
            entity = load_entity(entity_path)
            txns = [
                {
                    "id": f"live_{idx}",
                    "date": f"2026-02-{idx + 1:02d}",
                    "description": f"Live txn {idx}",
                    "amount": "12.34",
                    "accountName": "Checking",
                    "accountType": "depository",
                }
                for idx in range(25)
            ]

            result = import_transactions(
                entity,
                txns,
                session_id="scale-import",
                categorizer=lambda _txn: ("Income:Revenue:Consulting", "high"),
                ts="2026-06-22T12:00:00Z",
            )
            self.assertEqual(result.new_entries, 25)

            store = LedgerStore(default_store_path(entity_path))
            self.assertEqual(store.counts().entries, 25)
            self.assertEqual(store.verify_audit_chain(), [])
            with store.connection() as conn:
                payload_count = conn.execute("SELECT COUNT(*) FROM source_transactions").fetchone()[0]
            self.assertEqual(payload_count, 25)

            snapshot = (entity_path / "books.beancount").read_text(encoding="utf-8")
            self.assertIn("live_0", snapshot)
            self.assertIn("Income:Revenue:Consulting", snapshot)


if __name__ == "__main__":
    unittest.main()
