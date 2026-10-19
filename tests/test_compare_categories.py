"""Tests for compare_categories: category accuracy against a ground-truth file."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

LEDGER = """\
option "title" "Truth Test LLC"
option "operating_currency" "USD"
option "inferred_tolerance_default" "USD:0.005"

2026-01-01 open Assets:Bank:Checking USD
2026-01-01 open Income:Sales USD
2026-01-01 open Expenses:Software USD
2026-01-01 open Expenses:Meals USD
2026-01-01 open Expenses:Office-Supplies USD
2026-01-01 open Equity:Opening-Balances USD

2026-01-02 * "Client" "Payment"
  source-id: "t-sale"
  Assets:Bank:Checking     500.00 USD
  Income:Sales            -500.00 USD

2026-01-03 * "Vendor" "Subscription"
  source-id: "t-soft"
  Expenses:Software         20.00 USD
  Assets:Bank:Checking     -20.00 USD

2026-01-04 * "Cafe" "Lunch"
  source-id: "t-meals"
  Expenses:Meals            15.00 USD
  Assets:Bank:Checking     -15.00 USD

2026-01-05 * "Store" "Mixed basket"
  source-id: "t-split"
  Expenses:Meals            10.00 USD
  Expenses:Office-Supplies  10.00 USD
  Assets:Bank:Checking     -20.00 USD
"""


def _make_entity(tmp: Path):
    from src.bookkeeping.entity import Entity
    from src.bookkeeping.reports.cache import regenerate

    for name in ("staging", "reports", "learned-context", "review-queue", "ingestion"):
        (tmp / name).mkdir(parents=True, exist_ok=True)
    config = {"name": "Truth Test LLC", "business_type": "consulting"}
    (tmp / "entity.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
    (tmp / "books.beancount").write_text(LEDGER, encoding="utf-8")
    result = regenerate(tmp)
    if not result:
        raise RuntimeError(result.error_message)
    return Entity(path=tmp, entity_config=config, trust_policy={"auto_post_threshold": 3})


def _write_truth(path: Path, rows: list[tuple[str, str]]) -> Path:
    path.write_text(
        "source_id,account\n" + "".join(f"{s},{a}\n" for s, a in rows), encoding="utf-8"
    )
    return path


class CompareCategoriesTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.tmp = Path(self._dir)
        self.entity = _make_entity(self.tmp / "company")
        (self.tmp / "company" / "staging" / "pending-categorization.json").write_text(
            json.dumps([{"id": "t-pending", "date": "2026-01-06", "amount": "-9.00"}]),
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _run(self, rows):
        from src.bookkeeping.compare import compare_categories
        return compare_categories(self.entity, _write_truth(self.tmp / "truth.csv", rows))

    def test_agreement_coverage_and_statuses(self):
        result = self._run([
            ("t-sale", "Income:Sales"),
            ("t-soft", "Expenses:Software"),
            ("t-meals", "Expenses:Office-Supplies"),   # ours says Meals
            ("t-split", "Expenses:Meals"),
            ("t-pending", "Expenses:Software"),
            ("t-gone", "Expenses:Software"),
        ])
        status = {r["source_id"]: r["status"] for r in result["per_transaction"]}
        self.assertEqual(status, {
            "t-sale": "agree", "t-soft": "agree", "t-meals": "disagree",
            "t-split": "not_comparable", "t-pending": "pending", "t-gone": "missing",
        })
        self.assertEqual(result["agreement"]["compared"], 3)
        self.assertEqual(result["agreement"]["agree"], 2)
        self.assertEqual(result["agreement"]["rate"], round(2 / 3, 4))
        self.assertEqual(result["agreement"]["end_to_end_rate"], round(2 / 6, 4))
        coverage = result["coverage"]
        self.assertEqual(coverage["truth_rows"], 6)
        self.assertEqual(coverage["pending"], 1)
        self.assertEqual(coverage["missing"], 1)
        self.assertEqual(coverage["not_comparable"], 1)
        self.assertEqual(coverage["compared"], 3)
        self.assertEqual(coverage["auto_posted"], 4)

    def test_confusion_table_and_per_account(self):
        result = self._run([
            ("t-soft", "Expenses:Software"),
            ("t-meals", "Expenses:Office-Supplies"),
        ])
        confusion = {(c["truth"], c["ours"]): c for c in result["confusion"]}
        self.assertEqual(confusion[("Expenses:Office-Supplies", "Expenses:Meals")]["count"], 1)
        self.assertFalse(confusion[("Expenses:Office-Supplies", "Expenses:Meals")]["agree"])
        self.assertTrue(confusion[("Expenses:Software", "Expenses:Software")]["agree"])
        per_account = {r["account"]: r for r in result["per_account"]}
        self.assertEqual(per_account["Expenses:Software"]["agreement_rate"], 1.0)
        self.assertEqual(per_account["Expenses:Office-Supplies"]["agreement_rate"], 0.0)

    def test_empty_compared_set_has_no_rate(self):
        result = self._run([("t-pending", "Expenses:Software")])
        self.assertIsNone(result["agreement"]["rate"])
        self.assertEqual(result["coverage"]["compared"], 0)

    def test_correction_chain_resolves_original_source_id(self):
        conn = sqlite3.connect(self.tmp / "company" / "reports" / "cache.sqlite")
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
            if "metadata_json" not in cols:
                conn.execute(
                    "ALTER TABLE entries ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '[]'"
                )
            new_id = conn.execute(
                "INSERT INTO entries (date, narration, payee, source_id) VALUES (?, ?, ?, ?)",
                ("2026-01-04", "Corrected", "Cafe", "t-meals-fixed"),
            ).lastrowid
            conn.execute(
                "UPDATE entries SET metadata_json = ? WHERE id = ?",
                ('[["correction-of", "t-meals"]]', new_id),
            )
            conn.executemany(
                "INSERT INTO postings (entry_id, account, amount, currency) VALUES (?, ?, ?, 'USD')",
                [(new_id, "Assets:Bank:Checking", "-15.00"),
                 (new_id, "Expenses:Office-Supplies", "15.00")],
            )
            conn.commit()
        finally:
            conn.close()
        result = self._run([("t-meals", "Expenses:Office-Supplies")])
        self.assertEqual(result["per_transaction"][0]["status"], "agree")

    def test_date_window_excludes_rows_as_missing(self):
        from src.bookkeeping.compare import compare_categories
        truth = _write_truth(self.tmp / "truth.csv", [("t-sale", "Income:Sales")])
        result = compare_categories(
            self.entity, truth, date(2026, 2, 1), date(2026, 2, 28)
        )
        self.assertEqual(result["per_transaction"][0]["status"], "missing")

    def test_truth_file_validation(self):
        from src.bookkeeping.compare import compare_categories
        bad = self.tmp / "bad.csv"
        bad.write_text("id,category\nx,y\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            compare_categories(self.entity, bad)
        dup = _write_truth(self.tmp / "dup.csv", [("a", "X:Y"), ("a", "X:Z")])
        with self.assertRaises(ValueError):
            compare_categories(self.entity, dup)
        half = self.tmp / "half.csv"
        half.write_text("source_id,account\nonly-id,\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            compare_categories(self.entity, half)


if __name__ == "__main__":
    unittest.main()
