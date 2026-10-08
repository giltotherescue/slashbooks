"""Bundled onboarding resources must preserve checkout and plugin behavior."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bookkeeping import entity  # noqa: E402


CANONICAL = ROOT / "skills" / "books-onboard" / "templates"
BUNDLED = ROOT / "src" / "bookkeeping" / "templates"
TEMPLATES = {
    "entity.json", "business-profile.md", "trust-policy.json", "gitignore-template",
}


class TestPackagedTemplates(unittest.TestCase):
    def test_copies_are_byte_identical_to_canonical_skill_templates(self) -> None:
        self.assertEqual({p.name for p in CANONICAL.iterdir() if p.is_file()}, TEMPLATES)
        self.assertEqual({p.name for p in BUNDLED.iterdir() if p.is_file()}, TEMPLATES)
        for name in sorted(TEMPLATES):
            with self.subTest(template=name):
                self.assertEqual((CANONICAL / name).read_bytes(), (BUNDLED / name).read_bytes())

    def test_package_data_explicitly_includes_all_templates(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            config = tomllib.load(handle)
        self.assertEqual(
            set(config["tool"]["setuptools"]["package-data"]["bookkeeping"]),
            {f"templates/{name}" for name in TEMPLATES},
        )
        self.assertEqual(config["project"]["dependencies"], [])

    def test_env_override_takes_precedence_even_if_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "explicit-override"
            with mock.patch.dict(os.environ, {"BOOKKEEPING_TEMPLATES_DIR": str(override)}):
                self.assertEqual(entity._templates_dir(), override)

    def test_checkout_or_plugin_templates_take_precedence(self) -> None:
        with mock.patch.dict(os.environ, {"BOOKKEEPING_TEMPLATES_DIR": ""}):
            with mock.patch.object(entity, "_BUILTIN_TEMPLATES", CANONICAL):
                self.assertEqual(entity._templates_dir(), CANONICAL)

    def test_missing_checkout_uses_bundled_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"BOOKKEEPING_TEMPLATES_DIR": ""}):
                with mock.patch.object(entity, "_BUILTIN_TEMPLATES", Path(tmp) / "absent"):
                    self.assertEqual(entity._templates_dir(), BUNDLED)

    def test_env_override_still_wins_without_checkout_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"BOOKKEEPING_TEMPLATES_DIR": str(CANONICAL)}):
                with mock.patch.object(entity, "_BUILTIN_TEMPLATES", Path(tmp) / "absent"):
                    self.assertEqual(entity._templates_dir(), CANONICAL)

    def test_bundled_init_matches_canonical_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical_company = root / "canonical-company"
            bundled_company = root / "bundled-company"
            options = dict(
                name="Synthetic Packaging Company", business_type="consulting",
                legal_structure="LLC", cutover_date="2026-01-01",
            )
            with mock.patch.dict(os.environ, {"BOOKKEEPING_TEMPLATES_DIR": str(CANONICAL)}):
                expected = entity.init_entity(canonical_company, **options)
            with mock.patch.dict(os.environ, {"BOOKKEEPING_TEMPLATES_DIR": ""}):
                with mock.patch.object(entity, "_BUILTIN_TEMPLATES", root / "absent"):
                    actual = entity.init_entity(bundled_company, **options)
                    # Reinitialization must continue to preserve company edits.
                    profile = bundled_company / "business-profile.md"
                    original = profile.read_bytes()
                    profile.write_text("Existing company profile\n", encoding="utf-8")
                    entity.init_entity(bundled_company, **options)
                    self.assertEqual(profile.read_text(encoding="utf-8"), "Existing company profile\n")
                    profile.write_bytes(original)
            self.assertEqual(actual, expected)
            self.assertTrue((bundled_company / "ledger.sqlite").is_file())
            for name in ("entity.json", "business-profile.md", "trust-policy.json"):
                with self.subTest(template=name):
                    self.assertEqual(
                        (bundled_company / name).read_bytes(),
                        (canonical_company / name).read_bytes(),
                    )
            config = json.loads((bundled_company / "entity.json").read_text(encoding="utf-8"))
            self.assertEqual(config["cutover_date"], "2026-01-01")
            self.assertEqual(config["legal_structure"], "LLC")


if __name__ == "__main__":
    unittest.main()
