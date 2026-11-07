"""The product name in user-visible output is a per-run setting."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import sys
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping import brand, currency, taxview
from bookkeeping.cli import main as cli_main
from test_workbook import _find_csv, _make_entity, _read_csv

PROJECT_LINK = "https://github.com/giltotherescue/slashbooks"
FROM_DATE, TO_DATE = date(2026, 1, 1), date(2026, 1, 31)


def _clean_env():
    return patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != brand.ENV_VAR}, clear=True)


class ProductNameTests(unittest.TestCase):
    def test_default_is_slashbooks(self):
        with _clean_env():
            self.assertEqual(brand.product_name(), "Slashbooks")
            self.assertTrue(brand.is_default_name())

    def test_blank_variable_gives_default(self):
        with patch.dict(os.environ, {brand.ENV_VAR: "   "}):
            self.assertEqual(brand.product_name(), "Slashbooks")

    def test_environment_variable_is_a_fallback(self):
        with patch.dict(os.environ, {brand.ENV_VAR: "Env Books"}):
            self.assertEqual(brand.product_name(), "Env Books")
            with brand.use_product_name("Example Books"):
                self.assertEqual(brand.product_name(), "Example Books")
            self.assertEqual(brand.product_name(), "Env Books")

    def test_run_value_is_reset_and_nests(self):
        with _clean_env():
            with brand.use_product_name("Example Books"):
                with brand.use_product_name("Other Books"):
                    self.assertEqual(brand.product_name(), "Other Books")
                self.assertEqual(brand.product_name(), "Example Books")
                with brand.use_product_name(None):
                    self.assertEqual(brand.product_name(), "Example Books")
            self.assertEqual(brand.product_name(), "Slashbooks")

    def test_run_value_is_reset_after_an_error(self):
        with _clean_env():
            with self.assertRaises(RuntimeError):
                with brand.use_product_name("Example Books"):
                    raise RuntimeError("boom")
            self.assertEqual(brand.product_name(), "Slashbooks")

    def test_run_value_does_not_leak_between_threads(self):
        seen = {}
        ready, go = threading.Event(), threading.Event()

        def other():
            ready.set()
            go.wait()
            seen["other"] = brand.product_name()

        with _clean_env():
            thread = threading.Thread(target=other)
            thread.start()
            ready.wait()
            with brand.use_product_name("Example Books"):
                go.set()
                thread.join()
                self.assertEqual(brand.product_name(), "Example Books")
        self.assertEqual(seen["other"], "Slashbooks")

    def test_invalid_names_are_rejected(self):
        for bad in ["", "   ", "=cmd", "+1", "-x", "@sum", "two\nlines", "tab\there", "a" * 41, "bell\x07", None, 5]:
            with self.subTest(bad=bad):
                with self.assertRaises(brand.ProductNameError):
                    brand.validate_product_name(bad)
                with self.assertRaises(brand.ProductNameError):
                    with brand.use_product_name(bad) if bad is not None else brand.use_product_name(""):
                        pass

    def test_invalid_environment_variable_is_refused_not_printed(self):
        with patch.dict(os.environ, {brand.ENV_VAR: "=HYPERLINK()"}):
            with self.assertRaises(brand.ProductNameError):
                brand.product_name()

    def test_valid_names_are_trimmed(self):
        self.assertEqual(brand.validate_product_name("  Example Books  "), "Example Books")
        self.assertEqual(brand.validate_product_name("A" * 40), "A" * 40)
        self.assertEqual(brand.validate_product_name("Livre-Comptes é"), "Livre-Comptes é")


class OutputTests(unittest.TestCase):
    def test_message_uses_the_run_name(self):
        with _clean_env():
            self.assertIn("Slashbooks does not convert", currency.set_currency_note())
            with brand.use_product_name("Example Books"):
                note = currency.set_currency_note()
            self.assertIn("Example Books does not convert", note)
            self.assertNotIn("Slashbooks", note)

    def test_tax_labels_name_no_product(self):
        for label in taxview.LABELS.values():
            self.assertNotIn("Slashbooks", label)

    def test_export_cover_default_and_configured(self):
        from bookkeeping.reports.workbook import generate_accountant_package

        with tempfile.TemporaryDirectory() as tmp, _clean_env():
            entity = _make_entity(Path(tmp))
            result = generate_accountant_package(entity, FROM_DATE, TO_DATE, override=True)
            summary = {r[0]: r[1] for r in _read_csv(_find_csv(result.output_dir, "Summary"))[1:] if len(r) > 1}
            self.assertEqual(summary["Produced by"], "Slashbooks")
            self.assertEqual(summary["Project link"], PROJECT_LINK)
        with tempfile.TemporaryDirectory() as tmp, _clean_env(), brand.use_product_name("Example Books"):
            entity = _make_entity(Path(tmp))
            result = generate_accountant_package(entity, FROM_DATE, TO_DATE, override=True)
            rows = _read_csv(_find_csv(result.output_dir, "Summary"))
            summary = {r[0]: r[1] for r in rows[1:] if len(r) > 1}
            self.assertEqual(summary["Produced by"], "Example Books")
            self.assertNotIn("Project link", summary)
            text = " ".join(cell for r in rows for cell in r)
            self.assertNotIn("Slashbooks", text)
            self.assertNotIn("github.com", text)


class CliTests(unittest.TestCase):
    def _run(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli_main(argv)
        return code, err.getvalue()

    def test_invalid_option_exits_with_a_json_error(self):
        code, err = self._run(["--product-name", "=bad", "tax", "lines", "--form", "schc"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)["error"]["code"], "PRODUCT_NAME_INVALID")

    def test_option_sets_the_name_for_one_run_only(self):
        seen = []
        from bookkeeping import cli
        with _clean_env(), patch.object(cli, "_main", lambda argv: seen.append((brand.product_name(), list(argv))) or 0):
            self.assertEqual(cli_main(["--product-name", "Example Books", "tax", "lines"]), 0)
            self.assertEqual(cli_main(["tax", "lines"]), 0)
        self.assertEqual(seen, [("Example Books", ["tax", "lines"]), ("Slashbooks", ["tax", "lines"])])


if __name__ == "__main__":
    unittest.main()
