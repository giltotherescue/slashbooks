"""Optional supporting detail, additive coverage, ask basis/period, card sign hint, period-lock text."""
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
import io
import json
import unittest

from bookkeeping.accrual import cli as accrual_cli, items, journals, reports
from bookkeeping.entity import add_account, load_entity, record_source_coverage
from bookkeeping.readiness import assess_review_readiness, local_dependency_versions
from bookkeeping.reconcile import reconcile
from bookkeeping.reports import statements
from tests.test_accrual_schedules import AccrualFixture, AR, BANK, EXPENSE, INCOME

PAYROLL = "Liabilities:Accrued:Payroll"
CARD = "Liabilities:CreditCard:Synthetic"


class ReconV2Tests(AccrualFixture):
    def setUp(self):
        super().setUp()
        add_account(self.root, PAYROLL, open_date="2000-01-01")
        add_account(self.root, CARD, open_date="2000-01-01")

    def journal(self, identity, when, postings):
        request = {"id": identity, "date": when, "description": "Synthetic " + identity, "evidence_ids": [self.evidence(identity)],
                   "postings": [{"account": a, "amount": n, "currency": "USD"} for a, n in postings]}
        return journals.apply_journal(self.entity, journals.preview_journal(self.entity, request))

    def item(self, **changes):
        request = {"operation_id": "invoice-create", "id": "invoice-1", "kind": "receivable", "account": AR,
                   "currency": "USD", "original_amount": "1000.00", "as_of": "2026-01-01",
                   "origin": {"mode": "new", "source_id": "provider-invoice-1", "counter_account": INCOME},
                   "evidence_ids": [self.eid]}
        request.update(changes)
        return request

    def configure_bank(self, coverage_to="2026-01-31"):
        path = self.root / "entity.json"
        config = json.loads(path.read_text())
        config["declared_sources"] = [{"name": "Synthetic checking", "account": BANK,
                                       "coverage_from": "2026-01-01", "coverage_to": coverage_to}]
        path.write_text(json.dumps(config))
        self.entity = load_entity(self.root)
        reconcile(self.root, BANK, Decimal("0.00"), date(2026, 1, 31),
                  source_opening_balance=Decimal("0.00"), source_transaction_total=Decimal("0.00"),
                  source_snapshot_at="2026-02-01T12:00:00Z", source_through="2026-01-31",
                  source_evidence_refs=["synthetic:statement"], source_evidence_hashes={"synthetic:statement": "a" * 64})

    def assess(self):
        return assess_review_readiness(self.root, "2026-01", local_dependency_versions(self.root), as_of=date(2026, 2, 2)).to_document()

    def test_plain_accrued_payroll_journal_passes_close_without_support_snapshot(self):
        self.configure_bank()
        self.journal("payroll-accrual", "2026-01-31", [(EXPENSE, "4000.00"), (PAYROLL, "-4000.00")])
        rows = {r["account"]: r for r in reports.report(self.entity, "reconciliation", as_of="2026-01-31")["rows"]}
        self.assertEqual(rows[PAYROLL]["status"], "ledger_only")
        self.assertFalse(rows[PAYROLL]["support_required"])
        self.assertEqual(rows[PAYROLL]["note"], "Ledger balance, no supporting detail required.")
        self.assertEqual(rows[PAYROLL]["ledger_balance"], "-4000.00")
        assessment = self.assess()
        self.assertEqual(assessment["state"], "ready_for_review", assessment["blockers"])

    def test_item_backed_account_with_plain_journal_blocks_with_precise_message(self):
        self.configure_bank()
        request = self.item(origin={"mode": "new", "source_id": "provider-invoice-1", "counter_account": INCOME})
        items.apply_item(self.entity, items.preview_item(self.entity, request))
        self.journal("plain-into-receivable", "2026-01-20", [(AR, "25.00"), (INCOME, "-25.00")])
        assessment = self.assess()
        blocker = next(b for b in assessment["blockers"] if b["code"] == "control_balance_unreconciled")
        self.assertIn(AR, blocker["message"])
        self.assertIn("25.00", blocker["message"])
        self.assertIn("plain-into-receivable", blocker["message"])
        self.assertIn("books accrual support preview", blocker["action"])
        self.assertIn('"mode": "residual"', blocker["action"])
        self.assertIn('"balance": "25.00"', blocker["action"])
        self.assertIn("invoice-1", blocker["action"])
        self.assertEqual(assessment["state"], "blocked")

    def test_source_coverage_is_additive_and_warns_about_a_gap(self):
        record_source_coverage(self.root, "synthetic-bank", "2026-01-01", "2026-01-31", BANK)
        record_source_coverage(self.root, "synthetic-bank", "2026-02-01", "2026-02-28")
        saved = load_entity(self.root).entity_config["source_coverage"]["synthetic-bank"]
        self.assertEqual((saved["coverage_from"], saved["coverage_to"]), ("2026-01-01", "2026-02-28"))
        self.assertEqual(saved["account"], BANK)
        # An earlier month recorded later does not erase the later range.
        report = record_source_coverage(self.root, "synthetic-bank", "2026-01-10", "2026-01-20")
        self.assertTrue(report["already_covered"])
        saved = load_entity(self.root).entity_config["source_coverage"]["synthetic-bank"]
        self.assertEqual((saved["coverage_from"], saved["coverage_to"]), ("2026-01-01", "2026-02-28"))
        # A disjoint range keeps both; January stays covered for readiness.
        record_source_coverage(self.root, "synthetic-bank", "2026-04-01", "2026-04-30")
        saved = load_entity(self.root).entity_config["source_coverage"]["synthetic-bank"]
        self.assertEqual(saved["ranges"], [{"from": "2026-01-01", "to": "2026-02-28"}, {"from": "2026-04-01", "to": "2026-04-30"}])
        self.configure_bank_from_coverage()
        january = next(r for r in self.assess()["sources"] if r["source"] == "synthetic-bank")
        self.assertEqual(january["coverage_status"], "complete")

    def configure_bank_from_coverage(self):
        self.entity = load_entity(self.root)

    def test_source_coverage_cli_prints_resulting_ranges_and_gap_warning(self):
        from bookkeeping import cli
        out = io.StringIO()
        with redirect_stdout(out):
            for first, last in (("2026-01-01", "2026-01-31"), ("2026-03-01", "2026-03-31")):
                cli.main(["entity", "source-coverage", str(self.root), "--source", "bank", "--from", first, "--to", last, "--account", BANK])
        text = out.getvalue()
        self.assertIn("2026-01-01 through 2026-01-31, 2026-03-01 through 2026-03-31", text)
        self.assertIn("gap", text)

    def test_ask_answers_a_cash_question_from_the_cash_view_and_states_basis_and_period(self):
        items.apply_item(self.entity, items.preview_item(self.entity, self.item(
            operation_id="sale-create", id="sale", original_amount="500.00", as_of="2026-01-10",
            origin={"mode": "new", "source_id": "sale-src", "counter_account": INCOME})))
        today = date(2026, 5, 31)
        cash = statements.ask(self.root, "What is our cash-basis profit from January through May?", today=today)
        self.assertEqual((cash.data["from_date"], cash.data["to_date"]), ("2026-01-01", "2026-05-31"))
        self.assertTrue(cash.answer_text.startswith("Answer covers: cash basis (derived from accrual books), from 2026-01-01 to 2026-05-31."))
        # The invoice is recorded but not collected, so the cash profit is 0.00 while the accrual profit is 500.00.
        self.assertEqual(cash.data["net_income"], "0.00")
        self.assertNotIn("unavailable", cash.data)
        answer = statements.ask(self.root, "What is our net income from January through May?", today=today)
        self.assertEqual((answer.data["from_date"], answer.data["to_date"]), ("2026-01-01", "2026-05-31"))
        self.assertEqual(answer.data["net_income"], "500.00")
        self.assertTrue(answer.answer_text.startswith("Answer covers: accrual basis, from 2026-01-01 to 2026-05-31."))
        unnamed = statements.ask(self.root, "What is our net income?", today=today)
        self.assertIn("No period was named", unnamed.answer_text)

    def test_card_reconcile_sign_mistake_gets_corrected_command(self):
        self.post("card-purchase", "2026-01-10", [(EXPENSE, "3600.00"), (CARD, "-3600.00")])
        result = reconcile(self.root, CARD, Decimal("3600.00"), date(2026, 1, 31))
        self.assertEqual(result.discrepancy, Decimal("7200.00"))
        text = "\n".join(result.causes)
        self.assertIn("sign", text)
        self.assertNotIn("cause is unclear", text)
        self.assertIn(f"books reconcile --entity {self.root} --account {CARD} --source-balance -3600.00 --as-of 2026-01-31", text)
        again = reconcile(self.root, CARD, Decimal("-3600.00"), date(2026, 1, 31))
        self.assertEqual(again.status, "clean")

    def test_closed_period_action_names_exact_reopen_steps_and_agent_limit(self):
        config = json.loads((self.root / "entity.json").read_text())
        config["closed_periods"] = ["2026-01"]
        (self.root / "entity.json").write_text(json.dumps(config))
        request = {"id": "late", "date": "2026-01-15", "description": "late", "evidence_ids": [self.evidence("late")],
                   "postings": [{"account": EXPENSE, "amount": "1.00", "currency": "USD"}, {"account": PAYROLL, "amount": "-1.00", "currency": "USD"}]}
        path = self.root / "late.json"
        path.write_text(json.dumps(request))
        out = io.StringIO()
        from bookkeeping import cli
        with redirect_stdout(out):
            code = cli.main(["accrual", "journal", "preview", "--entity", str(self.root), "--input", str(path)])
        self.assertEqual(code, 1)
        error = json.loads(out.getvalue())["errors"][0]
        self.assertEqual(error["code"], "closed_period")
        self.assertIn("closed_periods", error["action"])
        self.assertIn('"action": "reopen"', error["action"])
        self.assertIn(f"books accrual period preview --entity {self.root}", error["action"])
        self.assertIn("An agent cannot reopen a period", error["action"])
        self.assertNotIn("existing reopen authority", error["action"])

    def test_readiness_blockers_name_account_period_and_command(self):
        self.configure_bank(coverage_to="2026-01-15")
        blocker = next(b for b in self.assess()["blockers"] if b["code"] == "source_coverage_incomplete")
        self.assertIn("2026-01-15", blocker["message"])
        self.assertIn("2026-01", blocker["message"])
        self.assertIn(f'books entity source-coverage {self.root.resolve()} --source "Synthetic checking" --from 2026-01-01 --to 2026-01-31', blocker["action"])


if __name__ == "__main__":
    unittest.main()
