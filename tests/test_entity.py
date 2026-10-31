"""Tests for src/bookkeeping/entity.py — entity directory scaffolding."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
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
    map_bank_account,
    record_source_coverage,
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

    def test_website_saved_and_reinit_preserves_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "company"
            init_entity(root, name="Example", website_url="example.com")
            self.assertEqual(json.loads((root / "entity.json").read_text())["website_url"], "https://example.com")
            init_entity(root, website_url="https://other.example")
            self.assertEqual(json.loads((root / "entity.json").read_text())["website_url"], "https://example.com")

    def test_invalid_website_does_not_create_books(self) -> None:
        for website in ["file:///tmp/a", "https://user:password@example.com", "https://", "bad site"]:
            with self.subTest(website=website), tempfile.TemporaryDirectory() as folder:
                root = Path(folder) / "company"
                with self.assertRaises(ValueError):
                    init_entity(root, website_url=website)
                self.assertFalse(root.exists())

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

    def test_bank_account_mapping_uses_stable_feed_id_and_existing_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)

            result = map_bank_account(target, "provider-account-42", "Assets:Bank:Checking")

            self.assertEqual(result["status"], "created")
            self.assertEqual(
                load_entity(target).entity_config["bank_account_mappings"]["provider-account-42"],
                "Assets:Bank:Checking",
            )

    def test_source_coverage_preserves_existing_source_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            config_path = target / "entity.json"
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["declared_sources"] = ["Cancelled corporate card CSV"]
            config_path.write_text(json.dumps(data), encoding="utf-8")

            result = record_source_coverage(
                target,
                "Cancelled corporate card CSV",
                "2026-01-01",
                "2026-06-30",
            )

            self.assertEqual(result["status"], "updated")
            saved = load_entity(target).entity_config
            self.assertEqual(saved["declared_sources"], ["Cancelled corporate card CSV"])
            self.assertEqual(saved["source_coverage"]["Cancelled corporate card CSV"], {
                "coverage_from": "2026-01-01",
                "coverage_to": "2026-06-30",
            })

    def test_source_coverage_can_bind_affirmative_statement_scope_to_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            result = record_source_coverage(
                target, "Operating statement", "2026-09-01", "2026-09-30",
                "Assets:Bank:Checking",
            )
            self.assertEqual(result["account"], "Assets:Bank:Checking")
            saved = load_entity(target).entity_config["source_coverage"]["Operating statement"]
            self.assertEqual(saved["account"], "Assets:Bank:Checking")
            record_source_coverage(target, "Operating statement", "2026-01-01", "2026-10-31")
            saved = load_entity(target).entity_config["source_coverage"]["Operating statement"]
            self.assertEqual(saved["account"], "Assets:Bank:Checking")
            with self.assertRaisesRegex(ValueError, "asset or liability"):
                record_source_coverage(
                    target, "Revenue export", "2026-09-01", "2026-09-30", "Income:Consulting",
                )

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
        for cutover in ("January 1", "2026-02-30"):
            with self.subTest(cutover=cutover), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "my-entity"
                with self.assertRaises(ValueError):
                    init_entity(target, cutover_date=cutover)
                self.assertFalse(target.exists())

    def test_new_starter_accounts_use_explicit_past_and_future_cutover(self) -> None:
        from bookkeeping.ledger.store import LedgerStore

        for cutover in ("2024-07-01", "2030-04-15"):
            with self.subTest(cutover=cutover), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "company"
                init_entity(target, business_type="saas", cutover_date=cutover)
                opens = LedgerStore(target / "ledger.sqlite").load_opens()
                self.assertTrue(opens)
                self.assertEqual({item.date for item in opens}, {date.fromisoformat(cutover)})
                self.assertEqual(load_entity(target).entity_config["cutover_date"], cutover)

    def test_omitted_cutover_and_add_account_keep_existing_default_date(self) -> None:
        from bookkeeping.entity import add_account
        from bookkeeping.ledger.store import LedgerStore

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "company"
            init_entity(target)
            store = LedgerStore(target / "ledger.sqlite")
            self.assertEqual({item.date for item in store.load_opens()}, {date(2026, 1, 1)})
            explicit = Path(tmp) / "explicit-cutover"
            init_entity(explicit, cutover_date="2024-07-01")
            add_account(explicit, "Expenses:New-Account")
            opens = {item.account: item.date for item in LedgerStore(explicit / "ledger.sqlite").load_opens()}
            self.assertEqual(opens["Expenses:New-Account"], date(2026, 1, 1))

    def test_reinit_does_not_rewrite_existing_accounts_for_changed_cutover(self) -> None:
        from bookkeeping.ledger.importer import import_transactions

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "company"
            init_entity(target, cutover_date="2026-01-01")
            result = import_transactions(load_entity(target), [{"id": "synthetic-existing", "date": "2026-01-02",
                "description": "Synthetic income", "amount": "100.00", "accountName": "Checking", "pending": False}],
                "original-session", categorizer=lambda _: ("Income:Consulting", "high"),
                session_date=date(2026, 1, 2), ts="2026-01-02T00:00:00Z")
            self.assertFalse(result.errors)
            self.assertEqual(result.new_entries, 1)
            before = {name: (target / name).read_bytes() for name in _EXPECTED_FILES}
            for cutover in ("2024-07-01", "2030-04-15"):
                with self.subTest(cutover=cutover):
                    init_entity(target, cutover_date=cutover)
                    self.assertEqual({name: (target / name).read_bytes() for name in _EXPECTED_FILES}, before)

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
            self.assertEqual(store.get_meta("account_catalog"), "sqlite")

    def test_existing_beancount_prevents_empty_canonical_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            target.mkdir()
            (target / "books.beancount").write_text(
                '2026-01-01 open Assets:Bank:Checking USD\n',
                encoding="utf-8",
            )

            report = _init_simple(target)

            self.assertFalse((target / "ledger.sqlite").exists())
            self.assertIn("books.beancount", report["existed"])

    def test_sqlite_account_catalog_contains_open_directives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            from bookkeeping.ledger.store import LedgerStore

            accounts = LedgerStore(target / "ledger.sqlite").load_account_names()
            self.assertIn("Assets:Bank:Checking", accounts)
            self.assertIn("Income:Consulting", accounts)
            self.assertFalse((target / "chart-of-accounts.beancount").exists())

    def test_saas_account_catalog_has_saas_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            init_entity(target, business_type="saas")
            from bookkeeping.ledger.store import LedgerStore

            accounts = LedgerStore(target / "ledger.sqlite").load_account_names()
            self.assertIn("Income:Subscriptions", accounts)
            self.assertIn("Expenses:Hosting", accounts)
            self.assertIn("Expenses:Payment-Fees", accounts)
            self.assertIn("Expenses:Marketing", accounts)

    def test_services_and_subscriptions_catalog_has_both_revenue_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            init_entity(target, business_type="services_and_subscriptions")
            from bookkeeping.ledger.store import LedgerStore

            accounts = LedgerStore(target / "ledger.sqlite").load_account_names()
            self.assertIn("Income:Consulting", accounts)
            self.assertIn("Income:Subscriptions", accounts)
            self.assertIn("Expenses:Subcontractors", accounts)
            self.assertIn("Expenses:Hosting", accounts)
            self.assertIn("Expenses:Payment-Fees", accounts)

    def test_add_account_adds_to_sqlite_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            from bookkeeping.entity import add_account
            from bookkeeping.ledger.store import LedgerStore

            report = add_account(target, "Expenses:Marketing-Events", open_date="2026-06-27")

            self.assertEqual(report["status"], "created")
            self.assertIn(
                "Expenses:Marketing-Events",
                LedgerStore(target / "ledger.sqlite").load_account_names(),
            )

            repeat = add_account(target, "Expenses:Marketing-Events", open_date="2026-06-27")
            self.assertEqual(repeat["status"], "existed")

    def test_consulting_account_catalog_has_consulting_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-entity"
            _init_simple(target)
            from bookkeeping.ledger.store import LedgerStore

            accounts = LedgerStore(target / "ledger.sqlite").load_account_names()
            self.assertIn("Income:Consulting", accounts)
            # Should NOT have SaaS-only accounts
            self.assertNotIn("Income:Subscriptions", accounts)

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
            original_trust = (target / "trust-policy.json").read_bytes()
            original_profile = (target / "business-profile.md").read_bytes()

            # Re-init with different name — should NOT change anything
            _init_simple(target, name="Second Name")

            self.assertEqual((target / "entity.json").read_bytes(), original_entity)
            self.assertEqual((target / "ledger.sqlite").read_bytes(), original_store)
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
                self.assertTrue((target / "ledger.sqlite").exists())
                self.assertFalse((target / "chart-of-accounts.beancount").exists())


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
        parsed = parent.parse_args([
            "entity",
            "init",
            "/tmp/foo",
            "--business-type",
            "services_and_subscriptions",
        ])
        self.assertEqual(parsed.business_type, "services_and_subscriptions")

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


class TestAccountAddSealing(unittest.TestCase):
    def setUp(self) -> None:
        from bookkeeping.ledger.store import LedgerStore

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "company"
        _init_simple(self.root, name="Synthetic Account Tests")
        self.store = LedgerStore(self.root / "ledger.sqlite")

    def test_integrity_check_runs_under_writer_reservation_before_changes(self) -> None:
        import sqlite3
        from bookkeeping.entity import add_account
        from bookkeeping.ledger.importer import check_integrity

        before = self.store.content_digest(), self.store.load_audit_events()

        def reserved_check(entity):
            other = sqlite3.connect(self.store.path, timeout=0)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    other.execute("BEGIN IMMEDIATE")
            finally:
                other.close()
            self.assertEqual((self.store.content_digest(), self.store.load_audit_events()), before)
            return check_integrity(entity)

        with unittest.mock.patch("bookkeeping.ledger.importer.check_integrity", side_effect=reserved_check) as check:
            add_account(self.root, "Expenses:Reserved-Write")
        check.assert_called_once()
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_effective_catalog_changes_are_sealed_and_true_retries_are_noops(self) -> None:
        from bookkeeping.entity import add_account
        from bookkeeping.ledger.importer import check_integrity

        account = "Expenses:New-Test"
        for opened, currency, status in [("2026-03-01", "USD", "created"),
                                         ("2026-01-01", "USD", "existed"),
                                         ("2026-01-01", "EUR", "existed")]:
            before = self.store.load_audit_events()
            result = add_account(self.root, account, opened, currency)
            self.assertEqual(result["status"], status)
            events = self.store.load_audit_events()
            self.assertEqual(events[:len(before)], before)
            self.assertEqual([item["type"] for item in events[len(before):]], ["intent", "ledger-store-sealed"])
            self.assertIn(f"open {opened}, currency {currency}", events[-2]["payload"]["description"])
            self.assertEqual(events[-1]["payload"]["entries"], 0)
            self.assertEqual(events[-1]["payload"]["source_ids"], [])
            self.assertEqual(events[-1]["payload"]["store_sha256"], self.store.content_digest())
            self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
            self.assertEqual(self.store.counts().entries, 0)
            image = self.store.path.read_bytes()
            for retry_date in (opened, "2026-12-31"):
                self.assertEqual(add_account(self.root, account, retry_date, currency)["status"], "existed")
                self.assertEqual(self.store.load_audit_events(), events)
                self.assertEqual(self.store.path.read_bytes(), image)

    def test_blank_currency_retains_usd_default_and_retry_does_not_seal(self) -> None:
        from bookkeeping.entity import add_account

        add_account(self.root, "Expenses:Default-Currency", currency="")
        events = self.store.load_audit_events()
        add_account(self.root, "Expenses:Default-Currency", currency="USD")
        self.assertEqual(self.store.load_audit_events(), events)
        opened = next(item for item in self.store.load_opens() if item.account == "Expenses:Default-Currency")
        self.assertEqual(opened.currencies, ("USD",))

    def test_damaged_baseline_is_not_resealed_even_for_a_retry(self) -> None:
        from bookkeeping.entity import add_account
        from bookkeeping.ledger.importer import check_integrity

        for damage in ("digest", "chain", "incomplete"):
            with self.subTest(damage=damage):
                root = self.root.parent / damage
                _init_simple(root)
                from bookkeeping.ledger.store import LedgerStore
                store = LedgerStore(root / "ledger.sqlite")
                add_account(root, "Expenses:Original")
                with store.transaction() as conn:
                    if damage == "digest":
                        conn.execute("UPDATE accounts SET open_date='2025-01-01' WHERE name='Expenses:Original'")
                    elif damage == "chain":
                        conn.execute("UPDATE audit_events SET record_hash=? WHERE id=(SELECT max(id) FROM audit_events)", ("0" * 64,))
                    else:
                        store.append_audit_event("intent", {"description": "Synthetic incomplete write"}, conn)
                self.assertNotEqual(check_integrity(load_entity(root)).status, "ok")
                image = store.path.read_bytes()
                for account in ("Expenses:Original", "Expenses:New"):
                    with self.assertRaisesRegex(ValueError, "ledger integrity is invalid"):
                        add_account(root, account)
                    self.assertEqual(store.path.read_bytes(), image)

    def test_populated_unaudited_baseline_is_not_given_a_first_seal(self) -> None:
        from decimal import Decimal
        from bookkeeping.entity import add_account
        from bookkeeping.ledger.importer import check_integrity
        from bookkeeping.ledger.model import Entry, Posting

        with self.store.transaction() as conn:
            self.store.insert_entries([Entry(date=date(2026, 1, 2), narration="Synthetic unaudited entry",
                postings=(Posting("Expenses:Software", Decimal("1.00")),
                          Posting("Assets:Bank:Checking", Decimal("-1.00"))))], conn)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        image = self.store.path.read_bytes()
        for account in ("Expenses:New", "Expenses:Software"):
            with self.assertRaisesRegex(ValueError, "populated ledger without audit history"):
                add_account(self.root, account)
            self.assertEqual(self.store.path.read_bytes(), image)
            self.assertEqual(self.store.load_audit_events(), [])

    def test_account_add_preserves_distinct_metadata_and_missing_catalog_marker(self) -> None:
        from bookkeeping.entity import add_account
        from bookkeeping.ledger.importer import check_integrity
        from bookkeeping.ledger.store import LedgerStore

        with self.store.transaction() as conn:
            conn.execute("DELETE FROM meta WHERE key='account_catalog'")
            self.store.set_meta("title", "Distinct stored title", conn)
            self.store.set_meta("canonical", "false", conn)
            self.store.set_meta("synthetic_custom_meta", "preserve exactly", conn)
        with self.store.connection() as conn:
            before = dict(conn.execute("SELECT key,value FROM meta"))
        with unittest.mock.patch.object(LedgerStore, "initialize", side_effect=AssertionError("No schema initialization")):
            add_account(self.root, "Expenses:No-Catalog-Repair")
            add_account(self.root, "Expenses:No-Catalog-Repair", open_date="2025-01-01")
        with self.store.connection() as conn:
            self.assertEqual(dict(conn.execute("SELECT key,value FROM meta")), before)
        self.assertIsNone(self.store.get_meta("account_catalog"))
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_missing_store_is_rejected_without_creation(self) -> None:
        from bookkeeping.entity import add_account

        self.store.path.unlink()
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(ValueError, "initialized SQLite ledger"):
            add_account(self.root, "Expenses:No-Creation")
        self.assertFalse(self.store.path.exists())
        self.assertEqual({path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*")
                          if path.is_file() and path.name != ".books.lock"}, before)

    def test_unsupported_schema_is_rejected_before_integrity_or_mutation(self) -> None:
        from bookkeeping.entity import add_account

        for version in ("0", "999", None):
            with self.subTest(version=version):
                # Simulate an external/newer writer. Compatible store APIs now
                # reject these formats at connection admission.
                import sqlite3
                with sqlite3.connect(self.store.path) as conn:
                    if version is None:
                        conn.execute("DELETE FROM meta WHERE key='schema_version'")
                    else:
                        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (version,))
                image = self.store.path.read_bytes()
                with unittest.mock.patch("bookkeeping.ledger.importer.check_integrity",
                                         side_effect=AssertionError("Schema must be checked first")):
                    with self.assertRaisesRegex(ValueError, "Unsupported ledger schema"):
                        add_account(self.root, "Expenses:No-Upgrade")
                self.assertEqual(self.store.path.read_bytes(), image)

    def test_uninitialized_sqlite_is_rejected_without_schema_creation(self) -> None:
        from bookkeeping.entity import add_account

        self.store.path.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "initialized supported-schema SQLite ledger"):
            add_account(self.root, "Expenses:No-Schema-Creation")
        self.assertEqual(self.store.path.read_bytes(), b"")

    def test_account_add_does_not_force_migrate_a_legacy_ledger(self) -> None:
        from bookkeeping.entity import add_account

        self.store.path.unlink()
        legacy = self.root / "books.beancount"
        legacy.write_text('2026-01-01 open Assets:Bank:Checking USD\n', encoding="utf-8")
        original = legacy.read_bytes()
        with self.assertRaisesRegex(ValueError, "Migrate the legacy ledger explicitly"):
            add_account(self.root, "Expenses:No-Implicit-Migration")
        self.assertFalse(self.store.path.exists())
        self.assertEqual(legacy.read_bytes(), original)

    def test_conflicting_currency_of_used_accounts_is_rejected_without_changes(self) -> None:
        from decimal import Decimal
        from bookkeeping.entity import add_account
        from bookkeeping.ledger.importer import _atomic_ledger_write, check_integrity
        from bookkeeping.ledger.model import Balance, Entry, Posting

        account = "Expenses:Used-Test"
        add_account(self.root, account)
        _atomic_ledger_write(load_entity(self.root), [], [Entry(date=date(2026, 1, 2), narration="Synthetic usage",
            postings=(Posting(account, Decimal("1.00")), Posting("Assets:Bank:Checking", Decimal("-1.00"))))],
            "synthetic-usage", None, "Synthetic account currency test")
        add_account(self.root, "Assets:Assertion-Only")
        with self.store.transaction() as conn:
            self.store.insert_balances([Balance(date(2026, 1, 2), "Assets:Assertion-Only", Decimal("0.00"))], conn)
            self.store.append_audit_event("ledger-store-sealed", {"store_sha256": self.store.content_digest(conn)}, conn)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")
        image = self.store.path.read_bytes()
        for used in (account, "Assets:Assertion-Only"):
            with self.assertRaisesRegex(ValueError, "currency conflicts"):
                add_account(self.root, used, currency="EUR")
            self.assertEqual(self.store.path.read_bytes(), image)
        entries = self.store.load_entries()
        add_account(self.root, account, open_date="2025-01-01")
        self.assertEqual(self.store.load_entries(), entries)
        self.assertEqual(check_integrity(load_entity(self.root)).status, "ok")

    def test_seal_failure_rolls_back_account_and_intent(self) -> None:
        from bookkeeping.entity import add_account
        from bookkeeping.ledger.store import LedgerStore

        with self.store.transaction() as conn:
            self.store.set_meta("title", "Rollback must retain this title", conn)
            self.store.set_meta("canonical", "false", conn)
        add_account(self.root, "Expenses:Original")
        with self.store.connection() as conn:
            before = list(conn.iterdump())
        append = LedgerStore.append_audit_event

        def fail_seal(store, record_type, *args, **kwargs):
            if record_type == "ledger-store-sealed":
                raise RuntimeError("Synthetic seal failure")
            return append(store, record_type, *args, **kwargs)

        with unittest.mock.patch.object(LedgerStore, "append_audit_event", fail_seal):
            with self.assertRaisesRegex(RuntimeError, "Synthetic seal failure"):
                add_account(self.root, "Expenses:Rollback")
        with self.store.connection() as conn:
            self.assertEqual(list(conn.iterdump()), before)
        self.assertNotIn("Expenses:Rollback", self.store.load_account_names())

    def test_demo_account_adds_related_entity_then_import_and_export_keep_integrity(self) -> None:
        from bookkeeping.demo import init_demo
        from bookkeeping.entity import add_account, record_related_entity
        from bookkeeping.ledger.importer import check_integrity, import_transactions
        from bookkeeping.ledger.store import LedgerStore
        from bookkeeping.migration_bundle import build_bundle, load_bundle, validate_files

        root = self.root.parent / "demo"
        init_demo(root, as_of=date(2026, 6, 26))
        store = LedgerStore(root / "ledger.sqlite")
        entries, history = store.load_entries(), store.load_audit_events()
        for account in ("Assets:Receivable:RelatedQA", "Liabilities:Payable:RelatedQA"):
            add_account(root, account, open_date="2025-01-01")
            self.assertEqual(check_integrity(load_entity(root)).status, "ok")
        record_related_entity(root, "Synthetic Related QA", "Assets:Receivable:RelatedQA", "Liabilities:Payable:RelatedQA",
                              "settle-receivable", "create-receivable")
        self.assertEqual(store.load_entries(), entries)
        self.assertEqual(store.load_audit_events()[:len(history)], history)
        self.assertEqual(len(store.load_audit_events()), len(history) + 4)
        self.assertEqual(check_integrity(load_entity(root)).status, "ok")
        result = import_transactions(load_entity(root), [{"id": "synthetic-after-account-add", "date": "2026-06-26",
            "description": "Synthetic new income", "amount": "10.00", "accountName": "Checking", "pending": False}],
            "after-account-add", categorizer=lambda _: ("Income:Subscriptions", "high"),
            session_date=date(2026, 6, 26), ts="2026-06-26T13:00:00Z")
        self.assertFalse(result.errors)
        self.assertEqual(result.new_entries, 1)
        self.assertEqual(check_integrity(load_entity(root)).status, "ok")
        destination = root.parent / "demo.zip"
        exported = build_bundle(root, destination)
        self.assertEqual(exported["status"], "exported", exported)
        loaded = load_bundle(destination)
        self.assertEqual(validate_files(loaded["manifest"], loaded["files"])["status"], "validated")
        self.assertEqual(check_integrity(load_entity(root)).status, "ok")


class TestAccountingBasis(unittest.TestCase):
    def test_legacy_defaults_aliases_and_authoritative_mismatch(self) -> None:
        from bookkeeping.entity import resolve_basis, require_accrual
        self.assertEqual(resolve_basis({}), "cash")
        self.assertEqual(resolve_basis({"accounting_basis": "accrual"}), "accrual")
        self.assertEqual(resolve_basis({"basis": "accrual", "accounting_basis": "accrual"}), "accrual")
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            resolve_basis({"basis": "cash", "accounting_basis": "accrual"})
        with self.assertRaisesRegex(ValueError, "authoritative"):
            resolve_basis({"basis": "accrual"}, authoritative_basis="cash")
        for version in (True, "1", -1, 2):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "capability version"):
                resolve_basis({"accrual_capability_version": version})
        for config in ({}, {"basis": "accrual"}, {"basis": "cash", "accrual_capability_version": 1}):
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, "explicit accrual"):
                require_accrual(config)
        require_accrual({"basis": "accrual", "accrual_capability_version": 1})

    def test_explicit_accrual_init_sets_supported_capability_and_schema(self) -> None:
        from bookkeeping.entity import require_accrual
        from bookkeeping.ledger.store import LedgerStore
        from bookkeeping.ledger.importer import check_integrity
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "company"
            init_entity(root, basis="accrual")
            entity = load_entity(root)
            self.assertEqual(entity.basis, "accrual")
            require_accrual(entity)
            self.assertEqual(LedgerStore(root / "ledger.sqlite").get_meta("schema_version"), "2")
            self.assertEqual(check_integrity(entity).status, "ok")
            before = (root / "entity.json").read_bytes()
            init_entity(root)
            self.assertEqual((root / "entity.json").read_bytes(), before)

    def test_empty_company_selection_and_populated_relabel_rejection(self) -> None:
        from bookkeeping.entity import configure_basis, require_accrual
        from bookkeeping.ledger.importer import _atomic_ledger_write
        from bookkeeping.ledger.model import Entry, Posting
        from decimal import Decimal
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "company"
            init_entity(root)
            configure_basis(root, "accrual")
            entity = load_entity(root)
            require_accrual(entity)
            _atomic_ledger_write(entity, [], [Entry(date(2026, 1, 1), "Synthetic", postings=(
                Posting("Expenses:Software", Decimal("1.00")),
                Posting("Assets:Bank:Checking", Decimal("-1.00")),
            ))], "synthetic", None, "Synthetic")
            before = (root / "entity.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "populated company"):
                configure_basis(root, "cash")
            self.assertEqual((root / "entity.json").read_bytes(), before)
            with self.assertRaisesRegex(ValueError, "Re-init preserves"):
                init_entity(root, basis="cash")

    def test_configuration_edit_cannot_relabel_ledger_basis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "company"
            init_entity(root)
            config = json.loads((root / "entity.json").read_text())
            config.update(basis="accrual", accrual_capability_version=1)
            (root / "entity.json").write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "authoritative"):
                load_entity(root)


class TestOnboardingComplete(unittest.TestCase):
    """`books entity onboarding-complete` records one completion marker in entity.json."""

    def _run(self, *argv: str) -> tuple[int, dict]:
        import argparse
        import contextlib
        import io
        parent = argparse.ArgumentParser()
        add_parser(parent.add_subparsers(dest="command"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run(parent.parse_args(["entity", *argv]))
        return code, json.loads(out.getvalue())

    def test_records_the_marker_and_keeps_the_first_one(self) -> None:
        from bookkeeping.entity import onboarding_completed
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "company"
            init_entity(root)
            self.assertIsNone(onboarding_completed(load_entity(root).entity_config))
            ledger = (root / "ledger.sqlite").read_bytes()
            code, result = self._run("onboarding-complete", str(root), "--via", "agent", "--on", "2026-10-20")
            self.assertEqual(code, 0)
            self.assertEqual(result, {"status": "completed", "onboarding_completed": {"on": "2026-10-20", "via": "agent"}})
            config = json.loads((root / "entity.json").read_text(encoding="utf-8"))
            self.assertEqual(config["onboarding_completed"], {"on": "2026-10-20", "via": "agent"})
            self.assertEqual(onboarding_completed(config), {"on": "2026-10-20", "via": "agent"})
            # A second call, by any path, keeps the first record and writes nothing.
            before = (root / "entity.json").read_bytes()
            code, result = self._run("onboarding-complete", str(root), "--via", "staff")
            self.assertEqual(code, 0)
            self.assertEqual(result, {"status": "already_complete", "onboarding_completed": {"on": "2026-10-20", "via": "agent"}})
            self.assertEqual((root / "entity.json").read_bytes(), before)
            # The ledger does not change.
            self.assertEqual((root / "ledger.sqlite").read_bytes(), ledger)

    def test_defaults_to_today_and_keeps_other_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "company"
            init_entity(root, name="Synthetic Co", website_url="https://example.com")
            code, result = self._run("onboarding-complete", str(root), "--via", "meeting")
            self.assertEqual(code, 0)
            self.assertEqual(result["onboarding_completed"], {"on": date.today().isoformat(), "via": "meeting"})
            config = json.loads((root / "entity.json").read_text(encoding="utf-8"))
            self.assertEqual(config["name"], "Synthetic Co")
            self.assertEqual(config["website_url"], "https://example.com")

    def test_refuses_a_bad_date_or_a_missing_company(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "company"
            init_entity(root)
            before = (root / "entity.json").read_bytes()
            code, result = self._run("onboarding-complete", str(root), "--via", "agent", "--on", "soon")
            self.assertEqual(code, 1)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual((root / "entity.json").read_bytes(), before)
            code, result = self._run("onboarding-complete", str(Path(tmp) / "missing"), "--via", "agent")
            self.assertEqual(code, 1)
            self.assertFalse((Path(tmp) / "missing").exists())

    def test_refuses_a_date_after_today(self) -> None:
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "company"
            init_entity(root)
            before = (root / "entity.json").read_bytes()
            latest = max(date.today(), datetime.now(timezone.utc).date())
            code, result = self._run("onboarding-complete", str(root), "--via", "agent", "--on", (latest + timedelta(days=1)).isoformat())
            self.assertEqual(code, 1)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("after today", result["errors"][0]["message"])
            self.assertEqual((root / "entity.json").read_bytes(), before)
            # Today itself is accepted.
            code, result = self._run("onboarding-complete", str(root), "--via", "agent", "--on", latest.isoformat())
            self.assertEqual(code, 0)
            self.assertEqual(result["onboarding_completed"]["on"], latest.isoformat())

    def test_a_malformed_marker_counts_as_not_complete(self) -> None:
        from bookkeeping.entity import onboarding_completed
        for value in (None, "yes", {"on": "2026-10-20"}, {"on": "2026-13-01", "via": "agent"}, {"on": "2026-10-20", "via": "robot"}):
            with self.subTest(value=value):
                self.assertIsNone(onboarding_completed({"onboarding_completed": value}))

    def test_rejects_an_unknown_via(self) -> None:
        import argparse
        parent = argparse.ArgumentParser()
        add_parser(parent.add_subparsers(dest="command"))
        with self.assertRaises(SystemExit):
            parent.parse_args(["entity", "onboarding-complete", "/tmp/foo", "--via", "robot"])


class TestIndustryStarterCharts(unittest.TestCase):
    """Each business type opens a chart with the common accounts and its own industry accounts."""

    COMMON = {"Expenses:Rent", "Expenses:Payroll", "Expenses:Payroll-Taxes", "Expenses:Utilities",
              "Expenses:Insurance", "Expenses:Bank-Fees", "Expenses:Software", "Expenses:Marketing",
              "Expenses:Meals", "Expenses:Travel", "Expenses:Professional-Fees",
              "Equity:Owner-Draws", "Equity:Owner-Contributions", "Assets:Bank:Checking"}
    INDUSTRY = {
        "retail": {"Income:Sales", "Assets:Other:Inventory", "Expenses:Cost-of-Goods-Sold", "Expenses:Payment-Fees",
                   "Liabilities:Sales-Tax-Payable"},
        "restaurant": {"Income:Sales", "Assets:Other:Inventory", "Expenses:Cost-of-Goods-Sold", "Expenses:Payment-Fees",
                       "Liabilities:Sales-Tax-Payable"},
        "trades": {"Income:Services", "Expenses:Cost-of-Sales:Materials", "Expenses:Cost-of-Sales:Subcontractors",
                   "Expenses:Tools-and-Equipment"},
        "real_estate": {"Income:Rental-Income", "Expenses:Repairs", "Expenses:Property-Taxes",
                        "Expenses:Mortgage-Interest", "Liabilities:Loans:Mortgage"},
        "services": {"Income:Services", "Income:Consulting"},
        "saas": {"Income:Subscriptions", "Expenses:Hosting"},
        "other": {"Income:Sales", "Income:Services"},
    }

    def test_every_chart_is_valid_and_has_common_accounts(self) -> None:
        from bookkeeping.entity import _ACCRUAL_STARTER_ACCOUNTS, _STARTER_ACCOUNTS, BUSINESS_TYPES

        self.assertTrue({"services", "saas", "retail", "restaurant", "trades", "real_estate", "other",
                         "consulting", "services_and_subscriptions"} <= set(BUSINESS_TYPES))
        for key, chart in _STARTER_ACCOUNTS.items():
            with self.subTest(chart=key):
                self.assertEqual(len(chart), len(set(chart)))
                self.assertTrue(self.COMMON <= set(chart), self.COMMON - set(chart))
                self.assertTrue(all(a.split(":")[0] in {"Assets", "Liabilities", "Equity", "Income", "Expenses"} for a in chart))
                self.assertTrue(any(a.startswith("Income:") for a in chart))
                # No name may match an accrual starter account under another root: accrual enable refuses that.
                tails = {a.split(":", 1)[1].lower() for a in _ACCRUAL_STARTER_ACCOUNTS}
                self.assertFalse(tails & {a.split(":", 1)[1].lower() for a in chart})

    def test_industry_accounts_by_type(self) -> None:
        from bookkeeping.entity import starter_chart

        for key, expected in self.INDUSTRY.items():
            with self.subTest(business_type=key):
                self.assertTrue(expected <= set(starter_chart(key)), expected - set(starter_chart(key)))

    def test_cost_of_goods_accounts_are_found_as_cost_of_sales(self) -> None:
        from bookkeeping.reports.structure import is_cost_of_sales
        from bookkeeping.entity import check_category_account, starter_chart

        for key in ("retail", "restaurant", "trades"):
            names = [a for a in starter_chart(key) if is_cost_of_sales(a)]
            self.assertTrue(names, key)
            for name in names:
                check_category_account(name)

    def test_existing_types_keep_their_earlier_accounts_in_order(self) -> None:
        from bookkeeping.entity import starter_chart

        earlier_consulting = ["Assets:Bank:Checking", "Liabilities:CreditCard", "Equity:Opening-Balances",
                              "Equity:Owner-Contributions", "Equity:Owner-Draws", "Income:Consulting",
                              "Expenses:Software", "Expenses:Subcontractors", "Expenses:Meals", "Expenses:Marketing",
                              "Expenses:Travel", "Expenses:Office", "Expenses:Fees", "Expenses:Taxes"]
        self.assertEqual(list(starter_chart("consulting"))[:len(earlier_consulting)], earlier_consulting)
        self.assertEqual(starter_chart("consulting"), starter_chart("services"))
        self.assertEqual(starter_chart("no-such-type"), starter_chart("services"))

    def test_init_with_each_type_creates_its_chart(self) -> None:
        from bookkeeping.entity import BUSINESS_TYPES, starter_chart
        from bookkeeping.ledger.store import LedgerStore

        for key in BUSINESS_TYPES:
            with self.subTest(business_type=key), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "company"
                init_entity(target, business_type=key)
                names = {o.account for o in LedgerStore(target / "ledger.sqlite").load_opens()}
                self.assertEqual(names, set(starter_chart(key)))

    def test_cli_accepts_every_business_type(self) -> None:
        import argparse

        parser = argparse.ArgumentParser()
        add_parser(parser.add_subparsers(dest="command"))
        for key in ("retail", "restaurant", "trades", "real_estate", "other", "services", "consulting"):
            self.assertEqual(parser.parse_args(["entity", "init", "/tmp/x", "--business-type", key]).business_type, key)
            self.assertEqual(parser.parse_args(["entity", "starter-accounts", "/tmp/x", "--business-type", key]).business_type, key)


class TestStarterAccounts(unittest.TestCase):
    """books entity starter-accounts applies a chart to a company that has no entries."""

    def _company(self, tmp: str, **kwargs: object) -> Path:
        target = Path(tmp) / "company"
        init_entity(target, name="Bakery", **kwargs)
        return target

    def _opens(self, target: Path) -> dict[str, date]:
        from bookkeeping.ledger.store import LedgerStore
        return {o.account: o.date for o in LedgerStore(target / "ledger.sqlite").load_opens()}

    def test_adds_missing_accounts_and_sets_the_type(self) -> None:
        from bookkeeping.entity import apply_starter_accounts, starter_chart
        from bookkeeping.ledger.importer import check_integrity
        from bookkeeping.ledger.store import LedgerStore

        with tempfile.TemporaryDirectory() as tmp:
            target = self._company(tmp, business_type="consulting", cutover_date="2025-03-01")
            before = self._opens(target)
            result = apply_starter_accounts(target, "restaurant")
            after = self._opens(target)
            self.assertEqual(set(after), set(starter_chart("restaurant")))
            self.assertEqual(set(result["added"]), set(after) - set(before))
            self.assertEqual(set(result["removed"]), set(before) - set(after))
            self.assertEqual(set(result["removed"]), {"Income:Consulting", "Expenses:Subcontractors", "Expenses:Fees", "Income:Services"})
            self.assertIn("Expenses:Cost-of-Goods-Sold", result["added"])
            self.assertTrue(all(after[a] == before[a] for a in before if a in after))
            self.assertEqual({after[a] for a in result["added"]}, {date(2025, 3, 1)})
            self.assertEqual(load_entity(target).business_type, "restaurant")
            self.assertEqual(LedgerStore(target / "ledger.sqlite").get_meta("business_type"), "restaurant")
            self.assertEqual(check_integrity(load_entity(target)).status, "ok")

    def test_repeat_adds_nothing(self) -> None:
        from bookkeeping.entity import apply_starter_accounts

        with tempfile.TemporaryDirectory() as tmp:
            target = self._company(tmp)
            apply_starter_accounts(target, "retail")
            before = self._opens(target)
            again = apply_starter_accounts(target, "retail")
            self.assertEqual(again["added"], [])
            self.assertEqual(self._opens(target), before)

    def test_accrual_company_keeps_accrual_accounts(self) -> None:
        from bookkeeping.entity import _ACCRUAL_STARTER_ACCOUNTS, apply_starter_accounts

        with tempfile.TemporaryDirectory() as tmp:
            target = self._company(tmp, basis="accrual")
            apply_starter_accounts(target, "trades")
            self.assertTrue(set(_ACCRUAL_STARTER_ACCOUNTS) <= set(self._opens(target)))

    def test_type_only_change_preserves_seal_and_repeat_is_noop(self) -> None:
        from bookkeeping.entity import apply_starter_accounts
        from bookkeeping.ledger.importer import check_integrity
        from bookkeeping.ledger.store import LedgerStore

        for basis in ("cash", "accrual"):
            with self.subTest(basis=basis), tempfile.TemporaryDirectory() as tmp:
                target = self._company(tmp, basis=basis, business_type="consulting")
                store = LedgerStore(target / "ledger.sqlite")
                accounts = self._opens(target)
                before = store.load_audit_events()
                result = apply_starter_accounts(target, "services")
                self.assertEqual(result["added"], [])
                self.assertEqual(result["removed"], [])
                self.assertEqual(self._opens(target), accounts)
                self.assertEqual(store.get_meta("business_type"), "services")
                self.assertEqual(load_entity(target).business_type, "services")
                self.assertEqual(check_integrity(load_entity(target)).status, "ok")
                events = store.load_audit_events()
                self.assertEqual(len(events), len(before) + 2)
                self.assertEqual(events[:len(before)], before)
                self.assertEqual(events[-1]["type"], "ledger-store-sealed")
                digest = store.content_digest()
                again = apply_starter_accounts(target, "services")
                self.assertEqual(again["added"], [])
                self.assertEqual(again["removed"], [])
                self.assertEqual(store.load_audit_events(), events)
                self.assertEqual(store.content_digest(), digest)
                self.assertEqual(check_integrity(load_entity(target)).status, "ok")

    def test_type_config_recovery_does_not_change_sealed_ledger(self) -> None:
        from bookkeeping.entity import apply_starter_accounts
        from bookkeeping.ledger.importer import check_integrity
        from bookkeeping.ledger.store import LedgerStore

        with tempfile.TemporaryDirectory() as tmp:
            target = self._company(tmp, basis="accrual", business_type="consulting")
            apply_starter_accounts(target, "services")
            store = LedgerStore(target / "ledger.sqlite")
            digest, events = store.content_digest(), store.load_audit_events()
            config = json.loads((target / "entity.json").read_text())
            config["business_type"] = "consulting"
            (target / "entity.json").write_text(json.dumps(config))
            result = apply_starter_accounts(target, "services")
            self.assertEqual(result["added"], [])
            self.assertEqual(result["removed"], [])
            self.assertEqual(load_entity(target).business_type, "services")
            self.assertEqual(store.content_digest(), digest)
            self.assertEqual(store.load_audit_events(), events)
            self.assertEqual(check_integrity(load_entity(target)).status, "ok")

    def test_refuses_a_company_with_entries_and_changes_nothing(self) -> None:
        from bookkeeping.entity import apply_starter_accounts
        from bookkeeping.ledger.importer import import_transactions

        with tempfile.TemporaryDirectory() as tmp:
            target = self._company(tmp)
            result = import_transactions(load_entity(target), [{"id": "synthetic-1", "date": "2026-01-02",
                "description": "Synthetic income", "amount": "100.00", "accountName": "Checking", "pending": False}],
                "session", categorizer=lambda _: ("Income:Consulting", "high"),
                session_date=date(2026, 1, 2), ts="2026-01-02T00:00:00Z")
            self.assertEqual(result.new_entries, 1)
            files = {n: (target / n).read_bytes() for n in ("ledger.sqlite", "entity.json")}
            with self.assertRaisesRegex(ValueError, "already has entries"):
                apply_starter_accounts(target, "retail")
            self.assertEqual({n: (target / n).read_bytes() for n in files}, files)

    def test_keeps_user_added_referenced_and_new_chart_accounts(self) -> None:
        from bookkeeping.entity import add_account, apply_starter_accounts

        with tempfile.TemporaryDirectory() as tmp:
            target = self._company(tmp, business_type="consulting")
            add_account(target, "Expenses:Flour")  # not a starter account
            config = json.loads((target / "entity.json").read_text(encoding="utf-8"))
            config["bank_account_mappings"] = {"acct-1": "Expenses:Subcontractors"}  # named in settings
            (target / "entity.json").write_text(json.dumps(config), encoding="utf-8")
            result = apply_starter_accounts(target, "retail")
            opens = self._opens(target)
            self.assertIn("Expenses:Flour", opens)
            self.assertIn("Expenses:Subcontractors", opens)
            self.assertNotIn("Expenses:Subcontractors", result["removed"])
            self.assertIn("Income:Consulting", result["removed"])
            self.assertNotIn("Income:Consulting", opens)
            self.assertIn("Liabilities:Sales-Tax-Payable", opens)
            self.assertEqual(apply_starter_accounts(target, "retail")["removed"], [])

    def test_unknown_type_is_refused(self) -> None:
        from bookkeeping.entity import apply_starter_accounts

        with tempfile.TemporaryDirectory() as tmp:
            target = self._company(tmp)
            with self.assertRaisesRegex(ValueError, "Unknown business type"):
                apply_starter_accounts(target, "bakery")

    def test_cli_json_and_refusal_exit_code(self) -> None:
        import argparse
        import contextlib
        import io

        parser = argparse.ArgumentParser()
        add_parser(parser.add_subparsers(dest="command"))
        with tempfile.TemporaryDirectory() as tmp:
            target = self._company(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = run(parser.parse_args(["entity", "starter-accounts", str(target), "--business-type", "retail", "--json"]))
            self.assertEqual(code, 0)
            self.assertIn("Expenses:Cost-of-Goods-Sold", json.loads(out.getvalue())["added"])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = run(parser.parse_args(["entity", "starter-accounts", str(Path(tmp) / "missing"), "--business-type", "retail"]))
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
