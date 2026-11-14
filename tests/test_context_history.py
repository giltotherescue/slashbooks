"""Context-only history: a feed account whose books already cover its history through a date.

The scenario: a company fed one bank account from provider A (BankSync) and later maps the
same account to provider B (Plaid). B sends the rows A already delivered, plus older rows.
The synthetic numbers are 40 rows that A already posted and 60 older rows.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping import context_history  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.entity import add_account, init_entity, load_entity, map_bank_account  # noqa: E402
from bookkeeping.ledger.importer import _load_pending_categorization, import_transactions  # noqa: E402
from bookkeeping.ledger.store import LedgerStore, default_store_path  # noqa: E402
from bookkeeping.queue import list_duplicate_candidates, list_queue_items, set_taught  # noqa: E402
from bookkeeping.readiness import _open_work  # noqa: E402

CHECKING = "Assets:Bank:Operating-Checking"
SAVINGS = "Assets:Bank:Reserve-Savings"
POST = lambda t: ("Expenses:Services", "high")  # noqa: E731
CUTOVER = "2026-03-31"
START = date(2026, 1, 1)


def _day(offset: int) -> str:
    return (START + timedelta(days=offset)).isoformat()


def _row(source_id: str, day: str, amount: str, description: str, account: str = "plaid_1") -> dict:
    return {"id": source_id, "date": day, "description": description, "amount": amount,
            "accountId": account, "accountName": "Checking", "accountType": "checking", "pending": False}


def _a_rows() -> list[dict]:
    """40 rows that provider A delivered (ids ``a<n>``), 2026-02-01 .. 2026-03-12, all posted."""
    return [_row(f"a{i}", _day(31 + i), f"-{10 + i}.00", f"VENDOR {i}", "bs_1") for i in range(40)]


def _b_rows() -> list[dict]:
    """What provider B sends: the same 40 rows with new ids, plus 60 older rows (2025-10-01 ..)."""
    same = [_row(f"b{i}", _day(31 + i), f"-{10 + i}.00", f"VENDOR {i}") for i in range(40)]
    older = [_row(f"o{i}", (date(2025, 10, 1) + timedelta(days=i)).isoformat(), f"-{200 + i}.50", f"OLDER {i}")
             for i in range(60)]
    return same + older


def _company(tmp: str, *, mode: str | None = "context_only", cutover: str | None = CUTOVER) -> Path:
    root = Path(tmp).resolve() / "company"
    init_entity(root, name="Harbor Studio", business_type="consulting", legal_structure="llc")
    for account in (CHECKING, SAVINGS, "Expenses:Services"):
        add_account(root, account, open_date="2025-01-01")
    map_bank_account(root, "bs_1", CHECKING, provider="banksync")
    map_bank_account(root, "plaid_1", CHECKING, provider="plaid", cutover_date=cutover,
                     history_before_cutover=mode)
    map_bank_account(root, "plaid_2", SAVINGS, provider="plaid")
    import_transactions(load_entity(root), [{**r, "id": r["id"]} for r in _a_rows()], "old", POST)
    return root


def _ingest(root: Path, rows: list[dict], *extra: str) -> tuple[int, str]:
    path = root.parent / "feed.json"
    path.write_text(json.dumps({"transactions": rows}), encoding="utf-8")
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = main(["ingest", str(path), "--entity", str(root), "--source", "plaid", "--no-feed-window", *extra])
    return code, out.getvalue()


def _cli(*argv: str) -> tuple[int, dict | str]:
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = main(list(argv))
    text = out.getvalue()
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text


def _counts(root: Path) -> dict[str, int]:
    entity = load_entity(root)
    posted = [e for e in LedgerStore(default_store_path(root)).load_entries()]
    return {"posted": len(posted), "queued": len(_load_pending_categorization(entity)),
            "held": len(list_duplicate_candidates(entity)),
            "context": len(context_history.list_context_history(entity))}


def _posted_ids(root: Path) -> set[str]:
    return {e.source_id for e in LedgerStore(default_store_path(root)).load_entries()}


class SwitchProviderTableTests(unittest.TestCase):
    def test_default_review_mode_is_unchanged_and_holds_every_early_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp, mode=None)
            code, out = _ingest(root, _b_rows())
            self.assertEqual(code, 0)
            # Today's measured behavior: posted 40 (from A), nothing queued, 100 held in To check.
            self.assertEqual(_counts(root), {"posted": 40, "queued": 0, "held": 100, "context": 0})
            self.assertNotIn("context only", out)

    def test_no_cutover_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp, mode=None, cutover=None)
            _ingest(root, _b_rows())
            self.assertEqual(_counts(root), {"posted": 40, "queued": 60, "held": 40, "context": 0})

    def test_context_only_makes_no_to_do_and_no_to_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            code, out = _ingest(root, _b_rows())
            self.assertEqual(code, 0)
            self.assertEqual(_counts(root), {"posted": 40, "queued": 0, "held": 0, "context": 100})
            self.assertEqual(_posted_ids(root), {f"a{i}" for i in range(40)})  # nothing from B is posted
            self.assertIn("context only", out)
            self.assertNotIn("go to review", out)
            entity = load_entity(root)
            self.assertEqual(list_queue_items(entity, status="actionable"), [])
            self.assertEqual(list_queue_items(entity), [])
            # A period close sees no open work for these rows.
            blockers, snapshot, _, _ = _open_work(root, date(2025, 10, 1), date(2026, 3, 31))
            self.assertEqual((blockers, snapshot), ([], []))

    def test_one_summary_per_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            _ingest(root, _b_rows())
            code, doc = _cli("queue", "context-history", "--entity", str(root), "--json")
            self.assertEqual(code, 0)
            self.assertEqual(doc["total_count"], 100)
            (item,) = doc["accounts"]
            self.assertEqual((item["account"], item["feed"], item["count"], item["cutover_date"]),
                             (CHECKING, "plaid", 100, CUTOVER))
            self.assertEqual((item["first_date"], item["last_date"]), ("2025-10-01", "2026-03-12"))
            self.assertEqual(item["message"], "100 earlier transactions from plaid were not imported because the "
                                              f"books already cover them through {CUTOVER}.")
            code, page = _cli("queue", "context-history", "--entity", str(root), "--account", CHECKING,
                              "--json", "--limit", "10")
            self.assertEqual((code, len(page["rows"]), page["total_count"]), (0, 10, 100))

    def test_a_resync_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            _ingest(root, _b_rows())
            before = (root / "staging" / "context-history.json").read_text()
            _ingest(root, _b_rows())
            self.assertEqual(_counts(root), {"posted": 40, "queued": 0, "held": 0, "context": 100})
            self.assertEqual((root / "staging" / "context-history.json").read_text(), before)

    def test_rows_after_the_cutover_flow_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            new = [_row("n1", "2026-04-01", "-77.00", "NEW VENDOR"),          # first day after: to do
                   _row("n2", "2026-04-02", "-12.00", "OTHER VENDOR")]
            _ingest(root, [*_b_rows(), *new])
            self.assertEqual(sorted(i["id"] for i in _load_pending_categorization(load_entity(root))),
                             ["plaid:n1", "plaid:n2"])
            self.assertEqual(_counts(root)["held"], 0)

    def test_an_after_cutover_row_that_repeats_a_posted_row_is_still_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp, cutover="2026-02-10")
            # a15 is 2026-02-16 amount -25.00 from A. B repeats it with a new id and a later cutover-free date.
            _ingest(root, [_row("b15", _day(46), "-25.00", "VENDOR 15")])
            self.assertEqual(_counts(root)["held"], 1)
            self.assertEqual(_counts(root)["context"], 0)

    def test_backfill_still_imports_early_rows_normally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            _ingest(root, _b_rows()[40:], "--backfill")
            self.assertEqual(_counts(root), {"posted": 40, "queued": 60, "held": 0, "context": 0})

    def test_no_learned_rule_posts_a_context_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            set_taught(load_entity(root), "OLDER", "Expenses:Services", by="owner", by_name="Owner",
                       ts="2026-01-01T00:00:00Z")
            _ingest(root, _b_rows()[40:])
            self.assertEqual(_counts(root), {"posted": 40, "queued": 0, "held": 0, "context": 60})

    def test_another_mapping_without_the_mode_is_unaffected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            map_bank_account(root, "plaid_2", SAVINGS, provider="plaid", cutover_date=CUTOVER)  # review mode
            _ingest(root, [_row("s1", "2026-01-05", "-9.00", "SAVINGS ROW", "plaid_2"),
                           _row("c1", "2026-01-05", "-9.00", "CHECKING ROW")])
            entity = load_entity(root)
            self.assertEqual([c["source_id"] for c in list_duplicate_candidates(entity)], ["plaid:s1"])
            self.assertEqual([r["source_id"] for r in context_history.list_context_history(entity)], ["plaid:c1"])


class MappingTests(unittest.TestCase):
    def test_the_mode_needs_a_cutover_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp, mode=None, cutover=None)
            with self.assertRaisesRegex(ValueError, "needs a cutover_date"):
                map_bank_account(root, "plaid_1", CHECKING, history_before_cutover="context_only")
            with self.assertRaisesRegex(ValueError, "must be 'review' or 'context_only'"):
                map_bank_account(root, "plaid_1", CHECKING, cutover_date=CUTOVER, history_before_cutover="skip")

    def test_clearing_the_cutover_needs_the_mode_cleared_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = main(["entity", "bank-account-map", str(root), "--feed-account-id", "plaid_1",
                             "--account", CHECKING, "--clear-cutover-date"])
            self.assertEqual(code, 1)
            self.assertIn("needs a cutover_date", err.getvalue())
            code, _ = _cli("entity", "bank-account-map", str(root), "--feed-account-id", "plaid_1",
                           "--account", CHECKING, "--clear-cutover-date", "--history-before-cutover", "review")
            self.assertEqual(code, 0)
            entry = json.loads((root / "entity.json").read_text())["bank_account_mappings"]["plaid_1"]
            self.assertNotIn("cutover_date", entry)
            self.assertNotIn("history_before_cutover", entry)

    def test_review_is_stored_as_no_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            map_bank_account(root, "plaid_1", CHECKING, history_before_cutover="review")
            entry = json.loads((root / "entity.json").read_text())["bank_account_mappings"]["plaid_1"]
            self.assertEqual(entry["cutover_date"], CUTOVER)
            self.assertNotIn("history_before_cutover", entry)

    def test_a_flat_mapping_is_never_context_only(self) -> None:
        from bookkeeping.bank_accounts import context_only_ids
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            self.assertEqual(context_only_ids(load_entity(root), [_row("x", "2020-01-01", "-1", "x", "plaid_2")]), {})


class SafetyTests(unittest.TestCase):
    def test_a_row_can_be_released_on_purpose_and_stays_released(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            _ingest(root, _b_rows())
            code, doc = _cli("queue", "context-release", "--entity", str(root), "--source-id", "plaid:o3")
            self.assertEqual((code, doc["released"], doc["queued_for_review"]), (0, 1, 1))
            self.assertEqual([i["id"] for i in _load_pending_categorization(load_entity(root))], ["plaid:o3"])
            _ingest(root, _b_rows())  # a resync neither duplicates nor re-hides it
            self.assertEqual(_counts(root), {"posted": 40, "queued": 1, "held": 0, "context": 99})
            code, _ = _cli("queue", "context-release", "--entity", str(root), "--source-id", "plaid:o3")
            self.assertEqual(code, 1)  # no longer context

    def test_release_of_an_unknown_row_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            _ingest(root, _b_rows())
            code, _ = _cli("queue", "context-release", "--entity", str(root),
                           "--source-id", "plaid:o3", "--source-id", "plaid:missing")
            self.assertEqual(code, 1)
            self.assertEqual(_counts(root), {"posted": 40, "queued": 0, "held": 0, "context": 100})

    def test_cutover_set_too_late_is_found_and_released(self) -> None:
        """The owner chose 2026-03-31, but A stops at 2026-03-12 and B rows to 03-31 are real."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            real = [_row("r1", "2026-03-20", "-310.00", "REAL EXPENSE"), _row("r2", "2026-03-30", "-45.00", "REAL TOO"),
                    _row("b39", _day(31 + 39), "-49.00", "VENDOR 39")]  # repeats A's a39
            _ingest(root, [*_b_rows()[40:], *real])
            self.assertEqual(_counts(root)["context"], 63)
            self.assertEqual(_counts(root)["queued"], 0)  # silently not imported: nothing asks the owner
            code, doc = _cli("queue", "context-history", "--entity", str(root), "--json")
            self.assertEqual(doc["accounts"][0]["last_date"], "2026-03-30")  # the summary shows the end date
            # The owner moves the cutover earlier and releases what is after it, with the duplicate check on.
            map_bank_account(root, "plaid_1", CHECKING, cutover_date="2026-03-11")
            code, doc = _cli("queue", "context-release", "--entity", str(root), "--account", CHECKING,
                             "--after", "2026-03-11")
            self.assertEqual(code, 0)
            self.assertEqual(doc["released"], 3)
            self.assertEqual(doc["held_as_duplicate"], 1)   # b39 repeats a39 and is held, not posted twice
            self.assertEqual(sorted(i["id"] for i in _load_pending_categorization(load_entity(root))),
                             ["plaid:r1", "plaid:r2"])
            self.assertEqual(_counts(root)["context"], 60)

    def test_cutover_set_too_early_holds_duplicates_as_today(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp, cutover="2025-12-31")  # after the 60 older rows, before A's history
            _ingest(root, _b_rows())
            # Every old row is on or before the cutover; the 40 repeats are after it and held as duplicates.
            self.assertEqual(_counts(root), {"posted": 40, "queued": 0, "held": 40, "context": 60})

    def test_rows_held_by_an_earlier_import_can_be_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp, mode=None)
            _ingest(root, _b_rows())
            self.assertEqual(_counts(root)["held"], 100)
            map_bank_account(root, "plaid_1", CHECKING, history_before_cutover="context_only")
            _ingest(root, _b_rows())  # a resync does not duplicate the held rows
            self.assertEqual(_counts(root), {"posted": 40, "queued": 0, "held": 100, "context": 0})
            code, doc = _cli("queue", "context-adopt", "--entity", str(root))
            self.assertEqual((code, doc["moved"], doc["left_in_review"]), (0, 100, 0))
            self.assertEqual(_counts(root), {"posted": 40, "queued": 0, "held": 0, "context": 100})
            self.assertEqual(load_entity(root) and list_queue_items(load_entity(root)), [])
            _ingest(root, _b_rows())
            self.assertEqual(_counts(root), {"posted": 40, "queued": 0, "held": 0, "context": 100})

    def test_adopt_leaves_a_row_after_the_cutover_in_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp, mode=None, cutover="2026-02-10")
            _ingest(root, [_row("b15", _day(46), "-25.00", "VENDOR 15"), _row("o1", "2026-01-05", "-5.00", "OLD")])
            map_bank_account(root, "plaid_1", CHECKING, history_before_cutover="context_only")
            code, doc = _cli("queue", "context-adopt", "--entity", str(root))
            self.assertEqual((doc["moved"], doc["left_in_review"]), (1, 1))

    def test_pending_rows_before_the_cutover_are_skipped_and_the_posted_row_follows_its_own_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            pend = _row("p1", "2026-03-30", "-60.00", "CAFE", )
            pend["pending"] = True
            _ingest(root, [pend])
            self.assertEqual(_counts(root), {"posted": 40, "queued": 0, "held": 0, "context": 0})
            # It posts on a day after the cutover: it flows as a new row. Posted on or before: context.
            posted_after = {**_row("p1", "2026-04-02", "-60.00", "CAFE"), "pendingTransactionId": "p1"}
            posted_before = _row("p2", "2026-03-30", "-61.00", "CAFE TWO")
            _ingest(root, [posted_after, posted_before])
            self.assertEqual(_counts(root), {"posted": 40, "queued": 1, "held": 0, "context": 1})

    def test_a_provider_that_resends_a_row_with_new_values_does_not_create_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            _ingest(root, [_row("c1", "2026-01-10", "-10.00", "FIRST")])
            _ingest(root, [_row("c1", "2026-01-10", "-11.00", "CORRECTED")])
            self.assertEqual(_counts(root), {"posted": 40, "queued": 0, "held": 0, "context": 1})
            (row,) = context_history.list_context_history(load_entity(root))
            self.assertEqual(row["amount"], "-10.00")  # the first capture stays for audit

    def test_transfer_legs_on_both_sides_of_the_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            map_bank_account(root, "plaid_2", SAVINGS, provider="plaid")
            leg_out = _row("t1", "2026-03-31", "-500.00", "TRANSFER TO SAVINGS")            # context
            leg_in = _row("t2", "2026-04-01", "500.00", "TRANSFER FROM CHECKING", "plaid_2")  # after: a to-do
            _ingest(root, [leg_out, leg_in])
            self.assertEqual([i["id"] for i in _load_pending_categorization(load_entity(root))], ["plaid:t2"])
            self.assertEqual(_counts(root)["context"], 1)

    def test_a_damaged_context_file_stops_the_import_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            (root / "staging").mkdir(exist_ok=True)
            (root / "staging" / "context-history.json").write_text("{not json", encoding="utf-8")
            code, _ = _ingest(root, [*_b_rows()[:2], _row("n1", "2026-04-01", "-1.00", "NEW")])
            self.assertEqual(code, 1)
            self.assertEqual(_load_pending_categorization(load_entity(root)), [])

    def test_the_importer_itself_never_posts_a_context_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _company(tmp)
            row = _row("plaid:z1", "2026-01-02", "-3.00", "Z")
            result = import_transactions(load_entity(root), [row], "s", POST,
                                         context_only_cutovers={"plaid:z1": CUTOVER})
            self.assertEqual((result.new_entries, result.pending_categorization, result.duplicate_candidates,
                              result.context_only), (0, 0, 0, 1))


if __name__ == "__main__":
    unittest.main()
