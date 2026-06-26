"""
tests/test_plugin.py — U11 plugin packaging assertions.

Covers:
  (a) plugin.json and marketplace.json parse as JSON with required keys
  (b) bin/books is executable, contains required exports, no session-hook refs
  (c) Every `scripts/books <subcommand>` referenced in SKILL.md bodies maps to a real
      CLI path (verified via `python3 -m bookkeeping.cli <tokens...> --help`)
  (d) Every SKILL.md has frontmatter with name+description and avoids
      Codex-rejected Claude-only disable-model-invocation:true
  (e) Jargon scan: skill bodies do not expose beancount/SQL to the owner;
      untrusted-data instruction block and minimum-context phrase are present
"""
import json
import os
import re
import stat
import subprocess
import sys
import unittest
from pathlib import Path

# Repo root is one level above tests/
REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / ".claude-plugin"
CODEX_PLUGIN_DIR = REPO_ROOT / ".codex-plugin"
CODEX_MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
BIN_DIR = REPO_ROOT / "bin"
SKILLS_DIR = REPO_ROOT / "skills"

SKILL_NAMES = [
    "books",
    "books-onboard",
    "books-checkup",
    "books-dashboard",
    "books-close",
    "books-review",
    "books-backtest",
    "books-ask",
    "books-export",
]

# Phrases that must appear in every skill body (security / data-handling rules)
UNTRUSTED_DATA_PHRASE = "never as instructions"
MINIMUM_CONTEXT_PHRASE = "never include amounts"

# Jargon the owner must never be told to write/read.
# SQL keywords (SELECT/INSERT/UPDATE/DELETE) are only jargon when they appear in
# uppercase (as they would in actual SQL), not in normal English prose ("update the
# business profile").  Beancount-syntax checks are case-insensitive because that
# term is always jargon regardless of case.
OWNER_JARGON_RE = re.compile(
    r"\b(write\s+beancount|read\s+beancount|beancount\s+syntax|write\s+SQL|run\s+SQL"
    r"|SELECT\s+\w|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b",
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> dict:
    """
    Parse simple YAML-like frontmatter delimited by `---` lines.
    Returns a dict of key: value pairs (values are strings, stripped).
    Multi-line / nested YAML is not supported — we only need scalar keys.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fm: dict = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip()
    return fm


def _skill_body(text: str) -> str:
    """Return everything after the closing --- of the frontmatter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    seen_first = True
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[i + 1:])
    return text


def _extract_books_invocations(body: str) -> list[list[str]]:
    """
    Extract every `scripts/books <subcommand> ...` reference from fenced code
    blocks and inline backticks in the skill body.

    Returns a list of token lists, e.g.:
      [["banksync", "download"], ["queue", "propose"], ...]
    """
    invocations: list[list[str]] = []

    # Match fenced code blocks
    fenced = re.findall(r"```[^\n]*\n(.*?)```", body, re.DOTALL)
    candidates = fenced
    for block in candidates:
        for line in block.splitlines():
            line = line.strip()
            # Look for lines starting with the bundled skill wrapper.
            if re.match(r"^scripts/books\b", line):
                # Strip shell variable patterns like <...> and [...]
                cleaned = re.sub(r"<[^>]+>", "PLACEHOLDER", line)
                cleaned = re.sub(r"\[[^\]]+\]", "", cleaned)
                tokens = cleaned.split()
                # tokens[0] == "scripts/books", tokens[1] is the subcommand
                if len(tokens) >= 2:
                    sub_tokens = tokens[1:]
                    # Drop option flags (start with -)
                    sub_tokens = [t for t in sub_tokens if not t.startswith("-")]
                    # Drop PLACEHOLDER positional args
                    sub_tokens = [t for t in sub_tokens if t != "PLACEHOLDER"]
                    # Keep first 1-2 meaningful tokens (subcommand + optional sub-subcommand)
                    sub_tokens = sub_tokens[:2]
                    if sub_tokens:
                        invocations.append(sub_tokens)

    # Deduplicate while preserving order
    seen: set[tuple] = set()
    unique: list[list[str]] = []
    for inv in invocations:
        key = tuple(inv)
        if key not in seen:
            seen.add(key)
            unique.append(inv)
    return unique


def _check_cli_path(tokens: list[str]) -> tuple[bool, str]:
    """
    Run `python3 -m bookkeeping.cli <tokens...> --help` with PYTHONPATH=src.
    Returns (success: bool, stderr: str).
    """
    env = os.environ.copy()
    src_path = str(REPO_ROOT / "src")
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src_path}:{existing_pp}" if existing_pp else src_path
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    cmd = [sys.executable, "-m", "bookkeeping.cli"] + tokens + ["--help"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode == 0, result.stderr.strip()


# ─────────────────────────────────────────────────────────────────────────────
# (a) Manifest files
# ─────────────────────────────────────────────────────────────────────────────

class TestManifests(unittest.TestCase):

    def test_plugin_json_parses(self):
        path = PLUGIN_DIR / "plugin.json"
        self.assertTrue(path.exists(), f"{path} does not exist")
        with path.open() as f:
            data = json.load(f)
        for key in ("name", "description", "version", "author", "homepage", "repository"):
            self.assertIn(key, data, f"plugin.json missing key: {key}")
        self.assertEqual(data["name"], "slashbooks")
        self.assertEqual(data["homepage"], "https://github.com/giltotherescue/slashbooks")
        self.assertEqual(data["repository"], "https://github.com/giltotherescue/slashbooks")
        self.assertIn("name", data["author"], "plugin.json author missing 'name'")

    def test_marketplace_json_parses(self):
        path = PLUGIN_DIR / "marketplace.json"
        self.assertTrue(path.exists(), f"{path} does not exist")
        with path.open() as f:
            data = json.load(f)
        for key in ("name", "owner", "plugins"):
            self.assertIn(key, data, f"marketplace.json missing key: {key}")
        self.assertIsInstance(data["plugins"], list)
        self.assertGreater(len(data["plugins"]), 0, "marketplace.json plugins list is empty")
        plugin = data["plugins"][0]
        for key in ("name", "source", "description", "version", "license", "homepage", "repository", "keywords"):
            self.assertIn(key, plugin, f"marketplace.json plugin entry missing key: {key}")
        self.assertEqual(plugin["name"], "slashbooks")
        self.assertEqual(plugin["homepage"], "https://github.com/giltotherescue/slashbooks")
        self.assertEqual(plugin["repository"], "https://github.com/giltotherescue/slashbooks")

    def test_codex_plugin_json_parses(self):
        path = CODEX_PLUGIN_DIR / "plugin.json"
        self.assertTrue(path.exists(), f"{path} does not exist")
        with path.open() as f:
            data = json.load(f)
        for key in ("name", "description", "version", "author", "homepage", "repository", "skills", "interface"):
            self.assertIn(key, data, f".codex-plugin/plugin.json missing key: {key}")
        self.assertEqual(data["name"], "slashbooks")
        self.assertEqual(data["homepage"], "https://github.com/giltotherescue/slashbooks")
        self.assertEqual(data["repository"], "https://github.com/giltotherescue/slashbooks")
        self.assertEqual(data["skills"], "./skills/")
        self.assertIn("name", data["author"], ".codex-plugin author missing 'name'")
        for key in (
            "displayName",
            "shortDescription",
            "longDescription",
            "developerName",
            "category",
            "capabilities",
            "defaultPrompt",
        ):
            self.assertIn(key, data["interface"], f".codex-plugin interface missing key: {key}")

    def test_native_codex_marketplace_json_parses(self):
        path = CODEX_MARKETPLACE
        self.assertTrue(path.exists(), f"{path} does not exist")
        with path.open() as f:
            data = json.load(f)
        for key in ("name", "interface", "plugins"):
            self.assertIn(key, data, f".agents/plugins/marketplace.json missing key: {key}")
        self.assertIsInstance(data["plugins"], list)
        self.assertGreater(len(data["plugins"]), 0, "Codex marketplace plugins list is empty")
        plugin = data["plugins"][0]
        for key in ("name", "source", "policy", "category"):
            self.assertIn(key, plugin, f"Codex marketplace plugin entry missing key: {key}")
        self.assertEqual(plugin["name"], "slashbooks")
        self.assertEqual(plugin["source"], {"source": "local", "path": "./"})
        self.assertEqual(plugin["policy"]["installation"], "AVAILABLE")
        self.assertEqual(plugin["policy"]["authentication"], "ON_INSTALL")

    def test_claude_md_symlinks_to_agents_md(self):
        agents = REPO_ROOT / "AGENTS.md"
        claude = REPO_ROOT / "CLAUDE.md"
        self.assertTrue(agents.exists(), "AGENTS.md does not exist")
        self.assertTrue(claude.is_symlink(), "CLAUDE.md should be a symlink to AGENTS.md")
        self.assertEqual(claude.readlink(), Path("AGENTS.md"))


# ─────────────────────────────────────────────────────────────────────────────
# (b) bin/books wrapper
# ─────────────────────────────────────────────────────────────────────────────

class TestBinWrapper(unittest.TestCase):

    def setUp(self):
        self.wrapper = BIN_DIR / "books"

    def test_wrapper_exists(self):
        self.assertTrue(self.wrapper.exists(), f"{self.wrapper} does not exist")

    def test_wrapper_is_executable(self):
        mode = self.wrapper.stat().st_mode
        self.assertTrue(
            mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH),
            f"{self.wrapper} is not executable",
        )

    def test_wrapper_exports_pythonpath(self):
        content = self.wrapper.read_text()
        self.assertIn("PYTHONPATH", content, "bin/books does not export PYTHONPATH")
        self.assertIn("src", content, "bin/books does not reference src/ in PYTHONPATH")

    def test_wrapper_exports_templates_dir(self):
        content = self.wrapper.read_text()
        self.assertIn(
            "BOOKKEEPING_TEMPLATES_DIR",
            content,
            "bin/books does not export BOOKKEEPING_TEMPLATES_DIR",
        )
        self.assertIn(
            "skills/books-onboard/templates",
            content,
            "bin/books should point at the books-onboard templates directory",
        )

    def test_wrapper_sources_dotenv(self):
        content = self.wrapper.read_text()
        self.assertNotIn(". ./.env", content, "bin/books should not execute .env as shell code")
        self.assertNotIn("set -a", content, "bin/books should not shell-export .env")

    def test_wrapper_no_session_lifecycle_hooks(self):
        """
        The wrapper must not hook into Claude session lifecycle events.
        We check for actual hook-invocation patterns (e.g. CLAUDE_SESSION env var
        or the literal hook names), not the word 'lifecycle' which may appear in
        explanatory comments.
        """
        content = self.wrapper.read_text()
        for hook_phrase in (
            "CLAUDE_SESSION",
            "session-start",
            "session-stop",
            "on_session",
            "session_hook",
        ):
            self.assertNotIn(
                hook_phrase,
                content,
                f"bin/books contains session-lifecycle hook reference: {hook_phrase!r}",
            )

    def test_wrapper_exec_cli(self):
        content = self.wrapper.read_text()
        self.assertIn(
            "bookkeeping.cli",
            content,
            "bin/books does not invoke bookkeeping.cli",
        )

    def test_each_skill_has_executable_books_wrapper(self):
        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                wrapper = SKILLS_DIR / skill_name / "scripts" / "books"
                self.assertTrue(wrapper.exists(), f"{wrapper} does not exist")
                mode = wrapper.stat().st_mode
                self.assertTrue(
                    mode & stat.S_IXUSR,
                    f"{wrapper} is not executable by owner",
                )
                content = wrapper.read_text()
                self.assertIn("bookkeeping.cli", content)
                self.assertIn("BOOKKEEPING_TEMPLATES_DIR", content)
                result = subprocess.run(
                    [str(wrapper), "--help"],
                    cwd=str(wrapper.parent.parent),
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{wrapper} --help failed: {result.stderr[:200]}",
                )


# ─────────────────────────────────────────────────────────────────────────────
# (c) Command table assertion
# ─────────────────────────────────────────────────────────────────────────────

class TestCommandTable(unittest.TestCase):
    """
    Extract every `scripts/books <subcommand>` reference from all SKILL.md files
    and assert each resolves to a real CLI path via --help.
    """

    def test_all_skill_commands_exist(self):
        failures: list[str] = []
        command_table: dict[str, list[list[str]]] = {}

        for skill_name in SKILL_NAMES:
            skill_file = SKILLS_DIR / skill_name / "SKILL.md"
            if not skill_file.exists():
                failures.append(f"[{skill_name}] SKILL.md not found at {skill_file}")
                continue

            body = _skill_body(skill_file.read_text())
            invocations = _extract_books_invocations(body)
            command_table[skill_name] = invocations

            for tokens in invocations:
                ok, stderr = _check_cli_path(tokens)
                if not ok:
                    failures.append(
                        f"[{skill_name}] `scripts/books {' '.join(tokens)}` "
                        f"--help failed (rc!=0): {stderr[:200]}"
                    )

        # Print the command table for inspection
        print("\n\nPer-skill command table:")
        for skill_name, invocations in command_table.items():
            print(f"  {skill_name}:")
            for inv in invocations:
                print(f"    scripts/books {' '.join(inv)}")

        if failures:
            self.fail(
                f"{len(failures)} CLI path(s) failed:\n" + "\n".join(failures)
            )


# ─────────────────────────────────────────────────────────────────────────────
# (d) Frontmatter validation
# ─────────────────────────────────────────────────────────────────────────────

class TestSkillFrontmatter(unittest.TestCase):

    def _read_skill(self, skill_name: str) -> tuple[dict, str]:
        path = SKILLS_DIR / skill_name / "SKILL.md"
        self.assertTrue(path.exists(), f"SKILL.md not found: {path}")
        text = path.read_text()
        fm = _parse_frontmatter(text)
        body = _skill_body(text)
        return fm, body

    def test_all_skills_follow_agent_skills_frontmatter_spec(self):
        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                fm, _ = self._read_skill(skill_name)
                self.assertIn(
                    "name", fm,
                    f"[{skill_name}] frontmatter missing 'name'",
                )
                self.assertIn(
                    "description", fm,
                    f"[{skill_name}] frontmatter missing 'description'",
                )
                self.assertTrue(
                    fm["name"].strip(),
                    f"[{skill_name}] frontmatter 'name' is empty",
                )
                self.assertEqual(
                    fm["name"],
                    skill_name,
                    f"[{skill_name}] frontmatter name must match directory name",
                )
                self.assertLessEqual(
                    len(fm["name"]),
                    64,
                    f"[{skill_name}] frontmatter name exceeds Agent Skills limit",
                )
                self.assertRegex(
                    fm["name"],
                    r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$",
                    f"[{skill_name}] frontmatter name must be lowercase kebab-case",
                )
                self.assertNotIn("--", fm["name"], f"[{skill_name}] frontmatter name has consecutive hyphens")
                self.assertLessEqual(
                    len(fm["description"]),
                    1024,
                    f"[{skill_name}] frontmatter description exceeds Agent Skills limit",
                )
                if "allowed-tools" in fm:
                    self.assertIsInstance(fm["allowed-tools"], str)
                    self.assertTrue(
                        fm["allowed-tools"].strip(),
                        f"[{skill_name}] allowed-tools must be a non-empty string when present",
                    )

    def test_skills_do_not_use_codex_rejected_disable_model_invocation(self):
        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                path = SKILLS_DIR / skill_name / "SKILL.md"
                self.assertTrue(path.exists(), f"SKILL.md not found: {path}")
                text = path.read_text()
                self.assertNotRegex(
                    text,
                    r"disable-model-invocation\s*:\s*true",
                    f"[{skill_name}] uses disable-model-invocation:true, which Codex rejects",
                )


# ─────────────────────────────────────────────────────────────────────────────
# (e) Jargon scan and security phrase assertions
# ─────────────────────────────────────────────────────────────────────────────

class TestSkillSecurity(unittest.TestCase):

    def _body(self, skill_name: str) -> str:
        path = SKILLS_DIR / skill_name / "SKILL.md"
        text = path.read_text()
        return _skill_body(text)

    def test_untrusted_data_instruction_present(self):
        """Every skill body must contain the stable untrusted-data phrase."""
        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                body = self._body(skill_name)
                self.assertIn(
                    UNTRUSTED_DATA_PHRASE,
                    body,
                    f"[{skill_name}] body missing untrusted-data instruction "
                    f"(expected phrase: {UNTRUSTED_DATA_PHRASE!r})",
                )

    def test_minimum_context_phrase_present(self):
        """Every skill body must contain the minimum-context search rule."""
        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                body = self._body(skill_name)
                self.assertIn(
                    MINIMUM_CONTEXT_PHRASE,
                    body,
                    f"[{skill_name}] body missing minimum-context rule "
                    f"(expected phrase: {MINIMUM_CONTEXT_PHRASE!r})",
                )

    def test_no_owner_beancount_or_sql_instruction(self):
        """
        Skill bodies must not instruct the owner to write beancount syntax or SQL.
        Parenthetical asides like '(your books are stored as plain text files you
        can version)' are fine — we check for action-verb instructions only.
        """
        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                body = self._body(skill_name)
                match = OWNER_JARGON_RE.search(body)
                matched_text = match.group(0) if match else ""
                self.assertIsNone(
                    match,
                    f"[{skill_name}] body contains owner-facing jargon: {matched_text!r}",
                )

    def test_skills_hide_internal_commands_from_owner(self):
        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                body = self._body(skill_name)
                self.assertIn(
                    "Never show shell commands",
                    body,
                    f"[{skill_name}] missing owner-facing command hiding rule",
                )
                self.assertIn(
                    "plugin cache paths",
                    body,
                    f"[{skill_name}] missing plugin-cache hiding rule",
                )

    def test_owner_next_steps_use_slash_commands(self):
        searched_files = [
            *[SKILLS_DIR / skill_name / "SKILL.md" for skill_name in SKILL_NAMES],
            REPO_ROOT / "docs" / "doing-your-books.md",
        ]
        forbidden = re.compile(r"\b(?:run|use)\s+the\s+books-[a-z-]+\s+skill\b", re.IGNORECASE)
        for path in searched_files:
            with self.subTest(path=path):
                text = path.read_text()
                match = forbidden.search(text)
                if match is not None:
                    self.fail(
                        f"{path} should suggest slash commands like /books-review, "
                        f"not {match.group(0)!r}"
                    )

    def test_onboarding_is_audience_aware(self):
        body = self._body("books-onboard")
        for phrase in (
            "Always start onboarding by asking",
            "business\nowner, an accountant/bookkeeper, or someone developing/testing Slashbooks",
            "Wait\nfor the answer before asking business setup questions or showing demo answers",
            "If the user does not answer, continue in business-owner language",
            "Do not lead with file names",
            "Developer/tester",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)

    def test_all_skills_have_audience_language_guidance(self):
        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                body = self._body(skill_name)
                for phrase in (
                    "## Audience and language",
                    "Business owner",
                    "Accountant/bookkeeper",
                    "Developer/tester",
                    "Let the user",
                ):
                    self.assertIn(phrase, body)

    def test_demo_onboarding_asks_audience_before_profile(self):
        body = self._body("books-onboard")
        demo_section = body[body.index("### Northstar demo path"):]
        audience_prompt = demo_section.index("Before I show the demo setup")
        answer_list = demo_section.index("1. **Business name**")
        self.assertLess(audience_prompt, answer_list)
        self.assertIn("Do not treat \"demo company\" or\n\"sample books\" as an audience answer.", body)
        self.assertIn("ask it now\nand wait before showing the Northstar answers", demo_section)

    def test_demo_profile_requires_confirmation_before_creation(self):
        body = self._body("books-onboard")
        show_answers = body.index("### Northstar demo path")
        confirmation = body.index("After showing those answers, pause and ask")
        creation_command = body.index("scripts/books demo init <path>")
        self.assertLess(show_answers, confirmation)
        self.assertLess(confirmation, creation_command)
        for phrase in (
            "use the demo as\nshown or change anything first",
            "legal structure",
            "S corporation payroll",
            "confirm the revised demo profile before creating it",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)

    def test_review_can_add_better_missing_category(self):
        body = self._body("books-review")
        for phrase in (
            "do not keep the\ntransaction in the wrong bucket",
            "ask whether to add it",
            "scripts/books entity account-add",
            "Marketing events",
            "Expenses:Marketing",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)


if __name__ == "__main__":
    unittest.main()
