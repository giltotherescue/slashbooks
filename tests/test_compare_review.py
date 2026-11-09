"""Typed review of a QuickBooks comparison difference (``books compare review-diff``, request version 1).

The review binds the exact difference that the engine derives again (grain, account, period, ours, reference,
delta and category) and the digest of the reference files. It validates the request before anything that can
write, writes one deterministic record under the company lock, and refuses with a typed code and no write. It needs
no earlier saved compare. The legacy ``books compare accept-diff`` keeps its behavior.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from golden_support import run_cli  # noqa: E402
from test_compare import FIXTURE_LEDGER_WITH_DELTA, _QB_MATCH, _make_entity, _tree_state  # noqa: E402

_FROM = date(2026, 1, 1)
_TO = date(2026, 3, 31)
_KEY = "pnl:Income:Consulting-Revenue"
_OTHER = "pnl:Expenses:Office-Supplies"
_SOFTWARE = "pnl:Expenses:Software"  # in the February compare only
_LEGACY_FIELDS = ["key", "grain", "account", "ours", "reference", "delta", "category", "status", "note", "re_opened"]
_EXPLANATION = "The extra payment is a deposit that QuickBooks did not record."
_DROP = object()


def _request(**changes) -> dict:
    request = {
        "schema_version": 1,
        "period": {"from": "2026-01-01", "to": "2026-03-31"},
        "reference_digest": _digest(),
        "difference": {"grain": "pnl", "account": "Income:Consulting-Revenue", "ours": "10500.00",
                       "reference": "10000.00", "delta": "500.00", "category": "uncategorized"},
        "reviewer": "person-17",
        "explanation": _EXPLANATION,
    }
    for name, value in changes.items():
        if name in request["difference"]:
            request["difference"] = {**request["difference"], name: value}
        elif value is _DROP:
            request.pop(name, None)
        else:
            request[name] = value
    return request


def _software_request(**changes) -> dict:
    """An exact request for the Software difference of the February compare."""
    return _request(period={"from": "2026-01-01", "to": "2026-02-28"}, account="Expenses:Software",
                    ours="200.00", reference="350.00", delta="-150.00", **changes)


def _text(rows: list) -> str:
    return json.dumps(rows, indent=2) + "\n"


def _digest(folder: Path = _QB_MATCH) -> str:
    from bookkeeping.compare import reference_digest
    return reference_digest(folder)


class _Companies(unittest.TestCase):
    """One saved compare of a company whose books differ from the reference, copied for each test."""

    @classmethod
    def setUpClass(cls):
        from bookkeeping.compare import compare_period
        from bookkeeping.entity import load_entity
        from bookkeeping.ledger.migrate import migrate_beancount_to_store
        cls._class_tmp = tempfile.mkdtemp()
        cls._seed = Path(cls._class_tmp) / "seed"
        _make_entity(cls._seed, FIXTURE_LEDGER_WITH_DELTA)
        assert migrate_beancount_to_store(cls._seed).success
        compare_period(load_entity(cls._seed), _QB_MATCH, _FROM, _TO)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._class_tmp, ignore_errors=True)

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._tmp = Path(self._tmpdir)
        self.path = Path(shutil.copytree(self._seed, self._tmp / "company"))

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @property
    def differences(self) -> Path:
        return self.path / "reports" / "migration-confidence" / "differences.json"

    def rows(self) -> list:
        return json.loads(self.differences.read_text(encoding="utf-8"))

    def stored(self) -> dict:
        return {d["key"]: d for d in self.rows()}

    def write_rows(self, rows: list) -> None:
        self.differences.write_text(_text(rows), encoding="utf-8")

    def review(self, request, qb_folder: Path = _QB_MATCH, path: Path | None = None) -> dict:
        from bookkeeping.compare import review_difference
        return review_difference(path or self.path, qb_folder, request)

    def assert_refused(self, result: dict, code: str) -> dict:
        self.assertEqual((result["status"], result["code"]), ("refused", code), result)
        self.assertEqual(result["schema_version"], 1)
        self.assertIsInstance(result["message"], str)
        return result

    def refused_without_write(self, request, code: str, qb_folder: Path = _QB_MATCH) -> dict:
        """The review refuses, and no file or directory of the company changes, including mtimes."""
        before = _tree_state(self.path)
        result = self.assert_refused(self.review(request, qb_folder), code)
        self.assertEqual(_tree_state(self.path), before)
        return result

    def qb_copy(self, consulting_reference: str = "10000.00", software_reference: str = "350.00") -> Path:
        """A copy of the reference reports with other Consulting Revenue or Software totals."""
        folder = Path(shutil.copytree(_QB_MATCH, self._tmp / f"qb-{consulting_reference}-{software_reference}"))
        pnl = folder / "profit_and_loss.csv"
        text = pnl.read_text(encoding="utf-8")
        self.assertIn('Consulting Revenue,"10000.00"', text)
        self.assertIn('Software,"350.00"', text)
        text = text.replace('Consulting Revenue,"10000.00"', f'Consulting Revenue,"{consulting_reference}"')
        pnl.write_text(text.replace('Software,"350.00"', f'Software,"{software_reference}"'), encoding="utf-8")
        return folder

    def make_fresh(self) -> None:
        """A company that compared only read-only: no saved differences and no migration-confidence folder."""
        shutil.rmtree(self.differences.parent)


class TestExactReview(_Companies):
    def test_exact_review_writes_the_bound_record(self):
        before_rows = self.rows()
        result = self.review(_request(reviewed_at="2026-11-09T15:04:05Z"))
        digest = _digest()
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        expected_review = {
            "schema_version": 1,
            "period": {"from": "2026-01-01", "to": "2026-03-31"},
            "reference_digest": digest,
            "ours": "10500.00", "reference": "10000.00", "delta": "500.00", "category": "uncategorized",
            "reviewer": "person-17",
            "explanation": _EXPLANATION,
            "reviewed_at": "2026-11-09T15:04:05Z",
        }
        expected_record = {
            "key": _KEY, "grain": "pnl", "account": "Income:Consulting-Revenue",
            "ours": "10500.00", "reference": "10000.00", "delta": "500.00", "category": "uncategorized",
            "status": "accepted", "note": _EXPLANATION,
            "re_opened": False, "review": expected_review, "review_drift": [],
        }
        self.assertEqual(result, {"schema_version": 1, "status": "accepted", "key": _KEY, "record": expected_record,
                                  "review": expected_review, "review_drift": [], "reference_digest": digest})
        # The file is the saved list with only the selected row replaced in place: every other row keeps its bytes.
        expected_rows = [expected_record if row["key"] == _KEY else row for row in before_rows]
        self.assertEqual(self.differences.read_text(encoding="utf-8"), _text(expected_rows))

    def test_without_a_time_the_engine_records_none(self):
        result = self.review(_request())
        self.assertIsNone(result["record"]["review"]["reviewed_at"])
        self.assertIsNone(self.stored()[_KEY]["review"]["reviewed_at"])

    def test_amounts_bind_by_value(self):
        result = self.review(_request(ours="10500", reference="10000.0", delta="500.000"))
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["record"]["review"]["delta"], "500.00")

    def test_multi_line_explanation_is_kept(self):
        result = self.review(_request(explanation="First line.\n\tSecond line."))
        self.assertEqual(result["record"]["review"]["explanation"], "First line.\n\tSecond line.")

    def test_review_clears_re_opened(self):
        rows = self.rows()
        for row in rows:
            if row["key"] == _KEY:
                row.update(status="open", re_opened=True, note="Changed after an earlier acceptance")
        self.write_rows(rows)
        result = self.review(_request())
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(self.stored()[_KEY]["re_opened"])
        self.assertEqual(self.stored()[_KEY]["status"], "accepted")

    def test_same_inputs_give_the_same_bytes(self):
        other = Path(shutil.copytree(self._seed, self._tmp / "other"))
        request = _request(reviewed_at="2026-11-09T15:04:05.250+02:00")
        first = self.review(request)
        second = self.review(request, path=other)
        self.assertEqual(first, second)
        other_file = other / "reports" / "migration-confidence" / "differences.json"
        self.assertEqual(self.differences.read_bytes(), other_file.read_bytes())

    def test_reference_digest_ignores_the_folder_location(self):
        self.assertEqual(_digest(self.qb_copy()), _digest())
        self.assertNotEqual(_digest(self.qb_copy(software_reference="351.00")), _digest())

    def test_compare_after_review_keeps_the_review(self):
        from bookkeeping.compare import compare_period
        from bookkeeping.entity import load_entity
        self.review(_request())
        report = compare_period(load_entity(self.path), _QB_MATCH, _FROM, _TO).to_dict()
        row = next(d for d in report["material_diffs"] if d["key"] == _KEY)
        self.assertEqual((row["status"], row["review_drift"]), ("accepted", []))
        self.assertEqual(row["review"]["reviewer"], "person-17")
        self.assertEqual(self.stored()[_KEY]["review_drift"], [])


class TestFreshEnforcedEquivalent(_Companies):
    """A company that compared only read-only can review: the review itself writes its one record."""

    def test_fresh_enforced_equivalent_no_file_creates_one_record(self):
        from bookkeeping.compare import compare_period
        from bookkeeping.entity import load_entity
        self.make_fresh()
        before = _tree_state(self.path)
        result = self.review(_request(reviewed_at="2026-11-09T15:04:05Z"))
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual(self.differences.read_text(encoding="utf-8"), _text([result["record"]]))
        # Only the migration-confidence folder and its one file are new.
        after = _tree_state(self.path)
        added = sorted(set(after) - set(before))
        self.assertEqual(added, ["reports/migration-confidence", "reports/migration-confidence/differences.json"])
        self.assertEqual({k: v for k, v in after.items() if k in before and k != "reports"},
                         {k: v for k, v in before.items() if k != "reports"})
        # The record is the one that a saving compare writes for that difference, plus the review.
        control = Path(shutil.copytree(self.path, self._tmp / "control"))
        shutil.rmtree(control / "reports" / "migration-confidence")
        compare_period(load_entity(control), _QB_MATCH, _FROM, _TO)
        saved = {row["key"]: row for row in json.loads(
            (control / "reports" / "migration-confidence" / "differences.json").read_text(encoding="utf-8"))}
        self.assertEqual({**saved[_KEY], "status": "accepted", "note": _EXPLANATION,
                          "review": result["review"], "review_drift": []}, result["record"])
        # An exact retry gives the same result and writes nothing.
        settled = _tree_state(self.path)
        self.assertEqual(self.review(_request(reviewed_at="2026-11-09T15:04:05Z")), result)
        self.assertEqual(_tree_state(self.path), settled)

    def test_fresh_enforced_equivalent_file_without_the_key_appends_one_record(self):
        old = self.differences.read_text(encoding="utf-8")
        self.assertNotIn(_SOFTWARE, old)
        result = self.review(_software_request())
        self.assertEqual(result["status"], "accepted", result)
        new = self.differences.read_text(encoding="utf-8")
        self.assertEqual(new, _text(json.loads(old) + [result["record"]]))
        # Byte for byte, the old rows are a prefix of the new file: only the appended row is new.
        self.assertTrue(new.startswith(old[: -len("\n]\n")] + ",\n"))
        # The other current differences of the February compare are not written.
        self.assertEqual(len(json.loads(new)), len(json.loads(old)) + 1)
        settled = _tree_state(self.path)
        self.assertEqual(self.review(_software_request()), result)
        self.assertEqual(_tree_state(self.path), settled)

    def test_fresh_enforced_equivalent_empty_list_takes_one_record(self):
        self.differences.write_text("[]\n", encoding="utf-8")
        result = self.review(_request())
        self.assertEqual(self.differences.read_text(encoding="utf-8"), _text([result["record"]]))

    def test_fresh_enforced_equivalent_mismatch_writes_nothing(self):
        self.make_fresh()
        result = self.refused_without_write(_request(delta="499.00"), "DIFFERENCE_CHANGED")
        self.assertEqual(result["changed_fields"], ["delta"])
        self.refused_without_write(_request(account="Income:Other"), "DIFFERENCE_NOT_FOUND")
        self.refused_without_write(_request(schema_version=999), "REQUEST_INVALID")
        self.assertFalse(self.differences.parent.exists())


class TestReviewRefusals(_Companies):
    def test_each_changed_amount_or_category_refuses(self):
        cases = {"ours": "10499.99", "reference": "10000.01", "delta": "499.00", "category": "our-bug"}
        for name, value in cases.items():
            with self.subTest(field=name):
                result = self.refused_without_write(_request(**{name: value}), "DIFFERENCE_CHANGED")
                self.assertEqual(result["changed_fields"], [name])
                self.assertEqual(result["key"], _KEY)
                self.assertEqual(result["current"], {
                    "grain": "pnl", "account": "Income:Consulting-Revenue", "ours": "10500.00",
                    "reference": "10000.00", "delta": "500.00", "category": "uncategorized"})
                self.assertEqual(result["reference_digest"], _digest())

    def test_another_period_refuses(self):
        result = self.refused_without_write(_request(period={"from": "2026-01-01", "to": "2026-02-28"}),
                                            "DIFFERENCE_CHANGED")
        self.assertEqual(result["changed_fields"], ["ours", "delta"])
        self.assertEqual(result["current"]["ours"], "8000.00")

    def test_changed_reference_reports_refuse(self):
        folder = self.qb_copy("10010.00")
        result = self.refused_without_write(_request(), "REFERENCE_CHANGED", qb_folder=folder)
        self.assertEqual(result["reference_digest"], _digest(folder))
        result = self.refused_without_write(_request(reference_digest=_digest(folder)), "DIFFERENCE_CHANGED",
                                            qb_folder=folder)
        self.assertEqual(result["changed_fields"], ["reference", "delta"])

    def test_changed_category_on_the_saved_record_refuses(self):
        from bookkeeping.compare import categorize_diff
        from bookkeeping.entity import load_entity
        self.assertTrue(categorize_diff(load_entity(self.path), _KEY, "our-bug"))
        result = self.refused_without_write(_request(), "DIFFERENCE_CHANGED")
        self.assertEqual(result["changed_fields"], ["category"])

    def test_another_grain_or_account_is_not_found(self):
        for changes in ({"grain": "bs"}, {"account": "Income:Other"}):
            with self.subTest(changes=changes):
                self.refused_without_write(_request(**changes), "DIFFERENCE_NOT_FOUND")

    def test_corrupt_differences_file_refuses(self):
        good = self.rows()
        first = self.review(_request())
        reviewed = self.stored()[_KEY]
        self.write_rows(good)

        def with_row(target, /, **fields):
            return [dict(row, **fields) if row["key"] == target else row for row in good]

        forged = dict(reviewed, review=dict(reviewed["review"], reviewer=["forged"]))
        cases = {
            "not json": "{not json",
            "an object": _text({}),
            "a row that is not an object": _text(["x"]),
            "a missing field": _text([{k: v for k, v in row.items() if k != "key"} for row in good]),
            "a bad amount": _text(with_row(_KEY, ours="ten")),
            "a NaN amount on another row": _text(with_row(_OTHER, ours="NaN")),
            "an Infinity amount on another row": _text(with_row(_OTHER, ours="Infinity")),
            "a JSON NaN number on another row": _text(with_row(_OTHER, ours=float("nan"))),
            "a number amount on another row": _text(with_row(_OTHER, delta=100)),
            "a list category on another row": _text(with_row(_OTHER, category=[])),
            "a bad status": _text(with_row(_OTHER, status="done")),
            "a text re_opened": _text(with_row(_OTHER, re_opened="no")),
            "a duplicate key": _text(good + good[:1]),
            "a key that is not grain and account": _text(with_row(_OTHER, key="pnl:Other")),
            "an extra field on another row": _text(with_row(_OTHER, owner_flag=True)),
            "a review with a bad amount": _text(with_row(_KEY, review=dict(reviewed["review"], delta="x"),
                                                         review_drift=[])),
            "a forged earlier reviewer": _text([forged if row["key"] == _KEY else row for row in good]),
            "a drift name that is not known": _text(with_row(_KEY, review=reviewed["review"], review_drift=["x"])),
            "compact JSON": json.dumps(good),
        }
        self.assertEqual(first["status"], "accepted")
        for name, text in cases.items():
            with self.subTest(case=name):
                self.differences.write_text(text, encoding="utf-8")
                self.refused_without_write(_request(), "DIFFERENCES_FILE_CORRUPT")

    def test_invalid_requests_refuse(self):
        cases = {
            "not an object": [],
            "another version": _request(schema_version=999),
            "a boolean version": _request(schema_version=True),
            "an unknown field": {**_request(), "key": _KEY},
            "no reviewer": _request(reviewer=_DROP),
            "an empty reviewer": _request(reviewer=""),
            "a padded reviewer": _request(reviewer=" person-17"),
            "a reviewer with a tab": _request(reviewer="person\t17"),
            "a list reviewer": _request(reviewer=["person-17"]),
            "no explanation": _request(explanation=_DROP),
            "a blank explanation": _request(explanation="  "),
            "an explanation with NUL": _request(explanation="abc\x00def"),
            "a number amount": _request(ours=10500.0),
            "an exponent amount": _request(delta="5E+2"),
            "a NaN amount": _request(delta="NaN"),
            "an Infinity amount": _request(ours="Infinity"),
            "an unknown category": _request(category="other"),
            "a list category": _request(category=[]),
            "a list grain": _request(grain=["pnl"]),
            "an account with a line break": _request(account="Income:\nConsulting-Revenue"),
            "a bad day": _request(period={"from": "2026-02-30", "to": "2026-03-31"}),
            "a list day": _request(period={"from": ["2026-01-01"], "to": "2026-03-31"}),
            "a reversed period": _request(period={"from": "2026-03-31", "to": "2026-01-01"}),
            "an extra period field": _request(period={"from": "2026-01-01", "to": "2026-03-31", "view": "cash"}),
            "an extra difference field": {**_request(), "difference": {**_request()["difference"], "key": _KEY}},
            "a time without a zone": _request(reviewed_at="2026-11-09T15:04:05"),
            "a time that is a day": _request(reviewed_at="2026-11-09"),
            "a number time": _request(reviewed_at=1.5),
            "no reference digest": _request(reference_digest=_DROP),
            "a reference digest in upper case": _request(reference_digest=_digest().upper()),
            "a short reference digest": _request(reference_digest="sha256:abc"),
        }
        for name, request in cases.items():
            with self.subTest(case=name):
                self.refused_without_write(request, "REQUEST_INVALID")

    def test_invalid_request_is_refused_before_the_lock_and_its_repair(self):
        """A stale entity.json would be repaired under the write lock. An invalid request must not get there."""
        store = self.path / "ledger.sqlite"
        with contextlib.closing(sqlite3.connect(store)) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('currency', 'EUR')")
        (self.path / ".books.lock").unlink(missing_ok=True)
        self.refused_without_write(_request(schema_version=999), "REQUEST_INVALID")
        self.assertNotIn("currency", json.loads((self.path / "entity.json").read_text(encoding="utf-8")))
        self.assertFalse((self.path / ".books.lock").exists())

    def test_missing_company_or_reference_folder_refuses(self):
        self.assert_refused(self.review(_request(), path=self._tmp / "nowhere"), "ENTITY_NOT_FOUND")
        self.assertFalse((self._tmp / "nowhere").exists())
        self.refused_without_write(_request(), "REFERENCE_FOLDER_MISSING", qb_folder=self._tmp / "no-reports")


class TestRepeatedReview(_Companies):
    def test_exact_retry_is_idempotent(self):
        request = _request(reviewed_at="2026-11-09T15:04:05Z")
        first = self.review(request)
        before = _tree_state(self.path)
        second = self.review(request)
        self.assertEqual(second, first)
        self.assertEqual(_tree_state(self.path), before)

    def test_another_acceptance_of_the_same_difference_refuses(self):
        first = self.review(_request(reviewed_at="2026-11-09T15:04:05Z"))
        for changes in ({"reviewer": "person-18"}, {"explanation": "Another reason."},
                        {"reviewed_at": "2026-11-10T09:00:00Z"}, {"reviewed_at": _DROP}):
            with self.subTest(changes=changes):
                result = self.refused_without_write(_request(**changes), "ALREADY_ACCEPTED")
                self.assertEqual(result["accepted_review"], first["review"])
                self.assertEqual(result["review_drift"], [])

    def test_a_legacy_acceptance_can_take_a_typed_review(self):
        from bookkeeping.compare import accept_diff
        from bookkeeping.entity import load_entity
        self.assertTrue(accept_diff(load_entity(self.path), _KEY, "Accepted before"))
        result = self.review(_request())
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.stored()[_KEY]["review"]["reviewer"], "person-17")

    def test_review_decides_under_the_entity_lock(self):
        """Another writer holds the lock first and changes the category: the review sees the change and refuses."""
        from bookkeeping.compare import categorize_diff
        from bookkeeping.entity import load_entity
        from bookkeeping.ledger import importer
        real = importer._entity_write_lock
        entered = []

        @contextlib.contextmanager
        def other_writer_first(path, **kwargs):
            if not entered:
                entered.append((Path(path).resolve(), kwargs.get("repair", True)))
                with real(path, **kwargs):
                    self.assertTrue(categorize_diff(load_entity(self.path), _KEY, "timing-pending"))
            with real(path, **kwargs):
                yield

        with patch.object(importer, "_entity_write_lock", other_writer_first):
            result = self.review(_request())
        self.assertEqual(entered, [(self.path.resolve(), False)])
        self.assertEqual(self.assert_refused(result, "DIFFERENCE_CHANGED")["changed_fields"], ["category"])
        self.assertNotIn("review", self.stored()[_KEY])

    def test_every_writer_of_the_differences_file_takes_the_company_lock(self):
        from bookkeeping.compare import accept_diff, categorize_diff, compare_period
        from bookkeeping.entity import load_entity
        from bookkeeping.ledger import importer
        real = importer._entity_write_lock
        calls = []

        @contextlib.contextmanager
        def recording(path, **kwargs):
            calls.append((Path(path).resolve(), kwargs.get("repair", True)))
            with real(path, **kwargs):
                yield

        writers = {
            "categorize-diff": lambda: categorize_diff(load_entity(self.path), _OTHER, "our-bug"),
            "accept-diff": lambda: accept_diff(load_entity(self.path), _OTHER),
            "compare period": lambda: compare_period(load_entity(self.path), _QB_MATCH, _FROM, _TO),
            "review-diff": lambda: self.review(_request()),
        }
        with patch.object(importer, "_entity_write_lock", recording):
            for name, write in writers.items():
                with self.subTest(writer=name):
                    calls.clear()
                    write()
                    self.assertIn((self.path.resolve(), False), calls)

    def test_a_concurrent_write_during_the_compare_refuses_and_keeps_it(self):
        """A writer changes another row while the review compares: the review writes nothing and keeps that change."""
        from bookkeeping import compare
        from bookkeeping.entity import load_entity
        actual = compare.compare_period

        def interleave(*args, **kwargs):
            self.assertTrue(compare.categorize_diff(load_entity(self.path), _OTHER, "timing-pending"))
            return actual(*args, **kwargs)

        with patch.object(compare, "compare_period", side_effect=interleave):
            result = self.review(_request())
        self.assert_refused(result, "DIFFERENCES_FILE_CHANGED")
        self.assertEqual(self.stored()[_OTHER]["category"], "timing-pending")
        self.assertNotIn("review", self.stored()[_KEY])


class TestLaterChange(_Companies):
    def compare(self, qb_folder: Path, key: str = _KEY) -> dict:
        from bookkeeping.compare import compare_period
        from bookkeeping.entity import load_entity
        report = compare_period(load_entity(self.path), qb_folder, _FROM, _TO).to_dict()
        return next(d for d in report["material_diffs"] + report["immaterial_diffs"] if d["key"] == key)

    def test_a_non_material_change_keeps_the_acceptance_with_a_signal(self):
        self.review(_request())
        folder = self.qb_copy("10010.00")
        row = self.compare(folder)
        self.assertEqual((row["status"], row["re_opened"]), ("accepted", False))
        self.assertEqual((row["reference"], row["delta"]), ("10010.00", "490.00"))
        self.assertEqual((row["review"]["reference"], row["review"]["delta"]), ("10000.00", "500.00"))
        self.assertEqual(row["review_drift"], ["reference_digest", "reference", "delta"])
        self.assertEqual(self.stored()[_KEY]["review_drift"], ["reference_digest", "reference", "delta"])
        code, out, err = run_cli(["compare", "period", "--entity", str(self.path), "--qb-folder", str(folder),
                                  "--from", str(_FROM), "--to", str(_TO), "--no-save-differences"])
        self.assertEqual(code, 0, err)
        self.assertIn("changed since review: reference_digest, reference, delta", out)
        # A new review of the current amounts replaces the old one and clears the signal.
        result = self.review(_request(reference="10010.00", delta="490.00", reference_digest=_digest(folder)),
                             qb_folder=folder)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.compare(folder)["review_drift"], [])

    def test_other_reference_files_with_the_same_amounts_show_a_signal(self):
        first = self.review(_request())
        folder = self.qb_copy(software_reference="351.00")
        row = self.compare(folder)
        self.assertEqual((row["status"], row["reference"], row["delta"]), ("accepted", "10000.00", "500.00"))
        self.assertEqual(row["review_drift"], ["reference_digest"])
        # The out-of-date review may be replaced by a review of the new reference files.
        second = self.review(_request(reference_digest=_digest(folder)), qb_folder=folder)
        self.assertEqual(second["status"], "accepted")
        self.assertNotEqual(second["reference_digest"], first["reference_digest"])
        self.assertEqual(self.compare(folder)["review_drift"], [])

    def test_another_period_shows_a_signal(self):
        from bookkeeping.compare import compare_period
        from bookkeeping.entity import load_entity
        self.review(_request())
        report = compare_period(load_entity(self.path), _QB_MATCH, _FROM, date(2026, 2, 28),
                                persist_differences=False).to_dict()
        row = next(d for d in report["material_diffs"] if d["key"] == _KEY)
        self.assertEqual(row["review_drift"][0], "period")

    def test_a_material_change_reopens_and_keeps_the_old_review(self):
        self.review(_request())
        row = self.compare(self.qb_copy("9000.00"))
        self.assertEqual((row["status"], row["re_opened"]), ("open", True))
        self.assertEqual(row["review"]["delta"], "500.00")
        self.assertEqual(row["review_drift"], ["reference_digest", "reference", "delta"])

    def test_small_changes_add_up_against_the_reviewed_amounts(self):
        from bookkeeping.compare import _DEFAULT_MATERIAL_ABS, _DEFAULT_MATERIAL_PCT, _diff_record, _merge_differences
        reviewed = _diff_record("tb", "total_debits", Decimal("10100"), Decimal("10000"), status="accepted")
        reviewed.review = {"schema_version": 1, "period": {"from": "2026-01-01", "to": "2026-03-31"},
                           "reference_digest": "sha256:" + "0" * 64,
                           "ours": "10100", "reference": "10000", "delta": "100", "category": "uncategorized",
                           "reviewer": "person-17", "explanation": "Reviewed.", "reviewed_at": None}
        reviewed.review_drift = []
        # The saved delta already moved to 80 without a material change; 70 is 30 from the reviewed 100.
        reviewed.ours, reviewed.delta = Decimal("10080"), Decimal("80")
        fresh = _diff_record("tb", "total_debits", Decimal("10070"), Decimal("10000"))
        merged = _merge_differences([reviewed], [fresh], _DEFAULT_MATERIAL_ABS, _DEFAULT_MATERIAL_PCT)
        self.assertEqual((merged[0].status, merged[0].re_opened), ("open", True))
        self.assertEqual(merged[0].review_drift, ["ours", "delta"])
        # Legacy records keep the last saved delta as the baseline.
        legacy = _diff_record("tb", "total_debits", Decimal("10080"), Decimal("10000"), status="accepted")
        merged = _merge_differences([legacy], [fresh], _DEFAULT_MATERIAL_ABS, _DEFAULT_MATERIAL_PCT)
        self.assertEqual((merged[0].status, merged[0].re_opened), ("accepted", False))


class TestReferenceSnapshot(_Companies):
    """The review compares a private snapshot of the reference files, bound to the request's reference_digest."""

    def track_snapshots(self):
        """Patch the snapshot directory factory and record each directory that it makes."""
        import tempfile as tempfile_module
        made = []
        real = tempfile_module.mkdtemp

        def mkdtemp(*args, **kwargs):
            made.append(Path(real(*args, **kwargs)))
            return str(made[-1])

        return made, patch("bookkeeping.compare.tempfile.mkdtemp", side_effect=mkdtemp)

    def test_the_digest_comes_from_the_read_only_compare_result(self):
        from bookkeeping.compare import compare_period
        from bookkeeping.entity import load_entity
        before = _tree_state(self.path)
        report = compare_period(load_entity(self.path), _QB_MATCH, _FROM, _TO, persist_differences=False).to_dict()
        self.assertEqual(_tree_state(self.path), before)
        self.assertEqual(report["reference_digest"], _digest())
        result = self.review(_request(reference_digest=report["reference_digest"]))
        self.assertEqual(result["status"], "accepted")

    def test_an_old_request_is_refused_after_the_reference_files_change(self):
        folder = self.qb_copy()
        first = self.review(_request(), qb_folder=folder)
        self.assertEqual(first["status"], "accepted")
        pnl = folder / "profit_and_loss.csv"
        pnl.write_text(pnl.read_text(encoding="utf-8").replace('Software,"350.00"', 'Software,"351.00"'),
                       encoding="utf-8")
        result = self.refused_without_write(_request(), "REFERENCE_CHANGED", qb_folder=folder)
        self.assertEqual(result["reference_digest"], _digest(folder))
        self.assertEqual(self.stored()[_KEY]["review"]["reference_digest"], first["reference_digest"])

    def test_a_reference_change_during_the_compare_cannot_mix_digest_and_amounts(self):
        """The reference file changes after the snapshot. The compare reads only the snapshot."""
        from bookkeeping import compare
        folder = self.qb_copy()
        old_digest = _digest(folder)
        target = folder / "profit_and_loss.csv"
        actual = compare.compare_period
        compared = []

        def change_the_file(entity, qb_folder, *args, **kwargs):
            compared.append(Path(qb_folder))
            text = target.read_text(encoding="utf-8")
            target.write_text(text.replace('Consulting Revenue,"10000.00"', 'Consulting Revenue,"10010.00"'),
                              encoding="utf-8")
            return actual(entity, qb_folder, *args, **kwargs)

        with patch.object(compare, "compare_period", side_effect=change_the_file):
            mixed = self.review(_request(reference="10010.00", delta="490.00", reference_digest=old_digest),
                                qb_folder=folder)
        self.assert_refused(mixed, "DIFFERENCE_CHANGED")
        self.assertNotIn("review", self.stored()[_KEY])
        self.assertNotEqual(compared[0].resolve(), folder.resolve())
        # The same race with the reviewed amounts: the record holds the snapshot amounts and their digest.
        target.write_text(target.read_text(encoding="utf-8").replace("10010.00", "10000.00"), encoding="utf-8")
        with patch.object(compare, "compare_period", side_effect=change_the_file):
            result = self.review(_request(reference_digest=old_digest), qb_folder=folder)
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual((result["record"]["reference"], result["reference_digest"]), ("10000.00", old_digest))
        self.assertNotEqual(_digest(folder), old_digest)  # The folder changed, and the record does not claim it.

    def test_the_snapshot_is_removed_in_every_path_and_the_originals_do_not_change(self):
        from bookkeeping import compare
        folder = self.qb_copy()
        originals = _tree_state(folder)
        made, factory = self.track_snapshots()
        cases = {
            "accepted": (_request(), None),
            "retry": (_request(), None),
            "difference changed": (_request(delta="1.00"), None),
            "reference changed": (_request(reference_digest="sha256:" + "0" * 64), None),
            "error in the compare": (_request(), RuntimeError("compare failed")),
        }
        for name, (request, error) in cases.items():
            with self.subTest(case=name):
                made.clear()
                with factory, contextlib.ExitStack() as stack:
                    if error is not None:
                        stack.enter_context(patch.object(compare, "compare_period", side_effect=error))
                        with self.assertRaises(RuntimeError):
                            self.review(request, qb_folder=folder)
                    else:
                        self.review(request, qb_folder=folder)
                self.assertEqual(len(made), 1)
                self.assertFalse(made[0].exists())
                self.assertEqual(_tree_state(folder), originals)
        # An invalid request makes no snapshot.
        made.clear()
        with factory:
            self.review(_request(schema_version=999), qb_folder=folder)
        self.assertEqual(made, [])


class TestRepairControls(_Companies):
    """The legacy writers keep the repair behavior of the released engine; the typed review does not repair."""

    def stale(self) -> bytes:
        """entity.json is behind the ledger meta: a write path with the currency repair would rewrite it."""
        with contextlib.closing(sqlite3.connect(self.path / "ledger.sqlite")) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('currency', 'USD'), "
                         "('currency_source', 'inferred')")
        return (self.path / "entity.json").read_bytes()

    def test_the_repair_on_disk_would_change_this_company(self):
        from bookkeeping.currency import repair_on_disk
        before = self.stale()
        repair_on_disk(self.path)
        self.assertNotEqual((self.path / "entity.json").read_bytes(), before)

    def test_legacy_writers_and_the_typed_review_leave_a_stale_entity_json_as_before(self):
        from bookkeeping.compare import accept_diff, categorize_diff, compare_period
        from bookkeeping.entity import load_entity
        writers = {
            # The released categorize-diff, accept-diff and saving compare take no lock and do not repair.
            "categorize-diff": lambda: categorize_diff(load_entity(self.path), _OTHER, "our-bug"),
            "accept-diff": lambda: accept_diff(load_entity(self.path), _OTHER),
            "compare period": lambda: compare_period(load_entity(self.path), _QB_MATCH, _FROM, _TO),
            "review-diff": lambda: self.assertEqual(self.review(_request())["status"], "accepted"),
        }
        before = self.stale()
        for name, write in writers.items():
            with self.subTest(writer=name):
                write()
                self.assertEqual((self.path / "entity.json").read_bytes(), before)

    def test_the_read_only_compare_writes_nothing(self):
        from bookkeeping.compare import compare_period
        from bookkeeping.entity import load_entity
        self.stale()
        before = _tree_state(self.path)
        compare_period(load_entity(self.path), _QB_MATCH, _FROM, _TO, persist_differences=False)
        self.assertEqual(_tree_state(self.path), before)


class TestCommand(_Companies):
    def cli(self, *argv: str) -> tuple[int, str, str]:
        return run_cli(["compare", *argv])

    def write_request(self, request) -> Path:
        path = self._tmp / "request.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        return path

    def test_review_diff_prints_the_result(self):
        request = _request(reviewed_at="2026-11-09T15:04:05Z")
        code, out, err = self.cli("review-diff", "--entity", str(self.path), "--qb-folder", str(_QB_MATCH),
                                  "--request", str(self.write_request(request)))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["record"], self.stored()[_KEY])
        self.assertEqual((result["review_drift"], result["reference_digest"]), ([], _digest()))

    def test_review_diff_refusal_exits_one(self):
        code, out, _ = self.cli("review-diff", "--entity", str(self.path), "--qb-folder", str(_QB_MATCH),
                                "--request", str(self.write_request(_request(delta="1.00"))))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["code"], "DIFFERENCE_CHANGED")
        code, out, _ = self.cli("review-diff", "--entity", str(self.path), "--qb-folder", str(_QB_MATCH),
                                "--request", str(self._tmp / "absent.json"))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["code"], "REQUEST_INVALID")
        bad = self._tmp / "bad.json"
        bad.write_text("{", encoding="utf-8")
        code, out, _ = self.cli("review-diff", "--entity", str(self.path), "--qb-folder", str(_QB_MATCH),
                                "--request", str(bad))
        self.assertEqual((code, json.loads(out)["code"]), (1, "REQUEST_INVALID"))

    def test_review_diff_is_a_mutating_command_with_a_request_input(self):
        from bookkeeping.remote_contract import command_spec
        spec = command_spec(["compare", "review-diff", "--entity", "e", "--qb-folder", "q", "--request", "r.json"])
        self.assertTrue(spec["mutates"])
        self.assertIn("request", spec["input_fields"])


class TestLegacyAcceptDiff(_Companies):
    """``books compare accept-diff`` keeps its released behavior."""

    def test_accept_diff_writes_the_legacy_record_only(self):
        code, out, err = run_cli(["compare", "accept-diff", "--entity", str(self.path), "--key", _KEY,
                                  "--note", "Owner confirmed"])
        self.assertEqual((code, out.strip()), (0, f"Accepted difference {_KEY!r}"), err)
        row = self.stored()[_KEY]
        self.assertEqual(list(row), _LEGACY_FIELDS)
        self.assertEqual((row["status"], row["note"]), ("accepted", "Owner confirmed"))
        # A re-accept still succeeds, and the file has no review fields.
        code, _, _ = run_cli(["compare", "accept-diff", "--entity", str(self.path), "--key", _KEY])
        self.assertEqual(code, 0)
        self.assertTrue(all(list(r) == _LEGACY_FIELDS for r in self.rows()))

    def test_accept_diff_on_a_missing_or_corrupt_file_reads_as_not_found(self):
        for text in (None, "{not json"):
            with self.subTest(text=text):
                if text is None:
                    self.differences.unlink(missing_ok=True)
                else:
                    self.differences.write_text(text, encoding="utf-8")
                code, out, err = run_cli(["compare", "accept-diff", "--entity", str(self.path), "--key", _KEY])
                self.assertEqual((code, out), (1, ""))
                self.assertIn("No difference found", err)

    def test_legacy_compare_output_has_no_review_fields(self):
        from bookkeeping.compare import compare_period
        from bookkeeping.entity import load_entity
        report = compare_period(load_entity(self.path), _QB_MATCH, _FROM, _TO).to_dict()
        for row in report["material_diffs"] + report["immaterial_diffs"]:
            self.assertEqual(list(row), _LEGACY_FIELDS)


if __name__ == "__main__":
    unittest.main()
