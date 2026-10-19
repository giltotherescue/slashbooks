"""Golden output tests: existing companies must keep producing byte-identical outputs.

Three synthetic companies are built from scratch under a frozen clock (see
``golden_companies``). Every user-visible output is captured (``golden_capture``)
and compared with ``tests/fixtures/golden/<company>/``. A diff is a behavior change.

Regenerate only with a written reason in the pull request:

    UPDATE_GOLDEN=1 .venv/bin/python -m unittest tests.test_golden_outputs
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import golden_capture  # noqa: E402
import golden_companies  # noqa: E402
from golden_support import REGENERATE_HELP, Capture, Normalizer, compare_or_update  # noqa: E402


class GoldenOutputTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()

    def check(self, company: str, capture: Capture) -> None:
        problems = compare_or_update(company, capture)
        if problems:
            shown = problems[:12]
            more = f"\n... and {len(problems) - len(shown)} more changed outputs" if len(problems) > len(shown) else ""
            self.fail(f"\n{len(problems)} golden output(s) differ for {company}.\n\n{REGENERATE_HELP}\n\n"
                      + "\n\n".join(shown) + more)

    def run_company(self, kind: str, build) -> None:
        root = self.work / kind
        build(root)
        self.check(kind, golden_capture.capture_company(kind, root, self.work))

    def test_cash_company(self) -> None:
        self.run_company("cash", golden_companies.build_cash)

    def test_native_accrual_company(self) -> None:
        self.run_company("accrual", golden_companies.build_accrual)

    def test_quickbooks_migrated_accrual_company(self) -> None:
        self.run_company("quickbooks", golden_companies.build_quickbooks)

    def test_shared_command_contracts(self) -> None:
        capture = Capture(Normalizer(None, self.work))
        golden_capture.capture_shared(capture)
        self.check("shared", capture)


if __name__ == "__main__":
    unittest.main()
