"""Deterministic, standalone packaging for the hosted directory plugin."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hosted_plugin.py"
SPEC = importlib.util.spec_from_file_location("hosted_plugin_packaging", SCRIPT)
assert SPEC and SPEC.loader
hosted_plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hosted_plugin)


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True,
        env={**os.environ, **(env or {})}, stderr=subprocess.STDOUT,
    ).strip()


class PackageRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        git(root, "init", "-q")
        git(root, "config", "user.name", "Synthetic Test")
        git(root, "config", "user.email", "synthetic@example.invalid")
        self.write(
            "src/bookkeeping/procedures.py",
            (ROOT / "src/bookkeeping/procedures.py").read_text(encoding="utf-8"),
        )
        self.write("plugins/slashbooks-cloud/plugin.json", "{}\n")
        for skill, names in hosted_plugin.PROCEDURES_BY_SKILL.items():
            links = "\n".join(
                f"- [{name}](references/procedures/{name}.md)" for name in names
            )
            self.write(
                f"plugins/slashbooks-cloud/skills/{skill}/SKILL.md",
                f"---\nname: {skill}\ndescription: Synthetic skill.\n---\n\n{links}\n",
            )
            for name in names:
                canonical = self.procedure(name)
                self.write(f"src/bookkeeping/procedures/{name}.md", canonical)
                self.write(
                    f"plugins/slashbooks-cloud/skills/{skill}/references/procedures/{name}.md",
                    canonical,
                )
        hosted_plugin.sync(self.root)
        self.commit("fixture")

    @staticmethod
    def procedure(name: str, suffix: str = "") -> str:
        return f"---\nname: {name}\nversion: 1\ntitle: {name}\n---\n- Synthetic rule for {name}.{suffix}\n"

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def commit(self, message: str) -> str:
        git(self.root, "add", "-A")
        fixed = "2030-01-02T03:04:06+00:00"
        git(self.root, "commit", "-q", "-m", message, env={
            "GIT_AUTHOR_DATE": fixed,
            "GIT_COMMITTER_DATE": fixed,
        })
        return git(self.root, "rev-parse", "HEAD")


class HostedPluginPackagingTests(unittest.TestCase):
    def fixture(self, directory: str) -> PackageRepository:
        return PackageRepository(Path(directory))

    def test_build_is_deterministic_commit_only_and_standalone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(tmp)
            fixture.write("plugins/slashbooks-cloud/private-untracked.txt", "must not ship\n")
            first = fixture.root / "first.zip"
            second = fixture.root / "second.zip"
            hosted_plugin.build(fixture.root, "HEAD", first)
            time.sleep(3)
            hosted_plugin.build(fixture.root, "HEAD", second)
            self.assertEqual(first.read_bytes(), second.read_bytes())

            expected_stamp = datetime.fromtimestamp(
                int(git(fixture.root, "show", "-s", "--format=%ct", "HEAD")), timezone.utc
            ).timetuple()[:6]
            with ZipFile(first) as archive:
                names = archive.namelist()
                self.assertNotIn("private-untracked.txt", names)
                self.assertIn("plugin.json", names)
                self.assertNotIn("plugins/slashbooks-cloud/plugin.json", names)
                self.assertEqual(names, sorted(names))
                for info in archive.infolist():
                    self.assertEqual(info.date_time, expected_stamp)
                    self.assertEqual((info.external_attr >> 16) & 0o177777, stat.S_IFREG | 0o644)
                extract = fixture.root / "extracted"
                archive.extractall(extract)

            extracted = {
                path.relative_to(extract).as_posix(): path.read_bytes()
                for path in extract.rglob("*") if path.is_file()
            }
            hosted_plugin._check_links(extracted)
            guidance = json.loads(extracted[hosted_plugin.GUIDANCE_PATH])
            self.assertEqual(set(guidance), {"schema_version", "mcp_server_instructions"})
            self.assertEqual(guidance["schema_version"], 1)
            instructions = guidance["mcp_server_instructions"]
            self.assertEqual(set(instructions), {"text", "sources"})
            self.assertLessEqual(
                len(instructions["text"].encode("utf-8")),
                hosted_plugin.MAX_MCP_GUIDANCE_BYTES,
            )
            self.assertEqual(
                [source["name"] for source in instructions["sources"]],
                list(hosted_plugin.MCP_GUIDANCE_PROCEDURES),
            )
            parser = hosted_plugin._procedure_module(fixture.root)
            for source in instructions["sources"]:
                procedure = parser.get_procedure(source["name"])
                self.assertEqual(source["version"], procedure["version"])
                self.assertEqual(source["sha256"], procedure["sha256"])
            self.assertTrue(instructions["text"].startswith("hard-rules (procedure hard-rules"))

    def test_sync_repairs_drift_and_check_uses_canonical_meaning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(tmp)
            name = hosted_plugin.PROCEDURES_BY_SKILL["get-started"][0]
            generated = fixture.root / f"plugins/slashbooks-cloud/skills/get-started/references/procedures/{name}.md"
            generated.write_text(fixture.procedure(name, " Stale"), encoding="utf-8")
            extra = fixture.write(
                "plugins/slashbooks-cloud/skills/get-started/references/procedures/extra.md",
                fixture.procedure("extra"),
            )
            with self.assertRaises(hosted_plugin.PackagingError):
                hosted_plugin.check(fixture.root)
            hosted_plugin.sync(fixture.root)
            hosted_plugin.check(fixture.root)
            self.assertFalse(extra.exists())
            self.assertEqual(
                generated.read_bytes(),
                (fixture.root / f"src/bookkeeping/procedures/{name}.md").read_bytes(),
            )
            guidance = fixture.root / "plugins/slashbooks-cloud/references/agent-guidance.json"
            guidance.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(hosted_plugin.PackagingError, "stale generated guidance"):
                hosted_plugin.check(fixture.root)
            hosted_plugin.sync(fixture.root)
            hosted_plugin.check(fixture.root)

    def test_sync_refuses_symlinks_without_touching_external_files(self) -> None:
        for relative in (
            "skills/books/references/procedures/hard-rules.md",
            hosted_plugin.GUIDANCE_PATH,
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                fixture = self.fixture(tmp)
                sentinel = fixture.root.parent / f"sentinel-{fixture.root.name}.txt"
                sentinel.write_text("preserve me\n", encoding="utf-8")
                target = fixture.root / "plugins/slashbooks-cloud" / relative
                target.unlink()
                target.symlink_to(sentinel)
                with self.assertRaisesRegex(
                    hosted_plugin.PackagingError, "refusing to write through hosted plugin symlink"
                ):
                    hosted_plugin.sync(fixture.root)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me\n")
                sentinel.unlink()

    def test_sync_refuses_oversized_mcp_guidance_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(tmp)
            reference = fixture.root / (
                "plugins/slashbooks-cloud/skills/books/references/procedures/hard-rules.md"
            )
            before = reference.read_bytes()
            fixture.write(
                "src/bookkeeping/procedures/hard-rules.md",
                fixture.procedure("hard-rules", " " + "x" * hosted_plugin.MAX_MCP_GUIDANCE_BYTES),
            )
            with self.assertRaisesRegex(hosted_plugin.PackagingError, "maximum is 4096"):
                hosted_plugin.sync(fixture.root)
            self.assertEqual(reference.read_bytes(), before)

    def test_build_refuses_stale_or_missing_generated_procedures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(tmp)
            name = hosted_plugin.PROCEDURES_BY_SKILL["books"][0]
            fixture.write(f"src/bookkeeping/procedures/{name}.md", fixture.procedure(name, " Changed"))
            stale = fixture.commit("stale")
            with self.assertRaisesRegex(hosted_plugin.PackagingError, "stale generated procedure"):
                hosted_plugin.build(fixture.root, stale, fixture.root / "stale.zip")

        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(tmp)
            name = hosted_plugin.PROCEDURES_BY_SKILL["get-started"][0]
            (fixture.root / f"plugins/slashbooks-cloud/skills/get-started/references/procedures/{name}.md").unlink()
            missing = fixture.commit("missing")
            with self.assertRaisesRegex(hosted_plugin.PackagingError, "generated procedure set differs"):
                hosted_plugin.build(fixture.root, missing, fixture.root / "missing.zip")

    def test_build_refuses_symlinks_executables_and_escaping_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(tmp)
            link = fixture.root / "plugins/slashbooks-cloud/symlink"
            link.symlink_to("plugin.json")
            linked = fixture.commit("symlink")
            with self.assertRaisesRegex(hosted_plugin.PackagingError, "120000 blob"):
                hosted_plugin.build(fixture.root, linked, fixture.root / "symlink.zip")

        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(tmp)
            executable = fixture.write("plugins/slashbooks-cloud/run", "#!/bin/sh\n")
            executable.chmod(0o755)
            executable_commit = fixture.commit("executable")
            with self.assertRaisesRegex(hosted_plugin.PackagingError, "100755 blob"):
                hosted_plugin.build(fixture.root, executable_commit, fixture.root / "executable.zip")

        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(tmp)
            skill = fixture.root / "plugins/slashbooks-cloud/skills/books/SKILL.md"
            skill.write_text(skill.read_text() + "\n[escape](../outside.md)\n", encoding="utf-8")
            escaping = fixture.commit("escaping")
            with self.assertRaisesRegex(hosted_plugin.PackagingError, "escapes its directory"):
                hosted_plugin.build(fixture.root, escaping, fixture.root / "escaping.zip")

    def test_build_refuses_an_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(tmp)
            output = fixture.write("existing.zip", "old artifact\n")
            with self.assertRaisesRegex(hosted_plugin.PackagingError, "output already exists"):
                hosted_plugin.build(fixture.root, "HEAD", output)
            self.assertEqual(output.read_text(), "old artifact\n")


if __name__ == "__main__":
    unittest.main()
