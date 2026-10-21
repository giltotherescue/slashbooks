"""Turn accrual on inside an existing company, then choose the default report view.

The proof company is the golden cash company: three months of bank and card activity,
a split, card payments, a reconciliation and a closed January. Every expected number
below is hand-computed in the comment beside it, or compared with the same books before
accrual was turned on.

State setup note: accrual record writes in a company whose default view is cash are
allowed only once the treatment-scope change lands. These tests choose the accrual view
(``entity set-view``) before they write records, which is valid either way.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import golden_capture  # noqa: E402
import golden_companies  # noqa: E402
from golden_support import run_cli  # noqa: E402

from bookkeeping.accrual import cli as accrual_cli  # noqa: E402
from bookkeeping.accrual import enable as accrual_enable  # noqa: E402
from bookkeeping.automation_policy import PreparationDenied  # noqa: E402
from bookkeeping.entity import init_entity, load_entity  # noqa: E402
from bookkeeping.ledger.importer import check_integrity  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.preparation_runtime import supported_preparation_command  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402
from bookkeeping.reports import statements  # noqa: E402

D = "2026-04-01"
AR = "Assets:Receivable:Accounts-Receivable"
AP = "Liabilities:Payable:Accounts-Payable"
EQUITY = "Equity:Opening-Balance-Adjustments"
STARTER = (AR, "Assets:Other:Prepaid-Expenses", "Assets:Fixed:Equipment", "Assets:Fixed:Accumulated-Depreciation", AP,
           "Liabilities:Other:Accrued-Liabilities", "Liabilities:Other:Deferred-Revenue", "Liabilities:Other:Customer-Deposits",
           "Expenses:Depreciation")


def books(*argv: str) -> tuple[int, dict | str, str]:
    code, out, err = run_cli(list(argv))
    return code, (json.loads(out) if out.strip().startswith(("{", "[")) else out), err


class Fixture(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()
        self.root = self.work / "co"
        golden_companies.build_cash(self.root)
        (self.root / "evidence").mkdir(exist_ok=True)
        (self.root / "evidence/open-items.pdf").write_bytes(b"%PDF synthetic list of open invoices and bills")

    def store(self) -> LedgerStore:
        return LedgerStore(self.root / "ledger.sqlite")

    def config(self) -> dict:
        return json.loads((self.root / "entity.json").read_text())

    def enable(self, day: str = D, **flags: str) -> dict:
        code, plan, err = books("accrual", "enable", "preview", "--entity", str(self.root), "--tracking-start", day)
        self.assertEqual(code, 0, (plan, err))
        path = self.work / "enable-plan.json"
        path.write_text(json.dumps(plan))
        code, receipt, err = books("accrual", "enable", "apply", "--entity", str(self.root), "--input", str(path))
        self.assertEqual(code, 0, (receipt, err))
        self.plan_path = path
        return receipt

    def set_view(self, view: str) -> dict:
        code, result, err = books("entity", "set-view", str(self.root), "--view", view, "--apply")
        self.assertEqual(code, 0, (result, err))
        return result

    def apply_opening(self, request: dict, name: str = "opening") -> dict:
        path = self.work / f"{name}-request.json"
        path.write_text(json.dumps(request))
        code, plan, err = books("accrual", "opening", "preview", "--entity", str(self.root), "--input", str(path))
        self.assertEqual((code, plan.get("status")), (0, "ready"), (plan, err))
        plan_path = self.work / f"{name}-plan.json"
        plan_path.write_text(json.dumps(plan))
        code, receipt, err = books("accrual", "opening", "apply", "--entity", str(self.root), "--input", str(plan_path))
        self.assertEqual(code, 0, (receipt, err))
        self.opening_plan_path = plan_path
        return receipt

    OPEN_ITEMS = {"operation_id": "tracking-opening", "evidence_refs": ["evidence/open-items.pdf"], "items": [
        {"id": "inv-1", "kind": "receivable", "account": AR, "counterparty": "Orbit Labs", "reference": "INV-1",
         "due_date": "2026-03-20", "open_amount": "900.00"},
        {"id": "bill-1", "kind": "payable", "account": AP, "counterparty": "Delta Design", "reference": "B-1",
         "due_date": "2026-04-10", "open_amount": "250.00"}]}


class EnableProof(Fixture):
    """Turning accrual on changes nothing a person already sees."""

    # Names whose content legitimately follows the ledger digest, the schema or the company file.
    DIGEST_BOUND = ("state/", "bundle/", "files/entity.json", "files/FILES.txt")

    def test_every_existing_output_is_identical_after_enable(self) -> None:
        before_root, after_root = self.work / "before", self.work / "after"
        before_root.mkdir(), after_root.mkdir()
        golden_companies.build_cash(before_root / "co")
        golden_companies.build_cash(after_root / "co")
        before = golden_capture.capture_company("cash", before_root / "co", before_root)
        self.root = after_root / "co"
        self.enable()
        after = golden_capture.capture_company("cash", self.root, after_root)
        self.assertEqual(sorted(before.files), sorted(after.files))
        changed = [name for name in before.files if before.files[name] != after.files[name]
                   and not name.startswith(self.DIGEST_BOUND)]
        # Two chart listings gain the accrual starter accounts, each with no postings and a 0.00 balance:
        # the routine's category list and the tax line map. Nothing else in them changes.
        listings = ["routine/context.json", "tax/map_json.json", "tax/map_text.txt"]
        self.assertEqual(sorted(name for name in changed if name in listings), listings)
        starter = set(STARTER)
        context_before, context_after = (json.loads(c.files["routine/context.json"]) for c in (before, after))
        self.assertEqual([c for c in context_after["categories"] if c["name"] not in starter], context_before["categories"])
        self.assertEqual({c["name"] for c in context_after["categories"]} - {c["name"] for c in context_before["categories"]}, starter)
        self.assertEqual({k: v for k, v in context_after.items() if k != "categories"},
                         {k: v for k, v in context_before.items() if k != "categories"})
        map_before, map_after = (json.loads(c.files["tax/map_json.json"]) for c in (before, after))
        self.assertEqual([a for a in map_after["accounts"] if a["account"] not in starter], map_before["accounts"])
        self.assertEqual({a["balance"] for a in map_after["accounts"] if a["account"] in starter}, {"0.00"})
        for line in (l for l in after.files["tax/map_text.txt"].splitlines() if l not in before.files["tax/map_text.txt"].splitlines()):
            self.assertTrue(line.startswith(("Unmapped:", "Needs review:")) or any(f"  {a}  0.00 " in line for a in starter), line)
        changed = [name for name in changed if name not in listings]
        # A readiness package is bound to the ledger digest, which now includes the empty accrual tables.
        # Its fingerprint and package id are the only readiness lines that may differ.
        self.assertEqual(sorted(changed), [f"readiness/2026-0{m}.json" for m in (1, 2, 3)])
        for name in changed:
            lines = [(a, b) for a, b in zip(before.files[name].splitlines(), after.files[name].splitlines()) if a != b]
            self.assertEqual({a.split(":")[0].strip() for a, _ in lines}, {'"evidence_fingerprint"', '"package_id"'}, name)
        self.assertEqual(len(before.files[name].splitlines()), len(after.files[name].splitlines()))
        self.assertGreater(len(before.files), 150)  # statements, exports, tax, ask, queue, routine and more were compared

    def test_enable_writes_no_entry_and_keeps_the_default_view(self) -> None:
        entries_before = self.store().load_entries()
        config_before = self.config()
        receipt = self.enable()
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual(self.store().load_entries(), entries_before)
        config = self.config()
        self.assertEqual({k: v for k, v in config.items() if k not in {"accrual_capability_version", "accrual_tracking_start"}},
                         {k: v for k, v in config_before.items() if k != "accrual_capability_version"})
        self.assertEqual((config["accrual_capability_version"], config["accrual_tracking_start"], config["basis"]), (1, D, "cash"))
        store = self.store()
        self.assertEqual((store.schema_version(), store.get_meta("accrual_tracking_start"), store.get_meta("basis")), (2, D, "cash"))
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        events = [e["type"] if isinstance(e, dict) else e.type for e in store.load_audit_events()]
        self.assertIn("accrual-enabled", events)
        self.assertEqual(len(list((self.root / "backups").glob("*schema-v1*"))), 1)


class EnableCommand(Fixture):
    def test_preview_writes_nothing(self) -> None:
        digest, config = self.store().content_digest(), (self.root / "entity.json").read_bytes()
        code, plan, _ = books("accrual", "enable", "preview", "--entity", str(self.root), "--tracking-start", D)
        self.assertEqual(code, 0)
        self.assertEqual((plan["kind"], plan["replayed"], plan["effects"]["capability"], plan["effects"]["reports_changed"]),
                         ("accrual_enable", False, 1, False))
        self.assertEqual((plan["effects"]["ledger_schema"]["from"], plan["effects"]["ledger_schema"]["to"]), (1, 2))
        self.assertEqual((self.store().content_digest(), (self.root / "entity.json").read_bytes()), (digest, config))
        self.assertEqual(list((self.root / "backups").glob("*")), [])

    def test_receipt_replay_and_conflicts(self) -> None:
        first = self.enable()
        code, receipt, _ = books("accrual", "receipt", "--entity", str(self.root), "--id", first["id"])
        self.assertEqual((code, receipt["kind"], receipt["receipt"]["tracking_start"]), (0, "accrual_enable", D))
        events = len(self.store().load_audit_events())
        # The same unchanged preview again: the original receipt, marked as a replay, and nothing new written.
        code, again, _ = books("accrual", "enable", "apply", "--entity", str(self.root), "--input", str(self.plan_path))
        self.assertEqual((code, again["status"], again["plan_hash"]), (0, "already_applied", first["plan_hash"]))
        self.assertEqual(len(self.store().load_audit_events()), events)
        # A fresh preview of the same date is a replay too.
        code, plan, _ = books("accrual", "enable", "preview", "--entity", str(self.root), "--tracking-start", D)
        self.assertEqual((code, plan["replayed"]), (0, True))
        # Another date cannot replace the tracking start.
        code, blocked, _ = books("accrual", "enable", "preview", "--entity", str(self.root), "--tracking-start", "2026-05-01")
        self.assertEqual((code, blocked["errors"][0]["code"]), (1, "tracking_start_conflict"))

    def test_interrupted_apply_is_finished_by_a_repeat(self) -> None:
        self.enable()
        # Simulate a stop after the ledger commit: the company file still shows capability 0.
        config = self.config()
        config["accrual_capability_version"] = 0
        config.pop("accrual_tracking_start")
        (self.root / "entity.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
        code, result, _ = books("accrual", "enable", "apply", "--entity", str(self.root), "--input", str(self.plan_path))
        self.assertEqual((code, result["status"]), (0, "already_applied"))
        self.assertEqual((self.config()["accrual_capability_version"], self.config()["accrual_tracking_start"]), (1, D))

    def test_refusals(self) -> None:
        for day, code_name in (("2026-01-15", "closed_period"), ("not-a-date", "invalid_date")):
            code, blocked, _ = books("accrual", "enable", "preview", "--entity", str(self.root), "--tracking-start", day)
            self.assertEqual((code, blocked["errors"][0]["code"]), (1, code_name), day)
        # Nothing changed.
        self.assertEqual((self.store().schema_version(), self.config()["accrual_capability_version"]), (1, 0))
        # A tampered preview is refused.
        code, plan, _ = books("accrual", "enable", "preview", "--entity", str(self.root), "--tracking-start", D)
        plan["effects"]["capability"] = 0
        path = self.work / "tampered.json"
        path.write_text(json.dumps(plan))
        code, blocked, _ = books("accrual", "enable", "apply", "--entity", str(self.root), "--input", str(path))
        self.assertEqual((code, blocked["errors"][0]["code"]), (1, "plan_changed"))
        # A preview that went stale because the ledger changed is refused.
        plan_path = self.work / "stale.json"
        code, plan, _ = books("accrual", "enable", "preview", "--entity", str(self.root), "--tracking-start", D)
        plan_path.write_text(json.dumps(plan))
        books("entity", "account-add", str(self.root), "--account", "Expenses:Fees-Extra", "--open-date", "2026-01-01")
        code, blocked, _ = books("accrual", "enable", "apply", "--entity", str(self.root), "--input", str(plan_path))
        self.assertEqual((code, blocked["errors"][0]["code"]), (1, "stale_preview"))
        self.assertEqual(self.store().schema_version(), 1)

    def test_company_created_on_accrual_cannot_enable(self) -> None:
        root = self.work / "native"
        init_entity(root, name="Native", basis="accrual", cutover_date="2026-01-01")
        code, blocked, _ = books("accrual", "enable", "preview", "--entity", str(root), "--tracking-start", D)
        self.assertEqual((code, blocked["errors"][0]["code"]), (1, "accrual_already_primary"))

    def test_warns_when_entries_exist_on_or_after_the_start(self) -> None:
        code, plan, _ = books("accrual", "enable", "preview", "--entity", str(self.root), "--tracking-start", "2026-03-01")
        self.assertEqual(code, 0)
        self.assertEqual([w["code"] for w in plan["warnings"]], ["entries_on_or_after_tracking_start"])
        self.assertGreater(plan["effects"]["entries_on_or_after_tracking_start"], 0)


class SetView(Fixture):
    def test_accrual_is_refused_while_capability_is_off(self) -> None:
        code, blocked, _ = books("entity", "set-view", str(self.root), "--view", "accrual")
        self.assertEqual((code, blocked["errors"][0]["code"]), (1, "accrual_not_enabled"))
        code, blocked, _ = books("entity", "set-view", str(self.root), "--view", "accrual", "--apply")
        self.assertEqual((code, blocked["errors"][0]["code"]), (1, "accrual_not_enabled"))
        # Cash on a cash company is already the view.
        code, same, _ = books("entity", "set-view", str(self.root), "--view", "cash", "--apply")
        self.assertEqual((code, same["status"]), (0, "already_applied"))
        self.assertEqual(self.config()["accrual_capability_version"], 0)

    def test_change_touches_only_the_view(self) -> None:
        self.enable()
        entries, digest_entries = self.store().load_entries(), len(self.store().load_entries())
        reports = {p.name: p.read_bytes() for p in (self.root / "reports").glob("*")} if (self.root / "reports").exists() else {}
        config = self.config()
        code, plan, _ = books("entity", "set-view", str(self.root), "--view", "accrual")
        self.assertEqual((code, plan["changes"], plan["effects"]["from"], plan["effects"]["to"]), (0, True, "cash", "accrual"))
        self.assertEqual(self.config(), config)  # preview wrote nothing
        code, stale, _ = books("entity", "set-view", str(self.root), "--view", "accrual", "--apply", "--plan-hash", "0" * 64)
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))
        code, done, _ = books("entity", "set-view", str(self.root), "--view", "accrual", "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"], done["entries_changed"], done["published_statements_changed"]), (0, "applied", 0, False))
        changed = self.config()
        self.assertEqual({k: v for k, v in changed.items() if k != "basis"}, {k: v for k, v in config.items() if k != "basis"})
        self.assertEqual((changed["basis"], self.store().get_meta("basis")), ("accrual", "accrual"))
        self.assertEqual(len(self.store().load_entries()), digest_entries)
        self.assertEqual(self.store().load_entries(), entries)
        self.assertEqual(config["closed_periods"], changed["closed_periods"])
        after = {p.name: p.read_bytes() for p in (self.root / "reports").glob("*")} if (self.root / "reports").exists() else {}
        self.assertEqual(after, reports)
        events = [e["type"] if isinstance(e, dict) else e.type for e in self.store().load_audit_events()]
        self.assertIn("default-view-set", events)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        # The same view again is a no-op; the other direction works while accrual stays on.
        code, same, _ = books("entity", "set-view", str(self.root), "--view", "accrual", "--apply")
        self.assertEqual((code, same["status"]), (0, "already_applied"))
        back = self.set_view("cash")
        self.assertEqual((back["from"], back["view"]), ("accrual", "cash"))
        self.assertEqual(self.config()["basis"], "cash")
        self.assertEqual(self.config()["accrual_capability_version"], 1)

    def test_a_bad_view_is_refused_by_the_parser(self) -> None:
        self.assertEqual(books("entity", "set-view", str(self.root), "--view", "monthly")[0], 2)


class OpeningInsideAnExistingCompany(Fixture):
    def test_opening_items_are_dated_the_tracking_start_and_tie_out(self) -> None:
        self.enable()
        self.set_view("accrual")
        receipt = self.apply_opening(self.OPEN_ITEMS)
        self.assertEqual((receipt["status"], receipt["receipt"]["kind"], receipt["entries_written"]), ("applied", "migration", 1))
        entry = next(e for e in self.store().load_entries() if e.source_id == "tracking-opening")
        self.assertEqual(entry.date, date(2026, 4, 1))
        postings = {p.account: p.amount for p in entry.postings}
        # Receivable 900.00 debit, payable 250.00 credit, balancing 650.00 credit to equity.
        self.assertEqual(postings, {AR: Decimal("900.00"), AP: Decimal("-250.00"), EQUITY: Decimal("-650.00")})
        meta = dict(entry.meta)
        self.assertEqual((meta["accrual-tracking-opening"], meta["coverage-id"]), ("true", "tracking"))
        self.assertNotIn("accrual-opening", meta)
        # The opening cutover record keeps its own meaning: only the tracking record exists.
        from bookkeeping.accrual.store import list_records
        self.assertEqual([r["id"] for r in list_records(self.store(), "coverage")], ["tracking"])
        code, aging, _ = books("accrual", "report", "--entity", str(self.root), "--kind", "aging", "--as-of", "2026-04-30")
        self.assertEqual(code, 0)
        text = books("accrual", "report", "--entity", str(self.root), "--kind", "aging", "--as-of", "2026-04-30", "--format", "text")[1]
        self.assertIn("INV-1", text), self.assertIn("B-1", text)
        self.assertIn("Total receivable | 900.00", text)
        self.assertIn("Total payable | 250.00", text)
        # Every control balance ties to its support, and the books still balance.
        code, tb, _ = books("report", "trial-balance", "--entity", str(self.root), "--as-of", "2026-04-30", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_replay_returns_the_original_receipt(self) -> None:
        self.enable()
        self.set_view("accrual")
        first = self.apply_opening(self.OPEN_ITEMS)
        entries = len(self.store().load_entries())
        code, again, _ = books("accrual", "opening", "apply", "--entity", str(self.root), "--input", str(self.opening_plan_path))
        self.assertEqual((code, again["status"], again["entries_written"]), (0, "already_applied", 0))
        self.assertEqual(again["receipt"]["plan_hash"], first["receipt"]["plan_hash"])
        self.assertEqual(len(self.store().load_entries()), entries)

    def test_request_rules_for_a_tracking_company(self) -> None:
        self.enable()
        self.set_view("accrual")
        path = self.work / "bad.json"
        for patch, code_name in (({"lines": [{"account": AR, "amount": "1.00"}]}, "tracking_lines_derived"),
                                 ({"cutover": "2026-03-01"}, "tracking_cutover_mismatch")):
            path.write_text(json.dumps({**self.OPEN_ITEMS, **patch}))
            code, blocked, _ = books("accrual", "opening", "preview", "--entity", str(self.root), "--input", str(path))
            self.assertEqual((code, blocked["errors"][0]["code"]), (1, code_name))
        # The tracking start itself is accepted.
        path.write_text(json.dumps({**self.OPEN_ITEMS, "cutover": D}))
        code, plan, _ = books("accrual", "opening", "preview", "--entity", str(self.root), "--input", str(path))
        self.assertEqual((code, plan["status"]), (0, "ready"))

    def test_a_closed_period_refuses_the_opening(self) -> None:
        self.enable()
        self.set_view("accrual")
        config = self.config()
        config["closed_periods"] = [*config["closed_periods"], "2026-04"]
        (self.root / "entity.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
        path = self.work / "request.json"
        path.write_text(json.dumps(self.OPEN_ITEMS))
        code, blocked, _ = books("accrual", "opening", "preview", "--entity", str(self.root), "--input", str(path))
        self.assertEqual((code, blocked["errors"][0]["code"]), (1, "closed_period"))
        self.assertFalse(any(e.source_id == "tracking-opening" for e in self.store().load_entries()))

    def test_support_that_does_not_reconcile_blocks_apply(self) -> None:
        self.enable()
        self.set_view("accrual")
        bad = json.loads(json.dumps(self.OPEN_ITEMS))
        bad["items"][0]["open_amount"] = "900.00"
        bad["items"][0]["original_amount"] = "800.00"  # open more than original
        path = self.work / "bad-items.json"
        path.write_text(json.dumps(bad))
        code, blocked, _ = books("accrual", "opening", "preview", "--entity", str(self.root), "--input", str(path))
        self.assertEqual(code, 1)

    def test_draft_for_the_tracking_company(self) -> None:
        self.enable()
        code, draft, _ = books("accrual", "opening", "draft", "--entity", str(self.root), "--cutover", D)
        self.assertEqual((code, draft["request"]["cutover"], "lines" in draft["request"]), (0, D, False))
        code, blocked, _ = books("accrual", "opening", "draft", "--entity", str(self.root), "--cutover", "2026-05-01")
        self.assertEqual((code, blocked["errors"][0]["code"]), (1, "tracking_cutover_mismatch"))


class AccrualViewStartsAtTheTrackingDate(Fixture):
    def test_refuses_before_the_start_and_names_the_first_date(self) -> None:
        self.enable()
        self.set_view("accrual")
        for argv in (["report", "pnl", "--from", "2026-01-01", "--to", "2026-03-31"],
                     ["report", "pnl", "--from", "2026-03-15", "--to", "2026-04-30"],  # straddles the start
                     ["report", "balance-sheet", "--as-of", "2026-03-31"],
                     ["report", "trial-balance", "--as-of", "2026-03-31"]):
            with self.subTest(argv=argv):
                code, out, err = books(*argv, "--entity", str(self.root))
                self.assertEqual(code, 1, out)
                self.assertIn("2026-04-01", err)
                self.assertIn("Accrual figures start on 2026-04-01", err)
                self.assertIn("--basis cash", err)
        # The general ledger is a journal output: the tracking start never refuses it, and before the start it is cash basis.
        code, out, err = books("report", "general-ledger", "--from", "2026-01-01", "--to", "2026-03-31", "--entity", str(self.root))
        self.assertEqual(code, 0, err)
        self.assertIn("Accounting basis: cash", out)
        code, out, err = books("report", "pnl", "--from", D, "--to", "2026-04-30", "--entity", str(self.root))
        self.assertEqual(code, 0, err)
        self.assertIn("Accounting basis: accrual", out)

    def test_ask_before_the_start_is_refused_in_the_accrual_view_but_answered_in_the_cash_view(self) -> None:
        self.enable()
        self.set_view("accrual")
        code, out, err = books("ask", "--entity", str(self.root), "How much did we earn in January?", "--today", "2026-08-05")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("The first available date is 2026-04-01.", err)
        code, out, err = books("ask", "--entity", str(self.root), "How much did we earn in January on a cash basis?", "--today", "2026-08-05")
        self.assertEqual(code, 0, err)
        # January client receipts: 6,500.00 + 3,200.00 = 9,700.00. The 20,000.00 owner contribution is equity.
        self.assertIn("Revenue received (cash basis) from 2026-01-01 to 2026-01-31: 9,700.00", out)

    def test_the_cash_view_never_refuses(self) -> None:
        self.enable()
        self.set_view("accrual")
        code, out, err = books("report", "pnl", "--from", "2026-01-01", "--to", "2026-03-31", "--basis", "cash", "--entity", str(self.root))
        self.assertEqual(code, 0, err)
        # Hand-computed in the cash company's golden fixtures: the cash view equals the recorded books.
        self.assertIn("Cash basis", out)


class ProofCompanyEndToEnd(Fixture):
    """Cash company, three months of activity, accrual turned on, then new records."""

    @staticmethod
    def figures(result) -> dict:
        """Amounts by account row and the two totals. Section titles differ between the views (Money In or Revenue)."""
        rows = {row["label"]: row["amount"] for section in result.sections for row in section["rows"]}
        rows["totals"] = tuple(section["total"] for section in result.sections)
        return rows

    def test_cash_months_before_the_start_equal_the_original_cash_books(self) -> None:
        months = [(date(2026, 1, 1), date(2026, 1, 31)), (date(2026, 2, 1), date(2026, 2, 28)),
                  (date(2026, 3, 1), date(2026, 3, 31)), (date(2026, 1, 1), date(2026, 3, 31))]
        original = [statements.profit_and_loss(self.root, start, end) for start, end in months]
        self.enable()
        self.set_view("accrual")
        self.apply_opening(self.OPEN_ITEMS)
        for (start, end), recorded in zip(months, original):
            with self.subTest(month=start):
                derived = statements.profit_and_loss(self.root, start, end, basis="cash")
                self.assertEqual(self.figures(derived), self.figures(recorded))
        # And with the default view back on cash, the recorded books are what every report reads.
        self.set_view("cash")
        for (start, end), recorded in zip(months, original):
            self.assertEqual(self.figures(statements.profit_and_loss(self.root, start, end)), self.figures(recorded))
            self.assertEqual(self.figures(statements.profit_and_loss(self.root, start, end, basis="cash")), self.figures(recorded))

    def test_opening_items_and_a_new_bill_appear_in_the_accrual_view(self) -> None:
        from bookkeeping.accrual import items, journals
        self.enable()
        self.set_view("accrual")
        self.apply_opening(self.OPEN_ITEMS)
        entity = load_entity(self.root)
        evidence = journals.intake_evidence(entity, {"schema_version": 1, "source": {
            "namespace": "example.synthetic", "account_id": "co", "object_id": "bill-2", "version": "1"},
            "currency": "USD", "observed_facts": {"document": "bill-2"}})["id"]
        request = {"operation_id": "create-bill-2", "id": "bill-2", "kind": "payable", "account": AP, "currency": "USD",
                   "original_amount": "400.00", "as_of": "2026-04-15", "counterparty": "Granite Insurance",
                   "origin": {"mode": "new", "source_id": "bill-2-src", "counter_account": "Expenses:Insurance"},
                   "evidence_ids": [evidence]}
        items.apply_item(entity, items.preview_item(entity, request))
        # April, accrual view: the bill is an expense on its own date, unpaid. Expenses 400.00 (opening items carry none).
        april = statements.profit_and_loss(self.root, date(2026, 4, 1), date(2026, 4, 30))
        self.assertEqual(self.figures(april), {"Expenses › Insurance": Decimal("400.00"), "totals": (Decimal("0.00"), Decimal("400.00"))})
        text = books("accrual", "report", "--entity", str(self.root), "--kind", "outstanding", "--as-of", "2026-04-30", "--format", "text")[1]
        for needle in ("INV-1", "B-1", "Granite Insurance", "400.00"):
            self.assertIn(needle, text)
        # Payables 250.00 opening + 400.00 bill = 650.00; receivables 900.00.
        sheet = books("report", "balance-sheet", "--entity", str(self.root), "--as-of", "2026-04-30")[1]
        self.assertIn("Liabilities › Payable › Accounts-Payable         650.00", sheet)
        self.assertIn("Assets › Receivable › Accounts-Receivable         900.00", sheet)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")


class StarterAccounts(Fixture):
    """`accrual enable` opens the accrual starter accounts that a new accrual company gets."""

    def accounts(self, root: Path | None = None) -> dict[str, tuple[str, str]]:
        with LedgerStore((root or self.root) / "ledger.sqlite").connection() as conn:
            return {row[0]: (row[1], row[2]) for row in conn.execute("SELECT name, currency, open_date FROM accounts")}

    def preview(self, day: str = D) -> tuple[int, dict]:
        code, plan, _ = books("accrual", "enable", "preview", "--entity", str(self.root), "--tracking-start", day)
        return code, plan

    def apply_plan(self, plan: dict, name: str = "plan") -> tuple[int, dict]:
        path = self.work / f"{name}.json"
        path.write_text(json.dumps(plan))
        code, result, _ = books("accrual", "enable", "apply", "--entity", str(self.root), "--input", str(path))
        return code, result

    def test_enable_opens_the_starter_accounts_on_the_start_date(self) -> None:
        before, entries = self.accounts(), self.store().load_entries()
        self.assertFalse(set(STARTER) & set(before))
        code, plan = self.preview()
        self.assertEqual(code, 0)
        effects = plan["effects"]
        self.assertEqual((effects["accounts_to_open"], effects["entries_changed"], effects["reports_changed"]), (list(STARTER), 0, False))
        receipt = self.enable()
        self.assertEqual((receipt["status"], receipt["accounts_opened"], receipt["entries_changed"]), ("applied", list(STARTER), 0))
        after = self.accounts()
        self.assertEqual({name: row for name, row in after.items() if name not in STARTER}, before)
        self.assertEqual({after[name] for name in STARTER}, {("USD", D)})
        self.assertEqual(self.store().load_entries(), entries)
        with self.store().connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM postings WHERE account IN (%s)" % ",".join("?" * len(STARTER)), STARTER).fetchone()[0], 0)
        event = next(e for e in self.store().load_audit_events() if (e["type"] if isinstance(e, dict) else e.type) == "accrual-enabled")
        payload = event["payload"] if isinstance(event, dict) else event.payload
        self.assertEqual(payload["accounts_opened"], list(STARTER))
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        # A replay opens nothing new.
        code, again = books("accrual", "enable", "apply", "--entity", str(self.root), "--input", str(self.plan_path))[:2]
        self.assertEqual((code, again["status"]), (0, "already_applied"))
        self.assertEqual(self.accounts(), after)

    def test_each_business_type_gets_the_accounts_of_a_new_accrual_company(self) -> None:
        from bookkeeping.entity import _STARTER_ACCOUNTS
        for business_type in sorted(_STARTER_ACCOUNTS):
            with self.subTest(business_type=business_type):
                cash, native = self.work / f"cash-{business_type}", self.work / f"native-{business_type}"
                init_entity(cash, name="Cash Co", business_type=business_type)
                init_entity(native, name="Native Co", business_type=business_type, basis="accrual", cutover_date="2026-01-01")
                self.root = cash
                cash_accounts = set(self.accounts())
                self.enable()
                added = set(self.accounts()) - cash_accounts
                self.assertEqual(added, set(self.accounts(native)) - cash_accounts)
                self.assertEqual(added, set(STARTER))
                self.assertEqual(set(self.accounts()), set(self.accounts(native)))

    def test_existing_starter_accounts_are_left_alone(self) -> None:
        code, _, err = books("entity", "account-add", str(self.root), "--account", AR, "--open-date", "2026-01-01")
        self.assertEqual(code, 0, err)
        existing = self.accounts()[AR]
        code, plan = self.preview()
        self.assertEqual(plan["effects"]["accounts_to_open"], [name for name in STARTER if name != AR])
        code, receipt = self.apply_plan(plan)
        self.assertEqual((code, receipt["status"]), (0, "applied"))
        self.assertEqual(self.accounts()[AR], existing)
        self.assertEqual({self.accounts()[name] for name in STARTER if name != AR}, {("USD", D)})

    def test_a_clashing_account_refuses_enable(self) -> None:
        clashes = ((["--account", "Liabilities:Other:Prepaid-Expenses"], "Assets:Other:Prepaid-Expenses"),
                   (["--account", AR, "--currency", "EUR"], AR))
        for index, (flags, named) in enumerate(clashes):
            with self.subTest(flags=flags):
                self.root = self.work / f"clash-{index}"
                golden_companies.build_cash(self.root)
                code, _, err = books("entity", "account-add", str(self.root), *flags, "--open-date", "2026-01-01")
                self.assertEqual(code, 0, err)
                accounts, config = self.accounts(), self.config()
                code, blocked = self.preview()
                self.assertEqual((code, blocked["errors"][0]["code"]), (1, "starter_account_clash"))
                self.assertIn(named, blocked["errors"][0]["message"])
                self.assertEqual((self.accounts(), self.config(), self.store().schema_version()), (accounts, config, 1))

    def test_plan_hash_covers_the_accounts_and_a_stale_preview_is_refused(self) -> None:
        code, plan = self.preview()
        tampered = json.loads(json.dumps(plan))
        tampered["effects"]["accounts_to_open"] = tampered["effects"]["accounts_to_open"][1:]
        code, blocked = self.apply_plan(tampered, "tampered")
        self.assertEqual((code, blocked["errors"][0]["code"]), (1, "plan_changed"))
        # One of the accounts appears after the preview: the preview is stale.
        books("entity", "account-add", str(self.root), "--account", "Assets:Other:Prepaid-Expenses", "--open-date", "2026-01-01")
        code, blocked = self.apply_plan(plan, "stale")
        self.assertEqual((code, blocked["errors"][0]["code"]), (1, "stale_preview"))
        self.assertEqual(self.store().schema_version(), 1)
        self.assertNotIn(AR, self.accounts())

    CASH_VIEW = (["report", "balance-sheet", "--as-of", "2026-04-30", "--basis", "cash"],
                 ["report", "trial-balance", "--as-of", "2026-04-30", "--basis", "cash"],
                 ["report", "pnl", "--from", "2026-01-01", "--to", "2026-04-30", "--basis", "cash"],
                 ["report", "balance-sheet", "--as-of", "2026-03-31", "--basis", "cash", "--format", "json"])

    def cash_view(self) -> list:
        results = []
        for argv in self.CASH_VIEW:
            code, out, err = books(*argv, "--entity", str(self.root))
            self.assertEqual(code, 0, err)
            results.append(out)
        return results

    def test_cash_reports_are_identical_after_enable(self) -> None:
        before = self.cash_view()
        self.enable()
        self.assertEqual(self.cash_view(), before)

    def test_turned_on_later_then_opening_items_end_to_end(self) -> None:
        """The hosted flow: a cash company turns accrual on, then enters open invoices and bills."""
        months = [(date(2026, m, 1), date(2026, m, 28)) for m in (1, 2, 3, 4)] + [(date(2026, 1, 1), date(2026, 4, 30))]
        figures = ProofCompanyEndToEnd.figures

        def cash_figures() -> list:
            return [figures(statements.profit_and_loss(self.root, start, end, basis="cash")) for start, end in months]
        cash_before = cash_figures()
        self.enable()
        request = self.work / "opening.json"
        request.write_text(json.dumps(self.OPEN_ITEMS))
        code, plan, err = books("accrual", "opening", "preview", "--entity", str(self.root), "--input", str(request))
        self.assertEqual((code, plan.get("status"), plan.get("accounts_to_open")), (0, "ready", []), err)
        receipt = self.apply_opening(self.OPEN_ITEMS)
        self.assertEqual((receipt["status"], receipt["entries_written"]), ("applied", 1))
        # The cash view is unchanged (a balance sheet with receivables and payables has no cash view);
        # the accrual view shows the open invoice and bill.
        self.assertEqual(cash_figures(), cash_before)
        code, sheet, err = books("report", "balance-sheet", "--as-of", "2026-04-30", "--basis", "accrual", "--entity", str(self.root))
        self.assertEqual(code, 0, err)
        self.assertIn("Assets › Receivable › Accounts-Receivable         900.00", sheet)
        self.assertIn("Liabilities › Payable › Accounts-Payable         250.00", sheet)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_a_company_enabled_without_the_accounts_gets_them_from_the_opening(self) -> None:
        from unittest import mock
        with mock.patch.object(accrual_enable, "accrual_starter_accounts", return_value=()):
            receipt = self.enable()
        self.assertEqual(receipt["accounts_opened"], [])
        self.assertFalse(set(STARTER) & set(self.accounts()))
        # Enable again with the same date: a replay that changes nothing.
        code, plan = self.preview()
        self.assertEqual((code, plan["replayed"], plan["effects"]["accounts_to_open"]), (0, True, []))
        code, again = self.apply_plan(plan, "again")
        self.assertEqual((code, again["status"]), (0, "already_applied"))
        self.assertFalse(set(STARTER) & set(self.accounts()))
        # The opening preview adds the missing starter accounts, dated the start date.
        request = self.work / "opening.json"
        request.write_text(json.dumps(self.OPEN_ITEMS))
        code, preview, err = books("accrual", "opening", "preview", "--entity", str(self.root), "--input", str(request))
        self.assertEqual((code, preview.get("accounts_to_open")), (0, list(STARTER)), err)
        self.apply_opening(self.OPEN_ITEMS)
        accounts = self.accounts()
        self.assertEqual({accounts[name] for name in STARTER}, {("USD", D)})
        entry = next(e for e in self.store().load_entries() if e.source_id == "tracking-opening")
        self.assertEqual({p.account: p.amount for p in entry.postings},
                         {AR: Decimal("900.00"), AP: Decimal("-250.00"), EQUITY: Decimal("-650.00")})
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_human_messages_say_start_date(self) -> None:
        code, plan = self.preview("2026-03-01")
        message = plan["warnings"][0]["message"]
        self.assertIn("start date", message)
        self.assertNotIn("tracking start", message)
        self.assertNotIn("2026-03-01", message)
        self.assertEqual(plan["warnings"][0]["code"], "entries_on_or_after_tracking_start")


class CommandSurface(unittest.TestCase):
    def test_schema_and_capabilities_describe_the_new_commands(self) -> None:
        contracts = accrual_cli.contracts()
        self.assertIn("enable", contracts["requests"])
        self.assertIn("tracking_opening", contracts["requests"])
        self.assertTrue(any(line.startswith("accrual enable preview ") and "--tracking-start YYYY-MM-DD" in line for line in contracts["commands"]))
        self.assertTrue(any(line.startswith("accrual enable apply ") for line in contracts["commands"]))
        _, verbs = accrual_cli._command_lines()
        self.assertEqual(verbs["enable"], ["preview", "apply"])

    def test_remote_contract_classifies_enable_and_set_view(self) -> None:
        for verb, mutates in (("preview", False), ("apply", True)):
            argv = ["accrual", "enable", verb, "--entity", "@entity"] + (["--tracking-start", D] if verb == "preview" else ["--input", "@input/p.json"])
            with self.subTest(verb=verb):
                self.assertEqual(command_spec(argv)["mutates"], mutates)
        self.assertFalse(command_spec(["entity", "set-view", "@entity", "--view", "accrual"])["mutates"])
        self.assertTrue(command_spec(["entity", "set-view", "@entity", "--view", "accrual", "--apply"])["mutates"])

    def test_delegated_agents_can_preview_but_never_apply(self) -> None:
        self.assertEqual(supported_preparation_command(["accrual", "enable", "preview", "--entity", "@e", "--tracking-start", D]).effect, "read")
        for argv in (["accrual", "enable", "apply", "--entity", "@e", "--input", "@input/p.json"],
                     ["entity", "set-view", "@e", "--view", "accrual", "--apply"]):
            with self.subTest(argv=argv), self.assertRaises(PreparationDenied):
                supported_preparation_command(argv)


if __name__ == "__main__":
    unittest.main()
