"""``queue confirm-group`` writes its categorized members in one ledger transaction with one seal.

The result must be the same as one ``confirm`` for each member in turn (the path before the batch), every per-item
check must still refuse before any write, a stop must leave the ledger with all of the group or none of it, and a
split or a transfer pair in the group keeps its own confirmation.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from bookkeeping import queue as queue_module  # noqa: E402
from bookkeeping.ledger.importer import check_store_integrity  # noqa: E402
from bookkeeping.ledger.open_dates import AccountNotOpenError  # noqa: E402
from bookkeeping.ledger.store import LedgerStore, default_store_path  # noqa: E402
from bookkeeping.queue import (  # noqa: E402
    confirm, confirm_group, propose, propose_group, propose_split, propose_transfer, read_queue_item,
)
from test_queue import TS, _add_pending_categorization, _make_entity  # noqa: E402

SESSION = "batch-session"
CATEGORY = "Expenses:Software"
# Runtime files that a write may touch without a change to the books.
RUNTIME = {".books.lock", ".books-audit-head", "ledger.sqlite", "ledger.sqlite-wal", "ledger.sqlite-shm",
           "ledger.sqlite-journal"}


def store_of(entity) -> LedgerStore:
    return LedgerStore(default_store_path(entity.path))


def company_files(entity) -> dict[str, bytes]:
    """Every company file but the ledger database and runtime state, by its path in the company folder."""
    return {str(path.relative_to(entity.path)): path.read_bytes()
            for path in sorted(entity.path.rglob("*"))
            if path.is_file() and path.name not in RUNTIME and not path.name.startswith(".books-audit-head")}


def events_of(entity, kind: str) -> list[dict]:
    return [event["payload"] for event in store_of(entity).load_audit_events() if event["type"] == kind]


class BatchConfirmGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.entity = _make_entity(Path(self.tmp.name) / "main")
        patcher = patch.dict(os.environ, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("SLASHBOOKS_ACTOR", "SLASHBOOKS_FORBID_SELF_APPROVAL", "SLASHBOOKS_ENFORCE_ASK_FIRST"):
            os.environ.pop(name, None)

    def group(self, count: int = 7, category: str = CATEGORY) -> list[str]:
        """Propose a group of ``count`` rows: repeated and single payees, two bank feeds, an unrelated row."""
        ids = []
        for number in range(count):
            source_id = f"row-{number:02d}"
            payee = ["Vendor Alpha", "Vendor Beta", "Vendor Alpha", f"Vendor Single {number}"][number % 4]
            _add_pending_categorization(self.entity, source_id, payee, amount=f"-{10 + number}.25",
                                        txn_date=f"2026-01-{10 + number:02d}")
            ids.append(source_id)
        _add_pending_categorization(self.entity, "other", "Unrelated vendor", amount="-3.00")
        path = self.entity.staging_dir / "pending-categorization.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        rows[1]["accountName"] = "Amex"
        rows[1]["accountType"] = "credit_card"
        path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        propose_group(self.entity, ids, category, "Synthetic group.")
        return ids

    def copy(self, name: str):
        target = Path(self.tmp.name) / name / "entity"
        shutil.copytree(self.entity.path, target)
        return replace(self.entity, path=target)

    def one_by_one(self, entity, ids: list[str]) -> list[dict]:
        """The path before the batch: one ``confirm`` (one ledger transaction and one seal) for each member."""
        return [confirm(entity, source_id, SESSION, ts=TS) for source_id in ids]

    # -- the same result -----------------------------------------------------------------------------------------

    def test_the_batch_gives_the_same_books_and_files_as_one_confirm_for_each_item(self) -> None:
        ids = self.group()
        before = self.copy("before")
        expected = self.one_by_one(before, ids)

        confirmed = confirm_group(self.entity, CATEGORY, SESSION, ts=TS)

        self.assertEqual(confirmed, expected)
        self.assertEqual(store_of(self.entity).content_digest(), store_of(before).content_digest())
        self.assertEqual(company_files(self.entity), company_files(before))
        for name in ("queue-finalization-intent", "entry-written"):
            self.assertEqual(events_of(self.entity, name), events_of(before, name), name)
        # The expected change of shape: one intent and one seal for the group, not one for each item.
        intents, seals = events_of(self.entity, "intent"), events_of(self.entity, "ledger-store-sealed")
        self.assertEqual((len(intents), len(seals)), (1, 1))
        self.assertEqual(intents[0]["entries"], len(ids))
        self.assertEqual(seals[0]["source_ids"], ids)
        self.assertEqual(seals[0]["entries"], len(ids))
        self.assertEqual(seals[0]["store_sha256"], store_of(self.entity).content_digest())
        self.assertEqual(len(events_of(before, "ledger-store-sealed")), len(ids))
        self.assertEqual(check_store_integrity(store_of(self.entity)).status, "ok")
        self.assertFalse(any((self.entity.path / "review-queue" / ".transactions").glob("*")))
        context = queue_module.load_learned_context(self.entity)
        self.assertEqual(context["VENDOR ALPHA"]["confirmed_count"], 4)  # rows 0, 2, 4 and 6

    # -- all or none ---------------------------------------------------------------------------------------------

    def test_a_stop_inside_the_ledger_write_leaves_nothing_written(self) -> None:
        ids = self.group()
        files_before = company_files(self.entity)
        digest_before = store_of(self.entity).content_digest()
        events_before = store_of(self.entity).load_audit_events()
        real = LedgerStore.append_audit_event
        written = []

        def stop_in_the_middle(store, kind, payload, *args, **kwargs):
            if kind == "entry-written":
                written.append(payload)
                if len(written) == len(ids) // 2:
                    raise OSError("synthetic stop in the middle of the group")
            return real(store, kind, payload, *args, **kwargs)

        with patch.object(LedgerStore, "append_audit_event", stop_in_the_middle), \
                self.assertRaisesRegex(OSError, "synthetic stop"):
            confirm_group(self.entity, CATEGORY, SESSION, ts=TS)

        self.assertEqual(store_of(self.entity).content_digest(), digest_before)
        self.assertEqual(store_of(self.entity).load_audit_events(), events_before)
        self.assertEqual(company_files(self.entity), files_before)
        self.assertEqual(check_store_integrity(store_of(self.entity)).status, "ok")
        for source_id in ids:
            self.assertEqual(read_queue_item(self.entity, source_id)["status"], "open")

    def test_a_stop_after_the_ledger_write_is_finished_by_the_next_confirm(self) -> None:
        ids = self.group()
        reference = self.copy("reference")
        confirm_group(reference, CATEGORY, SESSION, ts=TS)

        with patch.object(queue_module, "_save_item_group", side_effect=OSError("synthetic stop")), \
                self.assertRaisesRegex(OSError, "synthetic stop"):
            confirm_group(self.entity, CATEGORY, SESSION, ts=TS)
        # The ledger holds the whole group; the items wait, and the learned context has not counted them yet.
        self.assertEqual(store_of(self.entity).content_digest(), store_of(reference).content_digest())
        self.assertEqual(queue_module.load_learned_context(self.entity), {})
        for source_id in ids:
            self.assertEqual(read_queue_item(self.entity, source_id)["status"], "open")

        again = confirm_group(self.entity, CATEGORY, SESSION, ts=TS)

        self.assertEqual([item["status"] for item in again], ["confirmed"] * len(ids))
        self.assertEqual(store_of(self.entity).content_digest(), store_of(reference).content_digest())
        self.assertEqual(company_files(self.entity), company_files(reference))
        self.assertEqual(check_store_integrity(store_of(self.entity)).status, "ok")

    def test_a_stop_inside_the_group_commit_is_rolled_forward(self) -> None:
        ids = self.group()
        reference = self.copy("reference")
        confirm_group(reference, CATEGORY, SESSION, ts=TS)
        real = queue_module._replace_group_destination
        calls = []

        def stop_after_three(path, payload):
            calls.append(path)
            if len(calls) == 3:
                raise OSError("synthetic stop")
            return real(path, payload)

        with patch.object(queue_module, "_replace_group_destination", stop_after_three), \
                self.assertRaisesRegex(OSError, "synthetic stop"):
            confirm_group(self.entity, CATEGORY, SESSION, ts=TS)
        with queue_module._queue_mutation(self.entity):
            pass  # The next queue mutation finishes the committed group.

        self.assertEqual(company_files(self.entity), company_files(reference))
        self.assertEqual([read_queue_item(self.entity, i)["status"] for i in ids], ["confirmed"] * len(ids))

    def test_an_uncommitted_group_journal_with_a_learned_context_is_rolled_back(self) -> None:
        self.group()
        before = company_files(self.entity)
        real = queue_module._write_fsynced

        def stop_at_commit(path, payload):
            if path.name == "COMMIT":
                raise OSError("synthetic stop")
            return real(path, payload)

        with patch.object(queue_module, "_write_fsynced", stop_at_commit), \
                self.assertRaisesRegex(OSError, "synthetic stop"):
            confirm_group(self.entity, CATEGORY, SESSION, ts=TS)
        with queue_module._queue_mutation(self.entity):
            pass
        after = company_files(self.entity)
        # The ledger write and the staged-row and seen-id updates are done; the items and the context are not.
        for name in ("learned-context/counterparties.json",):
            self.assertEqual(after.get(name), before.get(name))
        self.assertEqual({k: v for k, v in after.items() if k.startswith("review-queue/")},
                         {k: v for k, v in before.items() if k.startswith("review-queue/")})

    def test_a_group_journal_with_files_is_version_2_and_a_version_1_journal_cannot_hold_files(self) -> None:
        self.group(3)
        seen = []
        real = queue_module._remove_group_transaction

        def read_then_remove(path):
            seen.append(json.loads((path / "manifest.json").read_text(encoding="utf-8")))
            return real(path)

        with patch.object(queue_module, "_remove_group_transaction", read_then_remove):
            confirm_group(self.entity, CATEGORY, SESSION, ts=TS)
        self.assertEqual([(m["schema_version"], [f["path"] for f in m.get("files", [])]) for m in seen],
                         [(2, ["learned-context/counterparties.json"])])
        journal = Path(self.tmp.name) / "journal"
        journal.mkdir()
        manifest = dict(seen[0], schema_version=1)
        (journal / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "is invalid"):
            queue_module._load_group_transaction(journal)
        (journal / "manifest.json").write_text(json.dumps(seen[0]), encoding="utf-8")
        self.assertEqual(queue_module._load_group_transaction(journal)["schema_version"], 2)
        # A group without other files (a proposal group) keeps version 1.
        _add_pending_categorization(self.entity, "late-1", "Late vendor")
        _add_pending_categorization(self.entity, "late-2", "Late vendor")
        seen.clear()
        with patch.object(queue_module, "_remove_group_transaction", read_then_remove):
            propose_group(self.entity, ["late-1", "late-2"], CATEGORY, "r")
        self.assertEqual([(m["schema_version"], "files" in m) for m in seen], [(1, False)])

    def test_two_spellings_of_one_bank_row_post_it_once_as_one_confirm_for_each_does(self) -> None:
        _add_pending_categorization(self.entity, "abc", "Vendor A", amount="-10.00")
        _add_pending_categorization(self.entity, "banksync:abc", "Vendor A", amount="-10.00")
        _add_pending_categorization(self.entity, "xyz", "Vendor X", amount="-11.00")
        propose_group(self.entity, ["abc", "banksync:abc", "xyz"], CATEGORY, "r")
        ids = [item["source_id"] for item in queue_module.list_queue_items(self.entity, status="open")]
        self.assertEqual(ids, ["abc", "banksync:abc", "xyz"])
        before = self.copy("before")
        with self.assertRaises(ValueError) as one_by_one:
            self.one_by_one(before, ids)

        with self.assertRaises(ValueError) as batch:
            confirm_group(self.entity, CATEGORY, SESSION, ts=TS)

        self.assertEqual(str(batch.exception), str(one_by_one.exception))
        self.assertEqual(store_of(self.entity).content_digest(), store_of(before).content_digest())
        self.assertEqual(company_files(self.entity), company_files(before))
        self.assertEqual(store_of(self.entity).load_audit_events(), store_of(before).load_audit_events())
        with store_of(self.entity).connection() as conn:
            posted = [row[0] for row in conn.execute("SELECT source_id FROM entries")]
        self.assertEqual(posted, ["abc"])
        self.assertEqual([read_queue_item(self.entity, i)["status"] for i in ids], ["confirmed", "open", "open"])
        self.assertEqual(check_store_integrity(store_of(self.entity)).status, "ok")

    def test_a_group_journal_cannot_name_another_company_file(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot change"):
            queue_module._save_item_group(self.entity, [], [("entity.json", b"{}", None)])

    # -- mixed group ---------------------------------------------------------------------------------------------

    def test_a_split_and_a_transfer_pair_in_the_group_keep_their_own_confirmation(self) -> None:
        _add_pending_categorization(self.entity, "plain-1", "Vendor Plain", amount="-20.00")
        _add_pending_categorization(self.entity, "split-1", "Payroll debit", amount="-100.00")
        _add_pending_categorization(self.entity, "bank-payment", "Card payment", amount="-125.00",
                                    txn_date="2026-02-01")
        _add_pending_categorization(self.entity, "card-payment", "Payment received", amount="125.00",
                                    txn_date="2026-02-03")
        _add_pending_categorization(self.entity, "plain-2", "Vendor Plain", amount="-30.00")
        path = self.entity.staging_dir / "pending-categorization.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        rows[3]["accountName"] = "Amex"
        rows[3]["accountType"] = "credit_card"
        path.write_text(json.dumps(rows), encoding="utf-8")
        propose(self.entity, "plain-1", CATEGORY, "r")
        propose_split(self.entity, "split-1", "r", ["Expenses:Software=70.00", "Liabilities:CreditCard=30.00"])
        pair = propose_transfer(self.entity, ["bank-payment", "card-payment"], "r")
        propose(self.entity, "plain-2", CATEGORY, "r")
        for item_id in ("split-1", pair["source_id"]):  # Put the split and the pair in the same group.
            item_path = queue_module._item_path(self.entity, item_id)
            item = json.loads(item_path.read_text(encoding="utf-8"))
            item["proposed_category"] = CATEGORY
            item_path.write_text(json.dumps(item, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        ids = [item["source_id"] for item in queue_module.list_queue_items(self.entity)
               if item.get("proposed_category") == CATEGORY and item.get("status") == "open"]
        self.assertEqual(len(ids), 4)

        confirmed = confirm_group(self.entity, CATEGORY, SESSION, ts=TS)

        self.assertEqual([item["source_id"] for item in confirmed], ids)
        self.assertEqual({item["status"] for item in confirmed}, {"confirmed"})
        store = store_of(self.entity)
        with store.connection() as conn:
            posted = sorted(row[0] for row in conn.execute("SELECT source_id FROM entries"))
        self.assertEqual(posted, ["bank-payment", "card-payment", "plain-1", "plain-2", "split-1"])
        seals = events_of(self.entity, "ledger-store-sealed")
        self.assertEqual(seals[0]["source_ids"], ["plain-1", "plain-2"])  # the batch, before the others
        self.assertEqual(len(seals), 3)
        self.assertEqual(check_store_integrity(store).status, "ok")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])

    # -- every per-item refusal still refuses and writes nothing --------------------------------------------------

    def assert_refused_without_a_write(self, error, pattern: str = "") -> None:
        files_before = company_files(self.entity)
        digest_before = store_of(self.entity).content_digest()
        events_before = store_of(self.entity).load_audit_events()
        with self.assertRaisesRegex(error, pattern):
            confirm_group(self.entity, CATEGORY, SESSION, ts=TS)
        self.assertEqual(store_of(self.entity).content_digest(), digest_before)
        self.assertEqual(store_of(self.entity).load_audit_events(), events_before)
        self.assertEqual(company_files(self.entity), files_before)
        self.assertEqual(check_store_integrity(store_of(self.entity)).status, "ok")

    def test_self_approval_refuses_the_whole_group(self) -> None:
        _add_pending_categorization(self.entity, "g1", "Vendor One")
        _add_pending_categorization(self.entity, "g2", "Vendor Two")
        os.environ["SLASHBOOKS_ACTOR"] = "person:two"
        propose(self.entity, "g1", CATEGORY, "r")
        os.environ["SLASHBOOKS_ACTOR"] = "agent:one"
        propose(self.entity, "g2", CATEGORY, "r")
        os.environ["SLASHBOOKS_FORBID_SELF_APPROVAL"] = "1"
        self.assert_refused_without_a_write(queue_module.SelfApprovalError)

    def test_ask_first_refuses_the_whole_group(self) -> None:
        self.entity.trust_policy["ask_first_above"] = "500.00"
        (self.entity.path / "trust-policy.json").write_text(json.dumps(self.entity.trust_policy) + "\n")
        _add_pending_categorization(self.entity, "g1", "Vendor One", amount="-10.00")
        _add_pending_categorization(self.entity, "g2", "Vendor Two", amount="-900.00")
        _add_pending_categorization(self.entity, "g3", "Vendor Three", amount="-20.00")
        propose_group(self.entity, ["g1", "g2", "g3"], CATEGORY, "r")
        os.environ["SLASHBOOKS_ENFORCE_ASK_FIRST"] = "1"
        self.assert_refused_without_a_write(queue_module.AskFirstError)

    def test_a_row_before_its_account_open_date_refuses_the_whole_group(self) -> None:
        _add_pending_categorization(self.entity, "g1", "Vendor One", txn_date="2026-01-10")
        _add_pending_categorization(self.entity, "g2", "Vendor Two", txn_date="2025-12-15")
        _add_pending_categorization(self.entity, "g3", "Vendor Three", txn_date="2026-01-12")
        propose_group(self.entity, ["g1", "g2", "g3"], CATEGORY, "r")
        self.assert_refused_without_a_write(AccountNotOpenError)

    def test_a_changed_source_row_refuses_the_whole_group(self) -> None:
        ids = self.group(5)
        path = self.entity.staging_dir / "pending-categorization.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        rows[3]["description"] = "Changed after the proposal"
        path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        self.assert_refused_without_a_write(ValueError, "^Queue item 'row-03': Staged source evidence changed")
        self.assertEqual([read_queue_item(self.entity, i)["status"] for i in ids], ["open"] * len(ids))

    def test_a_changed_proposal_refuses_the_whole_group(self) -> None:
        ids = self.group(5)
        item_path = queue_module._item_path(self.entity, ids[2])
        item = json.loads(item_path.read_text(encoding="utf-8"))
        item["source_hashes"] = {ids[2]: "0" * 64}
        item_path.write_text(json.dumps(item, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.assert_refused_without_a_write(ValueError, "^Queue item 'row-02': .*source evidence")


class FeedMarkTests(unittest.TestCase):
    """Rows from a connected feed keep their ``feed-provider`` and ``feed-row-digest`` marks, also past the 40 rows
    that one intake lookup takes."""

    def test_a_group_of_feed_rows_gets_the_marks_that_one_confirm_for_each_gives(self) -> None:
        import contextlib
        import io
        from bookkeeping.cli import main
        from bookkeeping.entity import load_entity

        with tempfile.TemporaryDirectory() as tmp:
            company = Path(tmp) / "books"

            def cli(*argv: str) -> None:
                with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(out):
                    code = main(list(argv))
                self.assertEqual(code, 0, out.getvalue())

            cli("entity", "init", str(company), "--name", "Synthetic Co", "--business-type", "consulting",
                "--cutover-date", "2026-01-01")
            cli("entity", "bank-account-map", str(company), "--feed-account-id", "acct_1",
                "--account", "Assets:Bank:Checking", "--provider", "plaid")
            rows = [{"id": f"r{number}", "accountId": "acct_1", "date": f"2026-08-{1 + number % 27:02d}",
                     "amount": -(5 + number), "description": f"feed row {number}"} for number in range(45)]
            (Path(tmp) / "rows.json").write_text(json.dumps(rows))
            cli("ingest", str(Path(tmp) / "rows.json"), "--entity", str(company), "--no-feed-window",
                "--source", "plaid", "--intake-only")
            ids = [f"plaid:r{number}" for number in range(45)]
            cli("queue", "propose-group", "--entity", str(company), *[part for sid in ids for part in ("--source-id", sid)],
                "--category", "Expenses:Software", "--reasoning", "test")
            other = Path(tmp) / "other" / "books"
            shutil.copytree(company, other)

            confirm_group(load_entity(company), "Expenses:Software", SESSION, ts=TS)
            one_by_one = load_entity(other)
            for item in queue_module.list_queue_items(one_by_one, status="open"):
                confirm(one_by_one, item["source_id"], SESSION, ts=TS)

            first, second = LedgerStore(default_store_path(company)), LedgerStore(default_store_path(other))
            self.assertEqual(first.content_digest(), second.content_digest())
            with first.connection() as conn:
                marks = {sid: dict(json.loads(meta)) for sid, meta in
                         conn.execute("SELECT source_id, metadata_json FROM entries")}
            self.assertEqual(sorted(marks), sorted(ids))
            self.assertEqual({meta.get("feed-provider") for meta in marks.values()}, {"plaid"})
            self.assertTrue(all(len(meta.get("feed-row-digest", "")) == 64 for meta in marks.values()))
            self.assertEqual(check_store_integrity(first).status, "ok")


class SingleConfirmTests(unittest.TestCase):
    def test_a_single_confirm_keeps_one_seal_and_its_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity = _make_entity(Path(tmp))
            _add_pending_categorization(entity, "one", "Vendor One")
            propose(entity, "one", CATEGORY, "r")
            self.assertEqual(confirm(entity, "one", SESSION, ts=TS)["status"], "confirmed")
            kinds = [event["type"] for event in store_of(entity).load_audit_events()]
            self.assertEqual(kinds[-4:], ["queue-finalization-intent", "intent", "entry-written",
                                          "ledger-store-sealed"])
            self.assertEqual(check_store_integrity(store_of(entity)).status, "ok")


if __name__ == "__main__":
    unittest.main()
