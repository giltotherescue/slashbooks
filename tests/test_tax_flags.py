"""Tax profile, deposit kind, fixed-asset worksheet and missing-input flags in the tax-method view.

The engine stores facts, does the mechanical cash-view math with the recorded deposit kind and lists
missing inputs. It applies no tax rule to the tax profile. Every expected figure is hand-computed in the
comment beside it. The flag-off tests prove that a company without the view gets the same outputs as before.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from golden_support import run_cli  # noqa: E402
from test_accrual_cash_view import (AR, BANK, DEFERRED, DEPOSITS, INSURANCE, PREPAID, SERVICES,  # noqa: E402
                                    SUBSCRIPTIONS, CashViewFixture)

from bookkeeping import taxmap, taxview  # noqa: E402
from bookkeeping.accrual import schedules  # noqa: E402
from bookkeeping.accrual.journals import JournalError  # noqa: E402
from bookkeeping.automation_policy import PreparationDenied  # noqa: E402
from bookkeeping.entity import add_account, load_entity  # noqa: E402
from bookkeeping.ledger.importer import check_integrity  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.preparation_runtime import supported_preparation_command  # noqa: E402
from bookkeeping.readiness import assess_review_readiness, local_dependency_versions  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

D = Decimal
Y2026 = (date(2026, 1, 1), date(2026, 12, 31))
Y2027 = (date(2027, 1, 1), date(2027, 12, 31))
FORFEITED = "Income:Forfeited-Deposits"
EQUIPMENT, ACCUM = "Assets:Fixed:Equipment", "Assets:Fixed:Accumulated-Depreciation"
UNAPPLIED = taxview.DERIVED_ACCOUNTS["~unapplied_customer"]


def books(*argv: str):
    code, out, err = run_cli(list(argv))
    return code, (json.loads(out) if out.strip().startswith(("{", "[")) else out), err


def tb(handoff) -> dict[str, Decimal]:
    return {row["account"]: row["balance"] for row in handoff["trial_balance"]}


def codes(handoff) -> list[str]:
    return [f["code"] for f in handoff["tax_view"]["flags"]]


def flag(handoff, code):
    return next(f for f in handoff["tax_view"]["flags"] if f["code"] == code)


def audit_types(root: Path) -> list[str]:
    return [e["type"] if isinstance(e, dict) else e.type for e in LedgerStore(root / "ledger.sqlite").load_audit_events()]


class Base(CashViewFixture):
    def setUp(self) -> None:
        super().setUp()
        add_account(self.root, FORFEITED, open_date="2025-12-31")

    def cash_filer(self):
        code, result, err = books("entity", "set-tax-method", str(self.root), "--method", "cash", "--tax-view", "on", "--apply")
        self.assertEqual(code, 0, (result, err))
        self.entity = load_entity(self.root)

    def handoff(self, period=Y2026):
        result = taxmap.build_handoff(self.root, *period, form="schc")
        self.assertEqual(result["totals"]["difference"], D("0.00"))
        return result

    def deposit_kind(self, domain: str, record_id: str, kind: str, *, apply: bool = True):
        argv = ["accrual", domain, "deposit-kind", "--entity", str(self.root), "--id", record_id, "--deposit-kind", kind]
        code, result, err = books(*(argv + (["--apply"] if apply else [])))
        self.assertEqual(code, 0, (result, err))
        return result

    def receive(self, op: str, when: str, amount: str, deposit_id: str, who: str = "Vega"):
        return self.settle(op, when, amount, [], excess={"id": deposit_id, "account": DEPOSITS, "counterparty": who})


class TaxProfile(Base):
    def test_preview_apply_idempotent_and_audit(self):
        before = (self.root / "entity.json").read_text()
        code, plan, _ = books("entity", "set-tax-profile", str(self.root), "--prior-return-method", "accrual", "--entity-type", "s_corp")
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "entity.json").read_text(), before)
        self.assertEqual(plan["effects"]["to"], {"prior_return_method": "accrual", "entity_type": "s_corp"})
        self.assertTrue(plan["changes"])
        code, stale, _ = books("entity", "set-tax-profile", str(self.root), "--entity-type", "s_corp", "--apply", "--plan-hash", "0" * 64)
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))
        code, done, _ = books("entity", "set-tax-profile", str(self.root), "--prior-return-method", "accrual", "--entity-type", "s_corp",
                              "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"]), (0, "applied"))
        config = json.loads((self.root / "entity.json").read_text())
        self.assertEqual((config["prior_return_method"], config["entity_type"]), ("accrual", "s_corp"))
        code, same, _ = books("entity", "set-tax-profile", str(self.root), "--entity-type", "s_corp", "--apply")
        self.assertEqual(same["status"], "already_applied")
        self.assertEqual(audit_types(self.root).count("tax-profile-set"), 1)
        # A partial change keeps the other field.
        books("entity", "set-tax-profile", str(self.root), "--prior-return-method", "none", "--apply")
        config = json.loads((self.root / "entity.json").read_text())
        self.assertEqual((config["prior_return_method"], config["entity_type"]), ("none", "s_corp"))
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_enums_are_validated(self):
        self.assertEqual(books("entity", "set-tax-profile", str(self.root), "--entity-type", "llc")[0], 2)
        self.assertEqual(books("entity", "set-tax-profile", str(self.root), "--prior-return-method", "hybrid")[0], 2)
        code, result, _ = books("entity", "set-tax-profile", str(self.root))
        self.assertEqual((code, result["errors"][0]["code"]), (1, "nothing_to_set"))
        with self.assertRaises(JournalError):
            taxview.preview_set_tax_profile(self.entity, entity_type="corp")
        with self.assertRaises(ValueError):
            taxview.tax_profile({"entity_type": "corp"})

    def test_profile_is_listed_in_the_export_and_applies_no_rule(self):
        self.cash_filer()
        before = self.handoff()
        self.assertEqual(before["tax_view"]["tax_profile"], {})
        # A prior accrual return and a C corporation: the engine records and lists the facts. It blocks, warns and decides nothing.
        books("entity", "set-tax-profile", str(self.root), "--prior-return-method", "accrual", "--entity-type", "c_corp", "--apply")
        after = self.handoff()
        self.assertEqual(after["tax_view"]["tax_profile"], {"prior_return_method": "accrual", "entity_type": "c_corp"})
        self.assertEqual(codes(after), codes(before))
        self.assertEqual(tb(after), tb(before))
        code, result, _ = books("entity", "set-tax-method", str(self.root), "--method", "cash", "--tax-view", "on", "--apply")
        self.assertEqual((code, result["status"]), (0, "already_applied"))
        out = Path(self.tmp.name) / "profile-out"
        books("tax", "export", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--output-dir", str(out), "--formats", "csv")
        readme = (out / "README.txt").read_text()
        self.assertIn("Tax profile: entity_type: c_corp", readme)
        self.assertIn("Tax profile: prior_return_method: accrual", readme)

    def test_file_failure_rolls_back_audit(self):
        original = (self.root / "entity.json").read_bytes()
        before = self.store.load_audit_events()
        with patch("os.replace", side_effect=OSError("synthetic rename failure")):
            with self.assertRaises(OSError):
                taxview.apply_set_tax_profile(self.entity, "cash", "sole_proprietor")
        self.assertEqual((self.root / "entity.json").read_bytes(), original)
        self.assertEqual(self.store.load_audit_events(), before)
        self.assertFalse((self.root / "entity.json.tax-profile.tmp").exists())


class PersonOnly(Base):
    def test_preparation_authority_cannot_run_the_setters(self):
        for argv in (["entity", "set-tax-profile", "E", "--entity-type", "s_corp", "--apply"],
                     ["entity", "set-tax-profile", "E", "--entity-type", "s_corp"],
                     ["accrual", "schedule", "deposit-kind", "--entity", "E", "--id", "s", "--deposit-kind", "refundable", "--apply"],
                     ["accrual", "item", "deposit-kind", "--entity", "E", "--id", "i", "--deposit-kind", "refundable", "--apply"]):
            with self.assertRaises(PreparationDenied):
                supported_preparation_command(argv)

    def test_apply_refuses_under_preparation_and_writes_nothing(self):
        self.receive("dep", "2026-02-10", "500.00", "dep")
        before = self.store.content_digest()
        config = (self.root / "entity.json").read_bytes()
        with patch("bookkeeping.preparation_runtime.is_preparation", return_value=True):
            with self.assertRaises(JournalError) as caught:
                schedules.apply_deposit_kind(self.entity, "items", "dep", "refundable")
            self.assertEqual(caught.exception.code, "deposit_kind_person_only")
            with self.assertRaises(JournalError) as caught:
                taxview.apply_set_tax_profile(self.entity, entity_type="s_corp")
            self.assertEqual(caught.exception.code, "tax_profile_person_only")
        self.assertEqual(self.store.content_digest(), before)
        self.assertEqual((self.root / "entity.json").read_bytes(), config)
        self.assertNotIn("deposit_kind", next(i for i in self.store_items() if i["id"] == "dep"))

    def store_items(self):
        from bookkeeping.accrual.store import list_records
        return list_records(LedgerStore(self.root / "ledger.sqlite"), "items")

    def test_remote_contract_classification(self):
        pairs = (["entity", "set-tax-profile", "E", "--entity-type", "s_corp"],
                 ["accrual", "schedule", "deposit-kind", "--entity", "E", "--id", "s", "--deposit-kind", "refundable"],
                 ["accrual", "item", "deposit-kind", "--entity", "E", "--id", "i", "--deposit-kind", "advance_payment"])
        for argv in pairs:
            self.assertFalse(command_spec(argv)["mutates"], argv)
            self.assertTrue(command_spec(argv + ["--apply"])["mutates"], argv)


class DepositKind(Base):
    def test_validation_and_preview_apply_idempotent_audit(self):
        self.receive("dep", "2026-02-10", "500.00", "dep")
        self.item("inv", "receivable", AR, "100.00", "2026-02-01", "Vega", counter=SERVICES)
        self.schedule("ins", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-01-01", "2026-12-31", "equal_month", BANK)
        self.schedule("sub", "deferred", DEFERRED, SUBSCRIPTIONS, "1200.00", "2026-01-01", "2026-12-31", "equal_month", BANK)
        with self.assertRaises(JournalError) as caught:
            schedules.preview_deposit_kind(self.entity, "items", "dep", "security")
        self.assertEqual(caught.exception.code, "invalid_deposit_kind")
        for table, record_id in (("items", "inv"), ("schedules", "ins")):
            with self.assertRaises(JournalError) as caught:
                schedules.preview_deposit_kind(self.entity, table, record_id, "refundable")
            self.assertEqual(caught.exception.code, "deposit_kind_deposits_only")
        self.assertEqual(books("accrual", "item", "deposit-kind", "--entity", str(self.root), "--id", "dep", "--deposit-kind", "x")[0], 2)
        digest = self.store.content_digest()
        plan = self.deposit_kind("item", "dep", "refundable", apply=False)
        self.assertEqual((plan["changes"], plan["effects"]["current_source"]), (True, "assumed"))
        self.assertEqual(self.store.content_digest(), digest)
        code, stale, _ = books("accrual", "item", "deposit-kind", "--entity", str(self.root), "--id", "dep", "--deposit-kind", "refundable",
                               "--apply", "--plan-hash", "0" * 64)
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))
        code, done, _ = books("accrual", "item", "deposit-kind", "--entity", str(self.root), "--id", "dep", "--deposit-kind", "refundable",
                              "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"]), (0, "applied"))
        self.assertEqual(self.deposit_kind("item", "dep", "refundable")["status"], "already_applied")
        self.assertEqual(self.deposit_kind("schedule", "sub", "refundable")["status"], "applied")
        self.assertEqual(audit_types(self.root).count("deposit-kind-set"), 2)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        self.assertEqual(schedules.list_schedules(self.entity)[-1]["deposit_kind"], "refundable")

    def test_receipt_then_application(self):
        # 2026-12-10: Vega pays a 500.00 deposit. 2027-01-10: invoice 500.00. 2027-01-15: the deposit is applied to it.
        self.receive("dep", "2026-12-10", "500.00", "dep")
        self.item("inv", "receivable", AR, "500.00", "2027-01-10", "Vega", counter=SERVICES)
        self.offset("apply", "2027-01-15", "dep", "inv", "500.00")
        self.cash_filer()
        # Unset = advance payment: 2026 income 500.00 on receipt; 2027 the 500.00 moves to Services, net 0.
        y26, y27 = tb(self.handoff()), tb(self.handoff(Y2027))
        self.assertEqual(y26[UNAPPLIED], D("-500.00"))
        self.assertNotIn(DEPOSITS, y26)
        self.assertEqual((y27[SERVICES], y27[UNAPPLIED]), (D("-500.00"), D("500.00")))
        self.assertIn("deposit_kind_not_recorded", codes(self.handoff()))
        self.deposit_kind("item", "dep", "refundable")
        # Refundable: 2026 no income and a 500.00 liability; 2027 Services 500.00 when applied, the liability is 0.
        h26, h27 = self.handoff(), self.handoff(Y2027)
        self.assertNotIn(UNAPPLIED, tb(h26))
        self.assertNotIn(SERVICES, tb(h26))
        self.assertEqual(tb(h26)[DEPOSITS], D("-500.00"))
        self.assertEqual(tb(h27)[SERVICES], D("-500.00"))
        self.assertNotIn(UNAPPLIED, tb(h27))
        deposits = {r["account"]: r for r in h27["trial_balance"]}[DEPOSITS]
        self.assertEqual((deposits["beginning_balance"], deposits["balance"]), (D("-500.00"), D("0.00")))
        self.assertNotIn("deposit_kind_not_recorded", codes(h26))
        self.assertEqual(flag(h26, "deposit_refundable")["refs"], ["dep"])

    def test_receipt_then_forfeiture(self):
        # 2026-06-01: a 300.00 refundable deposit. 2027-03-01: the customer forfeits it (an adjustment into income).
        self.receive("dep", "2026-06-01", "300.00", "dep")
        self.adjust("forfeit", "dep", "credit", "300.00", "2027-03-01", FORFEITED)
        self.cash_filer()
        # Advance payment: 2026 income 300.00; the forfeiture moves no cash, so 2027 has no income.
        self.assertEqual(tb(self.handoff())[UNAPPLIED], D("-300.00"))
        self.assertNotIn(FORFEITED, tb(self.handoff(Y2027)))
        self.deposit_kind("item", "dep", "refundable")
        # Refundable: 2026 a 300.00 liability; 2027 income 300.00 on the forfeiture date.
        self.assertEqual(tb(self.handoff())[DEPOSITS], D("-300.00"))
        self.assertNotIn(UNAPPLIED, tb(self.handoff()))
        y27 = tb(self.handoff(Y2027))
        self.assertEqual(y27[FORFEITED], D("-300.00"))
        self.assertEqual(y27[DEPOSITS], D("0.00"))

    def test_two_receipts_in_different_years_and_a_refund(self):
        # 2026-05-01: 400.00 received. 2027-02-01: 250.00 received. 2027-08-01: the first deposit is refunded.
        self.receive("dep1", "2026-05-01", "400.00", "dep1")
        self.receive("dep2", "2027-02-01", "250.00", "dep2")
        self.settle("refund", "2027-08-01", "-400.00", [("dep1", "400.00")])
        self.cash_filer()
        # Advance payment: income 400.00 in 2026; 2027 income 250.00 - 400.00 refund = -150.00.
        self.assertEqual(tb(self.handoff())[UNAPPLIED], D("-400.00"))
        self.assertEqual(tb(self.handoff(Y2027))[UNAPPLIED], D("150.00"))
        self.deposit_kind("item", "dep1", "refundable")
        self.deposit_kind("item", "dep2", "refundable")
        # Refundable: no income in either year. The liability is 400.00 at the end of 2026 and 250.00 at the end of 2027.
        h26, h27 = self.handoff(), self.handoff(Y2027)
        self.assertNotIn(UNAPPLIED, tb(h26))
        self.assertNotIn(UNAPPLIED, tb(h27))
        self.assertEqual(tb(h26)[DEPOSITS], D("-400.00"))
        row = {r["account"]: r for r in h27["trial_balance"]}[DEPOSITS]
        self.assertEqual((row["beginning_balance"], row["balance"]), (D("-400.00"), D("-250.00")))
        listed = {r["id"]: r for r in h27["tax_view"]["deposits"]}
        self.assertEqual((listed["dep1"]["balance_at_end"], listed["dep2"]["balance_at_end"]), ("0.00", "250.00"))

    def test_refundable_deferred_schedule_is_income_as_recognized(self):
        # 1,200.00 received 2026-10-01 for 2026-10-01 to 2027-09-30, 100.00 a month.
        self.schedule("sub", "deferred", DEFERRED, SUBSCRIPTIONS, "1200.00", "2026-10-01", "2027-09-30", "equal_month", BANK)
        self.due("2026-12-31")
        self.cash_filer()
        self.assertEqual(tb(self.handoff())[SUBSCRIPTIONS], D("-1200.00"))
        self.deposit_kind("schedule", "sub", "refundable")
        # Refundable: 2026 income is the 3 months recognized (300.00); the other 900.00 stays a liability.
        rows = tb(self.handoff())
        self.assertEqual((rows[SUBSCRIPTIONS], rows[DEFERRED]), (D("-300.00"), D("-900.00")))

    def test_export_lists_every_deposit_with_its_kind(self):
        self.receive("dep1", "2026-02-10", "500.00", "dep1")
        self.receive("dep2", "2026-03-10", "200.00", "dep2", who="Rigel")
        self.cash_filer()
        self.deposit_kind("item", "dep2", "refundable")
        handoff = self.handoff()
        listed = {r["id"]: (r["deposit_kind"], r["note"], r["amount"]) for r in handoff["tax_view"]["deposits"]}
        self.assertEqual(listed, {"dep1": ("advance_payment", "assumed advance payment", "500.00"),
                                  "dep2": ("refundable", "recorded", "200.00")})
        self.assertEqual(flag(handoff, "deposit_kind_not_recorded")["refs"], ["dep1"])
        out = Path(self.tmp.name) / "dep-out"
        books("tax", "export", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--output-dir", str(out), "--formats", "csv")
        sheet = (out / "deposits-schc-2026-01-01_2026-12-31.csv").read_text()
        self.assertIn("dep1", sheet)
        self.assertIn("assumed advance payment", sheet)
        self.assertIn("Flag: deposit_kind_not_recorded", (out / "README.txt").read_text())

    def test_readiness_note_only_with_the_view_on(self):
        self.receive("dep", "2026-02-10", "500.00", "dep")

        def warnings():
            document = assess_review_readiness(self.root, "2026-02", local_dependency_versions(self.root), as_of=date(2026, 3, 1)).document
            return [w["code"] for w in document["warnings"]], [b["code"] for b in document["blockers"]]
        self.assertNotIn("deposit_kind_missing", warnings()[0])
        self.cash_filer()
        found, blockers = warnings()
        self.assertIn("deposit_kind_missing", found)
        self.assertNotIn("deposit_kind_missing", blockers)
        self.deposit_kind("item", "dep", "advance_payment")
        self.assertNotIn("deposit_kind_missing", warnings()[0])


class FixedAssetsAndPrepaid(Base):
    def test_fixed_asset_worksheet_rows(self):
        # A 3,600.00 van with a 36-month depreciation schedule, and a 2,500.00 machine bought with no schedule.
        self.schedule("van", "depreciation", EQUIPMENT, "Expenses:Depreciation", "3600.00", "2026-01-01", "2028-12-31",
                      "equal_month", BANK, contra_account=ACCUM)
        self.due("2026-12-31")
        self.journal("machine", "2026-05-04", [(EQUIPMENT, "2500.00"), (BANK, "-2500.00")])
        self.cash_filer()
        rows = {r["id"]: r for r in self.handoff()["tax_view"]["fixed_assets"]}
        van = rows["van"]
        # Book depreciation as recorded: 3,600.00 / 36 x 12 = 1,200.00.
        self.assertEqual((van["placed_in_service"], van["cost"], van["account"], van["book_depreciation_to_date"], van["in_period"]),
                         ("2026-01-01", "3600.00", EQUIPMENT, "1200.00", True))
        machine = next(r for r in rows.values() if r["source"] == "purchase")
        self.assertEqual((machine["placed_in_service"], machine["cost"], machine["book_depreciation_to_date"], machine["depreciation_schedule"]),
                         ("2026-05-04", "2500.00", "", "none"))
        self.assertEqual(len(rows), 2)  # The van purchase entry and its accumulated depreciation are not extra rows.
        self.assertFalse({"size_marker", "tax_depreciation"} & set(van))
        # 2027: still listed, not in the period, depreciation to date 2,400.00 once 2027 is recognized.
        self.due("2027-12-31")
        van27 = next(r for r in self.handoff(Y2027)["tax_view"]["fixed_assets"] if r["id"] == "van")
        self.assertEqual((van27["in_period"], van27["book_depreciation_to_date"]), (False, "2400.00"))
        out = Path(self.tmp.name) / "fa-out"
        books("tax", "export", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--output-dir", str(out), "--formats", "csv")
        sheet = (out / "fixed-assets-schc-2026-01-01_2026-12-31.csv").read_text()
        self.assertIn("Placed In Service (Purchase Date)", sheet)
        self.assertIn("3600.00", sheet)

    def test_prepaid_tax_treatment_not_recorded_lists_only_unset_schedules(self):
        self.schedule("a", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-01-01", "2026-12-31", "equal_month", BANK)
        self.schedule("b", "prepaid", PREPAID, INSURANCE, "600.00", "2026-01-01", "2026-06-30", "equal_month", BANK,
                      tax_treatment="expense_when_paid")
        self.cash_filer()
        self.assertEqual(flag(self.handoff(), "prepaid_tax_treatment_not_recorded")["refs"], ["a"])
        books("accrual", "schedule", "tax-treatment", "--entity", str(self.root), "--id", "a", "--treatment", "capitalize", "--apply")
        self.assertNotIn("prepaid_tax_treatment_not_recorded", codes(self.handoff()))

    def test_no_deposit_flags_without_deposits(self):
        self.schedule("a", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-01-01", "2026-12-31", "equal_month", BANK,
                      tax_treatment="expense_when_paid")
        self.cash_filer()
        handoff = self.handoff()
        self.assertFalse({"deposit_kind_not_recorded", "deposit_refundable", "prepaid_tax_treatment_not_recorded"} & set(codes(handoff)))
        self.assertEqual(handoff["tax_view"]["deposits"], [])


class FlagOffIsUnchanged(Base):
    def test_recorded_fields_without_the_view_change_no_output(self):
        self.receive("dep", "2026-02-10", "500.00", "dep")
        self.schedule("sub", "deferred", DEFERRED, SUBSCRIPTIONS, "1200.00", "2026-10-01", "2027-09-30", "equal_month", BANK)
        self.schedule("van", "depreciation", EQUIPMENT, "Expenses:Depreciation", "3600.00", "2026-01-01", "2028-12-31",
                      "equal_month", BANK, contra_account=ACCUM)
        self.due("2026-12-31")
        commands = (["tax", "map", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--format", "json"],
                    ["report", "pnl", "--entity", str(self.root), "--from", "2026-01-01", "--to", "2026-12-31", "--basis", "cash"])
        before = [run_cli(argv) for argv in commands]
        out_before = Path(self.tmp.name) / "before"
        books("tax", "export", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--output-dir", str(out_before), "--formats", "csv")
        # Record every new fact, and the tax method with the view off.
        self.deposit_kind("item", "dep", "refundable")
        self.deposit_kind("schedule", "sub", "refundable")
        books("entity", "set-tax-profile", str(self.root), "--prior-return-method", "accrual", "--entity-type", "c_corp", "--apply")
        books("entity", "set-tax-method", str(self.root), "--method", "cash", "--apply")
        self.assertEqual([run_cli(argv) for argv in commands], before)
        out_after = Path(self.tmp.name) / "after"
        books("tax", "export", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--output-dir", str(out_after), "--formats", "csv")
        self.assertEqual(sorted(p.name for p in out_before.iterdir()), sorted(p.name for p in out_after.iterdir()))
        for path in sorted(out_before.iterdir()):
            text = path.read_text().replace(str(out_before), "OUT")
            self.assertEqual(text, (out_after / path.name).read_text().replace(str(out_after), "OUT"), path.name)
        self.assertFalse(any(p.name.startswith(("fixed-assets", "deposits")) for p in out_after.iterdir()))


if __name__ == "__main__":
    unittest.main()
