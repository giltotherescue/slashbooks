from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from bookkeeping.cli import build_parser, main  # noqa: E402
from bookkeeping import hosted, hosted_migration, migration_bundle, remote  # noqa: E402
from bookkeeping.remote_contract import (  # noqa: E402
    ContractError, command_spec, parse_command, rewrite_paths,
)
from tests import test_hosted_migration as fixtures


class MigrationExportCommandTests(unittest.TestCase):
    """The hosted executor gets one fixed, offline migration-export leaf."""

    setUp = fixtures.MigrationPreflightTests.setUp
    write = fixtures.MigrationPreflightTests.write
    add_entry = fixtures.MigrationPreflightTests.add_entry
    snapshot = fixtures.MigrationPreflightTests.snapshot

    def run_export(self, destination: Path) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "migration", "export", "--entity", str(self.root),
                "--output", str(destination),
            ])
        return code, json.loads(output.getvalue())

    def test_command_exports_deterministic_lossless_bundle_and_roundtrips(self):
        evidence = b"%PDF synthetic migration evidence\n"
        (self.root / "documents").mkdir()
        (self.root / "documents/invoice.pdf").write_bytes(evidence)
        (self.root / ".env").write_bytes(b"BOOKS_API_TOKEN=synthetic-secret-that-must-not-export\n")
        (self.root / ".git/objects").mkdir(parents=True)
        (self.root / ".git/objects/private").write_bytes(b"synthetic repository metadata")
        (self.root / ".engine/artifacts/reports").mkdir(parents=True)
        (self.root / ".engine/bridge.json").write_text(json.dumps({
            "entries": {"synthetic-engine-id": "synthetic-cloud-id"},
            "sources": {},
            "proposals": {},
        }))
        (self.root / ".engine/company-artifacts.json").write_text(json.dumps({
            "reports/synthetic.csv": "0" * 64,
        }))
        (self.root / ".engine/artifacts/reports/synthetic.csv").write_bytes(
            b"host-derived artifact must not enter portable company state\n"
        )
        before = self.snapshot()

        first = self.root.parent / "company.zip"
        read = hosted_migration._read

        def read_without_host_state(fd, name, signature):
            self.assertFalse(name.startswith(".engine/"), name)
            return read(fd, name, signature)

        with patch.object(hosted_migration, "_read", side_effect=read_without_host_state):
            code, summary = self.run_export(first)
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["status"], "exported")
        self.assertTrue(summary["bundle_created"])
        self.assertFalse(summary["import_ready"])
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(self.snapshot(), before)

        loaded = migration_bundle.load_bundle(first)
        manifest, files = loaded["manifest"], loaded["files"]
        self.assertEqual(migration_bundle.validate_files(manifest, files)["status"], "validated")
        self.assertEqual(files["documents/invoice.pdf"], evidence)
        self.assertEqual(files["entity.json"], before["entity.json"][0])
        self.assertIn("ledger.sqlite", files)
        self.assertNotIn(".env", files)
        self.assertFalse(any(name.startswith(".git") for name in files))
        self.assertFalse(any(name.startswith(".engine") for name in files))
        self.assertGreaterEqual(manifest["excluded"].get("credentials", 0), 1)
        self.assertGreaterEqual(manifest["excluded"].get("local_vcs", 0), 1)
        self.assertGreaterEqual(manifest["excluded"].get("runtime_state", 0), 2)
        self.assertGreaterEqual(manifest["excluded"].get("derived_state", 0), 1)
        by_name = {item["path"]: item for item in manifest["files"]}
        self.assertEqual(by_name["documents/invoice.pdf"]["size"], len(evidence))
        self.assertNotIn(b"synthetic-secret-that-must-not-export", first.read_bytes())

        with zipfile.ZipFile(first) as archive:
            names = archive.namelist()
            self.assertEqual(names, ["manifest.json", *sorted("entity/" + name for name in files)])
            self.assertTrue(all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist()))

        second = self.root.parent / "company-second.zip"
        second_code, second_summary = self.run_export(second)
        self.assertEqual(second_code, 0, second_summary)
        self.assertEqual(second_summary, summary)
        self.assertEqual(second.read_bytes(), first.read_bytes())

    def test_bound_company_export_stays_local_and_offline(self):
        binding = self.root / remote.CONFIG_NAME
        binding.write_text(json.dumps({
            "endpoint": "https://books.example.invalid",
            "company": "synthetic-company",
            "token_env": "BOOKS_API_TOKEN",
        }), encoding="utf-8")
        args = build_parser().parse_args([
            "migration", "export", "--entity", str(self.root),
            "--output", str(self.root.parent / "bound-company.zip"),
        ])
        self.assertTrue(remote.is_bound(args))

        destination = self.root.parent / "bound-company.zip"
        with (
            patch.object(remote, "maybe_run", side_effect=AssertionError("remote routing is forbidden")),
            patch.object(hosted, "HostedClient", side_effect=AssertionError("network is forbidden")),
        ):
            code, summary = self.run_export(destination)

        self.assertEqual(code, 0, summary)
        self.assertTrue(summary["bundle_created"])
        loaded = migration_bundle.load_bundle(destination)
        self.assertEqual(
            migration_bundle.validate_files(loaded["manifest"], loaded["files"])["status"],
            "validated",
        )
        self.assertIn("entity.json", loaded["files"])
        self.assertIn("ledger.sqlite", loaded["files"])
        self.assertNotIn(remote.CONFIG_NAME, loaded["files"])

    def test_unsafe_symlink_and_existing_destination_never_publish_partial_output(self):
        outside = self.root.parent / "outside.pdf"
        outside.write_bytes(b"synthetic outside bytes")
        (self.root / "documents").mkdir()
        (self.root / "documents/escape.pdf").symlink_to(outside)
        destination = self.root.parent / "blocked.zip"

        code, result = self.run_export(destination)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("unsafe_file", result["blockers"])
        self.assertFalse(destination.exists())
        self.assertNotIn(str(outside), json.dumps(result))

        (self.root / "documents/escape.pdf").unlink()
        destination.write_bytes(b"keep this existing artifact")
        code, result = self.run_export(destination)
        self.assertEqual(code, 1)
        self.assertEqual(result["blockers"], ["destination_exists"])
        self.assertEqual(destination.read_bytes(), b"keep this existing artifact")

    def test_only_exact_engine_owned_metadata_is_excluded(self):
        for name in (
            ".engine/customer.json",
            ".engine/bridge.backup.json",
            ".engine/artifacts-custom/customer.json",
        ):
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic customer source")
                destination = self.root.parent / (name.replace("/", "-") + ".zip")
                code, result = self.run_export(destination)
                self.assertEqual(code, 1)
                self.assertIn("unsupported_file", result["blockers"])
                self.assertFalse(destination.exists())
                path.unlink()

        bridge = self.root / ".engine/bridge.json"
        bridge.symlink_to(self.root / "entity.json")
        destination = self.root.parent / "linked-bridge.zip"
        code, result = self.run_export(destination)
        self.assertEqual(code, 1)
        self.assertIn("unsafe_file", result["blockers"])
        self.assertFalse(destination.exists())

    def test_remote_contract_rewrites_only_entity_and_output_and_classifies_as_read(self):
        argv = ["migration", "export", "--entity", "@entity", "--output", "@output/company.zip"]
        spec = command_spec(argv)
        self.assertEqual(spec, {
            "path": ("migration", "export"),
            "entity_fields": ["entity"],
            "input_fields": [],
            "output_fields": ["output"],
            "mutates": False,
            "network": False,
        })
        rewritten = rewrite_paths(argv, {
            "@entity": "/sandbox/entity",
            "@output/company.zip": "/sandbox/output/company.zip",
        })
        args, _, path = parse_command(rewritten)
        self.assertEqual(path, ("migration", "export"))
        self.assertEqual(args.entity, Path("/sandbox/entity"))
        self.assertEqual(args.output, Path("/sandbox/output/company.zip"))

        # The compatible local transport aliases still parse and remain outside
        # the remotely executable engine contract.
        for alias in ("cloud", "hosted"):
            parsed = build_parser().parse_args([
                alias, "migration", "export", "--entity-dir", "@entity",
                "--output", "@output/company.zip",
            ])
            self.assertIn(parsed.command, {"cloud", "hosted"})
            with self.assertRaisesRegex(ContractError, "Transport management"):
                command_spec([
                    alias, "migration", "export", "--entity-dir", "@entity",
                    "--output", "@output/company.zip",
                ])


if __name__ == "__main__":
    unittest.main()
