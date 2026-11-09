"""Typed review of a QuickBooks comparison difference (``books compare review-diff``, request version 1).

The review binds the exact difference that the engine derives again: grain, account, period, ours, reference,
delta and category. It writes a deterministic record under the entity lock, and it refuses with a typed code.
The legacy ``books compare accept-diff`` keeps its behavior.
"""

from __future__ import annotations

import contextlib
import json
import shutil
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
from test_compare import FIXTURE_LEDGER_WITH_DELTA, _QB_MATCH, _make_entity  # noqa: E402

_FROM = date(2026, 1, 1)
_TO = date(2026, 3, 31)
_KEY = "pnl:Income:Consulting-Revenue"
_LEGACY_FIELDS = {"key", "grain", "account", "ours", "reference", "delta", "category", "status", "note", "re_opened"}


def _request(**changes) -> dict:
    request = {
        "schema_version": 1,
        "period": {"from": "2026-01-01", "to": "2026-03-31"},
        "difference": {"grain": "pnl", "account": "Income:Consulting-Revenue", "ours": "10500.00",
                       "reference": "10000.00", "delta": "500.00", "category": "uncategorized"},
        "reviewer": "person-17",
        "explanation": "The extra payment is a deposit that QuickBooks did not record.",
    }
    for name, value in changes.items():
        if name in request["difference"]:
            request["difference"] = {**request["difference"], name: value}
        elif value is _DROP:
            request.pop(name, None)
        else:
            request[name] = value
    return request


_DROP = object()


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

    def stored(self) -> dict:
        return {d["key"]: d for d in json.loads(self.differences.read_text(encoding="utf-8"))}

    def review(self, request, qb_folder: Path = _QB_MATCH, path: Path | None = None) -> dict:
        from bookkeeping.compare import review_difference
        return review_difference(path or self.path, qb_folder, request)

    def assert_refused(self, result: dict, code: str) -> dict:
        self.assertEqual((result["status"], result["code"]), ("refused", code), result)
        self.assertEqual(result["schema_version"], 1)
        self.assertIsInstance(result["message"], str)
        return result

    def qb_copy(self, consulting_reference: str) -> Path:
        """A copy of the reference reports with another Consulting Revenue total."""
        folder = Path(shutil.copytree(_QB_MATCH, self._tmp / f"qb-{consulting_reference}"))
        pnl = folder / "profit_and_loss.csv"
        text = pnl.read_text(encoding="utf-8")
        self.assertIn('Consulting Revenue,"10000.00"', text)
        pnl.write_text(text.replace('Consulting Revenue,"10000.00"', f'Consulting Revenue,"{consulting_reference}"'),
                       encoding="utf-8")
        return folder


class TestExactReview(_Companies):
    def test_exact_review_writes_the_bound_record(self):
        before = self.stored()
        result = self.review(_request(reviewed_at="2026-11-09T15:04:05Z"))
        expected_review = {
            "schema_version": 1,
            "period": {"from": "2026-01-01", "to": "2026-03-31"},
            "ours": "10500.00", "reference": "10000.00", "delta": "500.00", "category": "uncategorized",
            "reviewer": "person-17",
            "explanation": "The extra payment is a deposit that QuickBooks did not record.",
            "reviewed_at": "2026-11-09T15:04:05Z",
        }
        expected_record = {
            "key": _KEY, "grain": "pnl", "account": "Income:Consulting-Revenue",
            "ours": "10500.00", "reference": "10000.00", "delta": "500.00", "category": "uncategorized",
            "status": "accepted", "note": "The extra payment is a deposit that QuickBooks did not record.",
            "re_opened": False, "review": expected_review, "review_drift": [],
        }
        self.assertEqual(result, {"schema_version": 1, "status": "accepted", "key": _KEY, "record": expected_record})
        after = self.stored()
        self.assertEqual(after[_KEY], expected_record)
        # Only the reviewed record changes, and the order of the file stays the same.
        self.assertEqual(list(after), list(before))
        self.assertEqual({k: v for k, v in after.items() if k != _KEY}, {k: v for k, v in before.items() if k != _KEY})

    def test_without_a_time_the_engine_records_none(self):
        result = self.review(_request())
        self.assertIsNone(result["record"]["review"]["reviewed_at"])
        self.assertIsNone(self.stored()[_KEY]["review"]["reviewed_at"])

    def test_amounts_bind_by_value(self):
        result = self.review(_request(ours="10500", reference="10000.0", delta="500.000"))
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["record"]["review"]["delta"], "500.00")

    def test_review_clears_re_opened(self):
        rows = json.loads(self.differences.read_text(encoding="utf-8"))
        for row in rows:
            if row["key"] == _KEY:
                row.update(status="open", re_opened=True, note="Changed after an earlier acceptance")
        self.differences.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
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

    def test_compare_after_review_keeps_the_review(self):
        from bookkeeping.compare import compare_period
        from bookkeeping.entity import load_entity
        self.review(_request())
        report = compare_period(load_entity(self.path), _QB_MATCH, _FROM, _TO).to_dict()
        row = next(d for d in report["material_diffs"] if d["key"] == _KEY)
        self.assertEqual((row["status"], row["review_drift"]), ("accepted", []))
        self.assertEqual(row["review"]["reviewer"], "person-17")
        self.assertEqual(self.stored()[_KEY]["review_drift"], [])


class TestReviewRefusals(_Companies):
    def assert_unchanged(self, request, code: str, qb_folder: Path = _QB_MATCH) -> dict:
        before = self.differences.read_bytes()
        result = self.assert_refused(self.review(request, qb_folder), code)
        self.assertEqual(self.differences.read_bytes(), before)
        return result

    def test_each_changed_amount_or_category_refuses(self):
        cases = {"ours": "10499.99", "reference": "10000.01", "delta": "499.00", "category": "our-bug"}
        for name, value in cases.items():
            with self.subTest(field=name):
                result = self.assert_unchanged(_request(**{name: value}), "DIFFERENCE_CHANGED")
                self.assertEqual(result["changed_fields"], [name])
                self.assertEqual(result["key"], _KEY)
                self.assertEqual(result["current"], {
                    "grain": "pnl", "account": "Income:Consulting-Revenue", "ours": "10500.00",
                    "reference": "10000.00", "delta": "500.00", "category": "uncategorized"})

    def test_another_period_refuses(self):
        result = self.assert_unchanged(_request(period={"from": "2026-01-01", "to": "2026-02-28"}), "DIFFERENCE_CHANGED")
        self.assertEqual(result["changed_fields"], ["ours", "delta"])
        self.assertEqual(result["current"]["ours"], "8000.00")

    def test_changed_reference_reports_refuse(self):
        result = self.assert_unchanged(_request(), "DIFFERENCE_CHANGED", qb_folder=self.qb_copy("10010.00"))
        self.assertEqual(result["changed_fields"], ["reference", "delta"])

    def test_changed_category_on_the_saved_record_refuses(self):
        from bookkeeping.compare import categorize_diff
        from bookkeeping.entity import load_entity
        self.assertTrue(categorize_diff(load_entity(self.path), _KEY, "our-bug"))
        result = self.assert_unchanged(_request(), "DIFFERENCE_CHANGED")
        self.assertEqual(result["changed_fields"], ["category"])

    def test_another_grain_or_account_is_not_found(self):
        for changes in ({"grain": "bs"}, {"account": "Income:Other"}):
            with self.subTest(changes=changes):
                self.assert_unchanged(_request(**changes), "DIFFERENCE_NOT_FOUND")

    def test_a_difference_that_was_never_saved_refuses(self):
        # The February compare has a Software difference that the saved first-quarter compare does not have.
        request = _request(period={"from": "2026-01-01", "to": "2026-02-28"}, account="Expenses:Software",
                           ours="200.00", reference="350.00", delta="-150.00")
        self.assert_unchanged(request, "DIFFERENCE_NOT_SAVED")

    def test_missing_differences_file_refuses(self):
        self.differences.unlink()
        result = self.assert_refused(self.review(_request()), "DIFFERENCES_FILE_MISSING")
        self.assertNotIn("record", result)
        self.assertFalse(self.differences.exists())

    def test_corrupt_differences_file_refuses(self):
        good = json.loads(self.differences.read_text(encoding="utf-8"))
        bad_amount = [dict(row, ours="ten") if row["key"] == _KEY else row for row in good]
        no_key = [{k: v for k, v in row.items() if k != "key"} for row in good]
        bad_status = [dict(row, status="done") for row in good]
        review = {"schema_version": 1, "period": {"from": "2026-01-01", "to": "2026-03-31"}, "ours": "1",
                  "reference": "x", "delta": "1", "category": "uncategorized", "reviewer": "person-17",
                  "explanation": "Reviewed.", "reviewed_at": None}
        bad_review = [dict(row, review=review, review_drift=[]) if row["key"] == _KEY else row for row in good]
        cases = {
            "not json": "{not json",
            "an object": "{}",
            "a row that is not an object": json.dumps(["x"]),
            "a missing field": json.dumps(no_key),
            "a bad amount": json.dumps(bad_amount),
            "a bad status": json.dumps(bad_status),
            "a duplicate key": json.dumps(good + good[:1]),
            "a review with a bad amount": json.dumps(bad_review),
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.differences.write_text(text, encoding="utf-8")
                self.assert_unchanged(_request(), "DIFFERENCES_FILE_CORRUPT")

    def test_invalid_requests_refuse(self):
        cases = {
            "not an object": [],
            "another version": _request(schema_version=2),
            "a boolean version": _request(schema_version=True),
            "an unknown field": {**_request(), "key": _KEY},
            "no reviewer": _request(reviewer=_DROP),
            "an empty reviewer": _request(reviewer=""),
            "a padded reviewer": _request(reviewer=" person-17"),
            "no explanation": _request(explanation=_DROP),
            "a blank explanation": _request(explanation="  "),
            "a number amount": _request(ours=10500.0),
            "an exponent amount": _request(delta="5E+2"),
            "an unknown category": _request(category="other"),
            "a bad day": _request(period={"from": "2026-02-30", "to": "2026-03-31"}),
            "a reversed period": _request(period={"from": "2026-03-31", "to": "2026-01-01"}),
            "an extra period field": _request(period={"from": "2026-01-01", "to": "2026-03-31", "view": "cash"}),
            "an extra difference field": {**_request(), "difference": {**_request()["difference"], "key": _KEY}},
            "a time without a zone": _request(reviewed_at="2026-11-09T15:04:05"),
            "a time that is a day": _request(reviewed_at="2026-11-09"),
        }
        for name, request in cases.items():
            with self.subTest(case=name):
                self.assert_unchanged(request, "REQUEST_INVALID")

    def test_missing_company_or_reference_folder_refuses(self):
        self.assert_refused(self.review(_request(), path=self._tmp / "nowhere"), "ENTITY_NOT_FOUND")
        self.assertFalse((self._tmp / "nowhere").exists())
        self.assert_unchanged(_request(), "REFERENCE_FOLDER_MISSING", qb_folder=self._tmp / "no-reports")


class TestRepeatedReview(_Companies):
    def test_exact_retry_is_idempotent(self):
        from test_compare import _tree_state
        request = _request(reviewed_at="2026-11-09T15:04:05Z")
        first = self.review(request)
        before = _tree_state(self.path)
        second = self.review(request)
        self.assertEqual(second, first)
        self.assertEqual(_tree_state(self.path), before)

    def test_another_acceptance_of_the_same_amounts_refuses(self):
        self.review(_request(reviewed_at="2026-11-09T15:04:05Z"))
        before = self.differences.read_bytes()
        for changes in ({"reviewer": "person-18"}, {"explanation": "Another reason."},
                        {"reviewed_at": "2026-11-10T09:00:00Z"}, {"reviewed_at": _DROP}):
            with self.subTest(changes=changes):
                result = self.assert_refused(self.review(_request(**changes)), "ALREADY_ACCEPTED")
                self.assertEqual(result["accepted_review"]["reviewer"], "person-17")
                self.assertEqual(self.differences.read_bytes(), before)

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
                entered.append(Path(path).resolve())
                with real(path, **kwargs):
                    self.assertTrue(categorize_diff(load_entity(self.path), _KEY, "timing-pending"))
            with real(path, **kwargs):
                yield

        with patch.object(importer, "_entity_write_lock", other_writer_first):
            result = self.review(_request())
        self.assertEqual(entered, [self.path.resolve()])
        self.assertEqual(self.assert_refused(result, "DIFFERENCE_CHANGED")["changed_fields"], ["category"])
        self.assertNotIn("review", self.stored()[_KEY])


class TestLaterChange(_Companies):
    def compare(self, qb_folder: Path) -> dict:
        from bookkeeping.compare import compare_period
        from bookkeeping.entity import load_entity
        report = compare_period(load_entity(self.path), qb_folder, _FROM, _TO).to_dict()
        return next(d for d in report["material_diffs"] if d["key"] == _KEY)

    def test_a_non_material_change_keeps_the_acceptance_with_a_signal(self):
        self.review(_request())
        folder = self.qb_copy("10010.00")
        row = self.compare(folder)
        self.assertEqual((row["status"], row["re_opened"]), ("accepted", False))
        self.assertEqual((row["reference"], row["delta"]), ("10010.00", "490.00"))
        self.assertEqual((row["review"]["reference"], row["review"]["delta"]), ("10000.00", "500.00"))
        self.assertEqual(row["review_drift"], ["reference", "delta"])
        self.assertEqual(self.stored()[_KEY]["review_drift"], ["reference", "delta"])
        code, out, err = run_cli(["compare", "period", "--entity", str(self.path), "--qb-folder", str(folder),
                                  "--from", str(_FROM), "--to", str(_TO), "--no-save-differences"])
        self.assertEqual(code, 0, err)
        self.assertIn("changed since review: reference, delta", out)
        # A new review of the current amounts replaces the old one and clears the signal.
        result = self.review(_request(reference="10010.00", delta="490.00"), qb_folder=folder)
        self.assertEqual(result["status"], "accepted")
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
        self.assertEqual(row["review_drift"], ["reference", "delta"])

    def test_small_changes_add_up_against_the_reviewed_amounts(self):
        from bookkeeping.compare import _DEFAULT_MATERIAL_ABS, _DEFAULT_MATERIAL_PCT, _diff_record, _merge_differences
        reviewed = _diff_record("tb", "total_debits", Decimal("10100"), Decimal("10000"), status="accepted")
        reviewed.review = {"schema_version": 1, "period": {"from": "2026-01-01", "to": "2026-03-31"},
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
        self.assertEqual(set(row), _LEGACY_FIELDS)
        self.assertEqual((row["status"], row["note"]), ("accepted", "Owner confirmed"))
        # A re-accept still succeeds, and the file has no review fields.
        code, _, _ = run_cli(["compare", "accept-diff", "--entity", str(self.path), "--key", _KEY])
        self.assertEqual(code, 0)
        self.assertTrue(all(set(r) == _LEGACY_FIELDS for r in self.stored().values()))

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
            self.assertEqual(set(row), _LEGACY_FIELDS)


if __name__ == "__main__":
    unittest.main()
