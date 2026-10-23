"""``books entity account-remove``: remove an unused income or expense category.

An account that a posting, a balance assertion or a company setting uses stays. A system account stays.
The removal writes the audit event ``account-removed`` and changes no entry.
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

from bookkeeping.automation_policy import PreparationDenied  # noqa: E402
from bookkeeping.entity import add_account, load_entity  # noqa: E402
from bookkeeping.ledger.importer import _atomic_ledger_write, check_integrity, reverse_entry  # noqa: E402
from bookkeeping.ledger.model import Entry, Posting  # noqa: E402
from bookkeeping.ledger.store import LedgerStore  # noqa: E402
from bookkeeping.preparation_runtime import supported_preparation_command  # noqa: E402
from bookkeeping.remote_contract import command_spec  # noqa: E402

BANK = "Assets:Bank:Checking"
INSTALLS = "Income:Installs"


def books(*argv: str):
    code, out, err = run_cli(list(argv))
    return code, (json.loads(out) if out.strip().startswith(("{", "[")) else out), err


class AccountRemoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "company"
        code, out, err = run_cli(["entity", "init", str(self.root), "--name", "Test Co"])
        self.assertEqual(code, 0, (out, err))
        add_account(self.root, INSTALLS, open_date="2026-01-01")
        self.store = LedgerStore(self.root / "ledger.sqlite")

    def remove(self, account: str = INSTALLS, *extra: str):
        return books("entity", "account-remove", str(self.root), "--account", account, *extra)

    def catalog(self) -> set[str]:
        return {opened.account for opened in self.store.load_opens()}

    def state(self):
        files = {name: (self.root / name).read_bytes() for name in ("entity.json", "trust-policy.json")}
        return (self.store.content_digest(), len(self.store.load_audit_events()), self.catalog(), files)

    def assert_refused(self, code_name: str, account: str = INSTALLS):
        before = self.state()
        for extra in ((), ("--apply",)):
            code, result, _ = self.remove(account, *extra)
            self.assertEqual((code, result["errors"][0]["code"]), (1, code_name), result)
        self.assertEqual(self.state(), before)
        return result

    def post(self, account: str, source_id: str = "s1") -> None:
        entity = load_entity(self.root)
        _atomic_ledger_write(entity, [], [Entry(date=date(2026, 2, 3), narration="Install", payee="Oak Co",
                                                meta=(("source-id", source_id),),
                                                postings=(Posting(BANK, Decimal("80.00")), Posting(account, Decimal("-80.00"))))],
                             "test-seed", None, "seed")

    def test_unused_account_is_removed_after_preview(self):
        digest = self.store.content_digest()
        code, plan, _ = self.remove()
        self.assertEqual((code, plan["changes"], plan["effects"]["open_date"]), (0, True, "2026-01-01"), plan)
        self.assertEqual(self.store.content_digest(), digest)  # The preview writes nothing.
        code, done, _ = self.remove(INSTALLS, "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"], done["entries_changed"]), (0, "applied", 0), done)
        self.assertNotIn(INSTALLS, self.catalog())
        events = [e for e in self.store.load_audit_events() if e["type"] == "account-removed"]
        self.assertEqual([e["payload"]["account"] for e in events], [INSTALLS])
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        # The account can come back.
        add_account(self.root, INSTALLS, open_date="2026-01-01")
        self.assertIn(INSTALLS, self.catalog())

    def test_exact_retry_and_absent_account_are_already_applied(self):
        code, plan, _ = self.remove()
        self.remove(INSTALLS, "--apply", "--plan-hash", plan["plan_hash"])
        before = self.state()
        code, retry, _ = self.remove(INSTALLS, "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, retry["status"]), (0, "already_applied"), retry)
        code, absent, _ = self.remove("Income:Never-Added", "--apply")
        self.assertEqual((code, absent["status"]), (0, "already_applied"))
        self.assertEqual(self.state(), before)
        self.assertEqual(sum(e["type"] == "account-removed" for e in self.store.load_audit_events()), 1)

    def test_stale_plan_hash_refuses_a_removal(self):
        code, plan, _ = self.remove()
        add_account(self.root, "Income:Other-Line", open_date="2026-01-01")  # The ledger changed.
        code, stale, _ = self.remove(INSTALLS, "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))
        self.assertIn(INSTALLS, self.catalog())

    def test_account_with_postings_is_refused(self):
        self.post(INSTALLS)
        result = self.assert_refused("account_in_use")
        self.assertIn("1 postings", result["errors"][0]["message"])

    def test_account_used_only_by_a_reversed_entry_is_refused(self):
        self.post(INSTALLS, "rev")
        reverse_entry(load_entity(self.root), "rev", "test-reverse")
        self.assert_refused("account_in_use")  # The postings sum to zero, but they exist.

    def test_referenced_account_is_refused(self):
        cases = {
            "tax line": lambda: self.assertEqual(run_cli(["tax", "set", "--entity", str(self.root), "--account", INSTALLS,
                                                          "--line", "schc/I/1"])[0], 0),
            "bank mapping": lambda: self.edit_json("entity.json", lambda c: c.setdefault("bank_account_mappings", {}).update({"feed-1": INSTALLS})),
            "related entity": lambda: self.edit_json("entity.json", lambda c: c.setdefault("related_entities", []).append(
                {"name": "Sister Co", "inbound_income_account": INSTALLS})),
            "payee rule": lambda: self.write_json("learned-context/counterparties.json", {"oak co": {"category": INSTALLS}}),
            "open review item": lambda: self.write_json("review-queue/item-1.json", {"id": "item-1", "status": "open",
                                                                                     "proposed_category": INSTALLS}),
            "trust policy": lambda: self.edit_json("trust-policy.json", lambda c: c.update({"pinned": [INSTALLS]})),
        }
        self.assertEqual(run_cli(["tax", "set", "--entity", str(self.root), "--return-form", "schc"])[0], 0)
        clean = {name: (self.root / name).read_bytes() for name in ("entity.json", "trust-policy.json")}
        for label, make in cases.items():
            with self.subTest(label):
                make()
                result = self.assert_refused("account_referenced")
                self.assertTrue(result["errors"][0]["refs"], result)
                for name, data in clean.items():
                    (self.root / name).write_bytes(data)
                for name in ("learned-context/counterparties.json", "review-queue/item-1.json"):
                    (self.root / name).unlink(missing_ok=True)
        # A decided review item does not block the removal.
        self.write_json("review-queue/item-2.json", {"id": "item-2", "status": "confirmed", "proposed_category": INSTALLS})
        code, plan, _ = self.remove()
        self.assertEqual((code, plan["changes"]), (0, True), plan)

    def test_unreadable_reference_file_is_refused(self):
        (self.root / "learned-context" / "broken.json").write_text("{", encoding="utf-8")
        self.assert_refused("reference_check_failed")

    def test_system_accounts_are_refused(self):
        for account in ("Income:Consulting", "Expenses:Software", BANK, "Equity:Owner-Draws", "Liabilities:CreditCard"):
            with self.subTest(account):
                self.assert_refused("protected_account", account)
        add_account(self.root, "Liabilities:Sales-Tax-Payable", open_date="2026-01-01")
        self.assert_refused("protected_account", "Liabilities:Sales-Tax-Payable")

    def test_control_characters_and_bad_names_are_refused(self):
        for account in ("Income:Installs\n", "Income:Installs\r", "Income:Inst\talls", "Income:Installs\x00",
                        "Income:installs", "Income", "Revenue:Installs", "Income::Installs"):
            with self.subTest(account=account):
                self.assert_refused("invalid_account", account)
        self.assertIn(INSTALLS, self.catalog())

    def test_preview_is_read_only_for_hosts_and_not_standing_authority(self):
        base = ["entity", "account-remove", "E", "--account", INSTALLS]
        self.assertFalse(command_spec(base)["mutates"])
        self.assertTrue(command_spec(base + ["--apply"])["mutates"])
        with self.assertRaises(PreparationDenied):
            supported_preparation_command(base + ["--apply"])

    def test_engine_owned_list_covers_starters_and_stripe(self):
        from bookkeeping.engine_accounts import engine_owned_accounts
        from bookkeeping.stripe import STRIPE_ACCOUNTS
        owned = engine_owned_accounts()
        self.assertTrue({"Income:Consulting", "Income:Subscriptions", "Expenses:Depreciation", "Expenses:Uncategorized",
                         *STRIPE_ACCOUNTS.values()} <= owned)
        self.assertNotIn(INSTALLS, owned)

    def test_stripe_accounts_are_protected_after_a_real_stripe_prepare(self):
        from bookkeeping.stripe import stripe_prepare
        stripe_prepare(load_entity(self.root), opening_date="2026-01-01")
        from bookkeeping.account_remove import is_account_added_event
        self.assertTrue(any(is_account_added_event(e, "Expenses:Payment-Processing:Stripe")
                            for e in self.store.load_audit_events()))  # account-add created it ...
        for account in ("Expenses:Payment-Processing:Stripe", "Income:Refunds:Stripe"):
            with self.subTest(account):
                self.assert_refused("protected_account", account)  # ... but the engine owns it.

    def test_account_not_created_by_account_add_is_refused(self):
        from bookkeeping.ledger.model import Open
        _atomic_ledger_write(load_entity(self.root), [Open(date(2026, 1, 1), "Income:Opened-By-Write", ("USD",))], [],
                             "test-open", None, "open an account without account-add")
        self.assertIn("Income:Opened-By-Write", self.catalog())
        self.assert_refused("not_added_by_command", "Income:Opened-By-Write")

    def test_added_event_is_checked_and_a_readded_account_is_removable_again(self):
        from bookkeeping.account_remove import is_account_added_event
        added = [e for e in self.store.load_audit_events() if is_account_added_event(e, INSTALLS)][-1]
        code, wrong, _ = self.remove(INSTALLS, "--added-event", str(added["id"] - 1))
        self.assertEqual((code, wrong["errors"][0]["code"]), (1, "added_event_mismatch"))
        code, plan, _ = self.remove(INSTALLS, "--added-event", str(added["id"]))
        self.assertEqual((code, plan["effects"]["added_event"]), (0, added["id"]), plan)
        code, done, _ = self.remove(INSTALLS, "--added-event", str(added["id"]), "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, done["status"]), (0, "applied"), done)
        # Removed and added again with the same date and currency: the old preview no longer applies.
        add_account(self.root, INSTALLS, open_date="2026-01-01")
        code, stale, _ = self.remove(INSTALLS, "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))
        self.assertIn(INSTALLS, self.catalog())
        code, again, _ = self.remove(INSTALLS, "--apply")
        self.assertEqual((code, again["status"]), (0, "applied"), again)

    def test_changed_trust_policy_after_preview_is_stale(self):
        code, plan, _ = self.remove()
        self.edit_json("trust-policy.json", lambda c: c.update({"ask_first_above": "500.00"}))
        code, stale, _ = self.remove(INSTALLS, "--apply", "--plan-hash", plan["plan_hash"])
        self.assertEqual((code, stale["errors"][0]["code"]), (1, "stale_preview"))
        self.assertIn(INSTALLS, self.catalog())

    def test_populated_ledger_without_audit_history_is_refused(self):
        from bookkeeping.ledger.model import Open
        root = Path(self.tmp.name) / "no-audit"
        root.mkdir()
        (root / "entity.json").write_text(json.dumps({"name": "No audit"}), encoding="utf-8")
        store = LedgerStore(root / "ledger.sqlite")
        store.initialize()
        with store.transaction() as sql:
            store.insert_opens([Open(date(2026, 1, 1), BANK, ("USD",)), Open(date(2026, 1, 1), INSTALLS, ("USD",))], sql)
            store.insert_entries([Entry(date=date(2026, 1, 2), narration="x", meta=(("source-id", "x"),),
                                        postings=(Posting(BANK, Decimal("1.00")), Posting("Income:Other", Decimal("-1.00"))))], sql)
        before = (store.content_digest(), store.load_audit_events(), (root / "entity.json").read_bytes())
        for extra in ((), ("--apply",)):
            code, result, _ = books("entity", "account-remove", str(root), "--account", INSTALLS, *extra)
            self.assertEqual((code, result["errors"][0]["code"]), (1, "audit_history_missing"), result)
        self.assertEqual((store.content_digest(), store.load_audit_events(), (root / "entity.json").read_bytes()), before)
        self.assertIn(INSTALLS, {o.account for o in store.load_opens()})

    def test_symlinked_settings_fail_closed(self):
        import os
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "rules.json").write_text(json.dumps({"oak co": {"category": INSTALLS}}), encoding="utf-8")
        cases = {
            "symlinked file": (self.root / "learned-context" / "linked.json", outside / "rules.json"),
            "symlinked folder": (self.root / "learned-context" / "linked-dir", outside),
            "symlinked queue": (self.root / "review-queue", outside),
        }
        for label, (link, target) in cases.items():
            with self.subTest(label):
                moved = None
                if link.exists() and not link.is_symlink():
                    moved = link.with_name(link.name + ".bak")
                    link.rename(moved)
                os.symlink(target, link)
                try:
                    self.assert_refused("reference_check_failed")
                finally:
                    link.unlink()
                    if moved:
                        moved.rename(link)

    def test_accrual_rule_on_a_parent_account_uses_the_selector_semantics(self):
        add_account(self.root, "Expenses:Custom:Seats", open_date="2026-01-01")
        self.write_json("learned-context/accrual-rules.json", {"version": 1, "receipts": [], "rules": [
            {"id": "seats", "match": {"accounts": ["Expenses:Custom"]}, "treatment": "prepaid"}]})
        result = self.assert_refused("account_referenced", "Expenses:Custom:Seats")
        self.assertIn("learned-context/accrual-rules.json:rules[0].match.accounts[0]", result["errors"][0]["refs"])
        # With another account under the parent, the rule still selects something, so the removal is allowed.
        add_account(self.root, "Expenses:Custom:Desks", open_date="2026-01-01")
        code, plan, _ = self.remove("Expenses:Custom:Seats")
        self.assertEqual((code, plan["changes"]), (0, True), plan)

    def test_refused_apply_writes_no_company_file(self):
        # A ledger currency that entity.json does not show yet: the usual write lock would repair entity.json.
        with self.store.transaction() as sql:
            self.store.set_meta("currency", "EUR", sql)
            self.store.set_meta("currency_source", "set", sql)
        before = (self.root / "entity.json").read_bytes()
        for account in ("Income:Consulting", "Income:Never-Added"):
            code, result, _ = self.remove(account, "--apply")
            self.assertEqual((self.root / "entity.json").read_bytes(), before, result)

    def test_malformed_or_unreadable_settings_give_reference_check_failed(self):
        import os
        for name, data in (("entity.json", b"{"), ("trust-policy.json", b"{"), ("entity.json", b"[]")):
            with self.subTest(name=name, data=data):
                original = (self.root / name).read_bytes()
                (self.root / name).write_bytes(data)
                try:
                    for extra in ((), ("--apply",)):
                        code, result, _ = self.remove(INSTALLS, *extra)
                        self.assertEqual((code, result["errors"][0]["code"]), (1, "reference_check_failed"), result)
                finally:
                    (self.root / name).write_bytes(original)
        if os.geteuid() != 0:  # A root user can read the file anyway.
            for name in ("entity.json", "trust-policy.json"):
                with self.subTest(unreadable=name):
                    (self.root / name).chmod(0)
                    try:
                        code, result, _ = self.remove(INSTALLS)
                        self.assertEqual((code, result["errors"][0]["code"]), (1, "reference_check_failed"), result)
                    finally:
                        (self.root / name).chmod(0o600)
        self.assertIn(INSTALLS, self.catalog())

    def test_source_rows_and_import_sessions_are_references(self):
        from bookkeeping.accrual.enable import _seal
        _atomic_ledger_write(load_entity(self.root), [], [], "test-source", None, "a source row without an entry",
                             source_transactions=[{"id": "src-raw-1", "category": INSTALLS, "amount": "10.00"}])
        result = self.assert_refused("account_referenced")
        self.assertIn("source_transactions src-raw-1:category", result["errors"][0]["refs"])
        with self.store.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM source_transactions WHERE id='src-raw-1'")
            conn.execute("INSERT INTO import_sessions (id, created_at, metadata_json) VALUES (?, ?, ?)",
                         ("session-1", "2026-02-01T00:00:00Z", json.dumps({"default_category": INSTALLS})))
            _seal(self.store, conn, "test-session")
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        result = self.assert_refused("account_referenced")
        self.assertIn("import_sessions session-1:default_category", result["errors"][0]["refs"])

    def test_names_written_with_json_escapes_are_found(self):
        from bookkeeping.accrual.enable import _seal
        escaped = '"Income:\\u0049nstalls"'  # JSON text for "Income:Installs"
        self.assertEqual(json.loads(escaped), INSTALLS)
        rows = (("source_transactions", "INSERT INTO source_transactions (id, payload_json, status, imported_at) VALUES (?, ?, ?, ?)",
                 ("src-esc", '{"lines": [{"category": ' + escaped + '}]}', "posted", "2026-02-01T00:00:00Z"),
                 "source_transactions src-esc:lines[0].category"),
                ("import_sessions", "INSERT INTO import_sessions (id, created_at, metadata_json) VALUES (?, ?, ?)",
                 ("session-esc", "2026-02-01T00:00:00Z", '{"map": {' + escaped + ': "x"}}'),
                 "import_sessions session-esc:map.Income:Installs"))
        for table, sql, values, ref in rows:
            with self.subTest(table):
                with self.store.transaction(immediate=True) as conn:
                    conn.execute(sql, values)
                    _seal(self.store, conn, "test-escaped")
                result = self.assert_refused("account_referenced")
                self.assertIn(ref, result["errors"][0]["refs"])
                with self.store.transaction(immediate=True) as conn:
                    conn.execute(f"DELETE FROM {table} WHERE id=?", (values[0],))
                    _seal(self.store, conn, "test-escaped-clean")
        path = self.root / "learned-context" / "escaped.json"
        path.write_text('{"oak co": {"category": ' + escaped + '}}', encoding="utf-8")
        result = self.assert_refused("account_referenced")
        self.assertIn("learned-context/escaped.json:oak co.category", result["errors"][0]["refs"])

    def test_correction_and_removal_cannot_leave_a_dangling_posting(self):
        """A removal that runs while a correction waits for the lock: the correction decides under the lock."""
        import contextlib
        from unittest.mock import patch
        from bookkeeping import account_remove
        from bookkeeping.ledger import importer
        self.post("Income:Consulting", "corr")
        real = importer._entity_write_lock
        ran = []

        @contextlib.contextmanager
        def removal_first(path, **kwargs):
            if not ran:
                ran.append(True)  # The removal takes the lock first and deletes the unused account.
                self.assertEqual(account_remove.apply(self.root, INSTALLS)["status"], "applied")
            with real(path, **kwargs):
                yield

        with patch.object(importer, "_entity_write_lock", removal_first):
            code, out, err = books("ledger", "correct", "--entity", str(self.root), "--source-id", "corr",
                                   "--new-source-id", "corr-2", "--date", "2026-02-03", "--description", "Install",
                                   "--posting", f"{BANK}=80.00", "--posting", f"{INSTALLS}=-80.00")
        self.assertEqual((ran, code), ([True], 0), (out, err))
        posted = {p.account for e in self.store.load_entries() for p in e.postings}
        self.assertIn(INSTALLS, posted)
        self.assertLessEqual(posted, self.catalog())  # Every posting has an open directive.
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        code, out, err = run_cli(["ledger", "snapshot", "--entity", str(self.root), "--output", str(Path(self.tmp.name) / "snap.beancount")])
        self.assertEqual(code, 0, (out, err))

    def test_deeply_nested_json_is_walked_or_refused(self):
        deep = 900
        nested = "[" * deep + json.dumps({"category": INSTALLS}) + "]" * deep
        path = self.root / "learned-context" / "deep.json"
        path.write_text(nested, encoding="utf-8")
        result = self.assert_refused("account_referenced")
        self.assertTrue(result["errors"][0]["refs"][0].startswith("learned-context/deep.json:[0]"))
        # Far deeper than the Python call stack: the iterative walk finds the name, or the parser refuses.
        very_deep = 100000
        path.write_text("[" * very_deep + json.dumps(INSTALLS) + "]" * very_deep, encoding="utf-8")
        code, result, _ = self.remove()
        self.assertEqual(code, 1, result)
        self.assertIn(result["errors"][0]["code"], {"account_referenced", "reference_check_failed"})
        # A parser that runs out of stack fails closed.
        from unittest.mock import patch
        from bookkeeping import account_remove
        def out_of_stack(*args, **kwargs):
            raise RecursionError("maximum recursion depth exceeded")

        with patch.object(account_remove, "json", type("ParserOutOfStack", (), {"loads": staticmethod(out_of_stack)})):
            code, result, _ = self.remove()
        self.assertEqual((code, result["errors"][0]["code"]), (1, "reference_check_failed"), result)
        self.assertIn(INSTALLS, self.catalog())

    def test_settings_with_a_wrong_shape_are_refused(self):
        cases = {
            "trust-policy.json": [],
            "learned-context/counterparties.json": ["oak co"],
            "learned-context/accrual-rules.json": {"version": 1, "rules": [{"id": "r", "match": "wrong"}]},
            "review-queue/item-9.json": ["not", "an", "item"],
        }
        for name, value in cases.items():
            with self.subTest(name):
                path = self.root / name
                original = path.read_bytes() if path.exists() else None
                self.write_json(name, value)
                try:
                    result = self.assert_refused("reference_check_failed")
                    self.assertIn(name, result["errors"][0]["refs"])
                finally:
                    if original is None:
                        path.unlink()
                    else:
                        path.write_bytes(original)
        self.assertIn(INSTALLS, self.catalog())

    def test_teaching_cannot_save_an_account_that_a_removal_deleted_in_between(self):
        import contextlib
        from unittest.mock import patch
        from bookkeeping import account_remove, queue
        real = queue._queue_mutation

        @contextlib.contextmanager
        def removal_first(entity):
            # The removal runs between the call and the lock of the teach command.
            self.assertEqual(account_remove.apply(self.root, INSTALLS)["status"], "applied")
            with real(entity):
                yield

        with patch.object(queue, "_queue_mutation", removal_first), self.assertRaises(ValueError) as caught:
            queue.set_taught(load_entity(self.root), "oak co", INSTALLS, by="person", by_name="Owner")
        self.assertIn("not an account in the account catalog", str(caught.exception))
        self.assertNotIn(INSTALLS, self.catalog())
        learned = self.root / "learned-context" / "counterparties.json"
        self.assertNotIn(INSTALLS, learned.read_text(encoding="utf-8") if learned.exists() else "")

    def edit_json(self, name: str, change) -> None:
        path = self.root / name
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def write_json(self, name: str, value) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
