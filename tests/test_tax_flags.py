"""Tax profile, deposit kind, fixed-asset worksheet and missing-input flags in the tax method view.

The engine stores facts, does the mechanical cash-view math with the recorded deposit kind and lists
missing inputs. It applies no tax rule to the tax profile. Every expected figure is hand-computed in the
comment beside it. The no-tax-method tests prove that a company without a tax method gets the same outputs as before.
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
        code, result, err = books("entity", "set-tax-method", str(self.root), "--method", "cash", "--apply")
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
        # A prior accrual return and a C corporation: the engine records and lists the facts. It adds one flag and blocks nothing.
        books("entity", "set-tax-profile", str(self.root), "--prior-return-method", "accrual", "--entity-type", "c_corp", "--apply")
        after = self.handoff()
        self.assertEqual(after["tax_view"]["tax_profile"], {"prior_return_method": "accrual", "entity_type": "c_corp"})
        self.assertEqual([c for c in codes(after) if c != "method_change_possible"], codes(before))
        self.assertEqual(codes(after).count("method_change_possible"), 1)
        self.assertEqual(tb(after), tb(before))
        code, result, _ = books("entity", "set-tax-method", str(self.root), "--method", "cash", "--apply")
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
        self.assertEqual((van["purchase_date"], machine["purchase_date"]), ("2026-01-01", "2026-05-04"))
        self.assertEqual(len(rows), 2)  # The van purchase entry and its accumulated depreciation are not extra rows.
        self.assertFalse({"size_marker", "tax_depreciation"} & set(van))
        # 2027: still listed, not in the period, depreciation to date 2,400.00 once 2027 is recognized.
        self.due("2027-12-31")
        van27 = next(r for r in self.handoff(Y2027)["tax_view"]["fixed_assets"] if r["id"] == "van")
        self.assertEqual((van27["in_period"], van27["book_depreciation_to_date"]), (False, "2400.00"))
        out = Path(self.tmp.name) / "fa-out"
        books("tax", "export", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--output-dir", str(out), "--formats", "csv")
        sheet = (out / "fixed-assets-schc-2026-01-01_2026-12-31.csv").read_text()
        self.assertNotIn("Placed In Service", sheet)
        self.assertIn("Purchase Date,In Period,Account,Cost", sheet)
        self.assertIn("The placed-in-service date is not recorded", sheet)
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


class NoTaxMethodIsUnchanged(Base):
    def test_recorded_fields_without_a_tax_method_change_no_output(self):
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
        # Record every new fact except the tax method. Without a tax method, no tax output changes.
        self.deposit_kind("item", "dep", "refundable")
        self.deposit_kind("schedule", "sub", "refundable")
        books("entity", "set-tax-profile", str(self.root), "--prior-return-method", "accrual", "--entity-type", "c_corp", "--apply")
        self.assertEqual([run_cli(argv) for argv in commands], before)
        out_after = Path(self.tmp.name) / "after"
        books("tax", "export", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--output-dir", str(out_after), "--formats", "csv")
        self.assertEqual(sorted(p.name for p in out_before.iterdir()), sorted(p.name for p in out_after.iterdir()))
        for path in sorted(out_before.iterdir()):
            text = path.read_text().replace(str(out_before), "OUT")
            self.assertEqual(text, (out_after / path.name).read_text().replace(str(out_after), "OUT"), path.name)
        self.assertFalse(any(p.name.startswith(("fixed-assets", "deposits")) for p in out_after.iterdir()))



class ReviewFixes(Base):
    """Regression tests for the fresh-eyes review of the tax-flags pull request."""

    def open_with(self, lines, items):
        from bookkeeping.accrual import opening
        (self.root / "evidence").mkdir(exist_ok=True)
        (self.root / "evidence/open.pdf").write_bytes(b"%PDF synthetic opening")
        plan = opening.preview_opening(self.entity, {"operation_id": "opening", "cutover": "2026-01-01",
                                                     "evidence_refs": ["evidence/open.pdf"], "lines": lines, "items": items})
        self.assertEqual(plan["status"], "ready", plan)
        opening.apply_opening(self.entity, plan)
        self.entity = load_entity(self.root)

    def test_refundable_opening_deposit_is_income_when_applied(self):
        # Opening: bank 30,000.00 and a 400.00 customer deposit held. 2026-04-01 invoice 400.00; 2026-04-05 the deposit is applied.
        self.open_with([{"account": BANK, "amount": "30000.00"}, {"account": DEPOSITS, "amount": "-400.00"},
                        {"account": "Equity:Opening-Balances", "amount": "-29600.00"}],
                       [{"id": "open-dep", "kind": "credit", "account": DEPOSITS, "counterparty": "Vega", "reference": "DEP-1",
                         "open_amount": "400.00", "direction": "credit"}])
        self.item("inv", "receivable", AR, "400.00", "2026-04-01", "Vega", counter=SERVICES)
        self.offset("apply", "2026-04-05", "open-dep", "inv", "400.00")
        self.cash_filer()
        # Advance payment: the deposit was received before the cutover, so applying it places no 2026 income.
        self.assertNotIn(SERVICES, tb(self.handoff()))
        self.deposit_kind("item", "open-dep", "refundable")
        # Refundable: the application is Services income 400.00 in 2026. handoff() asserts the trial balance balances.
        rows = tb(self.handoff())
        self.assertEqual(rows[SERVICES], D("-400.00"))
        self.assertEqual(rows.get(DEPOSITS, D("0.00")), D("0.00"))
        tie = flag(self.handoff(), "difference_from_accrual_view")
        # Accrual 400.00 (the invoice); cash 400.00 (the application): no difference.
        self.assertEqual((tie["accrual_net_income"], tie["cash_net_income"]), ("400.00", "400.00"))

    def test_credit_memo_cannot_be_a_refundable_deposit_and_the_view_refuses_one(self):
        from bookkeeping.reports import cashview
        from bookkeeping.reports.cache import open_cache
        self.item("memo", "credit", DEPOSITS, "100.00", "2026-01-10", "Vega", counter=SERVICES, direction="credit")
        with self.assertRaises(JournalError) as caught:
            schedules.preview_deposit_kind(self.entity, "items", "memo", "refundable")
        self.assertEqual(caught.exception.code, "deposit_kind_credit_memo")
        conn = open_cache(self.root)
        try:
            with self.assertRaisesRegex(cashview.CashViewError, "refundable_deposit_not_cash_backed"):
                cashview.derive(conn, date(2026, 12, 31), refundable={"memo"})
        finally:
            conn.close()

    def test_existing_mode_deposit_receipt(self):
        from bookkeeping.accrual import items
        # A 300.00 receipt posted first (bank 300.00 / deposits), then recorded as a deposit item from that posted entry.
        self.journal("rcpt", "2026-03-01", [(BANK, "300.00"), (DEPOSITS, "-300.00")])
        request = {"operation_id": "create-dep", "id": "dep", "kind": "credit", "account": DEPOSITS, "currency": "USD",
                   "original_amount": "300.00", "as_of": "2026-03-01", "counterparty": "Vega", "direction": "credit",
                   "origin": {"mode": "existing", "source_id": "accrual:rcpt"}, "evidence_ids": [self.ev("dep")]}
        items.apply_item(self.entity, items.preview_item(self.entity, request))
        self.cash_filer()
        # Advance payment: income 300.00 on receipt (the deposit line).
        self.assertEqual(tb(self.handoff())[UNAPPLIED], D("-300.00"))
        self.deposit_kind("item", "dep", "refundable")
        rows = tb(self.handoff())
        self.assertNotIn(UNAPPLIED, rows)
        self.assertEqual(rows[DEPOSITS], D("-300.00"))

    def test_partial_application_then_refund(self):
        # 2026-02-10: 500.00 received. 2026-03-01: invoice 300.00. 2026-03-05: 300.00 applied. 2026-04-01: 200.00 refunded.
        self.receive("dep", "2026-02-10", "500.00", "dep")
        self.item("inv", "receivable", AR, "300.00", "2026-03-01", "Vega", counter=SERVICES)
        self.offset("apply", "2026-03-05", "dep", "inv", "300.00")
        self.settle("refund", "2026-04-01", "-200.00", [("dep", "200.00")])
        self.cash_filer()
        february = (date(2026, 2, 1), date(2026, 2, 28))
        # Advance payment: February income 500.00 on the deposit line.
        self.assertEqual(tb(self.handoff(february))[UNAPPLIED], D("-500.00"))
        self.deposit_kind("item", "dep", "refundable")
        # Refundable: February no income and a 500.00 liability; the year has Services 300.00 and no liability left.
        feb = tb(self.handoff(february))
        self.assertNotIn(UNAPPLIED, feb)
        self.assertEqual(feb[DEPOSITS], D("-500.00"))
        year = tb(self.handoff())
        self.assertEqual(year[SERVICES], D("-300.00"))
        self.assertNotIn(UNAPPLIED, year)
        self.assertEqual(year.get(DEPOSITS, D("0.00")), D("0.00"))

    def test_deposit_list_reads_the_current_kind(self):
        # A deferred schedule changed in 2027 has a dated snapshot for 2026 without the kind recorded later.
        self.schedule("sub", "deferred", DEFERRED, SUBSCRIPTIONS, "1200.00", "2026-10-01", "2027-09-30", "equal_month", BANK)
        self.due("2026-12-31")
        self.change("refund", "sub", "refund", "2027-01-31", amount="200.00", offset_account=BANK)
        self.cash_filer()
        self.deposit_kind("schedule", "sub", "refundable")
        handoff = self.handoff()
        listed = {r["id"]: r for r in handoff["tax_view"]["deposits"]}
        self.assertEqual((listed["sub"]["deposit_kind"], listed["sub"]["note"]), ("refundable", "recorded"))
        self.assertNotIn("deposit_kind_not_recorded", codes(handoff))
        # The math uses the same kind: 2026 income is the 300.00 recognized.
        self.assertEqual(tb(handoff)[SUBSCRIPTIONS], D("-300.00"))

    def test_fixed_assets_skip_reclass_and_accumulated_and_mark_opening(self):
        vehicles, furniture = "Assets:Fixed:Vehicles", "Assets:Fixed:Furniture"
        add_account(self.root, vehicles, open_date="2025-12-31")
        self.journal("reclass", "2026-02-01", [(vehicles, "1000.00"), (EQUIPMENT, "-1000.00")])
        self.journal("accum", "2026-03-01", [(ACCUM, "200.00"), ("Expenses:Depreciation", "-200.00")])
        self.journal("part", "2026-04-01", [(vehicles, "700.00"), (EQUIPMENT, "-200.00"), (BANK, "-500.00")])
        self.cash_filer()
        # An accrual opening of a fixed asset needs a depreciation schedule as support, so the opening entry is
        # added to the rows the worksheet reads: entry 9999 with the accrual-opening metadata.
        real = taxview._entry_rows
        opening_meta = json.dumps([["accrual-opening", "true"]])

        def with_opening(conn, to_date):
            return [(9999, "2025-12-31", "opening", None, opening_meta, furniture, "5000.00"),
                    (9999, "2025-12-31", "opening", None, opening_meta, "Equity:Opening-Balances", "-5000.00"), *real(conn, to_date)]
        with patch.object(taxview, "_entry_rows", with_opening):
            rows = self.handoff()["tax_view"]["fixed_assets"]
        # Opening 5,000.00 is marked opening; the reclass nets to 0 and the accumulated debit is left out;
        # the mixed entry nets 700.00 - 200.00 = 500.00 on the account it debits.
        self.assertEqual([(r["source"], r["account"], r["cost"]) for r in rows],
                         [("opening", furniture, "5000.00"), ("purchase", vehicles, "500.00")])

    def test_tax_treatment_refused_through_the_preparation_path(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from bookkeeping.preparation_runtime import run_preparation_command
        from test_accrual_journals import trusted_context
        request = {"operation_id": "create-ins", "id": "ins", "kind": "prepaid", "account": PREPAID, "recognition_account": INSURANCE,
                   "currency": "USD", "amount": "1200.00", "service_start": "2026-01-01", "service_end": "2026-12-31",
                   "method": "equal_month", "origin": {"mode": "new", "source_id": "ins-payment", "funding_account": BANK},
                   "evidence_ids": [self.ev("ins")], "tax_treatment": "expense_when_paid"}
        path = Path(self.tmp.name) / "schedule.json"
        path.write_text(json.dumps(request))
        before = self.store.content_digest()
        output = StringIO()
        with redirect_stdout(output):
            status = run_preparation_command(["accrual", "schedule", "preview", "--entity", str(self.root), "--input", str(path)],
                                             trusted_context(), entity_path=self.root, expected_ledger_digest=before, source_hashes={})
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())["errors"][0]["code"], "tax_treatment_person_only")
        self.assertEqual(self.store.content_digest(), before)
        # The setter refuses under preparation authority too.
        self.schedule("ins2", "prepaid", PREPAID, INSURANCE, "600.00", "2026-01-01", "2026-06-30", "equal_month", BANK)
        with patch("bookkeeping.preparation_runtime.is_preparation", return_value=True):
            with self.assertRaises(JournalError) as caught:
                schedules.apply_tax_treatment(self.entity, "ins2", "capitalize")
        self.assertEqual(caught.exception.code, "tax_treatment_person_only")

    def test_set_tax_profile_repairs_a_bad_stored_value(self):
        path = self.root / "entity.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "entity_type": "corp"}))
        code, plan, _ = books("entity", "set-tax-profile", str(self.root), "--entity-type", "s_corp")
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan["warnings"][0]["code"], "invalid_stored_value")
        code, done, _ = books("entity", "set-tax-profile", str(self.root), "--entity-type", "s_corp", "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"]), (0, "applied"))
        self.assertEqual(taxview.tax_profile(json.loads(path.read_text())), {"entity_type": "s_corp"})

    def test_xlsx_export_unchanged_without_a_tax_method(self):
        from golden_support import XLSX_AVAILABLE, xlsx_text
        if not XLSX_AVAILABLE:
            self.skipTest("needs the xlsx extra")
        self.receive("dep", "2026-02-10", "500.00", "dep")
        self.schedule("van", "depreciation", EQUIPMENT, "Expenses:Depreciation", "3600.00", "2026-01-01", "2028-12-31",
                      "equal_month", BANK, contra_account=ACCUM)

        def export(name):
            out = Path(self.tmp.name) / name
            taxmap.export_handoff(self.root, *Y2026, form="schc", output_dir=out, formats=("xlsx",))
            return xlsx_text(out / "tax-handoff-schc-2026-01-01_2026-12-31.xlsx")
        before = export("x-before")
        self.deposit_kind("item", "dep", "refundable")
        books("entity", "set-tax-profile", str(self.root), "--entity-type", "c_corp", "--apply")
        self.assertEqual(export("x-after"), before)


class MethodChangeFlag(Base):
    def profile(self, prior: str):
        code, result, err = books("entity", "set-tax-profile", str(self.root), "--prior-return-method", prior, "--apply")
        self.assertEqual(code, 0, (result, err))

    def test_cash_method_after_an_accrual_or_unknown_return_adds_the_flag_and_blocks_nothing(self):
        self.cash_filer()
        for prior in ("accrual", "unknown"):
            self.profile(prior)
            handoff = self.handoff()
            self.assertEqual(codes(handoff).count("method_change_possible"), 1, prior)
            message = flag(handoff, "method_change_possible")["message"]
            self.assertEqual(message, "Accrual books or an unknown prior return method do not show the method on the tax return. "
                             "Confirm the method on the last return. A change from accrual to cash needs IRS consent (Form 3115) "
                             "and a section 481(a) adjustment. The preparer handles it.")
            self.assertNotIn("Rev. Proc", message)
            out = Path(self.tmp.name) / ("mc-" + prior)
            code, _, err = books("tax", "export", "--entity", str(self.root), "--year", "2026", "--form", "schc",
                                 "--output-dir", str(out), "--formats", "csv")
            self.assertEqual(code, 0, err)
            self.assertIn("Flag: method_change_possible", (out / "README.txt").read_text())

    def test_no_flag_for_other_prior_methods_or_for_the_accrual_method(self):
        self.cash_filer()
        self.assertNotIn("method_change_possible", codes(self.handoff()))   # not recorded
        for prior in ("cash", "none"):
            self.profile(prior)
            self.assertNotIn("method_change_possible", codes(self.handoff()), prior)
        self.profile("accrual")
        books("entity", "set-tax-method", str(self.root), "--method", "accrual", "--apply")
        self.assertNotIn("method_change_possible", codes(self.handoff()))


class TaxAssumptions(Base):
    def assumptions(self, period=Y2026):
        return self.handoff(period)["tax_view"]["assumptions"]

    def test_object_has_settings_items_counts_and_the_preparer_list(self):
        self.schedule("a", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-01-01", "2026-12-31", "equal_month", BANK)
        self.schedule("b", "prepaid", PREPAID, INSURANCE, "600.00", "2026-01-01", "2026-06-30", "equal_month", BANK,
                      tax_treatment="expense_when_paid")
        self.receive("dep", "2026-02-10", "500.00", "dep")
        self.cash_filer()
        books("entity", "set-tax-profile", str(self.root), "--prior-return-method", "unknown", "--apply")
        result = self.assumptions()
        self.assertEqual(result["settings"], {
            "tax_method": "cash", "tax_method_source": "recorded in entity.json by a person (books entity set-tax-method)",
            "fiscal_year_start": "01-01", "fiscal_year_start_source": "default", "prior_return_method": "unknown",
            "entity_type": "not recorded", "currency": "USD", "period": "2026-01-01 to 2026-12-31",
            "books_basis": "accrual", "tax_view_used": "cash"})
        names = [item["item"] for item in result["items"]]
        self.assertEqual(names, ["Prepaid expenses", "Customer deposits", "Deferred revenue", "Opening receivables and payables",
                                 "Credit card charges", "Bad debts", "Depreciation, section 179, bonus, de minimis",
                                 "Accrued liabilities", "Inventory", "Tax year"])
        for item in result["items"]:
            self.assertEqual(set(item), {"item", "handling", "source", "preparer_decides"})
            self.assertIn(item["source"].split(" where")[0], {"Recorded by a person", "Assumed default", "Not adjusted"})
        counts = result["counts"]
        self.assertEqual(counts["prepaid_schedules_without_tax_treatment"], 1)
        self.assertEqual(counts["deposits_without_recorded_kind"], 1)
        self.assertEqual(counts["open_flags"]["method_change_possible"], 1)
        self.assertEqual(counts["open_flags"]["deposit_kind_not_recorded"], 1)
        self.assertNotIn("tax_method", counts["open_flags"])
        decides = " ".join(result["preparer_decides"])
        for text in ("Form 3115", "section 481(a)", "election out is made on the return", "inventory method", "trust or escrow",
                     "Do not decide it from an account name"):
            self.assertIn(text, decides)

    def test_text_names_no_dollar_amount_and_no_revenue_procedure(self):
        self.cash_filer()
        text = json.dumps({k: v for k, v in self.assumptions().items() if k != "settings"})
        self.assertNotRegex(text, r"\$|\d{1,3},\d{3}|Rev\. Proc|usually refundable")
        self.assertNotRegex(Path(taxview.__file__).with_name("taxassumptions.py").read_text(), r"\$\s?\d|\d{1,3},\d{3}")

    def test_the_counts_follow_the_books(self):
        self.schedule("a", "prepaid", PREPAID, INSURANCE, "1200.00", "2026-01-01", "2026-12-31", "equal_month", BANK)
        self.cash_filer()
        before = self.assumptions()["counts"]
        self.assertEqual((before["prepaid_schedules_without_tax_treatment"], before["deposits_without_recorded_kind"]), (1, 0))
        books("accrual", "schedule", "tax-treatment", "--entity", str(self.root), "--id", "a", "--treatment", "capitalize", "--apply")
        self.assertEqual(self.assumptions()["counts"]["prepaid_schedules_without_tax_treatment"], 0)

    def test_accrual_method_rows_say_not_adjusted(self):
        self.cash_filer()
        books("entity", "set-tax-method", str(self.root), "--method", "accrual", "--apply")
        result = self.assumptions()
        self.assertEqual(result["settings"]["tax_view_used"], "accrual")
        self.assertNotIn("deposits_without_recorded_kind", result["counts"])
        self.assertNotIn("prepaid_schedules_without_tax_treatment", result["counts"])
        out = Path(self.tmp.name) / "acc-out"
        taxmap.export_handoff(self.root, *Y2026, form="schc", output_dir=out, formats=("csv",))
        readme = (out / "README.txt").read_text()
        self.assertIn("Tax assumptions: The Tax Assumptions sheet", readme)
        self.assertNotIn("Prepaid schedules with no recorded tax treatment", readme)
        deposits = next(i for i in result["items"] if i["item"] == "Customer deposits")
        self.assertEqual(deposits["source"], "Not adjusted")
        self.assertIn("the recorded books are used as they are", deposits["handling"])

    def test_the_sheet_follows_the_read_me_and_the_read_me_has_a_section(self):
        from golden_support import XLSX_AVAILABLE, xlsx_text
        self.schedule("van", "depreciation", EQUIPMENT, "Expenses:Depreciation", "3600.00", "2026-01-01", "2028-12-31",
                      "equal_month", BANK, contra_account=ACCUM)
        self.cash_filer()
        out = Path(self.tmp.name) / "ta-out"
        manifest = taxmap.export_handoff(self.root, *Y2026, form="schc", output_dir=out, formats=("csv", "xlsx"))
        self.assertIn("tax-assumptions-schc-2026-01-01_2026-12-31.csv", manifest["files"])
        self.assertIn("assumptions", manifest["tax_view"])
        csv_text = (out / "tax-assumptions-schc-2026-01-01_2026-12-31.csv").read_text()
        self.assertIn("Item,Handling in this export,Source of the handling,Preparer decides?", csv_text)
        for text in ("Settings in effect", "Input completeness", "Preparer decides", "Flag: depreciation_not_tax_adjusted"):
            self.assertIn(text, csv_text)
        readme = (out / "README.txt").read_text()
        self.assertIn("Tax assumptions: The Tax Assumptions sheet", readme)
        self.assertIn("Preparer decides: ", readme)
        if XLSX_AVAILABLE:
            sheets = [line[len("=== sheet: "):] for line in xlsx_text(out / "tax-handoff-schc-2026-01-01_2026-12-31.xlsx").splitlines()
                      if line.startswith("=== sheet: ")]
            self.assertEqual(sheets[:3], ["Read Me", "Tax Assumptions", "Trial Balance"])

    def test_bad_debts_row_leaves_the_decision_to_the_preparer(self):
        self.cash_filer()
        row = next(i for i in self.assumptions()["items"] if i["item"] == "Bad debts")
        self.assertEqual(row["preparer_decides"], "Yes")
        self.assertIn("credits and true-ups", row["handling"])

    def test_the_tax_map_with_a_period_refuses_an_invalid_profile_like_the_export(self):
        self.cash_filer()
        path = self.root / "entity.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "prior_return_method": "hybrid"}))
        code, _, err = books("tax", "map", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--format", "json")
        self.assertEqual(code, 1)
        self.assertIn("prior_return_method must be one of", err)

    def test_the_same_method_with_an_old_view_key_is_already_applied(self):
        self.cash_filer()
        path = self.root / "entity.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "tax_method_view": True}))
        code, result, _ = books("entity", "set-tax-method", str(self.root), "--method", "cash", "--apply")
        self.assertEqual((code, result["status"]), (0, "already_applied"))

    def test_tax_map_json_carries_the_assumptions(self):
        self.cash_filer()
        code, report, err = books("tax", "map", "--entity", str(self.root), "--year", "2026", "--form", "schc", "--format", "json")
        self.assertEqual(code, 0, err)
        self.assertEqual(report["tax_view"]["assumptions"]["settings"]["tax_view_used"], "cash")
        code, report, _ = books("tax", "map", "--entity", str(self.root), "--form", "schc", "--format", "json")
        self.assertIsNone(report["tax_view"])   # no period: no assumptions


if __name__ == "__main__":
    unittest.main()
