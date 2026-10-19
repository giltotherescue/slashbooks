"""Tests for `books backtest run --truth`: the reproducible accuracy report."""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
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

FIXTURES = Path(__file__).parent / "fixtures" / "compare"
QB_MATCH = FIXTURES / "qb_match"
BANKSYNC = FIXTURES / "banksync_download.json"

RULES = [
    {"match": "prefix", "pattern": "Client payment", "account": "Income:Consulting-Revenue"},
    {"match": "prefix", "pattern": "Acme Software", "account": "Expenses:Software"},
]
TRUTH = [
    ("txn-jan-revenue", "Income:Consulting-Revenue"),
    ("txn-feb-revenue", "Income:Consulting-Revenue"),
    ("txn-mar-revenue", "Income:Consulting-Revenue"),
    ("txn-jan-software", "Expenses:Software"),
    ("txn-mar-software", "Expenses:Cloud"),            # rule says Software: a disagreement
    ("txn-feb-office", "Expenses:Office-Supplies"),    # no rule: stays pending
]


def _entity_dir(tmp: Path) -> Path:
    path = tmp / "company"
    for name in ("staging", "reports", "learned-context", "review-queue", "ingestion"):
        (path / name).mkdir(parents=True)
    config = {"name": "Acme Consulting LLC", "business_type": "consulting",
              "category_rules": RULES}
    (path / "entity.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
    (path / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n",
        encoding="utf-8",
    )
    return path


def _truth_file(tmp: Path) -> Path:
    path = tmp / "truth.csv"
    path.write_text(
        "source_id,account\n" + "".join(f"{s},{a}\n" for s, a in TRUTH), encoding="utf-8"
    )
    return path


class AccuracyReportTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.tmp = Path(self._dir)
        self.entity = _entity_dir(self.tmp)
        self.truth = _truth_file(self.tmp)
        self.out = self.tmp / "out"

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _run(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["backtest", "run", "--entity", str(self.entity), "--qb-folder", str(QB_MATCH),
                "--from", "2026-01-01", "--to", "2026-03-31", "--banksync-json", str(BANKSYNC),
                "--partial", *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_writes_json_and_markdown_with_stable_schema(self):
        rc, _out, _err = self._run("--truth", str(self.truth), "--report", str(self.out),
                                   "--seed", "7")
        self.assertEqual(rc, 0)
        report = json.loads((self.out / "accuracy-report.json").read_text(encoding="utf-8"))
        self.assertTrue((self.out / "accuracy-report.md").is_file())
        for key in ("schema_version", "report", "generated_at", "engine", "seed", "period",
                    "truth", "thresholds", "n", "headline", "category_agreement", "coverage",
                    "pnl_line_variance", "matched_count", "transaction_matching",
                    "comparison_errors", "notes", "does_not_measure", "per_transaction"):
            self.assertIn(key, report)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["seed"], 7)
        self.assertEqual(set(report["engine"]), {"version", "git_commit", "git_dirty"})
        self.assertEqual(report["thresholds"]["material_abs"], "25.00")
        self.assertEqual(len(report["truth"]["sha256"]), 64)
        self.assertTrue(report["does_not_measure"])

    def test_numbers(self):
        self._run("--truth", str(self.truth), "--report", str(self.out))
        report = json.loads((self.out / "accuracy-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["n"], 6)
        agreement = report["category_agreement"]
        self.assertEqual((agreement["compared"], agreement["agree"], agreement["disagree"]),
                         (5, 4, 1))
        self.assertEqual(agreement["rate"], 0.8)
        self.assertEqual(agreement["end_to_end_rate"], round(4 / 6, 4))
        coverage = report["coverage"]
        self.assertEqual((coverage["compared"], coverage["pending"], coverage["missing"]),
                         (5, 1, 0))
        self.assertEqual(agreement["disagreements"][0]["source_id"], "txn-mar-software")
        self.assertIn("matched_count", report)
        self.assertEqual(report["matched_count"], report["transaction_matching"]["matched_count"])

    def test_no_percentage_without_n_and_coverage(self):
        self._run("--truth", str(self.truth), "--report", str(self.out))
        report = json.loads((self.out / "accuracy-report.json").read_text(encoding="utf-8"))
        self.assertIn("pending review", report["headline"])
        self.assertIn("5 compared rows", report["headline"])
        self.assertIn("6 truth rows", report["headline"])
        md = (self.out / "accuracy-report.md").read_text(encoding="utf-8")
        self.assertIn("What this does not measure", md)
        self.assertIn("## Coverage", md)
        for line in md.splitlines():
            if re.search(r"\d%", line):
                self.assertTrue("n=" in line or "compared rows" in line,
                                f"percentage without n: {line}")

    def test_small_sample_note(self):
        self._run("--truth", str(self.truth), "--report", str(self.out))
        report = json.loads((self.out / "accuracy-report.json").read_text(encoding="utf-8"))
        self.assertTrue(any("too few" in n for n in report["notes"]))

    def test_json_flag_prints_only_json_on_stdout(self):
        rc, out, err = self._run("--truth", str(self.truth), "--report", str(self.out), "--json")
        self.assertEqual(rc, 0)
        printed = json.loads(out)
        self.assertEqual(printed["n"], 6)
        self.assertIn("Step 6", err)

    def test_same_inputs_give_same_numbers(self):
        self._run("--truth", str(self.truth), "--report", str(self.out))
        first = json.loads((self.out / "accuracy-report.json").read_text(encoding="utf-8"))
        shutil.rmtree(self.entity)
        self.entity = _entity_dir(self.tmp)
        self._run("--truth", str(self.truth), "--report", str(self.out))
        second = json.loads((self.out / "accuracy-report.json").read_text(encoding="utf-8"))
        for report in (first, second):
            report.pop("generated_at")
        self.assertEqual(first, second)

    def test_default_report_folder(self):
        self._run("--truth", str(self.truth))
        self.assertTrue(
            (self.entity / "reports" / "migration-confidence" / "accuracy-report.json").is_file()
        )

    def test_flags_need_truth(self):
        for extra in (["--json"], ["--report", str(self.out)], ["--seed", "1"]):
            rc, _out, err = self._run(*extra)
            self.assertEqual(rc, 1)
            self.assertIn("--truth", err)

    def test_missing_truth_file_is_an_error(self):
        rc, _out, err = self._run("--truth", str(self.tmp / "nope.csv"))
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)

    def test_invalid_truth_file_fails_without_report(self):
        bad = self.tmp / "bad.csv"
        bad.write_text("id,category\nx,y\n", encoding="utf-8")
        rc, out, _err = self._run("--truth", str(bad), "--report", str(self.out))
        self.assertEqual(rc, 1)
        self.assertIn("Accuracy report failed", out)
        self.assertFalse((self.out / "accuracy-report.json").exists())


class RemoteContractTests(unittest.TestCase):
    def test_truth_is_an_input_and_report_folder_an_output(self):
        from bookkeeping.remote_contract import command_spec
        spec = command_spec(["backtest", "run", "--entity", "e", "--qb-folder", "q",
                             "--from", "2026-01-01", "--to", "2026-03-31",
                             "--truth", "t.csv", "--report", "out"])
        self.assertIn("truth", spec["input_fields"])
        self.assertIn("report_dir", spec["output_fields"])


class RenderTests(unittest.TestCase):
    def test_rate_none_renders_with_n(self):
        from bookkeeping.accuracy_report import headline
        text = headline(
            {"rate": None, "compared": 0, "agree": 0, "disagree": 0},
            {"truth_rows": 3, "compared": 0, "pending": 3, "missing": 0, "not_comparable": 0},
        )
        self.assertIn("n/a on 0 compared rows", text)
        self.assertIn("3 pending review", text)


if __name__ == "__main__":
    unittest.main()
