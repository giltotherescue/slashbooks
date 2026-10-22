import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "plugins" / "slashbooks-cloud"
DEFAULT_SCOPES = [
    "books:read", "questions:write", "proposals:write", "evidence:write",
    "sources:write", "runs:write", "books:write",
]
CANONICAL_DESCRIPTION = (
    "Keep your business books up to date from ChatGPT. Review transactions, import bank statements, "
    "reconcile accounts, and get financial reports and cash forecasts using your Slashbooks Cloud company.\n\n"
    "Slashbooks records routine transactions under the permissions you approve and asks when something "
    "needs your judgment. You stay in control of approvals and closing the books.\n\n"
    "Sign in to Slashbooks to choose your company and access. No software installation is needed. "
    "Requires a Slashbooks Cloud account; request access at slashbooks.co."
)


class HostedDirectoryPluginTests(unittest.TestCase):
    def load(self, relative: str):
        return json.loads((PACKAGE / relative).read_text())

    def test_portable_identity_and_endpoint(self):
        manifest = self.load("plugin.json")
        self.assertEqual(manifest["name"], "slashbooks-cloud")
        self.assertEqual(manifest["version"], "0.9.0")
        self.assertEqual(manifest["extensions"]["com.openai"]["interface"]["displayName"], "Slashbooks")
        interface = manifest["extensions"]["com.openai"]["interface"]
        self.assertLessEqual(len(interface["shortDescription"]), 30)
        self.assertEqual(interface["category"], "Finance")
        self.assertEqual(interface["longDescription"], CANONICAL_DESCRIPTION)
        self.assertTrue(interface["defaultPrompt"][0].startswith("Update my books"))
        self.assertEqual(manifest["author"]["email"], "hi@slashbooks.co")

        mcp = self.load("mcp.json")["mcpServers"]
        self.assertEqual(list(mcp), ["slashbooks"])
        self.assertEqual(mcp["slashbooks"]["type"], "streamable-http")
        self.assertEqual(mcp["slashbooks"]["url"], "https://slashbooks.co/mcp")
        auth = mcp["slashbooks"]["extensions"]["com.openai"]["auth"]
        self.assertEqual(auth["type"], "oauth")
        self.assertEqual(auth["client"], {"mode": "dcr"})
        self.assertEqual(auth["baseScopes"], DEFAULT_SCOPES)
        self.assertNotIn("adjustments:write", auth["baseScopes"])

    def test_claude_compatibility_identity_and_endpoint(self):
        manifest = self.load(".claude-plugin/plugin.json")
        self.assertEqual(manifest["name"], "slashbooks-cloud")
        self.assertEqual(manifest["version"], "0.9.0")
        mcp = self.load(".mcp.json")["mcpServers"]
        self.assertEqual(mcp["slashbooks"]["url"], "https://slashbooks.co/mcp")

    def test_package_has_no_local_runtime_or_secrets(self):
        paths = [p for p in PACKAGE.rglob("*") if p.is_file()]
        forbidden_suffixes = {".py", ".sh", ".js", ".ts", ".exe", ".bin"}
        self.assertFalse([p for p in paths if p.suffix in forbidden_suffixes])
        self.assertFalse([p for p in paths if p.name in {"package.json", "requirements.txt", "uv.lock"}])

        text = "\n".join(p.read_text(errors="replace") for p in paths)
        self.assertNotRegex(text, re.compile(r"(?i)(client[_-]?secret|access[_-]?token|api[_-]?key)\s*[:=]\s*['\"]?[^\s,'\"]+"))
        self.assertNotIn("plugin_asdk_app_", text)
        self.assertNotIn("scripts/books", text)

    def test_all_declared_paths_exist_and_skills_have_frontmatter(self):
        manifest = self.load("plugin.json")
        onboarding = manifest["extensions"]["com.openai"]["onboardingSkill"]
        self.assertTrue((PACKAGE / onboarding.removeprefix("./")).is_file())
        for skill in (PACKAGE / "skills").glob("*/SKILL.md"):
            text = skill.read_text()
            self.assertTrue(text.startswith("---\n"))
            self.assertRegex(text, r"(?m)^name: [a-z0-9-]+$")
            self.assertRegex(text, r"(?m)^description: .+$")

    def test_skills_preserve_authorized_work_and_person_only_boundaries(self):
        getting_started = (PACKAGE / "skills/get-started/SKILL.md").read_text()
        books = (PACKAGE / "skills/books/SKILL.md").read_text()
        combined = getting_started + books
        self.assertNotIn("Ask whether the user is", combined)
        self.assertIn("Continue the task already requested", getting_started)
        self.assertIn("already authorized without asking for approval at\nevery step", books)
        self.assertIn("person-only", combined)
        self.assertIn("optional adjustments permission", combined)
        self.assertIn("OAuth step-up flow", books)


if __name__ == "__main__":
    unittest.main()
