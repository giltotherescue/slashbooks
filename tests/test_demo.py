from __future__ import annotations

"""Tests for fictional demo company scaffolding."""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.cli import main  # noqa: E402
from bookkeeping.demo import DEMO_COMPANY_NAME, demo_store_counts, init_demo  # noqa: E402
from bookkeeping.entity import load_entity  # noqa: E402
from bookkeeping.reports.workbook import run_sanity_checks  # noqa: E402


class DemoInitTests(unittest.TestCase):
    def test_init_demo_creates_sqlite_backed_company(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "northstar-demo"

            result = init_demo(target, as_of=date(2026, 6, 26))

            self.assertEqual(result.target, target.resolve())
            self.assertGreater(result.posted_entries, 250)
            self.assertEqual(result.queued_for_review, 3)
            self.assertEqual(result.period_start, date(2025, 1, 1))
            self.assertEqual(result.period_end, date(2026, 6, 26))
            self.assertTrue((target / "ledger.sqlite").exists())
            self.assertFalse((target / "books.beancount").exists())
            self.assertFalse((target / "chart-of-accounts.beancount").exists())
            self.assertTrue((target / "DEMO.md").exists())
            self.assertTrue((target / "ONBOARDING.md").exists())

            entity = load_entity(target)
            self.assertEqual(entity.name, DEMO_COMPANY_NAME)
            self.assertEqual(entity.business_type, "saas")
            self.assertEqual(entity.entity_config["cutover_date"], "2025-01-01")
            self.assertEqual(entity.entity_config["country"], "US")
            self.assertEqual(entity.entity_config["tax_jurisdiction"], "US")
            self.assertEqual(entity.entity_config["operating_currency"], "USD")
            self.assertEqual(entity.entity_config["indirect_tax"], {"registered": False, "type": None})

            counts = demo_store_counts(target)
            self.assertGreaterEqual(counts["entries"], result.posted_entries)
            self.assertEqual(counts["postings"], result.posted_entries * 2)
            self.assertGreater(counts["audit_events"], result.posted_entries)

            normalized = json.loads(
                (target / "ingestion" / "demo-normalized-transactions.json").read_text(
                    encoding="utf-8"
                )
            )
            dates = [txn["date"] for txn in normalized["transactions"]]
            self.assertTrue(any(txn_date.startswith("2025-") for txn_date in dates))
            self.assertTrue(any(txn_date.startswith("2026-") for txn_date in dates))
            self.assertLessEqual(max(dates), "2026-06-26")
            self.assertNotIn("2026-06-27", dates)

    def test_init_demo_writes_onboarding_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "northstar-demo"
            result = init_demo(target, as_of=date(2026, 6, 26))

            text = (target / "ONBOARDING.md").read_text(encoding="utf-8")
            for question, answer in result.onboarding_answers:
                self.assertIn(question, text)
                self.assertIn(answer, text)
            self.assertIn("2025-01-01", text)
            self.assertIn("2026 year-to-date through 2026-06-26", text)

    def test_demo_sanity_checks_do_not_warn_for_missing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "northstar-demo"
            init_demo(target, as_of=date(2026, 6, 26))

            result = run_sanity_checks(target, date(2025, 1, 1), date(2026, 6, 26))
            checks = {check.check: check for check in result.checks}

            self.assertEqual(checks["entity_metadata"].status, "pass")
            self.assertEqual(checks["currency_scope"].status, "pass")

    def test_init_demo_seeds_review_and_learned_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "northstar-demo"
            init_demo(target, as_of=date(2026, 6, 26))

            pending = json.loads(
                (target / "staging" / "pending-categorization.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["id"] for item in pending],
                ["demo-review-001", "demo-review-002", "demo-review-003"],
            )
            self.assertTrue((target / "review-queue" / "demo-review-001.json").exists())
            self.assertTrue((target / "review-queue" / "demo-review-002.json").exists())
            self.assertTrue((target / "review-queue" / "demo-review-003.json").exists())

            learned = json.loads(
                (target / "learned-context" / "counterparties.json").read_text(encoding="utf-8")
            )
            self.assertIn("AWS", learned)
            self.assertEqual(learned["AWS"]["canonical_category"], "Expenses:Hosting")
            self.assertGreaterEqual(learned["AWS"]["confirmed_count"], 3)

    def test_demo_init_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "northstar-demo"

            first = init_demo(target, as_of=date(2026, 6, 26))
            second = init_demo(target, as_of=date(2026, 6, 26))

            self.assertGreater(first.posted_entries, 0)
            self.assertEqual(second.posted_entries, 0)
            self.assertEqual(second.queued_for_review, 0)
            self.assertEqual(demo_store_counts(target)["entries"], first.posted_entries)

    def test_cli_registers_demo_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "northstar-demo"

            rc = main(["demo", "init", str(target)])

            self.assertEqual(rc, 0)
            self.assertTrue((target / "ledger.sqlite").exists())

    def test_demo_refuses_package_repo_path(self) -> None:
        target = ROOT / "demo-would-be-entity"
        self.assertEqual(main(["demo", "init", str(target)]), 1)


if __name__ == "__main__":
    unittest.main()
