"""Tests for src/bookkeeping/entity.py — entity directory scaffolding."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.entity import (  # noqa: E402
    Entity,
    _is_package_repo,
    _refuse_if_inside_package_repo,
    add_parser,
    init_entity,
    load_entity,
    run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_simple(tmpdir: Path, **kwargs: object) -> dict[str, list[str]]:
    """Convenience: init with default business type unless overridden."""
    kwargs.setdefault("business_type", "consulting")
    return init_entity(tmpdir, **kwargs)  # type: ignore[arg-type]


_EXPECTED_FILES = [
    "ledger.sqlite",
    "chart-of-accounts.beancount",
    "business-profile.md",
    "entity.json",
    "trust-policy.json",
]

_EXPECTED_DIRS = [
    "learned-context",
    "review-queue",
    "staging",
    "ingestion",
    "ingestion/quickbooks",
    "ingestion/stripe",
    "ingestion/mercury",
    "ingestion/custom",
    "reports",
]


class TestInitFullLayout(unittest.TestCase):
    """Happy path: init creates the full layout."""

    def test_all_files_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            for fname in _EXPECTED_FILES:
                self.assertTrue(
                    (target / fname).exists(),
                    f"Expected file not created: {fname}",
                )

    def test_all_dirs_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            for dname in _EXPECTED_DIRS:
                self.assertTrue(
                    (target / dname).is_dir(),
                    f"Expected directory not created: {dname}",
                )

    def test_entity_json_has_correct_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target, name="Acme Corp")
            data = json.loads((target / "entity.json").read_text(encoding="utf-8"))
            self.assertEqual(data["name"], "Acme Corp")
            self.assertEqual(data["business_type"], "consulting")
            self.assertEqual(data["legal_structure"], "")
            self.assertEqual(data["fiscal_year_start"], "01-01")
            self.assertEqual(data["declared_sources"], [])
            self.assertEqual(data["provider_sources"], [])
            self.assertEqual(data["bank_account_mappings"], {})
            self.assertEqual(data["csv_account_mappings"], {})
            self.assertIsNone(data["cutover_date"])

    def test_init_persists_legal_structure_and_cutover_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            init_entity(
                target,
                name="Owner Co",
                legal_structure="single-member LLC",
                cutover_date="2026-01-01",
            )
            data = json.loads((target / "entity.json").read_text(encoding="utf-8"))
            self.assertEqual(data["legal_structure"], "single-member LLC")
            self.assertEqual(data["cutover_date"], "2026-01-01")

    def test_init_rejects_invalid_cutover_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            with self.assertRaises(ValueError):
                init_entity(target, cutover_date="January 1")

    def test_trust_policy_default_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            data = json.loads((target / "trust-policy.json").read_text(encoding="utf-8"))
            self.assertEqual(data["auto_post_threshold"], 3)
            self.assertTrue(data["queue_all_until_confirmed"])

    def test_ledger_store_initialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            from bookkeeping.ledger.store import LedgerStore

            store = LedgerStore(target / "ledger.sqlite")
            self.assertEqual(store.get_meta("schema_version"), "1")
            self.assertEqual(store.get_meta("canonical"), "true")

    def test_coa_contains_open_directives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            coa_text = (target / "chart-of-accounts.beancount").read_text(encoding="utf-8")
            self.assertIn("open Assets:Bank:Checking", coa_text)
            self.assertIn("open Income:Consulting", coa_text)

    def test_saas_coa_has_saas_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            init_entity(target, business_type="saas")
            coa_text = (target / "chart-of-accounts.beancount").read_text(encoding="utf-8")
            self.assertIn("open Income:Subscriptions", coa_text)
            self.assertIn("open Expenses:Hosting", coa_text)
            self.assertIn("open Expenses:Payment-Fees", coa_text)

    def test_consulting_coa_has_consulting_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            coa_text = (target / "chart-of-accounts.beancount").read_text(encoding="utf-8")
            self.assertIn("open Income:Consulting", coa_text)
            # Should NOT have SaaS-only accounts
            self.assertNotIn("Income:Subscriptions", coa_text)

    def test_business_profile_has_expected_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            text = (target / "business-profile.md").read_text(encoding="utf-8")
            for section in [
                "Business Type",
                "Legal Structure",
                "Customer Patterns",
                "Vendor Patterns",
                "Owner Compensation Pattern",
                "Books Start Date",
                "Fiscal Year",
                "Declared Data Sources",
                "Commingling Rules",
            ]:
                self.assertIn(section, text, f"Missing section: {section}")

    def test_report_lists_all_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            report = _init_simple(target)
            self.assertGreater(len(report["created"]), 0)
            self.assertEqual(report["existed"], [])


class TestSecondInitDoesNotOverwrite(unittest.TestCase):
    """Second init (re-init) must not overwrite anything that exists."""

    def test_content_unchanged_after_reinit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target, name="First Name")

            # Capture original content
            original_entity = (target / "entity.json").read_bytes()
            original_store = (target / "ledger.sqlite").read_bytes()
            original_coa = (target / "chart-of-accounts.beancount").read_bytes()
            original_trust = (target / "trust-policy.json").read_bytes()
            original_profile = (target / "business-profile.md").read_bytes()

            # Re-init with different name — should NOT change anything
            _init_simple(target, name="Second Name")

            self.assertEqual((target / "entity.json").read_bytes(), original_entity)
            self.assertEqual((target / "ledger.sqlite").read_bytes(), original_store)
            self.assertEqual((target / "chart-of-accounts.beancount").read_bytes(), original_coa)
            self.assertEqual((target / "trust-policy.json").read_bytes(), original_trust)
            self.assertEqual((target / "business-profile.md").read_bytes(), original_profile)

    def test_reinit_reports_all_existed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            report = _init_simple(target)
            self.assertEqual(report["created"], [])
            self.assertGreater(len(report["existed"]), 0)

    def test_reinit_does_not_touch_learned_context_etc(self) -> None:
        """Re-init must not empty or remove ledger-adjacent directories."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            # Simulate some data in protected directories
            (target / "learned-context" / "counterparties.json").write_text("{}", encoding="utf-8")
            (target / "review-queue" / "item-001.json").write_text("{}", encoding="utf-8")
            (target / "staging" / "pending.json").write_text("{}", encoding="utf-8")

            _init_simple(target)

            # Files must survive re-init untouched
            self.assertTrue((target / "learned-context" / "counterparties.json").exists())
            self.assertTrue((target / "review-queue" / "item-001.json").exists())
            self.assertTrue((target / "staging" / "pending.json").exists())


class TestPartialLayoutCompletion(unittest.TestCase):
    """Init into a partially-initialised directory must complete missing pieces only."""

    def test_creates_missing_pieces_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            target.mkdir()
            # Pre-create entity.json (marks it as reinit territory)
            entity_data = {
                "name": "Partial Entity",
                "business_type": "consulting",
                "legal_structure": "",
                "fiscal_year_start": "01-01",
                "declared_sources": [],
                "csv_account_mappings": {},
                "cutover_date": None,
            }
            (target / "entity.json").write_text(
                json.dumps(entity_data, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            # Leave out trust-policy.json — it should be created even in re-init
            # and several directories
            self.assertFalse((target / "trust-policy.json").exists())
            self.assertFalse((target / "staging").exists())

            _init_simple(target)

            # Missing pieces created
            self.assertTrue((target / "trust-policy.json").exists())
            self.assertTrue((target / "staging").is_dir())
            self.assertTrue((target / "ingestion").is_dir())
            self.assertTrue((target / "reports").is_dir())

            # entity.json not overwritten (still has the original content)
            loaded = json.loads((target / "entity.json").read_text(encoding="utf-8"))
            self.assertEqual(loaded["name"], "Partial Entity")

    def test_report_distinguishes_created_vs_existed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            target.mkdir()
            # Only create entity.json so it's treated as re-init
            entity_data = {
                "name": "X",
                "business_type": "consulting",
                "legal_structure": "",
                "fiscal_year_start": "01-01",
                "declared_sources": [],
                "csv_account_mappings": {},
                "cutover_date": None,
            }
            (target / "entity.json").write_text(
                json.dumps(entity_data, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            report = _init_simple(target)
            self.assertIn("entity.json", report["existed"])
            # trust-policy.json should be newly created
            self.assertIn("trust-policy.json", report["created"])


class TestRefusesPathInsidePackageRepo(unittest.TestCase):
    """Init must refuse to run when the target path is inside the package repo."""

    def test_refuses_src_bookkeeping_subdir(self) -> None:
        # The actual package directory is definitely inside the package repo
        inside = ROOT / "src" / "bookkeeping" / "_test_entity_init_would_go_here"
        with self.assertRaises(SystemExit) as ctx:
            _refuse_if_inside_package_repo(inside)
        self.assertEqual(ctx.exception.code, 1)

    def test_refuses_repo_root_itself(self) -> None:
        with self.assertRaises(SystemExit):
            _refuse_if_inside_package_repo(ROOT)

    def test_allows_external_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # An external temp directory must NOT trigger the refusal
            try:
                _refuse_if_inside_package_repo(Path(tmp))
            except SystemExit:
                self.fail("_refuse_if_inside_package_repo raised SystemExit for an external path")

    def test_init_raises_on_package_repo_path(self) -> None:
        inside = ROOT / "src" / "bookkeeping" / "_would_be_entity"
        with self.assertRaises(SystemExit):
            init_entity(inside)

    def test_is_package_repo_detects_repo_root(self) -> None:
        self.assertTrue(_is_package_repo(ROOT))

    def test_is_package_repo_returns_false_for_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(_is_package_repo(Path(tmp)))


class TestLoadEntity(unittest.TestCase):
    """load_entity round-trips correctly."""

    def test_roundtrip_basic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            init_entity(target, name="Roundtrip Co", business_type="consulting")
            entity = load_entity(target)
            self.assertIsInstance(entity, Entity)
            self.assertEqual(entity.name, "Roundtrip Co")
            self.assertEqual(entity.business_type, "consulting")

    def test_paths_are_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            entity = load_entity(target)
            self.assertTrue(entity.path.is_absolute())
            self.assertTrue(entity.books_path.is_absolute())
            self.assertTrue(entity.coa_path.is_absolute())

    def test_path_accessors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            entity = load_entity(target)
            self.assertEqual(entity.books_path, target.resolve() / "books.beancount")
            self.assertEqual(entity.coa_path, target.resolve() / "chart-of-accounts.beancount")
            self.assertEqual(entity.staging_dir, target.resolve() / "staging")
            self.assertEqual(entity.learned_context_dir, target.resolve() / "learned-context")
            self.assertEqual(entity.review_queue_dir, target.resolve() / "review-queue")
            self.assertEqual(entity.ingestion_dir, target.resolve() / "ingestion")
            self.assertEqual(entity.reports_dir, target.resolve() / "reports")

    def test_default_trust_threshold_is_3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            entity = load_entity(target)
            self.assertEqual(entity.auto_post_threshold, 3)

    def test_trust_policy_values_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            # Override threshold
            custom_policy = {"auto_post_threshold": 5, "queue_all_until_confirmed": False}
            (target / "trust-policy.json").write_text(
                json.dumps(custom_policy, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            entity = load_entity(target)
            self.assertEqual(entity.auto_post_threshold, 5)
            self.assertFalse(entity.trust_policy["queue_all_until_confirmed"])

    def test_missing_trust_policy_uses_defaults(self) -> None:
        """load_entity must not raise when trust-policy.json is absent."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            (target / "trust-policy.json").unlink()
            entity = load_entity(target)
            self.assertEqual(entity.auto_post_threshold, 3)
            self.assertTrue(entity.trust_policy["queue_all_until_confirmed"])

    def test_raises_when_entity_json_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "uninitialised"
            target.mkdir()
            with self.assertRaises(FileNotFoundError):
                load_entity(target)

    def test_saas_entity_config_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "saas-entity"
            init_entity(target, name="SaaS Co", business_type="saas")
            entity = load_entity(target)
            self.assertEqual(entity.business_type, "saas")
            self.assertEqual(entity.name, "SaaS Co")


class TestEntityJsonDeterministic(unittest.TestCase):
    """entity.json must be deterministic (sorted keys, 2-space indent)."""

    def test_entity_json_is_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            raw = (target / "entity.json").read_text(encoding="utf-8")
            parsed = json.loads(raw)
            # Re-serialise with same rules and compare
            expected = json.dumps(parsed, indent=2, sort_keys=True) + "\n"
            self.assertEqual(raw, expected)

    def test_trust_policy_json_is_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            raw = (target / "trust-policy.json").read_text(encoding="utf-8")
            parsed = json.loads(raw)
            expected = json.dumps(parsed, indent=2, sort_keys=True) + "\n"
            self.assertEqual(raw, expected)


class TestTemplateEnvOverride(unittest.TestCase):
    """BOOKKEEPING_TEMPLATES_DIR env var must redirect template loading."""

    def test_uses_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_templates:
            tmp_templates_path = Path(tmp_templates)
            # Copy real templates into the override dir
            real_templates = ROOT / "skills" / "books-onboard" / "templates"
            for src in real_templates.iterdir():
                (tmp_templates_path / src.name).write_bytes(src.read_bytes())

            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "my-entity"
                with unittest.mock.patch.dict(
                    os.environ,
                    {"BOOKKEEPING_TEMPLATES_DIR": str(tmp_templates_path)},
                ):
                    _init_simple(target)
                self.assertTrue((target / "chart-of-accounts.beancount").exists())


# Need to import mock for the env-override test
import unittest.mock  # noqa: E402


class TestCLISurface(unittest.TestCase):
    """add_parser / run integration tests."""

    def _make_args(
        self,
        path: Path,
        name: str = "",
        business_type: str = "consulting",
        legal_structure: str = "",
        cutover_date: str = "",
    ) -> object:
        import argparse
        ns = argparse.Namespace()
        ns.entity_command = "init"
        ns.path = path
        ns.name = name
        ns.business_type = business_type
        ns.legal_structure = legal_structure
        ns.cutover_date = cutover_date
        return ns

    def test_run_returns_zero_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cli-entity"
            args = self._make_args(target)
            result = run(args)
            self.assertEqual(result, 0)

    def test_run_creates_entity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cli-entity"
            args = self._make_args(target, name="CLI Corp", business_type="saas")
            run(args)
            self.assertTrue((target / "entity.json").exists())
            data = json.loads((target / "entity.json").read_text(encoding="utf-8"))
            self.assertEqual(data["name"], "CLI Corp")
            self.assertEqual(data["business_type"], "saas")

    def test_run_creates_entity_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cli-entity"
            args = self._make_args(
                target,
                name="CLI Corp",
                legal_structure="S corporation",
                cutover_date="2026-01-01",
            )
            run(args)
            data = json.loads((target / "entity.json").read_text(encoding="utf-8"))
            self.assertEqual(data["legal_structure"], "S corporation")
            self.assertEqual(data["cutover_date"], "2026-01-01")

    def test_run_returns_one_on_invalid_cutover_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cli-entity"
            args = self._make_args(target, cutover_date="soon")
            self.assertEqual(run(args), 1)

    def test_run_returns_one_on_package_repo_path(self) -> None:
        inside = ROOT / "src" / "bookkeeping" / "_would_be_entity"
        args = self._make_args(inside)
        result = run(args)
        self.assertEqual(result, 1)

    def test_add_parser_registers_init_subcommand(self) -> None:
        import argparse
        parent = argparse.ArgumentParser()
        sub = parent.add_subparsers(dest="command")
        add_parser(sub)
        parsed = parent.parse_args(["entity", "init", "/tmp/foo"])
        self.assertEqual(parsed.entity_command, "init")
        self.assertEqual(str(parsed.path), "/tmp/foo")

    def test_add_parser_business_type_choices(self) -> None:
        import argparse
        parent = argparse.ArgumentParser()
        sub = parent.add_subparsers(dest="command")
        add_parser(sub)
        parsed = parent.parse_args(["entity", "init", "/tmp/foo", "--business-type", "saas"])
        self.assertEqual(parsed.business_type, "saas")

    def test_add_parser_onboarding_context(self) -> None:
        import argparse
        parent = argparse.ArgumentParser()
        sub = parent.add_subparsers(dest="command")
        add_parser(sub)
        parsed = parent.parse_args([
            "entity",
            "init",
            "/tmp/foo",
            "--legal-structure",
            "S corporation",
            "--cutover-date",
            "2026-01-01",
        ])
        self.assertEqual(parsed.legal_structure, "S corporation")
        self.assertEqual(parsed.cutover_date, "2026-01-01")

    def test_add_parser_rejects_unknown_business_type(self) -> None:
        import argparse
        parent = argparse.ArgumentParser()
        sub = parent.add_subparsers(dest="command")
        add_parser(sub)
        with self.assertRaises(SystemExit):
            parent.parse_args(["entity", "init", "/tmp/foo", "--business-type", "ecommerce"])


class TestGitignoreHygiene(unittest.TestCase):
    """Gitignore template is written when target is a git repo."""

    def _make_fake_git_repo(self, base: Path) -> Path:
        """Create a minimal fake git work tree at *base* so git commands pass."""
        git_dir = base / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
        return base

    def test_gitignore_written_when_git_managed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            git_root = Path(tmp)
            self._make_fake_git_repo(git_root)
            entity_dir = git_root / "my-entity"
            # patch _git_root to return the fake root
            import bookkeeping.entity as entity_mod
            with unittest.mock.patch.object(entity_mod, "_git_root", return_value=git_root):
                init_entity(entity_dir)
            self.assertTrue((entity_dir / ".gitignore").exists())
            content = (entity_dir / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".env", content)
            self.assertIn(".secrets/", content)
            self.assertIn("cache*.sqlite", content)
            self.assertIn("staging/*.tmp", content)

    def test_no_gitignore_when_not_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entity_dir = Path(tmp) / "my-entity"
            import bookkeeping.entity as entity_mod
            with unittest.mock.patch.object(entity_mod, "_git_root", return_value=None):
                init_entity(entity_dir)
            self.assertFalse((entity_dir / ".gitignore").exists())

    def test_gitignore_not_overwritten_on_reinit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            git_root = Path(tmp)
            entity_dir = git_root / "my-entity"
            import bookkeeping.entity as entity_mod
            with unittest.mock.patch.object(entity_mod, "_git_root", return_value=git_root):
                init_entity(entity_dir)
            original = (entity_dir / ".gitignore").read_bytes()
            with unittest.mock.patch.object(entity_mod, "_git_root", return_value=git_root):
                init_entity(entity_dir)
            self.assertEqual((entity_dir / ".gitignore").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
