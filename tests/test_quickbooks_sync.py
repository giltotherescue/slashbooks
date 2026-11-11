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

    def test_source_chart_retains_duplicate_and_blank_exact_numbers(self):
        rows, _ = sync._accounts([
            {"Id": "a", "Name": "Design", "AccountType": "Income", "AcctNum": "0010"},
            {"Id": "b", "Name": "Retainers", "AccountType": "Income", "AcctNum": "0010"},
            {"Id": "c", "Name": "Other", "AccountType": "Income", "AcctNum": ""},
        ])
        self.assertEqual([r["account_number"] for r in rows], ["0010", "0010", ""])
        with self.assertRaisesRegex(ValueError, "leading zeros"):
            sync._accounts([{"Id": "a", "Name": "Design", "AccountType": "Income", "AcctNum": 10}])

    def test_chart_manifest_requires_explicit_rebuild_and_retains_source_scope(self):
        self.snapshot["accounts"][0]["AcctNum"] = "001"
        manifest = sync.chart_manifest(self.snapshot, "a" * 64, "preserve")
        self.assertEqual(manifest["open_date"], self.snapshot["from"])
        self.assertEqual(manifest["scope"], "complete")
        self.assertEqual(len(manifest["accounts"]), len(self.snapshot["accounts"]))
        self.assertEqual(manifest["accounts"][0]["number"], "001")
        with self.assertRaisesRegex(ValueError, "reviewed target"):
            sync.chart_manifest(self.snapshot, "a" * 64, "rebuild")
        targets = {r["source_id"]: r["account"] for r in manifest["accounts"]}
        rebuilt = sync.chart_manifest(self.snapshot, "a" * 64, "rebuild", targets)
        self.assertEqual(rebuilt["presentation"], "evidence_only", "Rebuild preserves the chosen target display instead of relabelling it to source names")

    def test_typed_chart_uses_provider_kind_and_requires_review_for_ambiguous_assets(self):
        self.snapshot["accounts"][0]["AccountSubType"] = "CashOnHand"
        self.snapshot["accounts"].append({"Id": "4", "Name": "Settlement balance", "AccountType": "Other Current Asset"})
        with self.assertRaisesRegex(ValueError, "review.*financial kind"):
            sync.chart_manifest(self.snapshot, "a" * 64, "preserve", version=2)
        typed = sync.chart_manifest(self.snapshot, "a" * 64, "preserve", version=2,
                                    financial_kinds={"4": "cash"})
        rows = {row["source_id"]: row for row in typed["accounts"]}
        self.assertEqual(rows["1"]["financial_kind"], "cash")
        self.assertEqual(rows["4"]["financial_kind"], "cash")
        self.assertNotIn("financial_kind", rows["2"])
        self.assertEqual(typed["operation_id"], "qbo-chart-v2-" + "a" * 64)

    def test_import_marks_only_an_ambiguous_active_balance_sheet_account(self):
        self.snapshot["accounts"].append({"Id": "4", "Name": "Settlement balance", "AccountType": "Other Current Asset"})
        self.snapshot["accounts"].append({"Id": "5", "Name": "Old deposit", "AccountType": "Other Current Asset",
                                          "Active": False})
        result = sync.sync_import(self.entity, self.write())
        kinds = {row["id"]: row.get("financial_kind") for row in result["accounts"]}
        self.assertEqual(kinds, {
            "1": {"required": False, "kind": "bank", "allowed": ["bank"]}, "2": None, "3": None,
            "4": {"required": True, "kind": None, "allowed": ["bank", "cash", "other"]}, "5": None})
        self.assertEqual([i for i, kind in kinds.items() if kind and kind["required"]], ["4"])
        # The stored evidence keeps its earlier bytes: the decision is in the result only.
        stored = json.loads((self.entity_path / result["reference_path"]).parent.joinpath("manifest.json").read_text())
        self.assertTrue(all("financial_kind" not in row for row in stored["accounts"]))

    def test_offered_kinds_are_exactly_the_kinds_that_the_chart_accepts(self):
        from bookkeeping.ledger.store import FINANCIAL_KINDS, validate_financial_kind
        self.snapshot["accounts"].extend([
            {"Id": "4", "Name": "Settlement balance", "AccountType": "Other Current Asset"},
            {"Id": "6", "Name": "Payroll clearing", "AccountType": "Other Current Liability"},
            {"Id": "7", "Name": "Equipment loan", "AccountType": "Long Term Liability"}])
        rows = {row["id"]: row for row in sync.sync_import(self.entity, self.write())["accounts"]}
        self.assertEqual(rows["4"]["financial_kind"]["allowed"], ["bank", "cash", "other"])
        self.assertEqual(rows["6"]["financial_kind"]["allowed"], ["credit_card", "other"])
        self.assertEqual(rows["7"]["financial_kind"], {"required": False, "kind": "other", "allowed": ["other"]})
        self.assertEqual(rows["1"]["financial_kind"], {"required": False, "kind": "bank", "allowed": ["bank"]})
        for identity in ("4", "6"):
            account = rows[identity]["mapped_account"]
            allowed = rows[identity]["financial_kind"]["allowed"]
            for kind in FINANCIAL_KINDS:
                with self.subTest(account=identity, kind=kind):
                    if kind in allowed:
                        validate_financial_kind(account, kind)
                        manifest = sync.chart_manifest(self.snapshot, "a" * 64, "preserve", version=2,
                                                       financial_kinds={"4": "other", "6": "other",
                                                                        identity: kind})
                        chosen = {r["source_id"]: r["financial_kind"] for r in manifest["accounts"]
                                  if "financial_kind" in r}
                        self.assertEqual(chosen[identity], kind)
                    else:
                        with self.assertRaises(ValueError):
                            validate_financial_kind(account, kind)

    def test_import_of_an_unambiguous_chart_requires_no_kind_review(self):
        result = sync.sync_import(self.entity, self.write())
        self.assertFalse(any(row.get("financial_kind", {}).get("required") for row in result["accounts"]))
        sync.chart_manifest(self.snapshot, "a" * 64, "preserve", version=2)

    def test_kind_review_error_names_every_ambiguous_account_in_a_stable_order(self):
        extra = [{"Id": "9", "Name": "Settlement balance", "AccountType": "Other Current Asset"},
                 {"Id": "10", "Name": "Payroll clearing", "AccountType": "Other Current Liability"},
                 {"Id": "4", "Name": "Undeposited funds", "AccountType": "Other Current Asset"}]
        messages = set()
        for order in (extra, extra[::-1]):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot["accounts"].extend(order)
            with self.assertRaises(sync.FinancialKindReviewRequired) as caught:
                sync.chart_manifest(snapshot, "a" * 64, "preserve", version=2, financial_kinds={})
            self.assertEqual(caught.exception.account_ids, ("10", "4", "9"))
            self.assertIsInstance(caught.exception, ValueError)
            messages.add(str(caught.exception))
        self.assertEqual(messages, {"QuickBooks accounts 10, 4, 9 need review of their financial kind."})
        self.snapshot["accounts"].extend(extra)
        with self.assertRaises(sync.FinancialKindReviewRequired) as caught:
            sync.chart_manifest(self.snapshot, "a" * 64, "preserve", version=2, financial_kinds={"10": "other",
                                                                                                "9": "cash"})
        self.assertEqual(str(caught.exception), "QuickBooks account 4 needs review of its financial kind.")

    def test_chart_and_opening_commit_once_with_inactive_reference_only(self):
        from bookkeeping.ledger.store import LedgerStore
        from bookkeeping.chart_adoption import inspect
        self.snapshot["accounts"].append({"Id": "unused", "Name": "Unused design", "AccountType": "Income", "AcctNum": "001", "Active": False})
        source = self.write()
        preview = sync.sync_opening(self.entity, source, date(2026, 1, 1), chart_decision="preserve")
        self.assertEqual(next(r for r in preview["chart"]["accounts"] if r["source_id"] == "unused")["action"], "reference_only")
        result = sync.sync_opening(self.entity, source, date(2026, 1, 1), apply=True,
                                   chart_decision="preserve", chart_plan_hash=preview["chart"]["plan_hash"])
        self.assertTrue(result["applied"])
        store = LedgerStore(self.entity_path / "ledger.sqlite")
        before = store.content_digest()
        self.assertNotIn("Income:Unused-design", store.load_account_names())
        self.assertTrue(inspect(self.entity_path)["source_evidence"]["sources"])
        again = sync.sync_opening(self.entity, source, date(2026, 1, 1), apply=True,
                                  chart_decision="preserve", chart_plan_hash=preview["chart"]["plan_hash"])
        self.assertTrue(again["already_applied"])
        self.assertEqual(store.content_digest(), before)

    def test_chart_choice_preview_shows_the_complete_opening_entry_that_apply_records(self):
        """Both chart choices: the reviewed preview entry is the applied entry (same entry fingerprint)."""
        import hashlib
        from bookkeeping.ledger.books_policy_check import entry_fingerprint
        from bookkeeping.ledger.store import LedgerStore
        from bookkeeping.source_disposition import entry_from_document

        def files(root):
            return {f.relative_to(root).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
                    for f in sorted(root.rglob("*")) if f.is_file()}

        manifest = sync.chart_manifest(self.snapshot, "a" * 64, "preserve")
        targets = {row["source_id"]: row["account"] for row in manifest["accounts"]}
        bank = next(row for row in manifest["accounts"] if row["type"] == "Assets")
        targets[bank["source_id"]] = "Assets:Operating:Reviewed"
        cases = [(version, decision, choice)
                 for version in (1, 2)
                 for decision, choice in (("preserve", {}), ("rebuild", {"chart_targets": targets}))]
        for version, decision, choice in cases:
            with self.subTest(version=version, decision=decision):
                if version == 2:
                    choice = {**choice, "chart_manifest_version": version}
                path = self.root / f"books-{version}-{decision}"
                init_entity(path, name="Synthetic Example Books")
                entity = load_entity(path)
                source = self.write()
                store = LedgerStore(path / "ledger.sqlite")
                before_files, before_entries = files(path), store.load_entries()
                preview = sync.sync_opening(entity, source, date(2026, 1, 1), chart_decision=decision, **choice)
                self.assertFalse(preview["applied"])
                # No ledger change. The preview saves the snapshot reference data (sync import) and takes the
                # entity write lock; these are the only other changes.
                changed = {name for name, digest in files(path).items() if before_files.get(name) != digest}
                self.assertEqual(store.load_entries(), before_entries)
                self.assertNotIn("ledger.sqlite", changed)
                snapshot_folder = f"sources/quickbooks/{preview['snapshot_id']}/"
                self.assertTrue(preview["reference_path"].startswith(snapshot_folder), preview["reference_path"])
                self.assertEqual({n for n in changed if n != ".books.lock" and not n.startswith(snapshot_folder)},
                                 set())
                reviewed = entry_from_document(preview["opening"]["proposed_entry"])
                self.assertEqual(entry_fingerprint(reviewed), preview["opening"]["proposed_entry_fingerprint"])
                accounts = {p.account for p in reviewed.postings}
                self.assertEqual("Assets:Operating:Reviewed" in accounts, decision == "rebuild")
                applied = sync.sync_opening(entity, source, date(2026, 1, 1), apply=True, chart_decision=decision,
                                            chart_plan_hash=preview["chart"]["plan_hash"], **choice)
                self.assertTrue(applied["applied"])
                written = [e for e in store.load_entries() if e.source_id == reviewed.source_id]
                self.assertEqual(written, [reviewed])
                self.assertEqual(entry_fingerprint(written[0]), preview["opening"]["proposed_entry_fingerprint"])
                if version == 2:
                    target = targets[bank["source_id"]] if decision == "rebuild" else bank["account"]
                    self.assertEqual(store.load_financial_kinds(), {target: "bank"})
                    again = sync.sync_opening(entity, source, date(2026, 1, 1), apply=True,
                                               chart_decision=decision, chart_plan_hash=preview["chart"]["plan_hash"], **choice)
                    self.assertTrue(again["already_applied"])
                    self.assertEqual(store.load_entries(), [reviewed])

    def test_failed_opening_rolls_back_prepared_chart(self):
        from unittest.mock import patch
        from bookkeeping.ledger.store import LedgerStore
        source = self.write()
        preview = sync.sync_opening(self.entity, source, date(2026, 1, 1), chart_decision="preserve", chart_manifest_version=2)
        store = LedgerStore(self.entity_path / "ledger.sqlite")
        before = store.content_digest(), store.load_account_names(), store.load_audit_events()
        original = qb.import_opening
        def fail_after_chart(*args, **kwargs):
            if kwargs.get("_conn") is not None:
                raise RuntimeError("injected opening failure after chart write")
            return original(*args, **kwargs)
        with patch.object(qb, "import_opening", side_effect=fail_after_chart):
            with self.assertRaisesRegex(RuntimeError, "after chart"):
                sync.sync_opening(self.entity, source, date(2026, 1, 1), apply=True,
                                  chart_decision="preserve", chart_manifest_version=2, chart_plan_hash=preview["chart"]["plan_hash"])
        self.assertEqual((store.content_digest(), store.load_account_names(), store.load_audit_events()), before)
        self.assertEqual(store.load_financial_kinds(), {})
        self.assertEqual(store.get_meta("schema_version"), "1")

    def test_rebuild_opening_uses_reviewed_target_not_source_name(self):
        from bookkeeping.ledger.store import LedgerStore
        source = self.write()
        manifest = sync.chart_manifest(self.snapshot, "a" * 64, "preserve")
        targets = {row["source_id"]: row["account"] for row in manifest["accounts"]}
        chosen = next(row for row in manifest["accounts"] if row["type"] == "Assets")
        targets[chosen["source_id"]] = "Assets:Bank:Reviewed"
        preview = sync.sync_opening(self.entity, source, date(2026, 1, 1), chart_decision="rebuild", chart_targets=targets)
        self.assertTrue(any(p["account"] == "Assets:Bank:Reviewed" for p in preview["opening"]["proposed_postings"]))
        result = sync.sync_opening(self.entity, source, date(2026, 1, 1), apply=True,
                                  chart_decision="rebuild", chart_targets=targets, chart_plan_hash=preview["chart"]["plan_hash"])
        self.assertTrue(result["applied"])
        postings = [p for e in LedgerStore(self.entity_path / "ledger.sqlite").load_entries() for p in e.postings]
        self.assertTrue(any(p.account == "Assets:Bank:Reviewed" for p in postings))
        self.assertFalse(any(p.account == chosen["account"] for p in postings))

    def test_mapped_overlap_and_replay_use_the_final_crosswalk(self):
        from unittest.mock import patch
        from bookkeeping.ledger.store import LedgerStore
        manifest = sync.chart_manifest(self.snapshot, "a" * 64, "preserve")
        targets = {row["source_id"]: row["account"] for row in manifest["accounts"]}
        bank = next(row for row in manifest["accounts"] if row["type"] == "Assets")
        targets[bank["source_id"]] = "Assets:Bank:Reviewed"
        source = self.write()
        preview = sync.sync_opening(self.entity, source, date(2026, 1, 1), chart_decision="rebuild", chart_targets=targets)
        with patch.object(qb, "check_opening_overlap", wraps=qb.check_opening_overlap) as checked:
            applied = sync.sync_opening(self.entity, source, date(2026, 1, 1), apply=True,
                chart_decision="rebuild", chart_targets=targets, chart_plan_hash=preview["chart"]["plan_hash"])
        self.assertTrue(applied["applied"])
        self.assertGreaterEqual(checked.call_count, 2, "Overlap is checked again under the publication lock")
        for call in checked.call_args_list:
            self.assertIn("Assets:Bank:Reviewed", {row["account"] for row in call.args[2]})
        store = LedgerStore(self.entity_path / "ledger.sqlite")
        before = store.content_digest(), store.load_audit_events()
        replay = sync.sync_opening(self.entity, source, date(2026, 1, 1), apply=True,
            chart_decision="rebuild", chart_targets=targets, chart_plan_hash=preview["chart"]["plan_hash"])
        self.assertTrue(replay["already_applied"])
        changed = dict(targets, **{bank["source_id"]: "Assets:Bank:Other"})
        with self.assertRaisesRegex(ValueError, "identity reused"):
            sync.sync_opening(self.entity, source, date(2026, 1, 1), apply=True,
                chart_decision="rebuild", chart_targets=changed, chart_plan_hash=preview["chart"]["plan_hash"])
        self.assertEqual((store.content_digest(), store.load_audit_events()), before)

    def test_existing_chart_receipt_does_not_bypass_final_catalog_check(self):
        from unittest.mock import patch
        from bookkeeping import chart_adoption
        from bookkeeping.ledger.store import LedgerStore
        self.snapshot["accounts"].append({"Id": "zero-income", "Name": "Unused income", "AccountType": "Income", "Active": True})
        source = self.write()
        preview = sync.sync_opening(self.entity, source, date(2026, 1, 1), chart_decision="preserve")
        manifest = sync.chart_manifest(self.snapshot, preview["snapshot_id"], "preserve")
        chart_adoption.adopt(self.entity_path, manifest, apply=True, plan_hash=preview["chart"]["plan_hash"])
        store = LedgerStore(self.entity_path / "ledger.sqlite")
        zero_account = next(row["account"] for row in manifest["accounts"] if row["type"] == "Income")
        original = chart_adoption.adopt
        def concurrent_catalog_change(*args, **kwargs):
            receipt = original(*args, **kwargs)
            self.assertEqual(receipt["status"], "already_applied")
            # Simulate another audited catalog writer after the first target check.
            with store.transaction(immediate=True) as conn:
                conn.execute("UPDATE accounts SET currency='EUR' WHERE name=?", (zero_account,))
                store.append_audit_event("ledger-store-sealed", {"session_id": "catalog-race-fixture",
                    "entries": 0, "source_ids": [], "store_sha256": store.content_digest(conn)}, conn)
            return receipt
        with patch.object(chart_adoption, "adopt", side_effect=concurrent_catalog_change):
            with self.assertRaisesRegex(ValueError, "different currency"):
                sync.sync_opening(self.entity, source, date(2026, 1, 1), apply=True,
                    chart_decision="preserve", chart_plan_hash=preview["chart"]["plan_hash"])
        self.assertEqual(store.load_entries(), [])

    def test_composite_refuses_currency_adoption_without_active_chart_change(self):
        from bookkeeping.ledger.store import LedgerStore
        self.snapshot["company_info"]["CurrencyRef"] = {"value": "EUR"}
        self.snapshot["preferences"]["CurrencyPrefs"]["HomeCurrency"] = {"value": "EUR"}
        for report in self.snapshot["reports"].values():
            if "Header" in report:
                report["Header"]["Currency"] = "EUR"
        store = LedgerStore(self.entity_path / "ledger.sqlite")
        before = store.content_digest(), store.load_account_names()
        with self.assertRaisesRegex(ValueError, "company currency"):
            sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), chart_decision="preserve", apply=True)
        self.assertEqual((store.content_digest(), store.load_account_names()), before)

    def test_rebuild_refuses_reopening_a_later_target_account(self):
        from bookkeeping.entity import add_account
        from bookkeeping.ledger.store import LedgerStore
        add_account(self.entity_path, "Assets:Bank:Later", open_date="2027-01-01")
        manifest = sync.chart_manifest(self.snapshot, "a" * 64, "preserve")
        targets = {row["source_id"]: row["account"] for row in manifest["accounts"]}
        chosen = next(row for row in manifest["accounts"] if row["type"] == "Assets")
        targets[chosen["source_id"]] = "Assets:Bank:Later"
        store = LedgerStore(self.entity_path / "ledger.sqlite")
        before = store.content_digest()
        with self.assertRaisesRegex(ValueError, "later opening date"):
            sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), chart_decision="rebuild", chart_targets=targets)
        self.assertEqual(store.content_digest(), before)

    def test_inactive_source_with_balance_is_not_silently_activated(self):
        manifest = sync.chart_manifest(self.snapshot, "a" * 64, "preserve")
        chosen = next(row for row in manifest["accounts"] if row["type"] == "Assets")
        for row in self.snapshot["accounts"]:
            if row["Id"] == chosen["source_id"]:
                row["Active"] = False
        targets = {row["source_id"]: row["account"] for row in manifest["accounts"]}
        targets[chosen["source_id"]] = "Assets:Bank:Inactive-source"
        with self.assertRaisesRegex(ValueError, "inactive source account"):
            sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), chart_decision="rebuild", chart_targets=targets)

    def write(self):
        source = self.root / "snapshot.json"
        source.write_text(json.dumps(self.snapshot))
        return source

    def empty_opening_reports(self):
        for key in ("OpeningTrialBalance", "OpeningBalanceSheet"):
            report = self.snapshot["reports"][key]
            report["Header"]["Option"] = [{"Name": "NoReportData", "Value": "true"}]
            report["Columns"] = {"Column": [{"ColTitle": "", "ColType": "Account"}]}
            report["Rows"] = {"Row": [{"ColData": [{"value": "Synthetic account"}]}]}

    def test_explicit_empty_opening_reports_are_valid_empty_models(self):
        self.empty_opening_reports()
        result = sync.sync_import(self.entity, self.write())
        self.assertTrue(result["reference_complete"])
        self.assertFalse(result["opening_ready"], "A valid empty report cannot create an opening entry")
        self.assertIn("No opening balances are available to record", result["warnings"])
        with self.assertRaisesRegex(ValueError, "Opening source reports"):
            sync.sync_opening(self.entity, self.write(), date(2026, 1, 1))
        reference = self.entity_path / result["reference_path"]
        self.assertEqual(qb.parse_trial_balance(reference / "OpeningTrialBalance.qbo.json").entries, [])
        self.assertEqual(qb.parse_balance_sheet(reference / "OpeningBalanceSheet.qbo.json").rows, [])

    def test_empty_report_marker_cannot_hide_money_or_replace_missing_columns(self):
        for mode in ("missing_marker", "false_marker", "extra_cell", "money_column"):
            with self.subTest(mode=mode):
                self.empty_opening_reports()
                report = self.snapshot["reports"]["OpeningTrialBalance"]
                if mode == "missing_marker":
                    report["Header"]["Option"] = []
                elif mode == "false_marker":
                    report["Header"]["Option"][0]["Value"] = "false"
                elif mode == "extra_cell":
                    report["Rows"]["Row"][0]["ColData"].append({"value": "10.00"})
                else:
                    report["Columns"]["Column"].append({"ColTitle": "Total", "ColType": "Money"})
                result = sync.sync_import(self.entity, self.write())
                self.assertFalse(result["reference_complete"])
                self.assertEqual(result["coverage"]["OpeningTrialBalance"]["status"], "blocked")

    def test_purchase_credit_is_a_boolean_flag_not_a_monetary_amount(self):
        for credit in (True, False, None):
            with self.subTest(credit=credit):
                self.snapshot["entities"]["Purchase"] = [{
                    "Id": "synthetic-purchase", "TxnDate": "2026-01-15",
                    "Credit": credit, "TotalAmt": "12.00", "Line": [],
                }]
                result = sync.sync_import(self.entity, self.write())
                self.assertTrue(result["success"])

    def test_boolean_amounts_and_non_boolean_purchase_credit_stay_invalid(self):
        for fields in ({"Credit": "true"}, {"Credit": 1}, {"Amount": True}, {"TotalAmt": False}):
            with self.subTest(fields=fields):
                self.snapshot["entities"]["Purchase"] = [{
                    "Id": "synthetic-purchase", "TxnDate": "2026-01-15", "Line": [], **fields,
                }]
                with self.assertRaises(ValueError):
                    sync.sync_import(self.entity, self.write())

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
            {"account": "Assets:Bank:Checking", "amount": "100.00", "currency": "USD", "date": "2026-01-01", "debit": "100.00", "credit": "0.00",
             "meta": {"qb-name": "Checking"}},
            {"account": "Equity:Opening-Balances", "amount": "-100.00", "currency": "USD", "date": "2026-01-01", "debit": "0.00", "credit": "100.00",
             "meta": {}}])
        # The preview names the entry that an apply writes, so an approval can bind to it.
        self.assertEqual(preview["opening"]["proposed_source_id"], "quickbooks-opening-trial-balance-2026-01-01")
        applied = sync.sync_opening(self.entity, self.write(), date(2026, 1, 1), apply=True)
        from bookkeeping.ledger.store import LedgerStore
        self.assertEqual([e.source_id for e in LedgerStore(self.entity_path / "ledger.sqlite").load_entries()
                          if dict(e.meta).get("import-source") == "quickbooks-opening"],
                         [preview["opening"]["proposed_source_id"]])
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

    def cli_json(self, *argv):
        from contextlib import redirect_stdout
        from io import StringIO
        from bookkeeping import cli
        out = StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, json.loads(out.getvalue())

    def make_older_format(self, result):
        """Rewrite the saved derived evidence as an engine before account numbers wrote it."""
        folder = (self.entity_path / result["reference_path"]).parent
        for path in (folder / "manifest.json", folder / "reference" / "accounts.qbo.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            value["accounts"] = [{k: v for k, v in row.items() if k != "account_number"} for row in value["accounts"]]
            path.write_bytes(sync._json_bytes(value))
        return folder

    def test_saved_evidence_in_the_older_format_has_its_own_code(self):
        self.snapshot["accounts"][0]["AcctNum"] = "1000"
        result = sync.sync_import(self.entity, self.write())
        self.assertEqual(sync.sync_import(self.entity, self.write())["snapshot_id"], result["snapshot_id"])  # replay
        folder = self.make_older_format(result)
        with self.assertRaises(sync.SnapshotEvidenceError) as caught:
            sync.sync_import(self.entity, self.write())
        self.assertEqual(caught.exception.code, "snapshot_format_outdated")
        self.assertIn(result["snapshot_id"], str(caught.exception))
        for command in (["qb", "sync-import"], ["qb", "sync-opening"]):
            with self.subTest(command=command):
                extra = ["--cutover", "2026-01-01"] if command[1] == "sync-opening" else []
                code, out = self.cli_json(*command, str(self.write()), "--entity", str(self.entity_path), *extra)
                self.assertEqual((code, out["success"], out["code"]), (1, False, "snapshot_format_outdated"), out)
        # The older format with any other change is not an outdated format.
        target = folder / "reference" / "OpeningTrialBalance.qbo.json"
        target.write_text(target.read_text().replace('"100.00"', '"900.00"'))
        with self.assertRaises(sync.SnapshotEvidenceError) as caught:
            sync.sync_import(self.entity, self.write())
        self.assertEqual(caught.exception.code, "snapshot_evidence_mismatch")

    def test_changed_or_damaged_saved_evidence_has_the_mismatch_code(self):
        self.snapshot["accounts"][0]["AcctNum"] = "1000"
        cases = {
            "changed report": lambda folder: (folder / "reference" / "OpeningTrialBalance.qbo.json").write_text(
                (folder / "reference" / "OpeningTrialBalance.qbo.json").read_text().replace('"100.00"', '"900.00"')),
            "changed source": lambda folder: (folder / "snapshot.json").write_bytes(
                (folder / "snapshot.json").read_bytes().replace(b"Synthetic", b"Changed")),
            "extra file": lambda folder: (folder / "reference" / "extra.qbo.json").write_text("{}"),
            "missing file": lambda folder: (folder / "manifest.json").unlink(),
            "not json": lambda folder: (folder / "reference" / "accounts.qbo.json").write_text("{"),
        }
        for label, damage in cases.items():
            with self.subTest(label):
                result = sync.sync_import(self.entity, self.write())
                folder = (self.entity_path / result["reference_path"]).parent
                damage(folder)
                with self.assertRaises(sync.SnapshotEvidenceError) as caught:
                    sync.sync_import(self.entity, self.write())
                self.assertEqual(caught.exception.code, "snapshot_evidence_mismatch")
                self.assertEqual(str(caught.exception), "Snapshot evidence digest collision or corruption")
                code, out = self.cli_json("qb", "sync-import", str(self.write()), "--entity", str(self.entity_path))
                self.assertEqual((code, out["code"]), (1, "snapshot_evidence_mismatch"), out)
                import shutil
                shutil.rmtree(folder)

    def test_concurrent_different_evidence_has_its_own_code(self):
        from unittest.mock import patch
        real_rename = Path.rename

        def rival_first(stage, target):
            # Another import saves different evidence under the same ID between the check and the rename.
            Path(target).mkdir()
            (Path(target) / "snapshot.json").write_text("{}")
            raise FileExistsError(str(target))

        with patch.object(Path, "rename", rival_first):
            with self.assertRaises(sync.SnapshotEvidenceError) as caught:
                sync.sync_import(self.entity, self.write())
        self.assertEqual(caught.exception.code, "snapshot_evidence_concurrent")
        self.assertIs(Path.rename, real_rename)
        import shutil
        for folder in (self.entity_path / "sources" / "quickbooks").iterdir():
            shutil.rmtree(folder)  # the rival evidence
        # Other errors keep the earlier JSON shape: no code.
        code, out = self.cli_json("qb", "sync-opening", str(self.write()), "--entity", str(self.entity_path),
                                  "--cutover", "2026-01-02")
        self.assertEqual(code, 1)
        self.assertNotIn("code", out)

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
