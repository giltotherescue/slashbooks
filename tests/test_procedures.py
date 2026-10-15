"""Agent procedures: one versioned source, referenced (never copied) by skills."""
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from bookkeeping import procedures  # noqa: E402

SKILLS_DIR = REPO_ROOT / "skills"
REQUIRED = {
    "onboarding-profile", "categorize-record", "reconcile", "mail-triage",
    "emailed-answers", "grouped-replies", "question-rules", "non-blocking",
    "month-end-check", "document-intake", "draft-insights", "hard-rules",
}
REF_RE = re.compile(r"procedures show ([a-z][a-z0-9-]*)`\s*\(version (\d+)\)")
PRIVATE_RE = re.compile(r"sbat_|sba_|hosted/|wrangler|cloudflare|d1|r2 ", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("`", "")).strip().lower()


def _skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.rglob("*.md"))


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "bookkeeping.cli", *args],
                          capture_output=True, text=True, cwd=REPO_ROOT,
                          env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")})


class ProcedureSourceTests(unittest.TestCase):
    def test_required_procedures_exist_and_render(self):
        found = {p["name"]: p for p in procedures.list_procedures()}
        self.assertEqual(REQUIRED, set(found))
        for name, proc in found.items():
            with self.subTest(name=name):
                md = procedures.render_markdown(proc)
                self.assertIn(f"version {proc['version']}", md)
                self.assertGreaterEqual(len(proc["rules"]), 4)
                for rule in proc["rules"]:
                    self.assertIn(rule, md)
                    self.assertFalse(PRIVATE_RE.search(rule), rule)

    def test_cli_exposes_markdown_and_json_with_version(self):
        result = _cli("procedures", "show", "mail-triage", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["name"], "mail-triage")
        self.assertIsInstance(data["version"], int)
        self.assertEqual(len(data["sha256"]), 64)
        md = _cli("procedures", "show", "mail-triage")
        self.assertIn(data["rules"][0], md.stdout)
        every = json.loads(_cli("procedures", "show", "--all", "--format", "json").stdout)
        self.assertEqual(REQUIRED, {p["name"] for p in every["procedures"]})
        self.assertEqual(len(every["bundle_sha256"]), 64)
        listing = json.loads(_cli("procedures", "list").stdout)
        self.assertEqual(listing["bundle_sha256"], every["bundle_sha256"])

    def test_unknown_procedure_fails(self):
        result = _cli("procedures", "show", "../etc/passwd")
        self.assertEqual(result.returncode, 1)
        self.assertIn("UNKNOWN_PROCEDURE", result.stderr)


class SkillReferenceTests(unittest.TestCase):
    def test_skills_reference_current_versions(self):
        current = {p["name"]: p["version"] for p in procedures.list_procedures()}
        referenced = set()
        for path in SKILLS_DIR.glob("*/SKILL.md"):
            for name, version in REF_RE.findall(path.read_text()):
                with self.subTest(skill=path.parent.name, procedure=name):
                    self.assertIn(name, current)
                    self.assertEqual(int(version), current[name],
                                     f"{path} references a stale version of {name}")
                referenced.add(name)
        self.assertEqual(REQUIRED, referenced, "every procedure needs a skill reference")

    def test_skills_do_not_duplicate_procedure_text(self):
        corpus = {path: _norm(path.read_text()) for path in _skill_files()}
        for proc in procedures.list_procedures():
            for rule in proc["rules"]:
                needle = _norm(rule)
                for path, text in corpus.items():
                    with self.subTest(procedure=proc["name"], skill=str(path.relative_to(REPO_ROOT))):
                        self.assertNotIn(needle, text,
                                         f"{path} copies a rule of {proc['name']}; reference it instead")

    def test_duplicate_detector_catches_a_copy(self):
        rule = procedures.get_procedure("hard-rules")["rules"][0]
        self.assertIn(_norm(rule), _norm(f"intro\n\n- {rule}\n"))


if __name__ == "__main__":
    unittest.main()
