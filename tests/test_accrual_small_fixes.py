"""Small accrual fixes: adjustment source_id default, opening draft round trip, ask basis refusal, hint text."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import json
import re
import tempfile
import unittest

from tests.test_accrual_schedules import AccrualFixture, AR, INCOME
from bookkeeping.accrual.items import apply_adjustment, apply_item, preview_adjustment, preview_item
from bookkeeping.cli import main
from bookkeeping.entity import init_entity
from bookkeeping.reports.statements import ask

ROOT = Path(__file__).resolve().parent.parent


def cli(argv):
    out = StringIO()
    with redirect_stdout(out):
        status = main(argv)
    return status, out.getvalue()


class AdjustSourceIdDefaultTests(AccrualFixture):
    def invoice(self, item_id):
        request = {"operation_id": item_id + "-create", "id": item_id, "kind": "receivable", "account": AR, "currency": "USD",
                   "original_amount": "100.00", "as_of": "2026-03-01", "due_date": "2026-03-01", "counterparty": "Orbit",
                   "origin": {"mode": "new", "source_id": item_id + "-src", "counter_account": INCOME},
                   "evidence_ids": [self.evidence(item_id)]}
        apply_item(self.entity, preview_item(self.entity, request))

    def test_credit_and_writeoff_default_source_id_to_operation_id(self):
        for action in ("credit", "writeoff"):
            item_id = f"inv-{action}"
            self.invoice(item_id)
            plan = preview_adjustment(self.entity, {"operation_id": f"adj-{action}", "item_id": item_id, "action": action,
                "amount": "10.00", "date": "2026-03-05", "counter_account": INCOME, "reason": "Synthetic",
                "evidence_ids": [self.evidence("adj-" + action)]})
            self.assertEqual(plan["request"]["source_id"], f"adj-{action}")
            apply_adjustment(self.entity, plan)


class OpeningDraftRoundTripTests(unittest.TestCase):
    def test_preview_accepts_draft_wrapper_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "acc"
            init_entity(root, name="Synthetic", basis="accrual", cutover_date="2026-01-01")
            wrapper = {"status": "draft", "agent_must_add": ["x"], "next_steps": ["y"],
                       "request": {"operation_id": "op-draft", "cutover": "2026-01-01",
                                   "lines": [{"account": "Assets:Bank:Checking", "amount": "10.00"},
                                             {"account": "Equity:Opening-Balances", "amount": "-10.00"}]}}
            path = root / "draft.json"
            path.write_text(json.dumps(wrapper))
            status, out = cli(["accrual", "opening", "preview", "--entity", str(root), "--input", str(path)])
            self.assertEqual(status, 0, out)
            self.assertEqual(json.loads(out)["status"], "ready")


class AskBasisRefusalTests(unittest.TestCase):
    def test_cash_wording_is_removed_grammatically(self):
        from bookkeeping.reports.statements import _without_cash_wording
        for question, expected in [("What is my profit on a cash basis?", "What is my profit?"),
                                   ("What is my cash basis profit this year?", "What is my profit this year?"),
                                   ("Show cash profit for 2026", "Show profit for 2026"),
                                   ("How much did we make using the cash method this year?", "How much did we make this year?")]:
            self.assertEqual(_without_cash_wording(question), expected)

    def test_cash_books_refuse_an_accrual_question_with_a_grammatical_suggestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "cash"
            init_entity(root, name="Synthetic", basis="cash")
            for question, expected in [("What is my profit on an accrual basis?", "What is my profit?"),
                                       ("What is my accrual basis profit this year?", "What is my profit this year?"),
                                       ("Show accrual profit for 2026", "Show profit for 2026"),
                                       ("How much did we make using the accrual method this year?", "How much did we make this year?")]:
                text = ask(root, question).answer_text
                self.assertIn("Cash books keep no invoices, bills, prepaid or deferred records", text)
                suggestion = re.search(r'books ask --entity \S+ "([^"]*)"', text).group(1)
                self.assertEqual(suggestion, expected)
                self.assertNotIn("on a ?", text)
                self.assertNotIn("  ", suggestion)


class HintTextTests(unittest.TestCase):
    def test_no_hint_names_a_nonexistent_evidence_command(self):
        for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "docs").rglob("*.md")):
            self.assertNotRegex(path.read_text(), r"books accrual evidence(?!-link)", str(path))


if __name__ == "__main__":
    unittest.main()
