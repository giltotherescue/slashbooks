"""Remove an unused bank or card account, and undo a bank account link (``bank-account-unmap``).

Both go through the real CLI. An account that anything uses stays. A feed account that is unmapped imports no new
row, keeps every imported row, and can be mapped again without a duplicate.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from golden_support import run_cli  # noqa: E402

from bookkeeping.entity import add_account, load_entity  # noqa: E402
from bookkeeping.ledger.importer import _atomic_ledger_write, check_integrity  # noqa: E402
from bookkeeping.ledger.model import Balance, Entry, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402

CHECKING = "Assets:Bank:Checking"
CARD = "Liabilities:CreditCard"
SAVINGS = "Assets:Bank:Savings"


def books(*argv: str):
    code, out, err = run_cli(list(argv))
    return code, (json.loads(out) if out.strip().startswith(("{", "[")) else out), err


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "company"
        code, out, err = run_cli(["entity", "init", str(self.root), "--name", "Test Co", "--cutover-date", "2026-01-01"])
        self.assertEqual(code, 0, (out, err))
        self.store = LedgerStore(self.root / "ledger.sqlite")

    def catalog(self) -> set[str]:
        return {opened.account for opened in self.store.load_opens()}

    def events(self, kind: str):
        return [event for event in self.store.load_audit_events() if event["type"] == kind]

    def mapping(self, feed_id="acct_1", account=CHECKING, *extra):
        code, out, err = run_cli(["entity", "bank-account-map", str(self.root), "--feed-account-id", feed_id,
                                  "--account", account, "--provider", "plaid", *extra])
        self.assertEqual(code, 0, (out, err))

    def config(self):
        return json.loads((self.root / "entity.json").read_text())


class BankAccountRemoveTests(Base):
    def remove(self, account, *extra):
        return books("entity", "account-remove", str(self.root), "--account", account, *extra)

    def refused(self, code_name, account=CHECKING):
        before = (self.store.content_digest(), len(self.store.load_audit_events()), self.catalog())
        for extra in ((), ("--apply",)):
            code, result, _ = self.remove(account, *extra)
            self.assertEqual((code, result["errors"][0]["code"]), (1, code_name), result)
        self.assertEqual((self.store.content_digest(), len(self.store.load_audit_events()), self.catalog()), before)
        return result

    def post(self, account=CHECKING):
        _atomic_ledger_write(load_entity(self.root), [], [Entry(
            date=date(2026, 2, 3), narration="Sale", meta=(("source-id", "s1"),),
            postings=(Posting(account, Decimal("80.00")), Posting("Income:Consulting", Decimal("-80.00"))))],
            "test-seed", None, "seed")

    def test_unused_starter_bank_and_card_accounts_are_removed_and_audited(self):
        for account, kind in ((CHECKING, "bank"), (CARD, "credit_card")):
            code, plan, _ = self.remove(account)
            self.assertEqual((code, plan["changes"], plan["effects"]["financial_kind"]), (0, True, kind), plan)
            code, done, _ = self.remove(account, "--apply", "--plan-hash", plan["plan_hash"])
            self.assertEqual((code, done["status"], done["entries_changed"]), (0, "applied", 0), done)
            self.assertNotIn(account, self.catalog())
        removed = [(e["payload"]["account"], e["payload"]["financial_kind"]) for e in self.events("account-removed")]
        self.assertEqual(removed, [(CHECKING, "bank"), (CARD, "credit_card")])
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        # An exact retry changes nothing.
        code, retry, _ = self.remove(CHECKING, "--apply")
        self.assertEqual((code, retry["status"]), (0, "already_applied"))

    def test_a_bank_account_that_account_add_created_with_a_kind_is_removed(self):
        add_account(self.root, SAVINGS, open_date="2026-01-01", financial_kind="bank")
        code, done, _ = self.remove(SAVINGS, "--apply")
        self.assertEqual((code, done["status"]), (0, "applied"), done)
        self.assertNotIn(SAVINGS, self.catalog())

    def test_account_with_a_posting_is_refused(self):
        self.post()
        self.refused("account_in_use")

    def test_account_with_a_balance_assertion_is_refused(self):
        with self.store.transaction() as conn:
            self.store.insert_balances([Balance(date(2026, 3, 1), CHECKING, Decimal("0.00"))], conn)
        self.refused("account_in_use")

    def test_account_with_a_bank_mapping_is_refused_until_the_mapping_is_removed(self):
        self.mapping()
        result = self.refused("account_mapped")
        self.assertEqual(result["errors"][0]["refs"], ["entity.json:bank_account_mappings.acct_1"])
        code, out, err = run_cli(["entity", "bank-account-unmap", str(self.root), "--feed-account-id", "acct_1"])
        self.assertEqual(code, 0, (out, err))
        code, done, _ = self.remove(CHECKING, "--apply")
        self.assertEqual((code, done["status"]), (0, "applied"), done)

    def test_account_with_a_csv_mapping_is_refused(self):
        config = self.config()
        config["csv_account_mappings"] = {"key1": {"ledger_account": CHECKING, "confirmed": True}}
        (self.root / "entity.json").write_text(json.dumps(config))
        self.refused("account_mapped")

    def test_account_named_by_a_statement_record_is_refused(self):
        from bookkeeping.coverage import record_statement
        record_statement(self.root, {"statement_id": "s-jan", "account": CHECKING, "kind": "statement",
                                     "period_start": "2026-01-01", "period_end": "2026-01-31",
                                     "opening_balance": "0.00", "closing_balance": "0.00"})
        self.refused("account_referenced")

    def test_account_named_by_a_setting_or_a_staged_row_is_refused(self):
        (self.root / "learned-context" / "rules.json").write_text(json.dumps({"rule": {"account": CHECKING}}))
        self.refused("account_referenced")
        (self.root / "learned-context" / "rules.json").unlink()
        (self.root / "staging" / "context-history.json").write_text(json.dumps({"rows": [{"account": CHECKING}]}))
        self.refused("account_referenced")

    def test_other_accounts_stay(self):
        add_account(self.root, "Assets:Other-Holdings", open_date="2026-01-01")          # No financial kind.
        add_account(self.root, "Assets:Prepaid", open_date="2026-01-01", financial_kind="other")
        for account in ("Equity:Opening-Balances", "Assets:Other-Holdings", "Assets:Prepaid"):
            with self.subTest(account):
                self.refused("protected_account", account)

    def test_text_files_that_name_an_account_refuse_its_removal_for_every_account_type(self):
        add_account(self.root, "Income:Installs", open_date="2026-01-01")
        for folder, name, account in (("learned-context", "rules.md", CHECKING), ("procedures", "close.md", CARD),
                                      ("learned-context", "notes.txt", "Income:Installs")):
            (self.root / folder).mkdir(exist_ok=True)
            (self.root / folder / name).write_text(f"Reconcile {account} monthly.\n")
            result = self.refused("account_referenced", account)
            self.assertTrue(any(f"{folder}/{name}" in ref for ref in result["errors"][0]["refs"]), result)
        # A longer account name or a child account is not a mention.
        (self.root / "learned-context" / "rules.md").write_text(
            "Use Assets:Bank:Checking-Old and Assets:Bank:Checking:Sub.\n")
        code, result, _ = self.remove(CHECKING)
        self.assertEqual((code, result["changes"]), (0, True), result)


class BankAccountUnmapTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.mapping("acct_1", CHECKING, "--cutover-date", "2026-08-01", "--history-before-cutover", "context_only",
                     "--last4", "0021")

    def unmap(self, feed_id="acct_1"):
        return run_cli(["entity", "bank-account-unmap", str(self.root), "--feed-account-id", feed_id])

    def row(self, row_id, day="2026-08-03", amount=6.0, account=None, **extra):
        return {"id": row_id, "accountId": account or extra.pop("accountId", "acct_1"), "date": day, "amount": amount,
                "description": "feed row", **extra}

    def ingest(self, rows):
        path = Path(self.tmp.name) / "rows.json"
        path.write_text(json.dumps(rows))
        return run_cli(["ingest", str(path), "--entity", str(self.root), "--no-feed-window", "--source", "plaid"])

    def confirm(self, source_id):
        code, out, err = run_cli(["queue", "propose", "--entity", str(self.root), "--source-id", source_id,
                                  "--category", "Income:Consulting", "--reasoning", "test"])
        self.assertEqual(code, 0, (out, err))
        code, out, err = run_cli(["queue", "confirm", "--entity", str(self.root), "--item", source_id.replace(":", "_")])
        self.assertEqual(code, 0, (out, err))

    def entry_ids(self):
        with self.store.connection() as conn:
            return sorted(sid for (sid,) in conn.execute("SELECT source_id FROM entries WHERE source_id IS NOT NULL"))

    def test_unmap_keeps_imported_rows_stops_new_rows_and_records_the_mapping(self):
        self.ingest([self.row("a1")])
        self.confirm("plaid:a1")
        code, out, err = self.unmap()
        self.assertEqual(code, 0, (out, err))
        self.assertIn("acct_1", out)
        self.assertNotIn("acct_1", self.config().get("bank_account_mappings", {}))
        self.assertEqual(self.entry_ids(), ["plaid:a1"])  # The imported row stays.
        code, out, err = self.ingest([self.row("a2")])      # No new row of the feed account is imported.
        self.assertEqual(code, 0, (out, err))
        self.assertIn("skipped, feed account unmapped: acct_1: 1 row(s)", out)
        self.assertEqual(self.entry_ids(), ["plaid:a1"])
        self.assertEqual(json.loads((self.root / "staging" / "pending-categorization.json").read_text())
                         if (self.root / "staging" / "pending-categorization.json").exists() else [], [])
        event = self.events("bank-account-unmapped")[-1]["payload"]
        self.assertEqual(event["feed_account_id"], "acct_1")
        self.assertEqual(event["mapping"], {"ledger_account": CHECKING, "provider": "plaid", "last4": "0021",
                                            "cutover_date": "2026-08-01", "history_before_cutover": "context_only",
                                            "provider_account_id": "acct_1"})
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        self.assertEqual(self.store.verify_audit_chain(), [])

    def test_a_row_waiting_in_staging_refuses_the_unmap_until_it_is_decided(self):
        self.ingest([self.row("w1"), self.row("w2", "2026-08-04", pending=True)])
        before = (self.config(), len(self.store.load_audit_events()))
        code, out, err = self.unmap()
        self.assertEqual(code, 1)
        self.assertIn("2 row(s) waiting", err)
        self.assertEqual((self.config(), len(self.store.load_audit_events())), before)
        self.confirm("plaid:w1")
        code, out, err = self.unmap()  # The pending row still waits.
        self.assertEqual(code, 1, (out, err))
        pending = self.root / "staging" / "pending.json"
        pending.write_text("[]")
        code, out, err = self.unmap()
        self.assertEqual(code, 0, (out, err))

    def test_context_only_rows_stay_as_they_are(self):
        self.ingest([self.row("old", "2026-07-03")])
        history = self.root / "staging" / "context-history.json"
        before = history.read_bytes()
        self.assertTrue(before)
        code, out, err = self.unmap()
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(history.read_bytes(), before)

    def test_unknown_feed_account_and_empty_id_are_refused(self):
        for feed_id in ("nope", " "):
            code, out, err = self.unmap(feed_id)
            self.assertEqual(code, 1, (out, err))
        self.assertEqual(self.events("bank-account-unmapped"), [])

    def test_remap_to_the_same_or_another_account_imports_no_duplicate(self):
        add_account(self.root, SAVINGS, open_date="2026-01-01", financial_kind="bank")
        self.ingest([self.row("a1")])
        self.confirm("plaid:a1")
        self.assertEqual(self.unmap()[0], 0)
        for account in (CHECKING, SAVINGS):
            self.mapping("acct_1", account, "--restore-cutover")
            code, out, err = self.ingest([self.row("a1")])
            self.assertEqual(code, 0, (out, err))
            self.assertIn("duplicates skipped:  1", out)
            self.assertEqual(self.entry_ids(), ["plaid:a1"])
            self.assertEqual(self.unmap()[0], 0)
        # A new row after the re-map is imported to the new account.
        self.mapping("acct_1", SAVINGS, "--restore-cutover")
        self.assertEqual(self.ingest([self.row("a3", "2026-08-09")])[0], 0)
        self.confirm("plaid:a3")
        self.assertEqual(self.entry_ids(), ["plaid:a1", "plaid:a3"])

    def test_an_unmapped_account_can_then_be_removed(self):
        self.assertEqual(self.unmap()[0], 0)
        code, done, _ = books("entity", "account-remove", str(self.root), "--account", CHECKING, "--apply")
        self.assertEqual((code, done["status"]), (0, "applied"), done)


    def test_a_never_mapped_feed_account_still_refuses_the_whole_ingest_and_an_unmapped_one_is_skipped(self):
        self.assertEqual(self.unmap()[0], 0)
        stranger = {**self.row("z1"), "accountId": "acct_never"}
        code, out, err = self.ingest([self.row("a2"), stranger])
        self.assertEqual(code, 1, (out, err))
        self.assertIn("acct_never", err)
        self.assertEqual(self.entry_ids(), [])
        code, out, err = self.ingest([self.row("a2"), self.row("a3")])  # Only the unmapped account: skipped, reported.
        self.assertEqual(code, 0, (out, err))
        self.assertIn("acct_1: 2 row(s)", out)

    def test_a_remap_after_an_unmap_with_a_cutover_needs_an_explicit_decision(self):
        # Rows before the cutover are history only; after the unmap they must not come back as new rows.
        history = [self.row("h1", "2026-07-10", 100.0), self.row("p1", "2026-08-10", 5.0)]
        self.assertEqual(self.ingest(history)[0], 0)
        self.confirm("plaid:p1")
        self.assertEqual(self.unmap()[0], 0)
        code, out, err = run_cli(["entity", "bank-account-map", str(self.root), "--feed-account-id", "acct_1",
                                  "--account", CHECKING, "--provider", "plaid"])
        self.assertEqual(code, 1, (out, err))
        self.assertIn("cutover date 2026-08-01", err)
        self.assertNotIn("acct_1", self.config().get("bank_account_mappings", {}))
        # An explicit restore keeps the cutover and the history setting; the history rows post nothing.
        self.mapping("acct_1", CHECKING, "--restore-cutover")
        mapping = self.config()["bank_account_mappings"]["acct_1"]
        self.assertEqual((mapping["cutover_date"], mapping["history_before_cutover"]), ("2026-08-01", "context_only"))
        code, out, err = self.ingest(history)
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(self.entry_ids(), ["plaid:p1"])
        self.assertFalse((self.root / "staging" / "pending-categorization.json").exists()
                         and json.loads((self.root / "staging" / "pending-categorization.json").read_text()))
        # Clearing the cutover is an explicit decision too.
        self.assertEqual(self.unmap()[0], 0)
        self.mapping("acct_1", CHECKING, "--clear-cutover-date")
        self.assertNotIn("cutover_date", self.config()["bank_account_mappings"]["acct_1"])

    def test_an_unmapped_bank_account_with_postings_in_the_period_is_still_required_by_readiness(self):
        add_account(self.root, SAVINGS, open_date="2026-01-01", financial_kind="bank")
        self.mapping("acct_2", SAVINGS)
        self.assertEqual(self.ingest([{**self.row("r1", "2026-08-03", 6.0), "accountId": "acct_2"}])[0], 0)
        self.confirm("plaid:r1")

        def blockers():
            code, out, err = run_cli(["readiness", "--entity", str(self.root), "--period", "2026-08",
                                      "--as-of", "2026-10-20"])
            document = json.loads(out)
            return [item for item in document["blockers"] if SAVINGS in json.dumps(item) or "acct_2" in json.dumps(item)]

        before = blockers()
        self.assertTrue(before, "the mapped account needs coverage")
        self.assertEqual(run_cli(["entity", "bank-account-unmap", str(self.root), "--feed-account-id", "acct_2"])[0], 0)
        self.assertEqual(sorted(item["code"] for item in blockers()), sorted(item["code"] for item in before))
        # A bank account with no postings in the period and no mapping is not required.
        add_account(self.root, "Assets:Bank:Idle", open_date="2026-01-01", financial_kind="bank")
        code, out, err = run_cli(["readiness", "--entity", str(self.root), "--period", "2026-08", "--as-of", "2026-10-20"])
        self.assertNotIn("Assets:Bank:Idle", out)

    def readiness_codes(self, account, period="2026-08"):
        code, out, err = run_cli(["readiness", "--entity", str(self.root), "--period", period, "--as-of", "2026-10-20"])
        document = json.loads(out)
        return sorted({item["code"] for item in document["blockers"] if account in (item.get("refs") or [])})

    def test_an_unmapped_account_without_a_kind_or_with_only_a_balance_is_still_required(self):
        add_account(self.root, "Assets:Bank:NoKind", open_date="2026-01-01")  # No financial kind.
        self.mapping("acct_3", "Assets:Bank:NoKind")
        self.assertEqual(self.ingest([{**self.row("k1", "2026-08-03", 9.0), "accountId": "acct_3"}])[0], 0)
        self.confirm("plaid:k1")
        before = self.readiness_codes("Assets:Bank:NoKind")
        self.assertTrue(before)
        self.assertEqual(run_cli(["entity", "bank-account-unmap", str(self.root), "--feed-account-id", "acct_3"])[0], 0)
        self.assertLessEqual(set(before), set(self.readiness_codes("Assets:Bank:NoKind")))
        # September: no posting, but the account still holds 9.00, so it is still required.
        self.assertTrue(self.readiness_codes("Assets:Bank:NoKind", "2026-09"))
        # An account with no posting and a zero balance is not required.
        add_account(self.root, "Assets:Bank:Idle", open_date="2026-01-01", financial_kind="bank")
        code, out, err = run_cli(["readiness", "--entity", str(self.root), "--period", "2026-09", "--as-of", "2026-10-20"])
        self.assertNotIn("Assets:Bank:Idle", out)

    def test_a_relink_of_the_same_books_account_under_a_new_feed_id_needs_the_cutover_decision(self):
        history = [self.row("h1", "2026-07-10", 100.0), self.row("p1", "2026-08-10", 5.0)]
        self.assertEqual(self.ingest(history)[0], 0)
        self.confirm("plaid:p1")
        self.assertEqual(self.unmap()[0], 0)
        code, out, err = run_cli(["entity", "bank-account-map", str(self.root), "--feed-account-id", "acct_1b",
                                  "--account", CHECKING, "--provider", "plaid"])
        self.assertEqual(code, 1, (out, err))
        self.assertIn("cutover date 2026-08-01", err)
        self.mapping("acct_1b", CHECKING, "--restore-cutover")
        relinked = [self.row("h1b", "2026-07-10", 100.0, accountId="acct_1b"),
                    self.row("p1b", "2026-08-10", 5.0, accountId="acct_1b")]
        self.assertEqual(self.ingest(relinked)[0], 0)
        staged = self.root / "staging" / "pending-categorization.json"
        waiting = [row["id"] for row in json.loads(staged.read_text())] if staged.exists() else []
        self.assertNotIn("plaid:h1b", waiting)       # The history row before the cutover is not queued or posted.
        self.assertNotIn("plaid:h1b", self.entry_ids())
        # A second feed account for a books account that is mapped with a cutover needs the decision too.
        code, out, err = run_cli(["entity", "bank-account-map", str(self.root), "--feed-account-id", "acct_1c",
                                  "--account", CHECKING, "--provider", "plaid"])
        self.assertEqual(code, 1, (out, err))

    def test_a_host_prepared_snapshot_skips_the_rows_of_an_unmapped_account_too(self):
        from unittest import mock
        add_account(self.root, SAVINGS, open_date="2026-01-01", financial_kind="bank")
        self.mapping("acct_2", SAVINGS)
        self.assertEqual(self.unmap()[0], 0)
        rows = [self.row("a", "2026-08-03", 1.0, accountId="acct_1"), self.row("b", "2026-08-03", 2.0, accountId="acct_2")]
        path = Path(self.tmp.name) / "feed.json"
        path.write_text(json.dumps({"transactions": rows}))
        from bookkeeping.cli import main
        import contextlib
        import io
        out = io.StringIO()
        prepared = [{**row, "id": "plaid:" + row["id"]} for row in rows]
        with mock.patch("bookkeeping.preparation_runtime.prepared_ingest_transactions", return_value=(prepared, False)):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = main(["ingest", str(path), "--entity", str(self.root), "--source", "plaid", "--no-feed-window"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("skipped, feed account unmapped: acct_1: 1 row(s)", out.getvalue())
        self.assertEqual(self.entry_ids(), [])
        staged = json.loads((self.root / "staging" / "pending-categorization.json").read_text())
        self.assertEqual([row["id"] for row in staged], ["plaid:b"])


if __name__ == "__main__":
    unittest.main()
