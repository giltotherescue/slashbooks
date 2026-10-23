"""``books ledger reclassify``: move posted income and expense entries to another account.

Each move writes a reversal and a replacement in one ledger transaction. The replacement keeps
payee, tags, links and metadata. A locked month refuses the whole request. A repeated request
writes nothing.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_reporting_goals import BANK, CONSULTING, INSTALLS, CompanyFixture, audit_types, books, entry  # noqa: E402

from bookkeeping.entity import add_account, load_entity  # noqa: E402
from bookkeeping.ledger.importer import check_integrity  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.automation_policy import PreparationDenied  # noqa: E402
from bookkeeping.preparation_runtime import supported_preparation_command  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

REPAIRS = "Income:Services:Repairs"


class ReclassifyTests(CompanyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.post(
            entry("feb", "2026-02-03", "100.00", payee="Acme Homes", tags=("job-12",),
                  meta=(("import-source", "csv"), ("bank-ref", "R1"))),
            entry("mar", "2026-03-04", "250.00", payee="Birch LLC"),
            entry("jan", "2026-01-15", "75.00"),
            entry("apr", "2026-04-02", "30.00", account="Expenses:Software", payee="Tool Inc"),
        )

    def store(self) -> LedgerStore:
        return LedgerStore(self.root / "ledger.sqlite")

    def balances(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for item in self.store().load_entries():
            for posting in item.postings:
                totals[posting.account] += posting.amount
        return dict(totals)

    def reclassify(self, *ids: str, to: str = INSTALLS, frm: str = CONSULTING, extra: tuple[str, ...] = ()):
        argv = ["ledger", "reclassify", "--entity", str(self.root), "--from-account", frm, "--to-account", to]
        for source_id in ids:
            argv += ["--source-id", source_id]
        return books(*argv, *extra)

    def test_preview_writes_nothing_and_apply_moves_entries_keeping_payee_tags_and_meta(self):
        digest = self.store().content_digest()
        code, plan, _ = self.reclassify("feb", "mar")
        self.assertEqual(code, 0, plan)
        self.assertEqual([m["source_id"] for m in plan["moves"]], ["feb", "mar"])
        self.assertEqual([m["amount"] for m in plan["moves"]], ["-100.00", "-250.00"])
        self.assertEqual(plan["moves"][0]["tags"], ["job-12"])
        self.assertEqual(plan["effects"]["entries_written"], 4)
        self.assertEqual(self.store().content_digest(), digest)

        code, stale, _ = self.reclassify("feb", "mar", extra=("--apply", "--plan-hash", "0" * 64))
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))
        code, done, _ = self.reclassify("feb", "mar", extra=("--apply", "--plan-hash", plan["plan_hash"]))
        self.assertEqual((code, done["status"]), (0, "applied"), done)
        self.assertEqual(done["entries_written"], 4)

        balances = self.balances()
        self.assertEqual(balances[CONSULTING], Decimal("-75.00"))  # 100 + 250 left Consulting; 75 stays.
        self.assertEqual(balances[INSTALLS], Decimal("-350.00"))
        self.assertEqual(balances[BANK], Decimal("455.00"))  # The bank side does not change: 100 + 250 + 75 + 30.

        entries = self.store().load_entries()
        new_id = done["moved"][0]["new_source_id"]
        replacement = next(e for e in entries if e.source_id == new_id)
        meta = dict(replacement.meta)
        self.assertEqual((replacement.payee, replacement.tags, replacement.narration), ("Acme Homes", ("job-12",), "Payment feb"))
        self.assertEqual((meta["import-source"], meta["bank-ref"], meta["correction-of"]), ("csv", "R1", "feb"))
        self.assertEqual({p.account for p in replacement.postings}, {BANK, INSTALLS})
        reversal = next(e for e in entries if dict(e.meta).get("reverses") == "feb")
        self.assertEqual((reversal.payee, reversal.tags), ("Acme Homes", ("job-12",)))
        self.assertTrue(any(e.source_id == "feb" for e in entries))  # The original stays.
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_repeated_request_writes_nothing(self):
        self.reclassify("feb", extra=("--apply",))
        digest, audit = self.store().content_digest(), len(audit_types(self.root))
        code, again, _ = self.reclassify("feb", extra=("--apply",))
        self.assertEqual((code, again["status"]), (0, "already_applied"))
        self.assertEqual(again["already_moved"][0]["source_id"], "feb")
        self.assertEqual((self.store().content_digest(), len(audit_types(self.root))), (digest, audit))
        # A request with a moved entry and a new one moves only the new one.
        code, plan, _ = self.reclassify("feb", "mar")
        self.assertEqual(([m["source_id"] for m in plan["moves"]], [a["source_id"] for a in plan["already_moved"]]),
                         (["mar"], ["feb"]))

    def test_moved_entry_can_move_again_by_its_first_source_id(self):
        add_account(self.root, REPAIRS, open_date="2026-01-01")
        self.reclassify("feb", extra=("--apply",))
        code, done, _ = self.reclassify("feb", frm=INSTALLS, to=REPAIRS, extra=("--apply",))
        self.assertEqual((code, done["status"]), (0, "applied"), done)
        self.assertEqual(self.balances()[REPAIRS], Decimal("-100.00"))
        self.assertEqual(self.balances()[INSTALLS], Decimal("0.00"))
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_locked_month_refuses_the_whole_request(self):
        for lock in ({"closed_through": "2026-02-28"}, {"closed_periods": ["2026-02"]}):
            with self.subTest(lock=lock):
                config = self.entity_json()
                config.pop("closed_through", None)
                config.pop("closed_periods", None)
                (self.root / "entity.json").write_text(json.dumps({**config, **lock}), encoding="utf-8")
                digest = self.store().content_digest()
                for apply in ((), ("--apply",)):
                    code, result, _ = self.reclassify("mar", "feb", extra=apply)
                    error = result["errors"][0]
                    self.assertEqual((code, error["code"], error["refs"]), (1, "period_locked", ["feb"]), result)
                    self.assertIn("2026-02", error["message"])
                self.assertEqual(self.store().content_digest(), digest)
                code, plan, _ = self.reclassify("mar")  # An open month still moves.
                self.assertEqual((code, len(plan["moves"])), (0, 1))

    def test_malformed_period_lock_refuses_before_any_move(self):
        bad_values = ({"closed_through": "20260228"}, {"closed_through": "2026-02"}, {"closed_through": 20260228},
                      {"closed_through": "2026-02-30"}, {"closed_through": ""}, {"closed_periods": ["2026-2"]},
                      {"closed_periods": "2026-02"}, {"closed_periods": [202602]})
        clean = self.entity_json()
        digest = self.store().content_digest()
        for lock in bad_values:
            with self.subTest(lock=lock):
                (self.root / "entity.json").write_text(json.dumps({**clean, **lock}), encoding="utf-8")
                for apply in ((), ("--apply",)):
                    code, result, _ = self.reclassify("mar", "feb", extra=apply)
                    self.assertEqual((code, result["errors"][0]["code"]), (1, "invalid_period_state"), result)
                self.assertEqual(self.store().content_digest(), digest)

    def test_posting_metadata_is_kept_on_both_sides(self):
        from bookkeeping.ledger.model import Entry, Posting
        self.post(Entry(date=entry("x", "2026-05-01", "1").date, narration="Job", payee="Oak Co", meta=(("source-id", "meta"),),
                        postings=(Posting(BANK, Decimal("60.00"), meta=(("bank-line", "7"),)),
                                  Posting(CONSULTING, Decimal("-60.00"), meta=(("project", "X"),)))))
        code, done, _ = self.reclassify("meta", extra=("--apply",))
        self.assertEqual(code, 0, done)
        entries = self.store().load_entries()
        reversal = next(e for e in entries if dict(e.meta).get("reverses") == "meta")
        replacement = next(e for e in entries if e.source_id == done["moved"][0]["new_source_id"])
        self.assertEqual({p.account: dict(p.meta) for p in reversal.postings},
                         {BANK: {"bank-line": "7"}, CONSULTING: {"project": "X"}})
        self.assertEqual({p.account: dict(p.meta) for p in replacement.postings},
                         {BANK: {"bank-line": "7"}, INSTALLS: {"project": "X"}})
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_exact_retry_with_the_plan_hash_is_already_applied(self):
        code, plan, _ = self.reclassify("feb", "mar")
        self.assertEqual(self.reclassify("feb", "mar", extra=("--apply", "--plan-hash", plan["plan_hash"]))[1]["status"], "applied")
        digest = self.store().content_digest()
        code, retry, _ = self.reclassify("feb", "mar", extra=("--apply", "--plan-hash", plan["plan_hash"]))
        self.assertEqual((code, retry["status"]), (0, "already_applied"), retry)
        self.assertEqual(self.store().content_digest(), digest)
        # A stale hash still refuses a write.
        code, stale, _ = self.reclassify("jan", extra=("--apply", "--plan-hash", plan["plan_hash"]))
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))

    def test_refusals(self):
        add_account(self.root, "Income:Late-Account", open_date="2026-03-01")
        cases = [
            ("not_category_account", dict(ids=("feb",), to="Assets:Bank:Savings")),
            ("not_category_account", dict(ids=("feb",), frm=BANK)),
            ("same_account", dict(ids=("feb",), to=CONSULTING)),
            ("unknown_account", dict(ids=("feb",), to="Income:Not-Opened")),
            ("unknown_entry", dict(ids=("nope",))),
            ("repeated_entry", dict(ids=("feb", "feb"))),
            ("not_in_from_account", dict(ids=("apr",))),
            ("account_not_open", dict(ids=("feb", "mar"), to="Income:Late-Account")),
        ]
        digest = self.store().content_digest()
        for code_name, case in cases:
            with self.subTest(code_name, **{k: str(v) for k, v in case.items()}):
                code, result, _ = self.reclassify(*case["ids"], to=case.get("to", INSTALLS), frm=case.get("frm", CONSULTING),
                                                  extra=("--apply",))
                self.assertEqual((code, result["errors"][0]["code"]), (1, code_name), result)
        code, result, _ = self.reclassify("feb", "mar", to="Income:Late-Account")
        self.assertEqual(result["errors"][0]["refs"], ["feb"])
        self.assertIn("--open-date 2026-02-03", result["errors"][0]["action"])
        self.assertEqual(self.store().content_digest(), digest)

    def test_reversed_entry_without_replacement_is_refused(self):
        code, out, err = books("ledger", "reverse", "--entity", str(self.root), "--source-id", "jan")
        self.assertEqual(code, 0, (out, err))
        code, result, _ = self.reclassify("jan")
        self.assertEqual((code, result["errors"][0]["code"]), (1, "entry_reversed"))

    def test_preview_is_read_only_for_hosts_and_apply_changes_the_books(self):
        base = ["ledger", "reclassify", "--entity", "E", "--from-account", CONSULTING, "--to-account", INSTALLS, "--source-id", "a"]
        spec = command_spec(base)
        self.assertEqual((spec["mutates"], spec["entity_fields"]), (False, ["entity"]))
        self.assertTrue(command_spec(base + ["--apply"])["mutates"])
        with self.assertRaises(PreparationDenied):  # Standing posting authority does not cover a move.
            supported_preparation_command(base + ["--apply"])

    def test_work_events_report_the_move_as_corrected(self):
        out = self.root.parent / "work.json"
        code, _, err = books("ledger", "reclassify", "--entity", str(self.root), "--from-account", CONSULTING,
                             "--to-account", INSTALLS, "--source-id", "feb", "--apply", "--work-out", str(out))
        self.assertEqual(code, 0, err)
        events = json.loads(out.read_text(encoding="utf-8"))["work"]
        corrected = next(e for e in events if e["kind"] == "corrected")
        self.assertEqual((corrected["counts"]["entries"], corrected["counts"]["reversals"]), (1, 1))
        self.assertNotIn("imported", [e["kind"] for e in events])


if __name__ == "__main__":
    unittest.main()
