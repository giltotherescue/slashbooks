from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import unittest
from unittest.mock import patch
import warnings
import zipfile

from src.bookkeeping import hosted_migration as capture
from src.bookkeeping import migration_bundle as bundle
from src.bookkeeping.entity import (init_entity, load_entity, add_account,
                                    record_related_entity, approve_related_entity, get_related_entity)
from src.bookkeeping import queue
from src.bookkeeping.ledger.importer import import_transactions
from src.bookkeeping.ledger.auditlog import AuditLog, verify_chain
from src.bookkeeping.ledger.projections import render_store_ledger
from src.bookkeeping.ledger.store import LedgerStore
from src.bookkeeping.ledger.validator import validate
from src.bookkeeping.reports import statements
from tests import test_hosted_migration as fixtures


class MigrationBundleTests(unittest.TestCase):
    # Share synthetic setup, without re-running the preflight TestCase's tests.
    setUp = fixtures.MigrationPreflightTests.setUp
    write = fixtures.MigrationPreflightTests.write
    add_entry = fixtures.MigrationPreflightTests.add_entry
    snapshot = fixtures.MigrationPreflightTests.snapshot

    @property
    def destination(self):
        return self.root.parent / "company.zip"

    def export(self):
        result = capture.build_bundle(self.root, self.destination)
        self.assertEqual(result["status"], "exported", result)
        self.assertTrue(result["bundle_created"])
        self.assertFalse(result["import_ready"])
        for private in (str(self.root), "PRIVATE", "9876.54", "Assets:Bank"):
            self.assertNotIn(private, json.dumps(result))
        return result, bundle.load_bundle(self.destination)

    def blocked(self, code):
        result = bundle.build_bundle(self.root, self.destination)
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn(code, result["blockers"])
        self.assertFalse(result["bundle_created"])
        self.assertFalse(self.destination.exists())
        self.assertNotIn("PRIVATE", json.dumps(result))
        return result

    def archive(self, manifest, files, extras=(), compression=zipfile.ZIP_STORED):
        output = io.BytesIO()
        with warnings.catch_warnings(), zipfile.ZipFile(output, "w", compression=compression) as archive:
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("manifest.json", bundle._json_bytes(manifest))
            for name, data in files.items():
                archive.writestr("entity/" + name, data)
            for name, data in extras:
                archive.writestr(name, data)
        path = self.root.parent / "modified.zip"
        path.write_bytes(output.getvalue())
        return path

    def test_full_state_reopens_preserving_ids_history_and_financial_anchors(self):
        self.write("entity.json", {"id": "original-entity-id", "name": "PRIVATE COMPANY", "basis": "cash", "currency": "USD"})
        self.write("staging/pending.json", [{"id": "pending-id", "amount": "12.34"}])
        self.write("staging/seen-ids.json", ["one", "historical-id"])
        self.write("staging/pending-categorization.json", [{"source_id": "uncategorized-id"}])
        self.write("review-queue/retained-id.json", {"id": "retained-id", "source_path": "evidence/invoice.pdf"})
        self.write("learned-context/counterparties.json", {"PRIVATE VENDOR": {"account": "Income:Services"}})
        self.write("context/report.json", {"report_path": "reports/original.csv"})
        self.write("memory/history.json", {"source_id": "historical-id"})
        self.write("audit/history.jsonl", {"event": "prior-review", "file": "evidence/invoice.pdf"})
        (self.root / "evidence").mkdir()
        (self.root / "evidence/invoice.pdf").write_bytes(b"%PDF-synthetic-evidence")
        (self.root / "reports").mkdir()
        (self.root / "reports/original.csv").write_bytes(b"account,amount\nAssets:Bank,9876.54\n")
        (self.root / "business-profile.md").write_text("Synthetic company [report](reports/original.csv).")
        with self.store.transaction() as sql:
            sql.execute("INSERT INTO import_sessions VALUES (?,?,?)", ("original-session", "2026-01-02T00:00:00Z", '{"source_file":"reports/original.csv"}'))
            sql.execute("INSERT INTO source_transactions VALUES (?,?,?,?)", ("original-source-id", '{"id":"original-source-id"}', "imported", "2026-01-02T00:00:00Z"))
            self.add_entry(sql, "another", (("evidence_path", "evidence/invoice.pdf"),))
            before_tables = {table: [tuple(row) for row in sql.execute(f'SELECT * FROM "{table}"')]
                             for table in bundle.TABLES}
        before = self.snapshot()
        summary, loaded = self.export()
        manifest, files = loaded["manifest"], loaded["files"]
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(set(files), set(before))
        for name, data in files.items():
            if name != "ledger.sqlite":
                self.assertEqual(data, before[name][0])
        self.assertEqual(manifest["source_entity"]["id"], "original-entity-id")
        self.assertEqual(manifest["source_entity"]["name"], "PRIVATE COMPANY")
        self.assertNotIn("import_ready", manifest)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(set(manifest), {"format", "version", "source_entity", "ledger", "reports", "excluded", "files"})
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o600)
        for item in manifest["files"]:
            self.assertEqual(item["sha256"], hashlib.sha256(files[item["path"]]).hexdigest())
            self.assertEqual(item["size"], len(files[item["path"]]))
        reopened = self.root.parent / "reopened.sqlite"
        reopened.write_bytes(files["ledger.sqlite"])
        with closing(sqlite3.connect(reopened)) as sql:
            for table in bundle.TABLES:
                self.assertEqual([tuple(row) for row in sql.execute(f'SELECT * FROM "{table}"')], before_tables[table])
                self.assertEqual(manifest["ledger"]["tables"][table]["rows"], len(before_tables[table]))
            sql.row_factory = sqlite3.Row
            reports = [statements._compute_pnl(sql, date(2026, 1, 1), date(2026, 1, 2)),
                       statements._compute_balance_sheet(sql, date(2026, 1, 2)),
                       statements._compute_trial_balance(sql, date(2026, 1, 2)),
                       statements._compute_general_ledger(sql, date(2026, 1, 1), date(2026, 1, 2))]
            expected = {report.kind: hashlib.sha256(bundle._json_bytes(json.loads(report.to_json()))).hexdigest() for report in reports}
        self.assertEqual(manifest["reports"], {"from": "2026-01-01", "to": "2026-01-02", "fingerprints": expected})
        self.assertEqual(bundle.validate_files(manifest, files)["status"], "validated")
        validated = bundle.validate_bundle(self.destination)
        self.assertEqual(validated["manifest_sha256"], summary["manifest_sha256"])
        self.assertFalse(validated["import_ready"])

    def test_export_is_deterministic_and_destination_is_exclusive(self):
        first, loaded = self.export()
        original = self.destination.read_bytes()
        self.assertEqual(bundle.build_bundle(self.root, self.destination)["blockers"], ["destination_exists"])
        self.assertEqual(self.destination.read_bytes(), original)
        second = bundle.build_bundle(self.root, self.root.parent / "second.zip")
        self.assertEqual(second, first)
        self.assertEqual((self.root.parent / "second.zip").read_bytes(), original)

    def test_latest_seal_required_at_capture_and_receiver_even_with_matching_hashes(self):
        _, baseline = self.export()
        self.destination.unlink()
        original = (self.root / "ledger.sqlite").read_bytes()
        for change, code in (("stale-content", "ledger_seal_invalid"),
                             ("missing-seal", "ledger_write_incomplete"),
                             ("nonfinal-intent", "ledger_write_incomplete"),
                             ("nonfinal-entry-written", "ledger_write_incomplete"),
                             ("missing-digest", "ledger_seal_invalid")):
            with self.subTest(change=change):
                (self.root / "ledger.sqlite").write_bytes(original)
                with self.store.transaction() as sql:
                    if change == "stale-content":
                        sql.execute("UPDATE entries SET narration='Changed synthetic narration'")
                    elif change == "missing-seal":
                        sql.execute("DELETE FROM audit_events WHERE type='ledger-store-sealed'")
                    elif change == "missing-digest":
                        self.store.append_audit_event("ledger-store-sealed", {}, sql)
                    else:
                        self.store.append_audit_event(change.removeprefix("nonfinal-"), {}, sql)
                        self.store.append_audit_event("reviewed", {"note": "synthetic trailing event"}, sql)
                self.assertEqual(self.store.verify_audit_chain(), [])
                self.assertEqual(validate(render_store_ledger(self.root / "ledger.sqlite")), [])
                before = self.snapshot()
                self.blocked(code)
                files = {**baseline["files"], "ledger.sqlite": before["ledger.sqlite"][0]}
                manifest = deepcopy(baseline["manifest"])
                manifest["files"] = [{"path": name, "size": len(data), "sha256": bundle._sha(data)}
                                     for name, data in sorted(files.items())]
                with self.assertRaisesRegex(bundle.BundleError, code):
                    bundle.validate_files(manifest, files)
                self.assertEqual(self.snapshot(), before)

    def test_truly_empty_canonical_store_needs_no_invented_seal(self):
        self.root = self.root.parent / "empty-company"
        self.root.mkdir()
        self.write("entity.json", {"name": "PRIVATE COMPANY", "basis": "cash", "currency": "USD"})
        self.store = LedgerStore(self.root / "ledger.sqlite")
        self.store.initialize()
        with self.store.transaction() as sql:
            self.store.set_meta("canonical", "true", sql)
            self.store.set_meta("account_catalog", "sqlite", sql)
        before = self.snapshot()
        _, loaded = self.export()
        for table in ("accounts", "entries", "postings", "audit_events"):
            self.assertEqual(loaded["manifest"]["ledger"]["tables"][table]["rows"], 0)
        self.assertEqual(bundle.validate_files(loaded["manifest"], loaded["files"])["status"], "validated")
        self.assertEqual(self.snapshot(), before)

    def test_normal_data_snapshot_roundtrip_preserves_bytes_and_anchors(self):
        _, baseline = self.export()
        self.destination.unlink()
        attachments = {"original.csv": b"column\r\nsynthetic\r\n", "original.xlsx": b"synthetic workbook",
                       "original.pdf": b"%PDF-synthetic", "original.png": b"\x89PNG\r\nsynthetic",
                       "original.jpg": b"\xff\xd8synthetic", "original.jpeg": b"\xff\xd8synthetic",
                       "original.webp": b"RIFFsyntheticWEBP", "uppercase.PDF": b"%PDF-synthetic"}
        records = {"reconciliation/old.json": b'{"status":"retained"}\n',
                   "reconciliations/new.json": b'{ "file": "original.pdf" }\n',
                   "reconciliations/summary.md": b"[Evidence](original.csv)\r\n",
                   "outputs/package/report.xlsx": b"synthetic retained workbook",
                   "qb-exports/report.csv": b"synthetic\r\n",
                   "outputs/inspect.ndjson": b'{"file":"outputs/package/report.xlsx"}\r\n\n'
                                              b'{"nested":{"path":"qb-exports/report.csv"}}\n'}
        for name, data in {**attachments, **records}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        audit = AuditLog(self.root / "audit-log.jsonl")
        audit.append("pending-staged", ts="2026-01-02T00:00:00Z", source_id="synthetic-source")
        audit.append("entry-written", ts="2026-01-02T00:00:01Z", file="original.csv")
        self.assertEqual(verify_chain(self.root / "audit-log.jsonl"), [])
        excluded = {".DS_Store", "outputs/.DS_Store", "qb-exports/.DS_Store"}
        for name in excluded:
            (self.root / name).write_bytes(b"platform metadata not imported")
        before = self.snapshot()
        original_read = capture._read

        def read(fd, name, signature):
            self.assertNotIn(name, excluded)
            return original_read(fd, name, signature)

        with patch.object(capture, "_read", side_effect=read):
            _, loaded = self.export()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(set(loaded["files"]), set(before) - excluded)
        for name, data in loaded["files"].items():
            if name != "ledger.sqlite":
                self.assertEqual(data, before[name][0], name)
        self.assertEqual(loaded["manifest"]["excluded"], {"platform_metadata": 3})
        self.assertEqual(loaded["manifest"]["version"], 1)
        self.assertEqual(set(loaded["manifest"]), set(baseline["manifest"]))
        self.assertEqual(loaded["manifest"]["ledger"], baseline["manifest"]["ledger"])
        self.assertEqual(loaded["manifest"]["reports"], baseline["manifest"]["reports"])
        self.assertEqual(bundle.validate_files(loaded["manifest"], loaded["files"])["status"], "validated")

    def test_new_structured_paths_still_reject_secrets_configuration_and_bad_records(self):
        _, baseline = self.export()
        self.destination.unlink()
        cases = [(name, b'{"nested":{"apiKey":"PRIVATE"}}', "embedded_credentials")
                 for name in ("reconciliations/data.json", "qb-exports/data.json", "outputs/data.json",
                              "outputs/inspect.ndjson", "outputs/upper.NDJSON", "audit-log.jsonl")]
        cases += [("outputs/inspect.ndjson", raw, code) for raw, code in (
            (b'{"ok":true}\n{"nested":{"password":"PRIVATE"}}\n', "embedded_credentials"),
            (b'{"ok":true}\n{', "invalid_structured_data"),
            (b'{"duplicate":1,"duplicate":2}\n', "invalid_json"),
            (b'{"value":NaN}\n', "invalid_json"),
            (b'{"nested":{"command":"do not execute"}}\n', "custom_configuration"),
        )]
        cases.append(("audit-log.jsonl", b'{"command":"historical command"}\n', "custom_configuration"))
        for name, raw, code in cases:
            with self.subTest(name=name, code=code, raw=raw):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                before = self.snapshot()
                self.blocked(code)
                files = {**baseline["files"], name: raw}
                manifest = deepcopy(baseline["manifest"])
                manifest["files"] = [{"path": key, "size": len(data), "sha256": bundle._sha(data)}
                                     for key, data in sorted(files.items())]
                receiver_code = "bundle_validation_failed" if code == "invalid_structured_data" else code
                with self.assertRaisesRegex(bundle.BundleError, receiver_code):
                    bundle.validate_files(manifest, files)
                self.assertEqual(self.snapshot(), before)
                path.unlink()

    def test_new_documents_references_remain_strict(self):
        (self.root / "outputs").mkdir()
        (self.root / "outputs/retained.pdf").write_bytes(b"%PDF-synthetic")
        (self.root / ".DS_Store").write_bytes(b"platform metadata")
        (self.root / "outputs/.DS_Store").write_bytes(b"platform metadata")
        _, baseline = self.export()
        self.destination.unlink()
        for target, export_code, receiver_code in (
            ("missing.pdf", "referenced_bytes_missing", "referenced_bytes_missing"),
            ("../outside.pdf", "unsafe_or_external_reference", "unsafe_bundle_path"),
            ("/outside.pdf", "unsafe_or_external_reference", "absolute_reference_requires_rewrite"),
            (".DS_Store", "referenced_bytes_excluded", "referenced_bytes_excluded"),
            ("outputs/.DS_Store", "referenced_bytes_excluded", "referenced_bytes_excluded"),
        ):
            with self.subTest(target=target):
                self.write("outputs/inspect.ndjson", {"nested": {"path": target}})
                before = self.snapshot()
                self.blocked(export_code)
                files = {**baseline["files"], "outputs/inspect.ndjson": (self.root / "outputs/inspect.ndjson").read_bytes()}
                manifest = deepcopy(baseline["manifest"])
                manifest["files"] = [{"path": key, "size": len(data), "sha256": bundle._sha(data)}
                                     for key, data in sorted(files.items())]
                with self.assertRaisesRegex(bundle.BundleError, receiver_code):
                    bundle.validate_files(manifest, files)
                self.assertEqual(self.snapshot(), before)
        self.write("outputs/inspect.ndjson", {"path": "outputs"})
        self.blocked("referenced_bytes_excluded")

    def test_normal_data_does_not_admit_root_config_scripts_archives_or_hidden_files(self):
        for name, code in (("ordinary.json", "unsupported_file"), ("ordinary.ndjson", "unsupported_file"),
                           ("ordinary.md", "unsupported_file"), ("ordinary.txt", "unsupported_file"),
                           ("audit-log.ndjson", "unsupported_file"), (".DS_Store.json", "unsupported_file"),
                           (".ds_store", "unsupported_file"), ("outputs/archive.zip", "unsupported_file"),
                           ("outputs/custom.py", "custom_code"), ("outputs/.hidden.pdf", "unsupported_file"),
                           ("password-manager.pdf", "credentials")):
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"{}")
                self.blocked(code)
                path.unlink()
        path = self.root / "executable.pdf"
        path.write_bytes(b"not executable evidence")
        path.chmod(0o700)
        self.blocked("custom_code")

    def test_platform_metadata_exclusion_requires_regular_unlinked_file(self):
        path = self.root / ".DS_Store"
        path.mkdir()
        self.blocked("unsafe_file")
        path.rmdir()
        path.symlink_to(self.root / "entity.json")
        self.blocked("unsafe_file")
        path.unlink()
        os.link(self.root / "entity.json", path)
        self.blocked("unsafe_file")

    def test_new_document_paths_keep_existing_size_and_inventory_limits(self):
        path = self.root / "outputs/retained.pdf"
        path.parent.mkdir()
        path.write_bytes(b"x" * (32 * 1024))
        with patch.object(capture, "MAX_FILE", 16 * 1024):
            self.blocked("file_size_limit")
        with patch.object(capture, "MAX_TOTAL", 16 * 1024):
            self.blocked("workspace_size_limit")
        with patch.object(capture, "MAX_ITEMS", 4):
            self.blocked("inventory_limit")

    def test_native_new_company_template_exports_including_unset_cutover(self):
        root = self.root.parent / "fresh-company"
        init_entity(root, name="Synthetic Company", business_type="consulting")
        (root / ".env").write_text("PRIVATE SECRET NOT READ")
        result = bundle.build_bundle(root, self.destination)
        self.assertEqual(result["status"], "exported", result)
        loaded = bundle.load_bundle(self.destination)
        self.assertIsNone(loaded["manifest"]["source_entity"]["cutover_date"])
        self.assertNotIn(".env", loaded["files"])
        self.assertEqual(loaded["manifest"]["ledger"]["tables"]["entries"]["rows"], 0)

    def test_real_core_workflows_roundtrip_without_rewriting_history(self):
        self.root = self.root.parent / "core-company"
        init_entity(self.root, name="Synthetic", business_type="consulting")
        entity = load_entity(self.root)
        for account in ("Assets:DueFrom:Related", "Liabilities:DueTo:Related"):
            add_account(self.root, account)
        result = import_transactions(entity, [{"id": "core-source", "date": "2026-01-15",
            "description": "Synthetic income", "amount": "100.00", "accountName": "Checking", "pending": False}],
            "original-session", categorizer=lambda _: ("Income:Consulting", "high"),
            session_date=date(2026, 1, 15), ts="2026-01-15T00:00:00Z")
        self.assertFalse(result.errors)
        self.assertEqual(result.new_entries, 1)
        self.assertTrue((self.root / ".books.lock").is_file())
        queue.save_split_template(entity, "password-manager", ["Expenses:Software=10.00"])
        queue._update_learned_context(entity, "TOKEN", "Expenses:Software", False)
        record_related_entity(self.root, "Synthetic Related", "Assets:DueFrom:Related",
                              "Liabilities:DueTo:Related", "settle-receivable", "create-receivable")
        approve_related_entity(self.root, "Synthetic Related", "Original owner approval")
        for name, raw in (("broken", b"{not valid json[[["), ("incomplete", b'{"source_id":"incomplete"}'),
                          ("array", b'[1,2]')):
            (self.root / "review-queue" / (name + ".json")).write_bytes(raw)
        queue.list_queue_items(entity)
        before = self.snapshot()
        self.store = LedgerStore(self.root / "ledger.sqlite")
        with self.store.connection() as sql:
            tables = {table: [tuple(row) for row in sql.execute(f'SELECT * FROM "{table}"')]
                      for table in bundle.TABLES}
        _, loaded = self.export()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(loaded["manifest"]["excluded"], {"runtime_state": 1})
        self.assertEqual(set(loaded["files"]), set(before) - {".books.lock"})
        restored = self.root.parent / "restored"
        for name, data in loaded["files"].items():
            if name != "ledger.sqlite":
                self.assertEqual(data, before[name][0])
            path = restored / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        with LedgerStore(restored / "ledger.sqlite").connection() as sql:
            for table in bundle.TABLES:
                self.assertEqual([tuple(row) for row in sql.execute(f'SELECT * FROM "{table}"')], tables[table])
        recovered = load_entity(restored)
        self.assertEqual(queue.list_split_templates(recovered), queue.list_split_templates(entity))
        self.assertEqual(queue.load_learned_context(recovered), queue.load_learned_context(entity))
        self.assertEqual(get_related_entity(recovered, "Synthetic Related")["approval_note"], "Original owner approval")
        self.assertEqual(queue.list_queue_items(recovered), [])
        self.assertEqual(bundle.validate_files(loaded["manifest"], loaded["files"])["report_fingerprints"],
                         loaded["manifest"]["reports"]["fingerprints"])

    def test_core_git_init_exports_without_vcs_or_credentials(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                parent = self.root.parent / ("nested-repo" if nested else "company-repo")
                parent.mkdir()
                subprocess.run(["git", "init", "--quiet", str(parent)], check=True, capture_output=True)
                root = parent / "company" if nested else parent
                init_entity(root, name="Synthetic")
                self.assertTrue((root / ".gitignore").is_file())
                (root / ".env").write_text("PRIVATE NEVER UPLOADED")
                before = (root / ".gitignore").read_bytes()
                result = bundle.build_bundle(root, self.destination)
                self.assertEqual(result["status"], "exported", result)
                loaded = bundle.load_bundle(self.destination)
                self.assertFalse(any(name.startswith(".git") or name == ".env" for name in loaded["files"]))
                self.assertEqual((root / ".gitignore").read_bytes(), before)
                self.assertEqual(loaded["manifest"]["excluded"], {"credentials": 1, "local_vcs": 1 if nested else 2})
                self.destination.unlink()

    def test_map_label_exceptions_are_exact_and_nested_secrets_still_block(self):
        entity = load_entity(self.root)
        queue.save_split_template(entity, "password-manager", ["Income:Services=10.00"])
        queue._update_learned_context(entity, "TOKEN", "Income:Services", False)
        _, loaded = self.export()
        self.destination.unlink()
        for name, value in (
            ("staging/split-templates.json", {"password-manager": [{"account": "Income:Services", "amount": "1.00", "nested": {"apiKey": "PRIVATE"}}]}),
            ("learned-context/counterparties.json", {"TOKEN": {"credentials": {"anything": "PRIVATE"}}}),
            ("context/other.json", {"password-manager": {"account": "Income:Services"}}),
        ):
            with self.subTest(name=name):
                self.write(name, value)
                self.blocked("embedded_credentials")
                # Even with recomputed hashes, receiver inspection must reject.
                files = {**loaded["files"], name: (self.root / name).read_bytes()}
                manifest = deepcopy(loaded["manifest"])
                manifest["files"] = [{"path": key, "size": len(data), "sha256": bundle._sha(data)}
                                     for key, data in sorted(files.items())]
                with self.assertRaisesRegex(bundle.BundleError, "embedded_credentials"):
                    bundle.validate_files(manifest, files)
                if name in loaded["files"]:
                    (self.root / name).write_bytes(loaded["files"][name])
                else:
                    (self.root / name).unlink()

    def test_quarantine_scans_valid_and_truncated_nested_credentials(self):
        entity = load_entity(self.root)
        path = self.root / "review-queue/broken.json"
        path.write_bytes(b'{"source_id":"missing-status"}')
        queue.list_queue_items(entity)
        _, baseline = self.export()
        self.destination.unlink()
        for raw in (b'{"nested":{"apiKey":"PRIVATE"}}', b'{"nested":{"api\\u004bey":"PRIVATE"',
                    b'{"nested":{"authorization":"PRIVATE"},', b'{"nested":{"base_url":"https://invalid"}',
                    b'{"nested":{api_key:"PRIVATE"}}',
                    # Built at runtime so the source never contains a key-shaped literal.
                    b'{"note":"sk_' + b'live_0123456789abcdef"'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                queue.list_queue_items(entity)
                result = bundle.build_bundle(self.root, self.destination)
                self.assertEqual(result["status"], "blocked", result)
                self.assertTrue(set(result["blockers"]) & {"embedded_credentials", "custom_configuration"}, result)
                self.assertFalse(self.destination.exists())
                files = {**baseline["files"], "review-queue/quarantine/broken.json": raw}
                manifest = deepcopy(baseline["manifest"])
                manifest["files"] = [{"path": name, "size": len(data), "sha256": bundle._sha(data)}
                                     for name, data in sorted(files.items())]
                with self.assertRaisesRegex(bundle.BundleError, "embedded_credentials|custom_configuration"):
                    bundle.validate_files(manifest, files)

    def test_quarantine_requires_core_sidecar_and_only_provenance_is_inert(self):
        self.write("review-queue/quarantine/item.json", {"source_id": "incomplete"})
        self.blocked("invalid_quarantine")
        sidecar = {"original_path": "/old-machine/review-queue/item.json", "error": "Missing status",
                   "quarantined_at": "2026-01-01T00:00:00Z"}
        self.write("review-queue/quarantine/item.error.json", sidecar)
        _, loaded = self.export()
        self.destination.unlink()
        self.write("review-queue/quarantine/item.error.json", {**sidecar, "extra": {"api_key": "PRIVATE"}})
        self.blocked("invalid_quarantine")
        (self.root / "review-queue/quarantine/item.error.json").write_bytes(loaded["files"]["review-queue/quarantine/item.error.json"])
        self.write("context/live.json", {"original_path": sidecar["original_path"]})
        self.blocked("unsafe_or_external_reference")

    def test_related_entity_policy_invariants_and_pending_authorization(self):
        record_related_entity(self.root, "Synthetic Related", "Assets:Bank", "Income:Services", "income", "settle-payable", "Income:Services")
        _, loaded = self.export()
        self.destination.unlink()
        config = json.loads(loaded["files"]["entity.json"])
        self.assertFalse(config["related_entities"][0]["owner_authorized"])
        for changes in ({"receivable_account": "Assets:Missing"}, {"inbound_policy": "automatic"},
                        {"outbound_policy": "automatic"}, {"inbound_income_account": ""},
                        {"owner_authorized": "true"}, {"owner_authorized": True},
                        {"owner_authorized": True, "approval_note": "Original", "approved_at": "invalid"},
                        {"company_id": "invented-hosted-binding"}):
            with self.subTest(changes=changes):
                bad = deepcopy(config)
                bad["related_entities"][0].update(changes)
                self.write("entity.json", bad)
                self.blocked("invalid_related_entity_policy")
                files = {**loaded["files"], "entity.json": (self.root / "entity.json").read_bytes()}
                with self.assertRaisesRegex(bundle.BundleError, "invalid_related_entity_policy"):
                    bundle._manifest(files, {})

    def test_export_reference_roots_do_not_widen_conservative_assessment(self):
        self.write("evidence/source.json", {})
        self.assertIn("unsupported_file", capture.assess(self.root)["blockers"])
        _, loaded = self.export()
        self.assertEqual(loaded["files"]["evidence/source.json"], b"{}")

    def test_committed_wal_becomes_canonical_without_opening_original_sqlite(self):
        sql = self.store.connect()
        self.addCleanup(sql.close)
        sql.execute("PRAGMA journal_mode=WAL")
        sql.execute("PRAGMA wal_autocheckpoint=0")
        self.add_entry(sql, "wal-only")
        sql.commit()
        expected = self.store.content_digest(sql)
        before = self.snapshot()
        original_connect = sqlite3.connect

        def connect(path, *args, **kwargs):
            self.assertNotIn(str(self.root), str(path))
            return original_connect(path, *args, **kwargs)

        with patch.object(sqlite3, "connect", side_effect=connect):
            _, loaded = self.export()
        self.assertEqual(self.snapshot(), before)
        self.assertGreater(len(before["ledger.sqlite-wal"][0]), 0)
        self.assertNotIn("ledger.sqlite-wal", loaded["files"])
        self.assertNotIn("ledger.sqlite-shm", loaded["files"])
        self.assertEqual(loaded["manifest"]["ledger"]["content_sha256"], expected)
        self.assertEqual(loaded["manifest"]["ledger"]["tables"]["entries"]["rows"], 2)

    def test_known_configuration_and_cache_excluded_without_reading_assess_unchanged(self):
        names = [".env", ".env.production", ".slashbooks-remote.json", ".secrets/hidden.json", "reports/cache.sqlite"]
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"PRIVATE SECRET MUST NEVER BE READ")
        original = capture._read

        def read(fd, name, signature):
            self.assertNotIn(name, names)
            return original(fd, name, signature)

        with patch.object(capture, "_read", side_effect=read):
            assessment = capture.assess(self.root)
            _, loaded = self.export()
        self.assertIn("credentials", assessment["blockers"])
        self.assertFalse(assessment["local_checks_passed"])
        self.assertEqual(loaded["manifest"]["excluded"], {"credentials": 4, "derived_state": 1})
        self.assertFalse(set(names) & set(loaded["files"]))
        self.assertNotIn(b"PRIVATE SECRET MUST NEVER BE READ", self.destination.read_bytes())

    def test_secret_looking_evidence_is_a_review_blocker_never_silently_dropped(self):
        names = ["evidence/password-manager-invoice.pdf", "intake/token-receipt.csv", "credentials.json", "private.key", ".environment-invoice.pdf", ".env.invoice.pdf"]
        original = capture._read
        for name in names:
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"PRIVATE financial evidence")
                def read(fd, candidate, signature):
                    self.assertNotEqual(candidate, name)
                    return original(fd, candidate, signature)
                with patch.object(capture, "_read", side_effect=read):
                    self.blocked("credentials")
                path.unlink()

    def test_references_to_excluded_configuration_block_even_in_extended_fields(self):
        (self.root / ".env").write_text("PRIVATE SECRET")
        for field in ("path", "attachmentPath", "files"):
            with self.subTest(field=field):
                self.write("context/reference.json", {field: [".env"] if field == "files" else ".env"})
                self.blocked("referenced_bytes_excluded")

    def test_extended_directory_reference_cannot_hide_excluded_cache(self):
        self.write("reports/included.json", {})
        (self.root / "reports/cache.sqlite").write_bytes(b"excluded")
        self.write("context/reference.json", {"evidence": "reports"})
        self.blocked("referenced_bytes_excluded")

    def test_missing_absolute_and_traversal_references_block_without_artifact(self):
        for value in ("reports/missing.pdf", "/PRIVATE/outside.pdf", str(self.root / "entity.json"), "../PRIVATE.pdf", "C:\\PRIVATE\\outside.pdf"):
            with self.subTest(value=value):
                self.write("context/reference.json", {"attachmentPath": value})
                result = bundle.build_bundle(self.root, self.destination)
                self.assertEqual(result["status"], "blocked", result)
                self.assertFalse(self.destination.exists())
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_custom_code_embedded_secrets_unknown_files_and_invalid_state_block(self):
        for name, data, code in (
            ("ingestion/custom/tool.py", b"do not execute", "custom_code"),
            ("context/profile.json", b'{"apiKey":"PRIVATE secret"}', "embedded_credentials"),
            ("business-profile.md", b"Authorization: Bearer PRIVATE", "embedded_credentials"),
            ("unknown.json", b"{}", "unsupported_file"),
            ("staging/pending.json", b"{}", "unsupported_state_shape"),
        ):
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                previous = path.read_bytes() if path.exists() else None
                path.write_bytes(data)
                self.blocked(code)
                path.unlink() if previous is None else path.write_bytes(previous)

    def test_source_change_and_unsafe_files_never_publish(self):
        original = capture._read
        def read(fd, name, signature):
            data = original(fd, name, signature)
            if name == "ledger.sqlite":
                self.write("staging/seen-ids.json", ["changed"])
            return data
        with patch.object(capture, "_read", side_effect=read):
            self.blocked("source_changed")
        (self.root / "context").symlink_to(self.root.parent, target_is_directory=True)
        self.blocked("unsafe_file")

    def test_output_inside_source_symlinks_and_partial_write_cleanup(self):
        result = bundle.build_bundle(self.root, self.root / "snapshot.zip")
        self.assertEqual(result["blockers"], ["destination_inside_source"])
        self.assertFalse((self.root / "snapshot.zip").exists())
        alias = self.root.parent / "alias"
        alias.symlink_to(self.root.parent, target_is_directory=True)
        self.assertEqual(bundle.build_bundle(self.root, alias / "snapshot.zip")["status"], "blocked")
        self.destination.symlink_to(self.root / "entity.json")
        before = (self.root / "entity.json").read_bytes()
        self.assertEqual(bundle.build_bundle(self.root, self.destination)["blockers"], ["destination_exists"])
        self.assertEqual((self.root / "entity.json").read_bytes(), before)
        self.destination.unlink()
        with patch.object(bundle.os, "fsync", side_effect=OSError("PRIVATE disk error")):
            self.blocked("bundle_export_failed")

    def test_native_bounds_export_and_file_map_validation(self):
        for constant, value, code in (("MAX_FILE", 10, "file_size_limit"), ("MAX_TOTAL", 10, "workspace_size_limit"), ("MAX_ITEMS", 1, "inventory_limit")):
            with self.subTest(constant=constant), patch.object(capture, constant, value):
                self.blocked(code)
        _, loaded = self.export()
        for constant, value, code in (("MAX_FILE", 10, "file_size_limit"), ("MAX_TOTAL", 10, "workspace_size_limit"), ("MAX_ITEMS", 1, "inventory_limit")):
            with self.subTest(constant=constant), patch.object(capture, constant, value):
                with self.assertRaises(bundle.BundleError) as error:
                    bundle.validate_files(loaded["manifest"], loaded["files"])
                self.assertEqual(error.exception.code, code)

    def test_framed_path_limit_includes_prefix_and_utf8_bytes_on_all_surfaces(self):
        _, baseline = self.export()
        self.destination.unlink()
        for component in ("a" * 200, "\u00e9" * 100):
            for size in (512, 513):
                with self.subTest(multibyte=component.startswith("\u00e9"), size=size):
                    prefix = "reports/" + component + "/" + "b" * 200 + "/"
                    name = prefix + "c" * (size - len(("entity/" + prefix).encode("utf-8")) - 4) + ".txt"
                    self.assertEqual(len(("entity/" + name).encode("utf-8")), size)
                    path = self.root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"synthetic report")
                    if size == 512:
                        _, loaded = self.export()
                        self.assertIn(name, loaded["files"])
                        self.assertEqual(bundle.validate_files(loaded["manifest"], loaded["files"])["status"], "validated")
                        self.destination.unlink()
                    else:
                        self.blocked("framed_path_limit")
                        files = {**baseline["files"], name: path.read_bytes()}
                        manifest = deepcopy(baseline["manifest"])
                        manifest["files"] = [{"path": key, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                                             for key, data in sorted(files.items())]
                        with self.assertRaises(bundle.BundleError) as error:
                            bundle.validate_files(manifest, files)
                        self.assertEqual(error.exception.code, "framed_path_limit")
                        archive = self.archive(manifest, files)
                        with patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("must reject path before reading")):
                            self.assertIn("framed_path_limit", bundle.validate_bundle(archive)["blockers"])
                    path.unlink()

    def test_4095_files_validate_but_4096_leave_no_derived_file_slot(self):
        _, loaded = self.export()
        files = dict(loaded["files"])
        for index in range(4095 - len(files)):
            files[f"reports/report-{index:04d}.txt"] = b""
        self.assertEqual(len(files), 4095)
        manifest = bundle._manifest(files, {})
        # No speculative byte reserve: the exact current byte total is admitted.
        with patch.object(capture, "MAX_TOTAL", sum(map(len, files.values()))):
            self.assertEqual(bundle.validate_files(manifest, files)["status"], "validated")
        self.assertEqual(bundle.validate_bundle(self.archive(manifest, files))["status"], "validated")
        files["reports/extra.txt"] = b""
        self.assertEqual(len(files), 4096)
        manifest["files"] = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                             for name, data in sorted(files.items())]
        with self.assertRaises(bundle.BundleError) as error:
            bundle.validate_files(manifest, files)
        self.assertEqual(error.exception.code, "inventory_limit")
        archive = self.archive(manifest, files)
        with patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("must reject count before reading")):
            self.assertIn("inventory_limit", bundle.validate_bundle(archive)["blockers"])

    def test_export_rejects_capture_without_derived_file_slot(self):
        report, files, snapshot, inventory = capture._capture(self.root, for_export=True)
        for index in range(4096 - len(files)):
            files[f"reports/report-{index:04d}.txt"] = b""
        with patch.object(capture, "_capture", return_value=(report, files, snapshot, inventory)):
            self.blocked("inventory_limit")

    def test_native_header_budget_rejects_long_descriptor_sets_on_all_surfaces(self):
        report, files, snapshot, inventory = capture._capture(self.root, for_export=True)
        files["ledger.sqlite"] = snapshot
        # Well below 4095 members and the 2 MiB manifest, but above the native
        # 1 MiB descriptor header. Each framed path is within its 512-byte cap.
        for index in range(2300):
            name = "reports/" + "a" * 200 + "/" + f"{index:04d}-" + "b" * 200 + ".txt"
            files[name] = b""
        entries = [{"path": name, "size": len(data), "sha256": bundle._sha(data)} for name, data in sorted(files.items())]
        self.assertLess(len(bundle._json_bytes(entries)), bundle.MAX_MANIFEST - 10000)
        self.assertGreater(bundle._native_header_sizes(files)[0], bundle.MAX_FRAME_HEADER)
        with patch.object(capture, "_capture", return_value=(report, files, snapshot, inventory)):
            self.blocked("framed_header_limit")
        # Receiver also rejects a forged manifest whose individual hashes match.
        small = {name: data for name, data in files.items() if not name.startswith("reports/")}
        manifest = bundle._manifest(small, {})
        manifest["files"] = entries
        with self.assertRaisesRegex(bundle.BundleError, "framed_header_limit"):
            bundle.validate_files(manifest, files)
        self.assertIn("framed_header_limit", bundle.validate_bundle(self.archive(manifest, files))["blockers"])

    def test_header_budget_reserves_response_projection_and_exact_json_escaping(self):
        files = {"reports/quote\"\u00e9.txt": b""}
        request_size, response_size = bundle._native_header_sizes(files)
        self.assertGreater(response_size, request_size)
        descriptor = {"path": "entity/reports/quote\"\u00e9.txt", "size": 0, "sha256": "0" * 64}
        request = {"version": 1, "files": [
            {"path": "meta/job.json", "size": bundle.MAX_FRAME_HEADER, "sha256": "0" * 64},
            {"path": "input/manifest.json", "size": bundle.MAX_MANIFEST, "sha256": "0" * 64}, descriptor]}
        self.assertEqual(request_size, len(json.dumps(request, separators=(",", ":")).encode()))
        # The return header is the binding constraint; no arbitrary byte pad.
        with patch.object(bundle, "MAX_FRAME_HEADER", response_size):
            bundle._file_checks(files, set())
        with patch.object(bundle, "MAX_FRAME_HEADER", response_size - 1):
            with self.assertRaisesRegex(bundle.BundleError, "framed_header_limit"):
                bundle._file_checks(files, set())

    def test_loader_rejects_traversal_duplicate_links_executables_and_compression(self):
        _, loaded = self.export()
        for name in ("../escape", "/absolute", "entity/../escape", "entity/a\\b", "entity/%2e%2e/escape", "manifest.json", "entity/entity.json", "unexpected"):
            with self.subTest(name=name):
                path = self.archive(loaded["manifest"], loaded["files"], [(name, b"PRIVATE")])
                self.assertEqual(bundle.validate_bundle(path)["status"], "blocked")
                self.assertFalse((self.root.parent / "escape").exists())
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFREG | 0o700, stat.S_IFDIR | 0o600):
            info = zipfile.ZipInfo("entity/context/unsafe.txt")
            info.create_system = 3
            info.external_attr = mode << 16
            path = self.archive(loaded["manifest"], loaded["files"], [(info, b"PRIVATE")])
            self.assertIn("unsafe_bundle_member", bundle.validate_bundle(path)["blockers"])
        path = self.archive(loaded["manifest"], loaded["files"], compression=zipfile.ZIP_DEFLATED)
        self.assertIn("unsupported_zip_encoding", bundle.validate_bundle(path)["blockers"])

    def test_loader_rejects_hash_anchor_identity_and_manifest_corruption(self):
        _, loaded = self.export()
        for field in ("ledger", "reports", "source_entity"):
            with self.subTest(field=field):
                manifest = deepcopy(loaded["manifest"])
                manifest[field] = {}
                path = self.archive(manifest, loaded["files"])
                self.assertIn("bundle_anchors_mismatch", bundle.validate_bundle(path)["blockers"])
        files = dict(loaded["files"])
        files["entity.json"] += b" "
        path = self.archive(loaded["manifest"], files)
        self.assertIn("bundle_hash_mismatch", bundle.validate_bundle(path)["blockers"])
        files = dict(loaded["files"])
        del files["entity.json"]
        self.assertEqual(bundle.validate_bundle(self.archive(loaded["manifest"], files))["status"], "blocked")
        for field, value in (("version", 2), ("version", True), ("import_ready", True)):
            manifest = {**loaded["manifest"], field: value}
            self.assertEqual(bundle.validate_bundle(self.archive(manifest, loaded["files"]))["status"], "blocked")
        self.destination.write_bytes(b"PRIVATE not a ZIP")
        result = bundle.validate_bundle(self.destination)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_archive_crc_and_malformed_manifest_fail_closed(self):
        _, loaded = self.export()
        raw = self.destination.read_bytes()
        position = raw.index(b"PRIVATE COMPANY")
        self.destination.write_bytes(raw[:position] + b"X" + raw[position + 1:])
        self.assertEqual(bundle.validate_bundle(self.destination)["status"], "blocked")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("manifest.json", '{"version":1,"version":1}')
        self.destination.write_bytes(output.getvalue())
        self.assertIn("invalid_json", bundle.validate_bundle(self.destination)["blockers"])

    def test_matching_file_hashes_do_not_bypass_ledger_or_reference_validation(self):
        _, loaded = self.export()
        for query, code in (("UPDATE audit_events SET record_hash='tampered'", "audit_chain_invalid"),
                            ("UPDATE meta SET value='99' WHERE key='schema_version'", "unsupported_schema")):
            with self.subTest(code=code):
                path = self.root.parent / "tampered.sqlite"
                path.write_bytes(loaded["files"]["ledger.sqlite"])
                with closing(sqlite3.connect(path)) as sql, sql:
                    sql.execute(query)
                files = {**loaded["files"], "ledger.sqlite": path.read_bytes()}
                manifest = deepcopy(loaded["manifest"])
                for item in manifest["files"]:
                    if item["path"] == "ledger.sqlite":
                        item.update(size=len(files["ledger.sqlite"]), sha256=hashlib.sha256(files["ledger.sqlite"]).hexdigest())
                self.assertIn(code, bundle.validate_bundle(self.archive(manifest, files))["blockers"])
        files = {**loaded["files"], "context/links.json": b'{"documentPath":"missing.pdf"}'}
        manifest = deepcopy(loaded["manifest"])
        manifest["files"] = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                             for name, data in sorted(files.items())]
        self.assertIn("referenced_bytes_missing", bundle.validate_bundle(self.archive(manifest, files))["blockers"])

    def test_markdown_and_ledger_references_are_not_dropped(self):
        (self.root / "business-profile.md").write_text("[Evidence](reports/missing.pdf)")
        self.blocked("referenced_bytes_missing")
        (self.root / "business-profile.md").unlink()
        with self.store.transaction() as sql:
            self.store.append_audit_event("reference", {"documentPath": "/PRIVATE/evidence.pdf"}, sql)
        self.blocked("absolute_reference_requires_rewrite")

    def test_zip_limits_are_checked_before_member_contents_are_read(self):
        _, loaded = self.export()
        for constant, limit, code in (("MAX_FILE", 10, "file_size_limit"), ("MAX_ITEMS", 1, "inventory_limit"), ("MAX_TOTAL", 10, "invalid_bundle")):
            with self.subTest(constant=constant), patch.object(capture, constant, limit):
                with patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("must not read oversized content")):
                    self.assertIn(code, bundle.validate_bundle(self.destination)["blockers"])

    def test_load_rejects_symlink_or_hardlinked_bundle(self):
        self.export()
        alias = self.root.parent / "alias.zip"
        alias.symlink_to(self.destination)
        self.assertIn("unsafe_bundle_file", bundle.validate_bundle(alias)["blockers"])
        alias.unlink()
        os.link(self.destination, alias)
        self.assertIn("unsafe_bundle_file", bundle.validate_bundle(alias)["blockers"])


if __name__ == "__main__":
    unittest.main()
