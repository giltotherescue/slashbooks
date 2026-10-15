from __future__ import annotations

"""Tests for `books ingest` (src/bookkeeping/ingest.py) and the importer's
pending-categorization dedup behavior it relies on.

Covers:
  - A connector's normalized JSON (array form and {"transactions": [...]} form)
    is ingested and routed to the review worklist.
  - Re-ingesting the same file is idempotent (no duplicate worklist items).
  - The importer now counts an already-queued repeat as a duplicate.
  - Malformed input is rejected with a clear error.
"""

import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping import ingest as ingest_module  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.entity import Entity  # noqa: E402
from bookkeeping.ledger.importer import import_transactions  # noqa: E402


def _make_entity(tmp_dir: Path) -> Path:
    entity_dir = tmp_dir / "co"
    entity_dir.mkdir(parents=True, exist_ok=True)
    (entity_dir / "staging").mkdir(exist_ok=True)
    (entity_dir / "reports").mkdir(exist_ok=True)
    (entity_dir / "entity.json").write_text(
        json.dumps({"name": "Test Co", "business_type": "consulting"}) + "\n",
        encoding="utf-8",
    )
    (entity_dir / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n",
        encoding="utf-8",
    )
    return entity_dir


def _txns() -> list[dict]:
    return [
        {
            "id": "x1", "date": "2026-01-03", "description": "AWS",
            "amount": "-340.12", "accountId": "acct_chk",
            "accountName": "Checking", "accountType": "checking", "pending": False,
        },
        {
            "id": "x2", "date": "2026-01-07", "description": "Stripe Payout",
            "amount": "5120.00", "accountId": "acct_chk",
            "accountName": "Checking", "accountType": "checking", "pending": False,
        },
    ]


def _pending(entity_dir: Path) -> list[dict]:
    path = entity_dir / "staging" / "pending-categorization.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


class IngestCommandTests(unittest.TestCase):
    def test_intake_only_cli_ignores_a_trusted_categorizer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = _make_entity(Path(tmp))
            infile = Path(tmp) / 'input.json'
            infile.write_text(json.dumps(_txns()))
            with patch.object(ingest_module.queue_module, 'make_categorizer',
                              return_value=lambda txn: ('Expenses:Software', 'auto')):
                rc = main(['ingest', str(infile), '--entity', str(entity_dir), '--intake-only'])
            self.assertEqual(0, rc)
            self.assertEqual(2, len(_pending(entity_dir)))

    def test_array_input_routes_to_review_worklist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = _make_entity(Path(tmp))
            infile = Path(tmp) / "conn.json"
            infile.write_text(json.dumps(_txns()), encoding="utf-8")

            rc = main(["ingest", str(infile), "--entity", str(entity_dir), "--source", "test"])

            self.assertEqual(rc, 0)
            self.assertEqual(len(_pending(entity_dir)), 2)

    def test_transactions_object_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = _make_entity(Path(tmp))
            infile = Path(tmp) / "conn.json"
            infile.write_text(json.dumps({"source": "x", "transactions": _txns()}), encoding="utf-8")

            rc = main(["ingest", str(infile), "--entity", str(entity_dir), "--source", "test"])

            self.assertEqual(rc, 0)
            self.assertEqual(len(_pending(entity_dir)), 2)

    def test_banksync_download_is_unwrapped_and_requires_stable_account_mapping(self) -> None:
        """The documented BankSync download can be ingested without JSON surgery."""
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = _make_entity(Path(tmp))
            (entity_dir / "entity.json").write_text(
                json.dumps({
                    "name": "Test Co",
                    "business_type": "consulting",
                    "bank_account_mappings": {"acct_chk": "Assets:Bank:Checking"},
                }) + "\n",
                encoding="utf-8",
            )
            infile = Path(tmp) / "banksync.json"
            infile.write_text(json.dumps({
                "source": "banksync",
                "banks": [{"accounts": [{
                    "account": {"id": "acct_chk", "accountName": "Checking", "accountType": "checking"},
                    "transactions": _txns(),
                }]}],
            }), encoding="utf-8")

            rc = main(["ingest", str(infile), "--entity", str(entity_dir), "--source", "banksync"])

            self.assertEqual(rc, 0)
            self.assertEqual(len(_pending(entity_dir)), 2)

    def _ledger_entity(self, tmp: str) -> Path:
        from datetime import date as _date

        from bookkeeping.ledger.model import Open
        from bookkeeping.ledger.store import LedgerStore

        entity_dir = _make_entity(Path(tmp))
        config = json.loads((entity_dir / "entity.json").read_text())
        config["bank_account_mappings"] = {"acct_chk": "Assets:Bank:Checking"}
        (entity_dir / "entity.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        store = LedgerStore(entity_dir / "ledger.sqlite")
        store.initialize()
        with store.transaction() as conn:
            store.insert_opens([Open(_date(2025, 1, 1), account, ("USD",)) for account in (
                "Assets:Bank:Checking", "Liabilities:CreditCard:Amex-CSV")], conn)
        return entity_dir

    def test_banksync_download_records_a_conservative_feed_window(self) -> None:
        from bookkeeping.coverage import load_statements

        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = self._ledger_entity(tmp)
            txns = _txns() + [{
                "id": "x3", "date": "2026-01-28", "description": "Card auth", "amount": "-12.00",
                "accountId": "acct_chk", "pending": True,
            }]
            infile = Path(tmp) / "banksync.json"
            infile.write_text(json.dumps({
                "source": "banksync",
                "dateRange": {"from": "2025-12-01", "to": "2026-01-31"},
                "banks": [{"accounts": [
                    {"account": {"id": "acct_chk", "accountName": "Checking", "accountNumberLast4": "0021"},
                     "transactions": txns},
                    {"account": {"id": "acct_unmapped_empty"}, "transactions": []},
                ]}],
            }), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                rc = main(["ingest", str(infile), "--entity", str(entity_dir), "--source", "banksync"])
            self.assertEqual(rc, 0)
            self.assertIn("feed windows:         1 recorded", out.getvalue())
            [record], findings = load_statements(entity_dir)
            self.assertEqual(findings, [])
            # Starts at the first posted row (the feed may return less history than
            # requested) and ends before the first pending row.
            self.assertEqual((record.kind, record.account), ("feed_sync", "Assets:Bank:Checking"))
            self.assertEqual((record.period_start.isoformat(), record.period_end.isoformat()),
                             ("2026-01-03", "2026-01-27"))
            self.assertEqual(record.source, "BankSync Checking ending 0021")
            self.assertEqual(len(record.evidence_sha256), 64)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["ingest", str(infile), "--entity", str(entity_dir),
                                       "--source", "banksync"]), 0)
            self.assertEqual(len(load_statements(entity_dir)[0]), 1)

    def test_explicit_coverage_windows_are_validated_then_recorded(self) -> None:
        from bookkeeping.coverage import load_statements

        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = self._ledger_entity(tmp)
            infile = Path(tmp) / "amex.json"
            infile.write_text(json.dumps({"transactions": _txns(), "coverage_windows": [
                {"account": "Assets:Bank:Ghost", "from": "2026-01-01", "to": "2026-01-31"}]}),
                encoding="utf-8")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = main(["ingest", str(infile), "--entity", str(entity_dir)])
            self.assertEqual(rc, 1)
            self.assertIn("not in the account catalog", err.getvalue())
            self.assertEqual(_pending(entity_dir), [])

            infile.write_text(json.dumps({"transactions": _txns(), "coverage_windows": [
                {"accountId": "acct_chk", "from": "2026-01-01", "to": "2026-01-31", "source": "Export"}]}),
                encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["ingest", str(infile), "--entity", str(entity_dir)]), 0)
            [record], _ = load_statements(entity_dir)
            self.assertEqual((record.kind, record.period_start.isoformat(), record.period_end.isoformat()),
                             ("feed_sync", "2026-01-01", "2026-01-31"))

    def test_banksync_download_without_mapping_stops_before_staging_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = _make_entity(Path(tmp))
            infile = Path(tmp) / "banksync.json"
            infile.write_text(json.dumps({
                "source": "banksync",
                "banks": [{"accounts": [{
                    "account": {"id": "acct_chk", "accountName": "Checking", "accountType": "checking"},
                    "transactions": _txns(),
                }]}],
            }), encoding="utf-8")

            error = io.StringIO()
            with redirect_stderr(error):
                rc = ingest_module.run(type("Args", (), {
                    "entity": entity_dir, "input": infile, "source": "ingest", "session_id": "test",
                })())
            self.assertEqual(rc, 1)
            self.assertIn("bank-account-map", error.getvalue())
            self.assertEqual(_pending(entity_dir), [])

    def test_reingest_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = _make_entity(Path(tmp))
            infile = Path(tmp) / "conn.json"
            infile.write_text(json.dumps(_txns()), encoding="utf-8")

            self.assertEqual(main(["ingest", str(infile), "--entity", str(entity_dir)]), 0)
            self.assertEqual(main(["ingest", str(infile), "--entity", str(entity_dir)]), 0)

            # No duplicate worklist items after the second run.
            self.assertEqual(len(_pending(entity_dir)), 2)

    def test_bad_input_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"nope": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                ingest_module.load_transactions(bad)

    def test_validation_errors_are_actionable_without_traceback_or_partial_state(self) -> None:
        bad_lineage = {**_txns()[1], "id": "fake-correction",
                       "metadata": {"correction-of": "original-source"}}
        typo = {**_txns()[1], "id": "wrong-account-field"}
        typo["account_id"] = typo.pop("accountId")
        cases = [("{", "Expecting"), (json.dumps({"nope": 1}), "JSON array"),
                 (json.dumps([_txns()[0], bad_lineage]), "ledger correct"),
                 (json.dumps([_txns()[0], typo]), "accountId")]
        for contents, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                entity_dir = _make_entity(Path(tmp))
                infile = Path(tmp) / "bad.json"
                infile.write_text(contents)
                before = {str(path.relative_to(entity_dir)): path.read_bytes()
                          for path in entity_dir.rglob("*") if path.is_file()}
                output, error = io.StringIO(), io.StringIO()

                with redirect_stdout(output), redirect_stderr(error):
                    rc = main(["ingest", str(infile), "--entity", str(entity_dir)])

                self.assertEqual(rc, 1)
                self.assertIn("Error:", error.getvalue())
                self.assertIn(message, error.getvalue())
                self.assertNotIn("Traceback", output.getvalue() + error.getvalue())
                self.assertNotIn("Ingested", output.getvalue())
                self.assertEqual({str(path.relative_to(entity_dir)): path.read_bytes()
                                  for path in entity_dir.rglob("*") if path.is_file()}, before)

    def test_unexpected_import_failure_is_not_hidden_as_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = _make_entity(Path(tmp))
            infile = Path(tmp) / "valid.json"
            infile.write_text(json.dumps(_txns()))
            with patch.object(ingest_module, "import_transactions", side_effect=RuntimeError("Unexpected defect")):
                with self.assertRaisesRegex(RuntimeError, "Unexpected defect"):
                    main(["ingest", str(infile), "--entity", str(entity_dir)])


class ImporterPendingDedupTests(unittest.TestCase):
    def test_already_queued_repeat_counts_as_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = _make_entity(Path(tmp))
            entity = Entity(
                path=entity_dir,
                entity_config={"name": "Test Co", "business_type": "consulting"},
                trust_policy={"auto_post_threshold": 3, "queue_all_until_confirmed": True},
            )
            queue_categorizer = lambda txn: ("", "queue")  # noqa: E731 - route all to review

            first = import_transactions(entity, _txns(), session_id="s1", categorizer=queue_categorizer, ts="2026-01-15T00:00:00Z")
            self.assertEqual(first.pending_categorization, 2)
            self.assertEqual(first.skipped_duplicate, 0)

            second = import_transactions(entity, _txns(), session_id="s2", categorizer=queue_categorizer, ts="2026-01-15T00:00:00Z")
            self.assertEqual(second.pending_categorization, 0)
            self.assertEqual(second.skipped_duplicate, 2)
            self.assertEqual(len(_pending(entity_dir)), 2)


if __name__ == "__main__":
    unittest.main()
