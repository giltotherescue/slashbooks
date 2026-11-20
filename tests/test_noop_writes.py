"""A command that changes nothing leaves ``ledger.sqlite`` byte for byte the same, without a change set capture.

A host app that publishes a new revision of the books when the ledger file changes must not publish one for a command
that changed nothing, such as a replay. All data is synthetic. Each command runs twice: the second run is a replay or
the same failure, and it must leave the bytes of the first run. The commands run on the plain command line and with
``--work-out`` (the work capture shares the run of the change set capture).
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.test_changeset import IMPORT, mutating_commands, run_cli  # noqa: E402

MODES = {"plain": [], "work-out": ["--work-out", "{scratch}/work.json"]}
#: Commands that add a record on each run by design, so a second run is not a no-op.
ALWAYS_WRITE = {("ledger", "mark-migrated"): "records a new store-migrated boundary on each run"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_quietly(argv: list[str]) -> object:
    """Run one command with no network and an empty stdin. A command that raises still counts as a run."""
    with mock.patch.object(socket.socket, "connect", side_effect=OSError("no network in this test")), \
            mock.patch("sys.stdin", io.StringIO("")):
        try:
            return run_cli(argv)[0]
        except SystemExit as exc:
            return f"exit {exc.code}"
        except Exception as exc:  # noqa: BLE001
            return f"raised {type(exc).__name__}"


class TwiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()

    def twice(self, ledger: Path, argv: list[str], mode: str, expect: object = None) -> None:
        """Run ``argv`` two times. The second run must leave the bytes of the first run."""
        flags = [part.format(scratch=self.scratch) for part in MODES[mode]]
        first = run_quietly([*flags, *argv])
        if expect is not None:
            self.assertEqual(first, expect, argv)
        before = digest(ledger)
        second = run_quietly([*flags, *argv])
        self.assertEqual(digest(ledger), before, f"the second run ({second}) changed the ledger file")


class EveryMutatingCommandTests(TwiceTests):
    """Each mutating command of the command contract, with placeholder arguments (most stop with an error)."""

    def test_every_mutating_command_run_twice_leaves_the_same_ledger_bytes(self) -> None:
        base = self.root / "base"
        self.assertEqual(run_cli(["demo", "init", str(base)])[0], 0)
        company, scratch = self.root / "co", self.root / "placeholders"
        commands = mutating_commands(company, scratch)
        self.assertGreater(len(commands), 90)
        for argv in commands:
            path = tuple(part for part in argv if not part.startswith("-") and not part.startswith(str(self.root)))[:2]
            if path in ALWAYS_WRITE:
                continue
            for mode in MODES:
                label = " ".join(part for part in argv if not part.startswith(str(self.root)))
                with self.subTest(command=label, mode=mode):
                    shutil.rmtree(company, ignore_errors=True)
                    shutil.copytree(base, company)
                    shutil.rmtree(scratch, ignore_errors=True)
                    scratch.mkdir()
                    self.twice(company / "ledger.sqlite", argv, mode)


class ReplayTests(TwiceTests):
    """Real replays: the first run changes the books, the second run changes nothing."""

    def company(self, name: str) -> Path:
        entity = self.root / name
        self.assertEqual(run_cli(["entity", "init", str(entity), "--name", "Synthetic Studio"])[0], 0)
        return entity

    def test_account_add_of_an_account_that_exists(self) -> None:
        for mode in MODES:
            for extra in ([], ["--open-date", "2025-06-01"], ["--category"]):
                with self.subTest(mode=mode, extra=extra):
                    entity = self.company(f"add-{mode}-{len(extra)}")
                    self.twice(entity / "ledger.sqlite",
                               ["entity", "account-add", str(entity), "--account", "Expenses:Synthetic-Tools", *extra],
                               mode, expect=0)

    def test_ingest_of_rows_that_the_books_hold(self) -> None:
        for mode in MODES:
            with self.subTest(mode=mode):
                entity = self.company(f"ingest-{mode}")
                path = self.root / "rows.json"
                path.write_text(json.dumps(IMPORT), encoding="utf-8")
                self.twice(entity / "ledger.sqlite",
                           ["ingest", str(path), "--entity", str(entity), "--session-id", "s"], mode, expect=0)

    def test_queue_confirm_of_an_item_that_is_posted(self) -> None:
        for mode in MODES:
            for verb in (["confirm", "--item", "cs-001"], ["confirm-group", "--category", "Expenses:Software"]):
                with self.subTest(mode=mode, verb=verb[0]):
                    entity = self.company(f"confirm-{mode}-{verb[0]}")
                    path = self.root / "rows.json"
                    path.write_text(json.dumps(IMPORT), encoding="utf-8")
                    run_cli(["ingest", str(path), "--entity", str(entity), "--session-id", "s"])
                    run_cli(["queue", "propose", "--entity", str(entity), "--source-id", "cs-001",
                             "--category", "Expenses:Software", "--reasoning", "Tool."])
                    self.twice(entity / "ledger.sqlite", ["queue", verb[0], "--entity", str(entity), *verb[1:]],
                               mode, expect=0)

    def test_accrual_replays(self) -> None:
        from tests.test_accrual_schedules import AR, BANK, INCOME, AccrualFixture
        from bookkeeping.accrual.items import apply_settlement, preview_settlement
        from bookkeeping.entity import add_account, load_entity

        for mode in MODES:
            with self.subTest(mode=mode):
                fixture = AccrualFixture()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                deposits = "Liabilities:CustomerDeposits"
                add_account(fixture.root, deposits, open_date="2000-01-01")
                entity = load_entity(fixture.root)
                apply_settlement(entity, preview_settlement(entity, {
                    "operation_id": "deposit", "date": "2026-02-03", "evidence_ids": [fixture.evidence("deposit")],
                    "source": {"mode": "new", "source_id": "deposit-bank", "bank_account": BANK, "amount": "500.00"},
                    "allocations": [], "excess": {"id": "deposit-credit", "account": deposits, "counterparty": "Orbit"}}))
                operations = (
                    ("schedule", "preview", "apply", fixture.schedule()),
                    ("item", "preview", "apply", {
                        "operation_id": "invoice-1-create", "id": "invoice-1", "kind": "receivable", "account": AR,
                        "currency": "USD", "original_amount": "2000.00", "as_of": "2026-03-01",
                        "due_date": "2026-03-01", "counterparty": "Orbit",
                        "origin": {"mode": "new", "source_id": "invoice-1-src", "counter_account": INCOME},
                        "evidence_ids": [fixture.evidence("invoice-1")]}),
                    ("item", "offset-preview", "offset-apply", {
                        "operation_id": "apply-deposit", "date": "2026-03-15", "credit_item_id": "deposit-credit",
                        "target_item_id": "invoice-1", "amount": "500.00", "reason": "Apply deposit"}),
                )
                for group, preview, apply, request in operations:
                    request_path = self.scratch / f"{apply}-request.json"
                    request_path.write_text(json.dumps(request), encoding="utf-8")
                    code, plan, err = run_cli(["accrual", group, preview, "--entity", str(fixture.root),
                                               "--input", str(request_path)])
                    self.assertEqual(code, 0, err)
                    plan_path = self.scratch / f"{apply}-plan.json"
                    plan_path.write_text(plan, encoding="utf-8")
                    self.twice(fixture.root / "ledger.sqlite",
                               ["accrual", group, apply, "--entity", str(fixture.root), "--input", str(plan_path)],
                               mode, expect=0)


if __name__ == "__main__":
    unittest.main()
