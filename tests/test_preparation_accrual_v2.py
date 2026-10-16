from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bookkeeping.automation_policy import PreparationDenied
from bookkeeping.preparation_runtime import supported_preparation_command
from bookkeeping.remote_contract import command_spec

_READS = [["accounts"], ["receipt"], ["opening", "draft"]]
_WRITES = [["opening", "preview"], ["opening", "apply"], ["item", "offset-preview"],
           ["item", "offset-apply"], ["schedule", "change-preview"], ["schedule", "change-apply"]]


def _argv(tail):
    return ["accrual", *tail]


class PreparationAccrualV2Tests(unittest.TestCase):
    def classify(self, tail):
        """Parse may reject synthetic missing arguments; classify via the parser path only."""
        from bookkeeping import preparation_runtime as pr
        from unittest.mock import patch
        with patch.object(pr, "parse_command", return_value=(None, [], ("accrual", *tail))):
            return supported_preparation_command(_argv(tail))

    def test_read_only_commands_need_no_preparation(self):
        for tail in _READS:
            with self.subTest(tail=tail):
                spec = self.classify(tail)
                self.assertEqual((spec.effect, spec.action), ("read", None))

    def test_write_commands_use_adjust_action(self):
        for tail in _WRITES:
            with self.subTest(tail=tail):
                spec = self.classify(tail)
                self.assertEqual((spec.effect, spec.action), ("ledger", "adjust"))

    def test_classification_matches_remote_contract(self):
        for tail in _READS + _WRITES:
            with self.subTest(tail=tail):
                self.assertEqual(_mutates(tail), tail[-1].endswith("apply"))

    def test_item_reverse_is_not_supported(self):
        with self.assertRaises(PreparationDenied) as ctx:
            self.classify(["item", "reverse"])
        self.assertEqual(ctx.exception.reason, "command_not_supported")

    def test_unlisted_commands_stay_denied(self):
        for tail in (["period", "apply"], ["opening", "delete"], ["unknown"]):
            with self.subTest(tail=tail), self.assertRaises(PreparationDenied):
                self.classify(tail)


def _mutates(tail):
    from argparse import Namespace
    from unittest.mock import patch
    from bookkeeping import remote_contract as rc
    with patch.object(rc, "parse_command", return_value=(Namespace(apply=False, work_out=None), [], ("accrual", *tail))):
        return rc.command_spec(_argv(tail))["mutates"]


if __name__ == "__main__":
    unittest.main()
