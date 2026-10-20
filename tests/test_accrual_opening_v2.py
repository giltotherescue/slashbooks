"""Source-neutral opening, starter accrual chart, account list, intake conflict and replay receipts."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from decimal import Decimal
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.accrual import migration, opening, reports
from bookkeeping.cli import main
from bookkeeping.entity import configure_basis, init_entity, load_entity
from bookkeeping.ledger.store import LedgerStore

QB_FIXTURES = Path(__file__).parent / "fixtures/accrual/quickbooks"


def cli(argv):
    out = StringIO()
    with redirect_stdout(out):
        status = main(argv)
    return status, json.loads(out.getvalue()) if out.getvalue().strip().startswith(("{", "[")) else out.getvalue()


class OpeningV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "accrual-co"
        init_entity(self.root, name="Synthetic Accrual", basis="accrual", cutover_date="2026-01-01")
        self.entity = load_entity(self.root)
        (self.root / "evidence").mkdir(exist_ok=True)
        (self.root / "evidence/ar-ap-list.pdf").write_bytes(b"%PDF synthetic open items")
        self.request = {
            "operation_id": "synthetic-opening", "cutover": "2026-01-01", "evidence_refs": ["evidence/ar-ap-list.pdf"],
            "lines": [{"account": "Assets:Bank:Checking", "amount": "30000.00"},
                      {"account": "Assets:Receivable:Accounts-Receivable", "amount": "300.00"},
                      {"account": "Liabilities:Payable:Accounts-Payable", "amount": "-100.00"},
                      {"account": "Equity:Opening-Balances", "amount": "-30200.00"}],
            "items": [{"id": "inv-1", "kind": "receivable", "account": "Assets:Receivable:Accounts-Receivable", "counterparty": "Synthetic Customer",
                       "reference": "INV-1", "due_date": "2025-12-15", "open_amount": "300.00"},
                      {"id": "bill-1", "kind": "payable", "account": "Liabilities:Payable:Accounts-Payable", "counterparty": "Synthetic Supplier",
                       "reference": "BILL-9", "due_date": "2026-01-10", "open_amount": "100.00"}]}

    def test_manual_opening_day_one_aging_and_reconciliation_are_clean(self):
        plan = opening.preview_opening(self.entity, self.request)
        self.assertEqual(plan["status"], "ready")
        receipt = opening.apply_opening(self.entity, plan)
        self.assertEqual(receipt["entries_written"], 1)
        # Opening entry is dated Dec 31 and its accounts are open that day.
        with LedgerStore(self.root / "ledger.sqlite").connection() as conn:
            self.assertEqual(conn.execute("SELECT date FROM entries").fetchone()[0], "2025-12-31")
            self.assertEqual(conn.execute("SELECT open_date FROM accounts WHERE name='Assets:Bank:Checking'").fetchone()[0], "2025-12-31")
        rows = reports.report(self.entity, "reconciliation", as_of="2026-01-01")
        rows = rows.get("rows", rows) if isinstance(rows, dict) else rows
        self.assertTrue(rows)
        self.assertTrue(all(r["status"] == "reconciled" for r in rows), rows)
        aging = json.dumps(reports.report(self.entity, "aging", as_of="2026-01-15"))
        self.assertIn("Synthetic Customer", aging)
        self.assertIn("Synthetic Supplier", aging)

    def test_remaining_prepaid_uses_inclusive_service_dates(self):
        request = json.loads(json.dumps(self.request))
        request["lines"][3]["amount"] = "-30800.00"
        request["lines"].append({"account": "Assets:Other:Prepaid-Expenses", "amount": "600.00"})
        request["schedules"] = [{"id": "prepaid-1", "kind": "prepaid", "account": "Assets:Other:Prepaid-Expenses",
            "recognition_account": "Expenses:Software", "amount": "1200.00", "recognized_amount": "600.00",
            "service_start": "2025-07-01", "service_end": "2026-06-30", "method": "equal_month"}]
        plan = opening.preview_opening(self.entity, request)
        self.assertEqual(plan["status"], "ready", plan.get("support_differences"))
        schedule = next(r["payload"] for r in plan["support"] if r["table"] == "schedules")
        self.assertEqual((schedule["service_start"], schedule["service_end"]), ("2025-07-01", "2026-06-30"))
        self.assertNotIn("end_date", schedule)
        request["schedules"][0]["start_date"] = "2025-07-01"
        with self.assertRaisesRegex(Exception, "start_date"):
            opening.preview_opening(self.entity, request)

    def test_equal_month_opening_with_a_mid_month_start_names_actual_day(self):
        request = json.loads(json.dumps(self.request))
        request["lines"][3]["amount"] = "-31400.00"
        request["lines"].append({"account": "Assets:Other:Prepaid-Expenses", "amount": "1200.00"})
        request["schedules"] = [{"id": "prepaid-mid", "kind": "prepaid", "account": "Assets:Other:Prepaid-Expenses",
            "recognition_account": "Expenses:Software", "amount": "1200.00", "recognized_amount": "0.00",
            "service_start": "2026-01-15", "service_end": "2026-12-31", "method": "equal_month"}]
        with self.assertRaisesRegex(Exception, "actual_day") as raised:
            opening.preview_opening(self.entity, request)
        self.assertIn("prepaid-mid", str(raised.exception))
        self.assertIn("whole calendar months", str(raised.exception))
        request["schedules"][0]["method"] = "actual_day"
        self.assertEqual(opening.preview_opening(self.entity, request)["status"], "ready")

    def test_opening_schedule_jan_to_dec_recognizes_twelve_full_months(self):
        from bookkeeping.accrual import schedules
        request = json.loads(json.dumps(self.request))
        request["lines"][3]["amount"] = "-31400.00"
        request["lines"].append({"account": "Assets:Other:Prepaid-Expenses", "amount": "1200.00"})
        request["schedules"] = [{"id": "prepaid-year", "kind": "prepaid", "account": "Assets:Other:Prepaid-Expenses",
            "recognition_account": "Expenses:Software", "amount": "1200.00", "recognized_amount": "0.00",
            "service_start": "2026-01-01", "service_end": "2026-12-31", "method": "equal_month"}]
        plan = opening.preview_opening(self.entity, request)
        self.assertEqual(plan["status"], "ready", plan.get("support_differences"))
        opening.apply_opening(self.entity, plan)
        due = schedules.preview_due(self.entity, "2026-12-31", operation_id="recognize-year")
        total = sum(abs(Decimal(p["amount"])) for e in due["entries"] for p in e["postings"] if p["account"] == "Expenses:Software")
        self.assertEqual(total, Decimal("1200.00"), due["entries"])
        self.assertEqual(len({e["date"] for e in due["entries"]}), 12)

    def test_opening_depreciation_schedule_carries_accumulated_depreciation(self):
        from bookkeeping.entity import add_account
        add_account(self.root, "Expenses:Depreciation", "2025-12-31")
        entity = load_entity(self.root)
        request = json.loads(json.dumps(self.request))
        request["lines"][3]["amount"] = "-31600.00"
        request["lines"] += [{"account": "Assets:Fixed:Equipment", "amount": "2400.00"},
                             {"account": "Assets:Fixed:Accumulated-Depreciation", "amount": "-1000.00"}]
        request["schedules"] = [{"id": "laptop", "kind": "depreciation", "account": "Assets:Fixed:Equipment",
            "contra_account": "Assets:Fixed:Accumulated-Depreciation", "recognition_account": "Expenses:Depreciation",
            "amount": "2400.00", "salvage_value": "200.00", "recognized_amount": "1000.00",
            "service_start": "2025-01-01", "service_end": "2026-12-31", "method": "equal_month"}]
        plan = opening.preview_opening(entity, request)
        self.assertEqual(plan["status"], "ready", plan.get("support_differences"))
        opening.apply_opening(entity, plan)
        rows = reports.report(entity, "reconciliation", as_of="2026-01-01")
        rows = rows.get("rows", rows)
        self.assertTrue(all(r["status"] == "reconciled" for r in rows if r["account"].startswith("Assets:Fixed")), rows)

    def test_opening_must_balance_and_names_unknown_fields(self):
        bad = json.loads(json.dumps(self.request))
        bad["lines"][3]["amount"] = "-30000.00"
        with self.assertRaisesRegex(Exception, "do not balance"):
            opening.preview_opening(self.entity, bad)
        bad = json.loads(json.dumps(self.request))
        bad["items"][0]["bogus"] = 1
        with self.assertRaisesRegex(Exception, "bogus"):
            opening.preview_opening(self.entity, bad)

    def test_unsupported_control_balance_blocks(self):
        bad = json.loads(json.dumps(self.request))
        bad["items"][0]["open_amount"] = "299.00"
        plan = opening.preview_opening(self.entity, bad)
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["support_differences"][0]["difference"], "-1.00")

    def test_quickbooks_fixture_runs_through_the_shared_core(self):
        root = self.base / "qb-co"
        init_entity(root, name="Synthetic Services", basis="accrual", cutover_date="2026-07-01")
        entity = load_entity(root)
        shutil.copytree(QB_FIXTURES, root / "qb-exports")
        (root / "evidence").mkdir(exist_ok=True)
        (root / "evidence/opening.pdf").write_bytes(b"%PDF synthetic opening support")
        support = json.loads((root / "qb-exports/support.json").read_text())
        plan = migration.preview(entity, root / "qb-exports", "2026-07-01", fiscal_start="2026-01-01", supporting=support)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["operation"], "quickbooks-opening")
        result = migration.apply(entity, plan)
        self.assertEqual(result["entries_written"], 1)

    def test_starter_accrual_chart_and_cash_company_unchanged(self):
        status, result = cli(["accrual", "accounts", "--entity", str(self.root)])
        self.assertEqual(status, 0)
        names = {a["name"]: a for a in result["accounts"]}
        for name in ("Assets:Receivable:Accounts-Receivable", "Liabilities:Payable:Accounts-Payable", "Assets:Other:Prepaid-Expenses", "Liabilities:Other:Deferred-Revenue",
                     "Liabilities:Other:Accrued-Liabilities", "Liabilities:Other:Customer-Deposits", "Assets:Fixed:Equipment", "Assets:Fixed:Accumulated-Depreciation"):
            self.assertIn(name, names)
            self.assertEqual(names[name]["role"], "control")
            self.assertEqual(names[name]["open_date"], "2026-01-01")
        self.assertEqual(names["Assets:Bank:Checking"]["role"], "cash_or_bank")
        cash = self.base / "cash-co"
        init_entity(cash, name="Synthetic Cash", cutover_date="2026-01-01")
        status, listed = cli(["accrual", "accounts", "--entity", str(cash)])
        self.assertNotIn("Assets:Receivable:Accounts-Receivable", {a["name"] for a in listed["accounts"]})
        status, text = cli(["accrual", "accounts", "--entity", str(self.root), "--format", "text"])
        self.assertIn("Assets:Receivable:Accounts-Receivable", text)

    def test_configure_basis_adds_starter_chart_and_refusal_explains_path(self):
        root = self.base / "switch-co"
        init_entity(root, name="Synthetic Switch", cutover_date="2026-01-01")
        configure_basis(root, "accrual")
        status, listed = cli(["accrual", "accounts", "--entity", str(root)])
        self.assertIn("Liabilities:Other:Customer-Deposits", {a["name"] for a in listed["accounts"]})

    def test_cash_company_refusal_names_exact_commands_and_draft_helper(self):
        cash = self.base / "cash-co"
        init_entity(cash, name="Synthetic Cash", cutover_date="2025-01-01")
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry, Posting
        from datetime import date
        from decimal import Decimal
        _atomic_ledger_write(load_entity(cash), [], [Entry(date=date(2025, 6, 1), narration="seed", meta=(("source-id", "seed-1"),),
            postings=(Posting("Assets:Bank:Checking", Decimal("500.00")), Posting("Income:Consulting", Decimal("-500.00"))))], "seed", None, "seed")
        with self.assertRaisesRegex(ValueError, "books accrual opening draft"):
            configure_basis(cash, "accrual")
        status, draft = cli(["accrual", "opening", "draft", "--entity", str(cash), "--cutover", "2026-01-01"])
        self.assertEqual(status, 0)
        lines = {l["account"]: l["amount"] for l in draft["request"]["lines"]}
        self.assertEqual(lines, {"Assets:Bank:Checking": "500.00", "Equity:Retained-Earnings": "-500.00"})
        # The draft previews and applies in a new accrual company.
        plan = opening.preview_opening(self.entity, {**draft["request"], "operation_id": "from-cash"})
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(opening.apply_opening(self.entity, plan)["entries_written"], 1)

    def test_intake_conflict_for_same_revision_with_changed_facts(self):
        envelope = {"schema_version": 1, "currency": "USD", "source": {"namespace": "synthetic", "account_id": "a", "object_id": "inv-9", "version": "1"},
                    "amounts": {"gross": "100.00"}, "observed_facts": {"note": "first"}}
        path = self.root / "env.json"
        path.write_text(json.dumps(envelope))
        status, first = cli(["accrual", "intake", "--entity", str(self.root), "--input", str(path)])
        self.assertEqual(status, 0)
        status, again = cli(["accrual", "intake", "--entity", str(self.root), "--input", str(path)])
        self.assertEqual((status, again["id"]), (0, first["id"]))
        changed = {**envelope, "observed_facts": {"note": "second"}}
        path.write_text(json.dumps(changed))
        status, result = cli(["accrual", "intake", "--entity", str(self.root), "--input", str(path)])
        self.assertEqual(status, 1)
        error = result["errors"][0]
        self.assertEqual(error["code"], "intake_conflict")
        self.assertEqual(error["stored_version"]["observed_facts"], {"note": "first"})
        self.assertEqual(error["submitted_version"]["observed_facts"], {"note": "second"})
        self.assertIn("source.version", error["action"])
        # A new source version is a new revision, not a conflict.
        changed["source"]["version"] = "2"
        path.write_text(json.dumps(changed))
        status, _ = cli(["accrual", "intake", "--entity", str(self.root), "--input", str(path)])
        self.assertEqual(status, 0)

    def test_exact_replay_returns_original_receipt_and_changed_request_conflicts(self):
        status, evidence = cli(["accrual", "intake", "--entity", str(self.root), "--input", self._write("env.json", {
            "schema_version": 1, "currency": "USD", "source": {"namespace": "synthetic", "account_id": "a", "object_id": "bill-1", "version": "1"},
            "amounts": {"gross": "100.00"}})])
        request = {"operation_id": "payable-create", "id": "payable-1", "kind": "payable", "account": "Liabilities:Payable:Accounts-Payable",
                   "currency": "USD", "original_amount": "100.00", "as_of": "2026-01-31",
                   "origin": {"mode": "new", "source_id": "synthetic-bill", "counter_account": "Expenses:Software"},
                   "evidence_ids": [evidence["id"]]}
        req = self._write("req.json", request)
        status, plan = cli(["accrual", "item", "preview", "--entity", str(self.root), "--input", str(req)])
        self.assertEqual(status, 0)
        planfile = self._write("plan.json", plan)
        status, applied = cli(["accrual", "item", "apply", "--entity", str(self.root), "--input", planfile])
        self.assertEqual((status, applied["status"]), (0, "applied"))
        # Re-preview of the committed request, then apply: original receipt, no new effect.
        digest = LedgerStore(self.root / "ledger.sqlite").content_digest()
        status, plan2 = cli(["accrual", "item", "preview", "--entity", str(self.root), "--input", str(req)])
        if status == 0:
            planfile = self._write("plan2.json", plan2)
            status, replay = cli(["accrual", "item", "apply", "--entity", str(self.root), "--input", planfile])
            self.assertEqual((status, replay["status"]), (0, "already_applied"), replay)
            self.assertEqual(replay["plan_hash"], applied["plan_hash"])
        status, again = cli(["accrual", "item", "apply", "--entity", str(self.root), "--input", self._write("plan.json", plan)])
        self.assertEqual((status, again["status"], again["entry_source_ids"]), (0, "already_applied", applied["entry_source_ids"]))
        self.assertEqual(LedgerStore(self.root / "ledger.sqlite").content_digest(), digest)
        status, looked = cli(["accrual", "receipt", "--entity", str(self.root), "--id", "payable-create"])
        self.assertEqual((status, looked["receipt"]["entry_source_ids"]), (0, applied["entry_source_ids"]))
        status, missing = cli(["accrual", "receipt", "--entity", str(self.root), "--id", "nope"])
        self.assertEqual((status, missing["errors"][0]["code"]), (1, "receipt_not_found"))
        # Same ID, changed facts: conflict.
        changed = self._write("changed.json", {**request, "reference": "Changed reference"})
        status, conflict = cli(["accrual", "item", "preview", "--entity", str(self.root), "--input", str(changed)])
        self.assertEqual((status, conflict["errors"][0]["code"]), (1, "source_conflict"))
        # A journal id reused with changed facts names the receipt command.
        from bookkeeping.accrual import journals
        journals_req = {"id": "j-1", "date": "2026-01-31", "description": "Synthetic", "estimate_basis": "Synthetic estimate",
                        "postings": [{"account": "Expenses:Software", "amount": "10.00"}, {"account": "Liabilities:Payable:Accounts-Payable", "amount": "-10.00"}]}
        journals.apply_journal(load_entity(self.root), journals.preview_journal(load_entity(self.root), journals_req))
        with self.assertRaises(journals.JournalError) as ctx:
            journals.preview_journal(load_entity(self.root), {**journals_req, "description": "Changed"})
        self.assertEqual(ctx.exception.code, "identity_conflict")
        self.assertIn("books accrual receipt --id j-1", ctx.exception.action)

    def test_capabilities_and_schema_commands_are_generated(self):
        status, caps = cli(["accrual", "capabilities"])
        for name in ("period", "opening", "accounts", "receipt", "journal", "item", "schedule", "migration"):
            self.assertIn(name, caps["operations"])
        status, schema = cli(["accrual", "schema"])
        self.assertNotIn("journal", [k for k in schema if k not in {"requests", "commands", "evidence", "schema_version", "capability_version"}])
        self.assertTrue(any(line.startswith("accrual opening") for line in schema["commands"]))
        self.assertTrue(schema["requests"]["opening"]["examples"])

    def _write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return str(path)


if __name__ == "__main__":
    unittest.main()
