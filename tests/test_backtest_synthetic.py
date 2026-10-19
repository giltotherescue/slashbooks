"""Headless end-to-end accuracy report on a seeded synthetic company.

The generator knows the right answers, so the report is checked against counts
that the engine did not produce.
"""

from __future__ import annotations

import contextlib
import filecmp
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.cli import main  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "synthetic_generate", Path(__file__).parent / "fixtures" / "synthetic" / "generate.py"
)
generate = importlib.util.module_from_spec(_spec)
sys.modules["synthetic_generate"] = generate
_spec.loader.exec_module(generate)


def _company(tmp: Path, data: Path) -> Path:
    company = tmp / "company"
    for name in ("staging", "reports", "learned-context", "review-queue", "ingestion"):
        (company / name).mkdir(parents=True)
    shutil.copy(data / "entity.json", company / "entity.json")
    (company / "trust-policy.json").write_text(
        json.dumps({"auto_post_threshold": 3, "queue_all_until_confirmed": True}) + "\n",
        encoding="utf-8",
    )
    return company


class SyntheticBacktestTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.tmp = Path(self._dir)

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _backtest(self, seed: int, rows: int) -> tuple[dict, dict, int]:
        data = self.tmp / f"data-{seed}-{rows}"
        manifest = generate.write_dataset(data, seed, rows)
        company = _company(self.tmp / f"run-{seed}-{rows}", data)
        out = self.tmp / f"report-{seed}-{rows}"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = main([
                "backtest", "run", "--entity", str(company), "--qb-folder", str(data / "qb"),
                "--from", "2026-01-01", "--to", "2026-03-31",
                "--banksync-json", str(data / "banksync.json"),
                "--truth", str(data / "truth.csv"), "--seed", str(seed),
                "--report", str(out), "--partial",
            ])
        report = json.loads((out / "accuracy-report.json").read_text(encoding="utf-8"))
        return manifest, report, rc

    def test_report_matches_generator_manifest(self):
        manifest, report, rc = self._backtest(seed=1, rows=120)
        self.assertEqual(rc, 0)
        agreement, coverage = report["category_agreement"], report["coverage"]
        self.assertEqual(report["n"], manifest["rows"])
        self.assertEqual(report["seed"], 1)
        self.assertEqual(coverage["compared"], manifest["auto_posted"])
        self.assertEqual(coverage["pending"], manifest["pending"])
        self.assertEqual(coverage["missing"], 0)
        self.assertEqual(agreement["agree"], manifest["agree"])
        self.assertEqual(agreement["disagree"], manifest["disagree"])
        self.assertEqual(report["matched_count"], manifest["auto_posted"])
        self.assertEqual(report["transaction_matching"]["unmatched_qb"], manifest["pending"])

    def test_report_names_the_wrong_rule_in_the_confusion_table(self):
        _manifest, report, _rc = self._backtest(seed=2, rows=90)
        wrong = [c for c in report["category_agreement"]["confusion"] if not c["agree"]]
        self.assertEqual(
            [(c["truth"], c["ours"]) for c in wrong],
            [(generate.EXPENSE_VENDORS[0][1], generate.WRONG_RULE_ACCOUNT)],
        )
        lines = {l["account"]: l for l in report["pnl_line_variance"]["lines"]}
        self.assertTrue(lines["Expenses:Software"]["material"])

    def test_same_seed_is_reproducible_and_other_seed_differs(self):
        first = self.tmp / "a"
        second = self.tmp / "b"
        other = self.tmp / "c"
        generate.write_dataset(first, 5, 60)
        generate.write_dataset(second, 5, 60)
        generate.write_dataset(other, 6, 60)
        match, mismatch, errors = filecmp.cmpfiles(
            first, second, ["banksync.json", "truth.csv", "entity.json", "manifest.json"],
            shallow=False,
        )
        self.assertEqual((mismatch, errors), ([], []))
        qb = filecmp.dircmp(first / "qb", second / "qb")
        self.assertEqual((qb.diff_files, qb.left_only, qb.right_only), ([], [], []))
        self.assertFalse(filecmp.cmp(first / "truth.csv", other / "truth.csv", shallow=False))

    def test_generator_rejects_empty_dataset(self):
        with self.assertRaises(ValueError):
            generate.build(1, 0)

    def test_generator_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = generate.main(["--seed", "3", "--rows", "20", "--out", str(self.tmp / "cli")])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["rows"], 20)
        self.assertTrue((self.tmp / "cli" / "qb" / "general_ledger.csv").is_file())


if __name__ == "__main__":
    unittest.main()
