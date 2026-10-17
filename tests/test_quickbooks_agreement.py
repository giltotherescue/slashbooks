"""QuickBooks category-agreement backtest tests. Every company and amount is synthetic."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from bookkeeping import quickbooks_agreement as agreement
from bookkeeping import quickbooks_sync as sync
from bookkeeping.cli import main
from bookkeeping.entity import init_entity, load_entity
from bookkeeping.queue import set_taught
from bookkeeping.remote_contract import command_spec

FIXTURE = Path(__file__).parent / "fixtures" / "quickbooks_api" / "snapshot.json"

ACCOUNTS = [("1", "Checking", "Bank"), ("2", "Opening Equity", "Equity"), ("3", "Software", "Expense"),
            ("4", "Meals", "Expense"), ("5", "Office", "Expense"), ("6", "Travel", "Expense"),
            ("7", "Legacy Expense", "Expense")]

# (id, date, vendor, amount, account id). Training is January to March.
# Held out by 3 months (April to June): H1..H8 below.
PURCHASES = [
    ("1", "2026-01-05", "Acme Software", "20.00", "3"), ("2", "2026-02-05", "Acme Software", "20.00", "3"),
    ("3", "2026-03-05", "Acme Software", "20.00", "3"),
    ("4", "2026-01-10", "Cafe Rio", "12.00", "4"), ("5", "2026-02-10", "Cafe Rio", "15.00", "4"),
    ("6", "2026-01-15", "Paper Co", "9.00", "5"), ("7", "2026-03-15", "Paper Co", "11.00", "5"),
    ("8", "2026-02-20", "Delta Air", "300.00", "6"),
    ("9", "2026-01-25", "Mixed Vendor", "8.00", "3"), ("10", "2026-03-25", "Mixed Vendor", "8.00", "4"),
    # Held out
    ("11", "2026-04-02", "Acme Software", "100.00", "3"),   # agree
    ("12", "2026-05-02", "Acme Software", "50.00", "3"),    # agree
    ("13", "2026-04-12", "Cafe Rio", "30.00", "4"),         # agree
    ("14", "2026-05-12", "Cafe Rio", "200.00", "5"),        # disagree
    ("15", "2026-06-10", "Brand New Vendor", "75.00", "6"),  # vendor only in held-out: no suggestion
    ("16", "2026-06-11", "Mixed Vendor", "5.00", "3"),      # tied precedent: no suggestion
    ("17", "2026-06-12", "Paper Co", "40.00", "5"),         # agree
    ("18", "2026-06-13", "Delta Air", "60.00", "7"),        # disagree, QuickBooks account has no local counterpart
]


def make_snapshot(purchases=PURCHASES, extra_entities=None):
    snapshot = json.loads(FIXTURE.read_text())
    snapshot["from"], snapshot["to"] = "2026-01-01", "2026-06-30"
    snapshot["accounts"] = [{"Id": i, "Name": n, "AccountType": t} for i, n, t in ACCOUNTS]
    snapshot["reports"] = {}
    rows = []
    for ident, day, vendor, amount, account in purchases:
        rows.append({"Id": ident, "TxnDate": day, "TotalAmt": amount, "EntityRef": {"value": "1", "name": vendor},
                     "Line": [{"Amount": amount, "DetailType": "AccountBasedExpenseLineDetail",
                               "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account}}}]})
    snapshot["entities"] = {"Purchase": rows, **(extra_entities or {})}
    return snapshot


class AgreementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "books"
        init_entity(self.path, name="Synthetic Example Books")
        self.entity = load_entity(self.path)

    def load(self, snapshot=None):
        source = self.root / "snapshot.json"
        source.write_text(json.dumps(snapshot or make_snapshot()))
        return sync.sync_import(self.entity, source)["snapshot_id"]

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["qb", "agreement", "run", "--entity", str(self.path), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_known_result_by_months_with_amount_weighting(self):
        self.load()
        result = agreement.run_agreement(self.entity)
        self.assertEqual(result["holdout"], {"method": "months", "value": 3, "as_of": "2026-06-13", "start": "2026-04-01"})
        self.assertEqual(result["slices"]["training"], 10)
        self.assertEqual(result["slices"]["held_out"], 8)
        t = result["totals"]
        self.assertEqual((t["scored"], t["suggested"], t["agree"], t["disagree"], t["no_suggestion"]), (8, 6, 4, 2, 2))
        self.assertEqual(t["agree_amount"], "220.00")
        self.assertEqual(t["disagree_amount"], "260.00")
        self.assertEqual(t["no_suggestion_amount"], "80.00")
        self.assertEqual(t["agreement_rate"], 0.6667)
        self.assertEqual(t["agreement_rate_amount"], 0.4583)  # differs from the count rate
        self.assertEqual(t["suggestion_rate"], 0.75)
        self.assertEqual(t["agreement_rate_of_all"], 0.5)
        self.assertEqual(result["suggestion_sources"], {"precedent": 6})

    def test_disagreements_grouped_by_counterparty_and_classified(self):
        self.load()
        rows = agreement.run_agreement(self.entity)["disagreements"]
        self.assertEqual([r["counterparty"] for r in rows], ["Cafe Rio", "Delta Air"])
        cafe, delta = rows
        self.assertEqual(cafe, {"counterparty": "Cafe Rio", "quickbooks_category": "Expenses:Office",
                                "slashbooks_category": "Expenses:Meals", "classification": "category_disagreement",
                                "suggestion_source": "precedent", "count": 1, "total": "200.00",
                                "sample_ids": ["Purchase:14"]})
        # QuickBooks account Legacy Expense has no account in the local chart.
        self.assertEqual(delta["classification"], "judgment-mapping")
        self.assertEqual(delta["quickbooks_category"], "Expenses:Legacy-Expense")

    def test_no_suggestion_is_not_a_disagreement_and_leak_guard(self):
        self.load()
        result = agreement.run_agreement(self.entity)
        names = {r["counterparty"]: r for r in result["no_suggestion"]["by_counterparty"]}
        # Brand New Vendor appears only in the held-out slice: no precedent, no match.
        self.assertEqual(names["Brand New Vendor"]["count"], 1)
        self.assertEqual(names["Mixed Vendor"]["total"], "5.00")  # tied precedent gives no answer
        self.assertEqual(result["no_suggestion"]["count"], 2)
        self.assertNotIn("Brand New Vendor", [r["counterparty"] for r in result["disagreements"]])
        # Even when the held-out category would have matched a guess, a vendor seen only held out is unsuggested.
        self.assertEqual(agreement.build_precedents([]), {})

    def test_training_never_contains_held_out_transactions(self):
        self.load()
        txns, _ = agreement._transactions(json.loads(next((self.path / "sources/quickbooks").glob("*/snapshot.json")).read_text()),
                                          json.loads(next((self.path / "sources/quickbooks").glob("*/manifest.json")).read_text())["accounts"], {})
        for method, value in (("months", 3), ("fraction", agreement.parse_holdout("0.3")[1])):
            training, held, _ = agreement.split_history(txns, method, value, date(2026, 6, 30), "abc")
            self.assertFalse({t.ref for t in training} & {t.ref for t in held})
            self.assertEqual(len(training) + len(held), len(txns))
            self.assertTrue(held)

    def test_by_account_agreement(self):
        self.load()
        rows = {r["quickbooks_category"]: r for r in agreement.run_agreement(self.entity)["by_account"]}
        self.assertEqual(rows["Expenses:Software"]["agree"], 2)
        self.assertEqual(rows["Expenses:Software"]["no_suggestion"], 1)
        self.assertEqual(rows["Expenses:Software"]["agreement_rate"], 1.0)
        self.assertEqual(rows["Expenses:Office"]["agree"], 1)
        self.assertEqual(rows["Expenses:Office"]["disagree"], 1)
        self.assertEqual(rows["Expenses:Office"]["agreement_rate"], 0.5)
        self.assertEqual(rows["Expenses:Office"]["agreement_rate_amount"], 0.1667)

    def test_holdout_by_fraction_is_stable_and_leak_free(self):
        self.load()
        first = agreement.run_agreement(self.entity, holdout="0.25")
        second = agreement.run_agreement(self.entity, holdout="25%")
        self.assertEqual(first["holdout"]["method"], "fraction")
        self.assertIsNone(first["holdout"]["start"])
        self.assertEqual(first["slices"]["held_out"], 5)  # 18 transactions: round(4.5) = 5
        self.assertEqual(first["slices"]["training"] + first["slices"]["held_out"], 18)
        self.assertEqual(first["totals"], second["totals"])
        self.assertEqual(first["disagreements"], second["disagreements"])

    def test_as_of_excludes_later_transactions(self):
        self.load()
        result = agreement.run_agreement(self.entity, as_of=date(2026, 5, 31))
        self.assertEqual(result["holdout"]["start"], "2026-03-01")
        self.assertEqual(result["slices"]["training"] + result["slices"]["held_out"], 14)

    def test_refuses_empty_holdout_and_missing_history(self):
        with self.assertRaisesRegex(agreement.AgreementError, "No QuickBooks history"):
            agreement.run_agreement(self.entity)
        self.load()
        with self.assertRaisesRegex(agreement.AgreementError, "held-out slice is empty"):
            agreement.run_agreement(self.entity, holdout="1", as_of=date(2026, 7, 31))
        for bad in ("0", "1.5", "abc", "-2", "0.0"):
            with self.assertRaises(agreement.AgreementError):
                agreement.run_agreement(self.entity, holdout=bad)
        code, out, err = self.run_cli("--holdout", "1", "--as-of", "2026-07-31", "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["success"])
        code, out, err = self.run_cli("--holdout", "1", "--as-of", "2026-07-31")
        self.assertEqual(code, 1)
        self.assertIn("held-out slice is empty", err)

    def test_nothing_scorable_is_refused(self):
        self.load(make_snapshot(purchases=[], extra_entities={"Deposit": [{"Id": "1", "TxnDate": "2026-01-02", "TotalAmt": "5"}]}))
        with self.assertRaisesRegex(agreement.AgreementError, "can be scored"):
            agreement.run_agreement(self.entity)

    def test_idempotent_never_posts_never_writes_learned_context(self):
        self.load()
        ledger = (self.path / "ledger.sqlite").read_bytes()
        learned = self.path / "learned-context" / "counterparties.json"
        before = learned.read_bytes() if learned.exists() else None
        first = agreement.run_agreement(self.entity)
        path = self.path / first["result_path"]
        written = path.read_bytes()
        second = agreement.run_agreement(self.entity)
        self.assertEqual(first, second)
        self.assertEqual(written, path.read_bytes())
        self.assertEqual(ledger, (self.path / "ledger.sqlite").read_bytes())
        self.assertEqual(before, learned.read_bytes() if learned.exists() else None)
        self.assertTrue(first["result_path"].startswith("reports/quickbooks-agreement/"))
        self.assertTrue(first["result_path"].endswith("holdout-3m-asof-2026-06-13.json"))
        self.assertEqual(json.loads(written), first)
        self.assertFalse(list(path.parent.glob(".agreement-*")))

    def test_taught_category_wins_and_is_reported_by_source(self):
        self.load()
        set_taught(self.entity, "CAFE RIO", "Expenses:Office", by="u1", by_name="Owner")
        learned = self.path / "learned-context" / "counterparties.json"
        before = learned.read_bytes()
        result = agreement.run_agreement(self.entity)
        self.assertEqual(learned.read_bytes(), before)
        self.assertEqual(result["suggestion_sources"], {"precedent": 4, "taught": 2})
        cafe = [r for r in result["disagreements"] if r["counterparty"] == "Cafe Rio"]
        self.assertEqual(cafe[0]["suggestion_source"], "taught")
        # Cafe Rio 200 (Office) now agrees, Cafe Rio 30 (Meals) no longer does.
        self.assertEqual(result["totals"]["agree"], 4)
        self.assertEqual(result["totals"]["agree_amount"], "390.00")
        self.assertEqual([r["total"] for r in cafe], ["30.00"])

    def test_crosswalk_changes_the_quickbooks_category(self):
        self.load()
        cfg = self.entity.entity_config
        cfg["compare"] = {"qbo_account_crosswalk": {"Legacy Expense": "Expenses:Fees"}}
        result = agreement.run_agreement(self.entity)
        delta = [r for r in result["disagreements"] if r["counterparty"] == "Delta Air"][0]
        self.assertEqual(delta["quickbooks_category"], "Expenses:Fees")
        self.assertEqual(delta["classification"], "category_disagreement")

    def test_split_and_item_based_transactions_are_excluded(self):
        snapshot = make_snapshot()
        split = copy.deepcopy(snapshot["entities"]["Purchase"][0])
        split["Id"], split["TxnDate"] = "90", "2026-06-01"
        split["Line"].append({"Amount": "5.00", "DetailType": "AccountBasedExpenseLineDetail",
                              "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "4"}}})
        item = copy.deepcopy(snapshot["entities"]["Purchase"][0])
        item["Id"], item["TxnDate"] = "91", "2026-06-01"
        item["Line"] = [{"Amount": "20.00", "DetailType": "ItemBasedExpenseLineDetail"}]
        snapshot["entities"]["Purchase"] += [split, item]
        snapshot["entities"]["Deposit"] = [{"Id": "1", "TxnDate": "2026-06-02", "TotalAmt": "9.00"}]
        self.load(snapshot)
        result = agreement.run_agreement(self.entity)
        self.assertEqual(result["slices"]["held_out"], 8)
        self.assertEqual(result["excluded_from_scoring"], {"item_based_or_empty": 1, "split": 1, "type:Deposit": 1})

    def test_cli_json_and_text_and_contract(self):
        self.load()
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        for key in ("success", "kind", "schema_version", "snapshot_id", "holdout", "slices", "excluded_from_scoring",
                    "totals", "by_account", "disagreements", "no_suggestion", "suggestion_sources",
                    "inputs_digest", "result_path", "warnings"):
            self.assertIn(key, data)
        self.assertEqual(set(data["disagreements"][0]), {
            "counterparty", "quickbooks_category", "slashbooks_category", "classification",
            "suggestion_source", "count", "total", "sample_ids"})
        self.assertEqual(set(data["no_suggestion"]), {"count", "total", "by_counterparty"})
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("66.7%", out)
        self.assertIn("reports/quickbooks-agreement/", out)
        spec = command_spec(["qb", "agreement", "run", "--entity", str(self.path)])
        self.assertFalse(spec["mutates"])
        self.assertEqual(spec["entity_fields"], ["entity_path"])

    def test_snapshot_choice_defaults_to_newest_and_can_be_pinned(self):
        first = self.load()
        later = make_snapshot()
        later["fetched_at"] = "2099-01-01T00:00:00+00:00"
        later["entities"]["Purchase"].pop()
        second = self.load(later)
        self.assertNotEqual(first, second)
        self.assertEqual(agreement.run_agreement(self.entity)["snapshot_id"], second)
        self.assertEqual(agreement.run_agreement(self.entity, snapshot_id=first)["snapshot_id"], first)
        with self.assertRaisesRegex(agreement.AgreementError, "not found"):
            agreement.run_agreement(self.entity, snapshot_id="0" * 64)


if __name__ == "__main__":
    unittest.main()
