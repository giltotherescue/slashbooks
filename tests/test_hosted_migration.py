from __future__ import annotations

from contextlib import closing, redirect_stdout, redirect_stderr
from datetime import date
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.bookkeeping import hosted_migration as migration
from src.bookkeeping.ledger.model import Entry, Open, Posting
from src.bookkeeping.ledger.store import LedgerStore


class MigrationPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="migration-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "private-company-name"
        self.root.mkdir()
        self.write("entity.json", {"name": "PRIVATE COMPANY", "bank_account_mappings": {}, "related_entities": []})
        self.write("trust-policy.json", {"auto_post_threshold": 3, "queue_all_until_confirmed": True})
        self.write("staging/pending.json", [])
        self.write("staging/seen-ids.json", [])
        self.write("learned-context/counterparties.json", {})
        (self.root / "review-queue").mkdir()
        self.store = LedgerStore(self.root / "ledger.sqlite")
        self.store.initialize()
        with self.store.transaction() as sql:
            self.store.set_meta("canonical", "true", sql)
            self.store.set_meta("account_catalog", "sqlite", sql)
            self.store.insert_opens([Open(date(2026, 1, 1), "Assets:Bank", ("USD",)),
                                     Open(date(2026, 1, 1), "Income:Services", ("USD",))], sql)
            self.add_entry(sql)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def add_entry(self, sql, source="one", metadata=()):
        self.store.insert_entries([Entry(date=date(2026, 1, 2), narration="PRIVATE NARRATION",
            payee="PRIVATE VENDOR", meta=(("source-id", source), *metadata),
            postings=(Posting("Assets:Bank", Decimal("9876.54")),
                      Posting("Income:Services", Decimal("-9876.54"))))], sql)
        self.store.append_audit_event("posted", {"source": source}, sql)
        self.store.append_audit_event("ledger-store-sealed", {"store_sha256": self.store.content_digest(sql)}, sql)

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ctime_ns)
                for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()}

    def assess(self):
        return migration.assess(self.root)

    def test_document_classification_is_not_security_validation(self):
        for root in ("documents", "evidence", "intake", "ingestion", "reconciliation",
                     "reports", "reconciliations", "outputs", "qb-exports"):
            self.assertTrue(migration.is_document_path(root + "/retained.json"))
            self.assertTrue(migration.is_document_path(root + "/custom.py"))
            self.assertTrue(migration.is_document_path(root + "/../outside.pdf"))
            self.assertFalse(migration.is_document_path(root))
        for name in ("audit-log.jsonl", "original.csv", "original.xlsx", "original.pdf",
                     "original.png", "original.jpg", "original.jpeg", "original.webp", "original.PDF"):
            self.assertTrue(migration.is_document_path(name), name)
        for name in ("entity.json", "ledger.sqlite", "notes.md", "notes.txt", "other.json",
                     "other.jsonl", "other.ndjson", "audit-log.ndjson", "script.py", "archive.zip",
                     ".DS_Store", "review-queue/item.json", "learned-context/counterparties.json",
                     "unknown/attachment.pdf", "audit/history.jsonl"):
            self.assertFalse(migration.is_document_path(name), name)

    def test_normal_data_assessment_keeps_unimplemented_gate(self):
        for name in ("reconciliations/retained.json", "outputs/inspect.ndjson",
                     "qb-exports/index.json", "audit-log.jsonl"):
            self.write(name, {"event": "retained"})
        (self.root / "original.csv").write_bytes(b"synthetic\n")
        for name in (".DS_Store", "outputs/.DS_Store"):
            (self.root / name).write_bytes(b"platform metadata")
        before = self.snapshot()
        result = self.assess()
        self.assertEqual(result["blockers"], ["migration_not_implemented"])
        self.assertTrue(result["local_checks_passed"])
        self.assertTrue(result["migration_not_implemented"])
        self.assertFalse(result["import_ready"])
        excluded = [item for item in result["inventory"] if item.get("reason") == "platform_metadata"]
        self.assertEqual(len(excluded), 2)
        self.assertTrue(all("sha256" not in item for item in excluded))
        self.assertEqual(self.snapshot(), before)

    def test_valid_company_still_blocks_migration_and_source_unchanged(self):
        before = self.snapshot()
        result = self.assess()
        self.assertEqual(result["blockers"], ["migration_not_implemented"])
        self.assertTrue(result["local_checks_passed"])
        self.assertFalse(result["import_ready"])
        self.assertTrue(result["checks"]["audit_chain"])
        self.assertEqual(result["ledger"]["chart"], "canonical_sqlite")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.assess(), result)
        rendered = json.dumps(result)
        for sensitive in (str(self.root), "PRIVATE", "9876.54", "Assets:Bank", "Income:Services"):
            self.assertNotIn(sensitive, rendered)

    def test_wal_committed_rows_are_captured_without_touching_source_or_shm(self):
        old = self.assess()["ledger"]["content_sha256"]
        sql = self.store.connect()
        self.addCleanup(sql.close)
        sql.execute("PRAGMA journal_mode=WAL")
        sql.execute("PRAGMA wal_autocheckpoint=0")
        self.add_entry(sql, "wal-only")
        sql.commit()
        expected = self.store.content_digest(sql)
        before = self.snapshot()
        self.assertGreater(len(before["ledger.sqlite-wal"][0]), 0)
        original_connect = sqlite3.connect

        def connect(path, *args, **kwargs):
            self.assertNotIn(str(self.root), str(path))
            return original_connect(path, *args, **kwargs)

        with patch.object(migration.sqlite3, "connect", side_effect=connect):
            result = self.assess()
        self.assertEqual(result["blockers"], ["migration_not_implemented"])
        self.assertTrue(result["ledger"]["wal_included"])
        self.assertEqual(result["ledger"]["content_sha256"], expected)
        self.assertNotEqual(expected, old)
        self.assertEqual(self.snapshot(), before)

    def test_tampered_audit_and_missing_audit_fail_without_details(self):
        for query, code in (("UPDATE audit_events SET record_hash='PRIVATE HASH'", "audit_chain_invalid"),
                            ("DELETE FROM audit_events", "audit_history_missing")):
            with self.subTest(code=code):
                with self.store.transaction() as sql:
                    sql.execute(query)
                result = self.assess()
                self.assertIn(code, result["blockers"])
                self.assertNotIn("PRIVATE HASH", json.dumps(result))

    def test_corrupt_database_and_future_schema(self):
        with self.store.transaction() as sql:
            self.store.set_meta("schema_version", "999", sql)
        self.assertIn("unsupported_schema", self.assess()["blockers"])
        (self.root / "ledger.sqlite").write_bytes(b"PRIVATE CORRUPTION")
        result = self.assess()
        self.assertIn("ledger_validation_failed", result["blockers"])
        self.assertNotIn("PRIVATE CORRUPTION", json.dumps(result))

    def test_foreign_keys_and_core_semantics(self):
        with closing(sqlite3.connect(self.root / "ledger.sqlite")) as sql, sql:
            sql.execute("INSERT INTO postings(entry_id,account,amount) VALUES (999,'Assets:Bank','1.00')")
        self.assertIn("sqlite_foreign_keys", self.assess()["blockers"])
        with closing(sqlite3.connect(self.root / "ledger.sqlite")) as sql, sql:
            sql.execute("DELETE FROM postings WHERE entry_id=999")
            sql.execute("UPDATE postings SET amount='1.00' WHERE amount='9876.54'")
            # Keep this fixture focused on semantic validation, not a stale seal.
            sql.row_factory = sqlite3.Row
            self.store.append_audit_event("ledger-store-sealed", {"store_sha256": self.store.content_digest(sql)}, sql)
        self.assertTrue({"core_ledger_invalid", "ledger_validation_failed"} & set(self.assess()["blockers"]))

    def test_custom_schema_is_rejected_without_executing_it(self):
        with self.store.transaction() as sql:
            sql.execute("CREATE VIEW private_view AS SELECT * FROM entries")
        self.assertIn("unsupported_schema", self.assess()["blockers"])

    def test_credentials_and_custom_code_never_opened_or_hashed(self):
        names = [".env", ".env.production", ".secrets/hidden.json", "credentials.json",
                 "ingestion/custom/private.py", "ingestion/private-credentials.json",
                 "intake/access-token.json", ".slashbooks-remote.json", "private.key"]
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PRIVATE SECRET MUST NOT BE READ")
        original = migration._read

        def read(fd, name, signature):
            self.assertNotIn(name, names)
            return original(fd, name, signature)

        with patch.object(migration, "_read", side_effect=read):
            result = self.assess()
        self.assertIn("credentials", result["blockers"])
        self.assertIn("custom_code", result["blockers"])
        for item in result["inventory"]:
            if item["disposition"] == "excluded":
                self.assertNotIn("sha256", item)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_embedded_credentials_and_custom_configuration_excluded(self):
        self.write("context/profile.json", {"api_key": "PRIVATE PROVIDER SECRET"})
        self.write("context/connector.json", {"command": "do not execute"})
        result = self.assess()
        self.assertIn("embedded_credentials", result["blockers"])
        self.assertIn("custom_configuration", result["blockers"])
        self.assertNotIn("PRIVATE PROVIDER SECRET", json.dumps(result))

    def test_ledger_with_embedded_secret_not_a_transfer_candidate(self):
        with self.store.transaction() as sql:
            self.add_entry(sql, "secret", (("api_key", "PRIVATE SECRET"),))
        result = self.assess()
        self.assertIn("embedded_credentials", result["blockers"])
        for item in result["inventory"]:
            if item["category"] == "ledger":
                self.assertEqual(item["disposition"], "excluded")
                self.assertNotIn("sha256", item)
        self.assertNotIn("PRIVATE SECRET", json.dumps(result))

    def test_reference_bytes_and_ledger_metadata(self):
        self.write("ingestion/source.json", [{"description": "PRIVATE TRANSACTION"}])
        self.write("review-queue/item.json", {"source_path": "ingestion/source.json"})
        with self.store.transaction() as sql:
            self.add_entry(sql, "reference", (("source_file", "ingestion/missing.pdf"),))
        result = self.assess()
        self.assertIn("referenced_bytes_missing", result["blockers"])
        self.assertEqual(result["references"]["resolved"], 1)
        self.assertEqual(result["references"]["recognized"], 2)
        self.assertFalse(result["references"]["exhaustive"])
        self.assertNotIn("missing.pdf", json.dumps(result))

    def test_absolute_external_traversal_and_excluded_references(self):
        self.write("context/links.json", {"file": "/outside/PRIVATE.json", "source_path": "../PRIVATE.json",
                                           "path": ".env", "folder": str(self.root / "staging")})
        (self.root / ".env").write_text("do not read")
        result = self.assess()
        for code in ("unsafe_or_external_reference", "referenced_bytes_excluded", "absolute_reference_requires_rewrite"):
            self.assertIn(code, result["blockers"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_symlink_ancestors_root_and_internal_links(self):
        alias = self.root.parent / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertIn("source_unavailable_or_unsafe", migration.assess(alias)["blockers"])
        self.assertIn("source_unavailable_or_unsafe", migration.assess(alias / "staging")["blockers"])
        (self.root / "context").symlink_to(self.root.parent, target_is_directory=True)
        (self.root / "ingestion").mkdir()
        (self.root / "ingestion/link.json").symlink_to(self.root / "entity.json")
        result = self.assess()
        self.assertIn("unsafe_file", result["blockers"])
        self.assertEqual(sum(x.get("reason") == "unsafe_file" for x in result["inventory"]), 2)

    def test_hardlinks_fifo_and_unsafe_filenames(self):
        (self.root / "intake").mkdir()
        os.link(self.root / "entity.json", self.root / "intake/hard.json")
        os.mkfifo(self.root / "intake/pipe.json")
        (self.root / "intake/unsafe%2fname.json").write_text("{}")
        result = self.assess()
        self.assertIn("unsafe_file", result["blockers"])
        self.assertFalse(result["local_checks_passed"])

    def test_source_change_blocks_snapshot(self):
        original = migration._read

        def read(fd, name, signature):
            data = original(fd, name, signature)
            if name == "ledger.sqlite":
                self.write("staging/seen-ids.json", ["changed"])
            return data

        with patch.object(migration, "_read", side_effect=read):
            result = self.assess()
        self.assertIn("source_changed", result["blockers"])
        self.assertNotIn("ledger", result)
        self.assertFalse(result["checks"].get("source_stable", False))

    def test_unfinished_journal_and_uncaptured_wal_never_validate_old_ledger(self):
        (self.root / "ledger.sqlite-journal").write_bytes(b"PRIVATE")
        self.assertNotIn("ledger", self.assess())
        (self.root / "ledger.sqlite-journal").unlink()
        (self.root / "ledger.sqlite-wal").symlink_to(self.root / "entity.json")
        result = self.assess()
        self.assertIn("wal_not_captured", result["blockers"])
        self.assertNotIn("ledger", result)

    def test_limits_missing_state_and_invalid_json(self):
        with patch.object(migration, "MAX_FILE", 10):
            self.assertIn("file_size_limit", self.assess()["blockers"])
        with patch.object(migration, "MAX_TOTAL", 10):
            self.assertIn("workspace_size_limit", self.assess()["blockers"])
        with patch.object(migration, "MAX_ITEMS", 1):
            self.assertIn("inventory_limit", self.assess()["blockers"])
        (self.root / "entity.json").write_text('{"name":"PRIVATE", "name":"duplicate"}')
        self.assertIn("invalid_json", self.assess()["blockers"])
        (self.root / "ledger.sqlite").unlink()
        self.assertIn("canonical_ledger_missing_or_excluded", self.assess()["blockers"])

    def test_unsupported_basis_currency_and_related_entities(self):
        for config, code in (({"basis": "accrual"}, "unsupported_basis"),
                             ({"currency": "EUR"}, "unsupported_currency"),
                             ({"related_entities": [{"name": "PRIVATE"}]}, "invalid_related_entity_policy")):
            with self.subTest(code=code):
                self.write("entity.json", config)
                self.assertIn(code, self.assess()["blockers"])

    def test_exact_runtime_and_vcs_exclusions_never_read_or_traverse(self):
        for name in (".books.lock", ".gitignore", ".git/hooks/pre-commit"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PRIVATE local state")
        before = self.snapshot()
        original = migration._read

        def read(fd, name, signature):
            self.assertFalse(name.startswith(".git") or name == ".books.lock")
            return original(fd, name, signature)

        with patch.object(migration, "_read", side_effect=read):
            result = self.assess()
        self.assertTrue(result["local_checks_passed"], result)
        self.assertEqual(self.snapshot(), before)
        for name in (".books.other", ".gitignore.backup", "context/.gitignore"):
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("not an allowed exclusion")
                self.assertFalse(self.assess()["local_checks_passed"])
                path.unlink()

    def test_schema_labels_do_not_disable_nested_credential_checks(self):
        self.write("staging/split-templates.json", {"password-manager": [{"account": "Income:Services", "amount": "1.00"}]})
        self.write("learned-context/counterparties.json", {"TOKEN": {"canonical_category": "Income:Services"}})
        self.assertTrue(self.assess()["local_checks_passed"])
        self.write("learned-context/counterparties.json", {"TOKEN": {"nested": {"apiKey": "PRIVATE"}}})
        self.assertIn("embedded_credentials", self.assess()["blockers"])

    def test_vcs_inventory_is_one_root_and_git_changes_do_not_change_books(self):
        git = self.root / ".git"
        (git / "objects").mkdir(parents=True)
        # More objects than the entire source budget, including hardlinks/hooks,
        # must not be inventoried. Only the declared .git root is relevant.
        object_path = git / "objects/original"
        object_path.write_text("PRIVATE history")
        for index in range(migration.MAX_ITEMS):
            os.link(object_path, git / "objects" / str(index))
        original_read, original_list = migration._read, os.listdir

        def listdir(fd):
            self.assertNotEqual(os.fstat(fd).st_ino, git.stat().st_ino)
            self.assertNotEqual(os.fstat(fd).st_ino, (git / "objects").stat().st_ino)
            return original_list(fd)

        def read(fd, name, signature):
            (git / "HEAD").write_text("changed independently")
            os.utime(git, ns=(1, 1))
            return original_read(fd, name, signature)

        with patch.object(migration.os, "listdir", side_effect=listdir), patch.object(migration, "_read", side_effect=read):
            result = self.assess()
        self.assertTrue(result["local_checks_passed"], result)
        self.assertEqual(sum(item.get("reason") == "local_vcs" for item in result["inventory"]), 1)

    def test_worktree_git_pointer_is_excluded_without_following_gitdir(self):
        pointer = self.root / ".git"
        pointer.write_text("gitdir: /nonexistent/external/gitdir\n")
        original = migration._read

        def read(fd, name, signature):
            self.assertNotEqual(name, ".git")
            return original(fd, name, signature)

        with patch.object(migration, "_read", side_effect=read):
            self.assertTrue(self.assess()["local_checks_passed"])

    def test_module_invocation_and_sanitized_argument_error(self):
        source = Path(__file__).resolve().parents[1] / "src"
        result = subprocess.run([sys.executable, "-B", "-m", "bookkeeping.hosted_migration", "--entity-dir", str(self.root)],
                                cwd=source, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(json.loads(result.stdout)["local_checks_passed"])
        self.assertEqual(result.stderr, "")
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as stopped:
            migration.main(["--PRIVATE-SECRET"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(output.getvalue(), '{"error":"invalid_arguments"}\n')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(migration.main(["--entity-dir", str(self.root / "missing")]), 1)
        self.assertNotIn(str(self.root), output.getvalue())


if __name__ == "__main__":
    unittest.main()
