"""QuickBooks API reference tests. Every company and amount is synthetic."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from datetime import date
from decimal import Decimal

from bookkeeping.entity import init_entity, load_entity
from bookkeeping import quickbooks as qb
from bookkeeping import quickbooks_sync as sync
from bookkeeping.remote_contract import command_spec

FIXTURE = Path(__file__).parent / "fixtures" / "quickbooks_api" / "snapshot.json"


class QuickBooksSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entity_path = self.root / "books"
        init_entity(self.entity_path, name="Synthetic Example Books")
        self.entity = load_entity(self.entity_path)
        self.snapshot = json.loads(FIXTURE.read_text())

    def write(self):
        source = self.root / "snapshot.json"
        source.write_text(json.dumps(self.snapshot))
        return source

    def test_import_is_content_addressed_idempotent_and_never_posts(self):
        before = (self.entity_path / "ledger.sqlite").read_bytes()
        first = sync.sync_import(self.entity, self.write())
        second = sync.sync_import(self.entity, self.write())
        self.assertEqual(first, second)
        self.assertEqual(before, (self.entity_path / "ledger.sqlite").read_bytes())
        self.assertFalse(Path(first["reference_path"]).is_absolute())
        self.assertEqual(first["counts"]["accounts"], 3)
        self.assertEqual(first["accounts"][0]["mapped_account"], "Assets:Bank:Checking")
        reference = self.entity_path / first["reference_path"]
        self.assertEqual(qb.parse_trial_balance(reference / "OpeningTrialBalance.qbo.json").entries[0].debit, Decimal("100.00"))
        self.assertEqual(qb.parse_profit_and_loss(reference / "ProfitAndLoss.qbo.json").rows[0].amount, Decimal("20.00"))
        self.assertEqual(qb.parse_general_ledger(reference / "GeneralLedger.qbo.json").transactions[0].amount, Decimal("-20.00"))
        inventory = qb.inventory(reference, from_date=date(2026, 1, 1), to_date=date(2026, 1, 31))
        slots = {slot.report_key: slot for slot in inventory.slots}
        self.assertEqual(slots["trial_balance"].status, "present")
        self.assertEqual(slots["balance_sheet_comparison"].status, "present")
        self.assertTrue(inventory.is_ready())
        self.assertTrue(first["comparison_ready"])
        self.assertEqual(first["coverage"]["TransactionDetail"]["derived_from"], "GeneralLedger")
        self.assertEqual(qb.parse_general_ledger(reference / "GeneralLedger.qbo.json"),
                         qb.parse_general_ledger(reference / "TransactionDetail.qbo.json"))
        from bookkeeping.compare import compare_period
        comparison = compare_period(self.entity, reference, date(2026, 1, 1), date(2026, 1, 31))
        self.assertTrue(comparison.readiness.is_ready())
        self.assertFalse([error for error in comparison.errors if "parse" in error.lower()])

    def test_preview_validates_without_ledger_write_and_apply_idempotent(self):
        before = (self.entity_path / "ledger.sqlite").read_bytes()
        preview = sync.sync_opening(self.entity, self.write(), date(2026, 1, 1))
        self.assertFalse(preview["applied"])
        self.assertEqual(before, (self.entity_path / "ledger.sqlite").read_bytes())
        self.assertEqual(preview["opening"]["proposed_postings"], [
            {"account": "Assets:Bank:Checking", "amount": "100.00", "currency": "USD", "date": "2026-01-01", "debit": "100.00", "credit": "0.00"},
            {"account": "Equity:Opening-Balances", "amount": "-100.00", "currency": "USD", "date": "2026-01-01", "debit": "0.00", "credit": "100.00"}])
        applied = sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)
        repeated = sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)
        self.assertEqual(applied["opening"]["entries_written"], 1)
        self.assertEqual(repeated["opening"]["entries_written"], 0)

    def test_fallback_opening_then_trial_balance_cannot_duplicate_balances(self):
        del self.snapshot["reports"]["OpeningTrialBalance"]
        first = sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)
        self.assertEqual(first["opening"]["entries_written"], 1)
        self.snapshot = json.loads(FIXTURE.read_text())
        second = sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)
        self.assertTrue(second["already_applied"])
        self.assertEqual(second["opening"]["entries_written"], 0)
        self.snapshot["reports"]["OpeningTrialBalance"]["Rows"]["Row"][0]["ColData"][1]["value"] = "200.00"
        self.snapshot["reports"]["OpeningTrialBalance"]["Rows"]["Row"][1]["ColData"][2]["value"] = "200.00"
        for row in self.snapshot["reports"]["OpeningBalanceSheet"]["Rows"]["Row"]:
            row["Rows"]["Row"][0]["ColData"][1]["value"] = "200.00"
        changed = sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)
        self.assertFalse(changed["applied"])
        self.assertFalse(changed["opening"]["success"])
        self.assertEqual(changed["opening"]["entries_written"], 0)

    def test_different_cutover_cannot_add_second_opening(self):
        first = sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)
        self.assertTrue(first["applied"])
        before = (self.entity_path / "ledger.sqlite").read_bytes()
        self.snapshot["from"], self.snapshot["to"] = "2026-02-01", "2026-02-28"
        for name, report in self.snapshot["reports"].items():
            report["Header"]["EndPeriod"] = "2026-01-31" if name.startswith("Opening") else "2026-02-28"
            report["Header"]["StartPeriod"] = "2026-01-01" if name.startswith("Opening") else "2026-02-01"
        self.snapshot["reports"]["GeneralLedger"]["Rows"]["Row"][0]["Rows"]["Row"][0]["ColData"][0]["value"] = "2026-02-15"
        self.snapshot["entities"]["Purchase"][0]["TxnDate"] = "2026-02-15"
        second = sync.sync_opening(self.entity, self.write(), date(2026, 2, 1), apply=True)
        self.assertFalse(second["applied"])
        self.assertFalse(second["opening"]["success"])
        self.assertEqual(before, (self.entity_path / "ledger.sqlite").read_bytes())

    def test_existing_ordinary_ledger_entries_block_overlapping_opening(self):
        from bookkeeping.ledger.model import Entry, Posting
        from bookkeeping.ledger.store import LedgerStore
        store = LedgerStore(self.entity_path / "ledger.sqlite")
        with store.transaction() as conn:
            store.insert_entries([Entry(date=date(2025, 12, 31), narration="Synthetic existing books",
                meta=(("source-id", "synthetic-existing"),), postings=(
                    Posting("Assets:Bank:Checking", Decimal("100.00"), "USD"),
                    Posting("Equity:Opening-Balances", Decimal("-100.00"), "USD")))], conn)
        result = sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)
        self.assertFalse(result["applied"])
        self.assertIn("overlap", result["opening"]["errors"][-1])
        self.assertEqual(len(store.load_entries()), 1)

    def test_duplicate_balanced_trial_balance_rows_are_blocked(self):
        report = self.snapshot["reports"]["OpeningTrialBalance"]
        report["Rows"]["Row"].extend(copy.deepcopy(report["Rows"]["Row"]))
        result = sync.sync_import(self.entity, self.write())
        self.assertEqual(result["coverage"]["OpeningTrialBalance"]["status"], "blocked")
        self.assertIn("Duplicate", result["coverage"]["OpeningTrialBalance"]["reason"])
        preview = sync.sync_opening(self.entity, self.write(), date(2026, 1, 1))
        self.assertEqual(preview["opening"]["proposed_postings"][0]["amount"], "100.00")

    def test_duplicate_financial_report_account_ids_block_normalization(self):
        report = self.snapshot["reports"]["ProfitAndLoss"]
        report["Rows"]["Row"].extend(copy.deepcopy(report["Rows"]["Row"]))
        result = sync.sync_import(self.entity, self.write())
        self.assertEqual(result["coverage"]["ProfitAndLoss"]["status"], "blocked")

    def test_opening_trial_balance_and_balance_sheet_mismatch_blocks_application(self):
        report = self.snapshot["reports"]["OpeningBalanceSheet"]
        report["Rows"]["Row"][0]["Rows"]["Row"][0]["ColData"][1]["value"] = "200.00"
        result = sync.sync_import(self.entity, self.write())
        self.assertTrue(result["reference_complete"])
        self.assertFalse(result["opening_ready"])
        self.assertTrue(any("disagree" in warning for warning in result["warnings"]))
        before = (self.entity_path / "ledger.sqlite").read_bytes()
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)
        self.assertEqual(before, (self.entity_path / "ledger.sqlite").read_bytes())

    def test_missing_and_mismatched_report_is_explicitly_blocked(self):
        del self.snapshot["reports"]["TrialBalance"]
        self.snapshot["reports"]["ProfitAndLoss"]["Header"]["ReportBasis"] = "Accrual"
        result = sync.sync_import(self.entity, self.write())
        self.assertTrue(result["success"])
        self.assertFalse(result["reference_complete"])
        self.assertEqual(result["coverage"]["TrialBalance"]["status"], "missing")
        self.assertEqual(result["coverage"]["ProfitAndLoss"]["status"], "blocked")

    def test_snapshot_validation_rejects_bad_identity_hierarchy_and_money(self):
        original = copy.deepcopy(self.snapshot)
        invalids = []
        value = copy.deepcopy(original); value["realm_id"] = "../company"; invalids.append(value)
        value = copy.deepcopy(original); value["from"] = "2026-02-01"; invalids.append(value)
        value = copy.deepcopy(original); value["accounts"][0]["ParentRef"] = {"value": "1"}; invalids.append(value)
        value = copy.deepcopy(original); value["accounts"][1]["Name"] = "Checking"; invalids.append(value)
        value = copy.deepcopy(original); value["entities"]["Purchase"][0]["TotalAmt"] = "NaN"; invalids.append(value)
        value = copy.deepcopy(original); value["entities"]["Purchase"][0]["Line"][0]["AccountBasedExpenseLineDetail"]["AccountRef"] = "bad"; invalids.append(value)
        value = copy.deepcopy(original); value["customers"] = [{"Id": "1"}, {"Id": "1"}]; invalids.append(value)
        value = copy.deepcopy(original); value["preferences"]["CurrencyPrefs"] = []; invalids.append(value)
        for snapshot in invalids:
            with self.subTest(snapshot=snapshot):
                self.snapshot = snapshot
                with self.assertRaises(ValueError):
                    sync.sync_import(self.entity, self.write())

    def test_incomplete_detail_columns_retains_source_but_blocks_certification(self):
        report = self.snapshot["reports"]["GeneralLedger"]
        report["Columns"]["Column"][3]["ColType"] = "unsupported_name"
        report["Columns"]["Column"][3]["MetaData"] = []
        result = sync.sync_import(self.entity, self.write())
        self.assertTrue(result["reference_complete"])
        self.assertFalse(result["comparison_ready"])
        self.assertEqual(result["coverage"]["TransactionDetail"]["status"], "blocked")

    def test_malformed_report_structures_are_blocked_and_original_evidence_retained(self):
        self.snapshot["reports"]["BalanceSheet"]["Rows"] = []
        result = sync.sync_import(self.entity, self.write())
        self.assertFalse(result["reference_complete"])
        self.assertEqual(result["coverage"]["BalanceSheet"]["status"], "blocked")
        self.assertTrue((self.entity_path / result["reference_path"]).parent.joinpath("snapshot.json").exists())

    def test_official_general_ledger_column_type_aliases(self):
        report = self.snapshot["reports"]["GeneralLedger"]
        aliases = {"tx_type": "txn_type", "amount": "subt_nat_amount", "running_bal": "rbal_nat_amount"}
        for column in report["Columns"]["Column"]:
            column["ColType"] = aliases.get(column["ColType"], column["ColType"])
            column.pop("MetaData", None)
        result = sync.sync_import(self.entity, self.write())
        self.assertTrue(result["comparison_ready"])

    def test_complete_accrual_source_is_retained_but_opening_is_blocked(self):
        self.snapshot["accounting_basis"] = "Accrual"
        for report in self.snapshot["reports"].values():
            report["Header"]["ReportBasis"] = "Accrual"
        result = sync.sync_import(self.entity, self.write())
        self.assertTrue(result["success"])
        self.assertTrue(result["reference_complete"])
        self.assertFalse(result["opening_supported"])
        self.assertFalse(result["comparison_supported"])
        with self.assertRaisesRegex(ValueError, "cash-basis"):
            sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)

    def test_multicurrency_is_evidence_only(self):
        self.snapshot["preferences"]["CurrencyPrefs"]["MultiCurrencyEnabled"] = True
        result = sync.sync_import(self.entity, self.write())
        self.assertFalse(result["opening_supported"])
        with self.assertRaises(ValueError):
            sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)

    def test_corrupted_reference_cannot_be_applied(self):
        result = sync.sync_import(self.entity, self.write())
        reference = self.entity_path / result["reference_path"] / "OpeningTrialBalance.qbo.json"
        reference.write_text(reference.read_text().replace('"100.00"', '"900.00"'))
        with self.assertRaisesRegex(ValueError, "corruption"):
            sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)

    def test_wrong_opening_date_and_unbalanced_tb_block(self):
        self.snapshot["reports"]["OpeningTrialBalance"]["Rows"]["Row"][0]["ColData"][1]["value"] = "101.00"
        del self.snapshot["reports"]["OpeningBalanceSheet"]
        with self.assertRaises(ValueError):
            sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)
        with self.assertRaises(ValueError):
            sync.sync_opening(self.entity, self.write(), date(2026, 1, 2), apply=True)

    def test_historical_rule_suggestions_require_repeated_unambiguous_classification(self):
        purchase = self.snapshot["entities"]["Purchase"][0]
        self.snapshot["entities"]["Purchase"] = [dict(copy.deepcopy(purchase), Id=str(i)) for i in range(1, 4)]
        result = sync.sync_import(self.entity, self.write())
        rules = result["rule_suggestions"]["proposed"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["evidence_count"], 3)
        self.assertFalse(rules[0]["auto_post"])
        self.assertFalse((self.entity_path / "learned-context" / "counterparties.json").exists())

    def test_rule_file_preview_apply_and_unsupported_layout(self):
        rules = self.root / "rules.csv"
        rules.write_text("Name,Field,Operator,Value,Account,Priority,Money direction\nSoftware,description,contains,Example,Expenses:Software,1,out\n")
        preview = sync.sync_rules(self.entity, rules)
        self.assertTrue(preview["success"])
        self.assertFalse(preview["applied"])
        applied = sync.sync_rules(self.entity, rules, apply=True)
        self.assertTrue(applied["applied"])
        self.assertFalse(applied["proposed"][0]["auto_post"])
        rules.write_text("Rule,Conditions,Splits\nExample,something,ambiguous\n")
        result = sync.sync_rules(self.entity, rules, apply=True)
        self.assertFalse(result["success"])
        self.assertFalse(result["applied"])

    def test_corrupted_rule_proposal_evidence_cannot_report_applied(self):
        rules = self.root / "rules.csv"
        rules.write_text("Name,Field,Operator,Value,Account,Priority,Money direction\nSoftware,description,contains,Example,Expenses:Software,1,out\n")
        result = sync.sync_rules(self.entity, rules, apply=True)
        target = self.entity_path / result["proposals_path"]
        target.write_text('{"corrupted":true}')
        with self.assertRaisesRegex(ValueError, "corrupted"):
            sync.sync_rules(self.entity, rules, apply=True)

    def test_remote_contract_classifies_input_and_mutation(self):
        imported = command_spec(["qb", "sync-import", "@input/snapshot.json", "--entity", "@entity"])
        self.assertEqual(imported["input_fields"], ["input"])
        self.assertTrue(imported["mutates"])
        preview = command_spec(["qb", "sync-rules", "@input/rules.csv", "--entity", "@entity"])
        self.assertFalse(preview["mutates"])
        applied = command_spec(["qb", "sync-rules", "@input/rules.csv", "--entity", "@entity", "--apply"])
        self.assertTrue(applied["mutates"])


if __name__ == "__main__":
    unittest.main()
