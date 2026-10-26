import json
import re
import struct
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "plugins" / "slashbooks-cloud"
DEFAULT_SCOPES = [
    "books:read", "questions:write", "proposals:write", "evidence:write",
    "sources:write", "runs:write", "books:write",
]
CHATGPT_DESCRIPTION = 'Slashbooks helps you handle your small business accounting through ChatGPT. Sync your business bank accounts, categorize transactions, close your books monthly, and prepare financial reports for your accountant. Ask questions about your finances and forecast cash flow.\n\nSlashbooks gives ChatGPT a double-entry accounting system that handles the calculations and checks that entries balance. Built-in workflows guide the bookkeeping for different types of businesses, from recording daily transactions to preparing for month-end close.\n\nRequires a Slashbooks Cloud account.'

CLAUDE_DESCRIPTION = CHATGPT_DESCRIPTION.replace("ChatGPT", "Claude")


class HostedDirectoryPluginTests(unittest.TestCase):
    def load(self, relative: str):
        return json.loads((PACKAGE / relative).read_text())

    def png_dimensions(self, relative: str):
        data = (PACKAGE / relative).read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(data[12:16], b"IHDR")
        return struct.unpack(">II", data[16:24])

    def test_portable_identity_and_endpoint(self):
        manifest = self.load("plugin.json")
        self.assertEqual(manifest["name"], "slashbooks-cloud")
        self.assertEqual(manifest["version"], "0.9.2")
        self.assertEqual(manifest["extensions"]["com.openai"]["interface"]["displayName"], "Slashbooks")
        interface = manifest["extensions"]["com.openai"]["interface"]
        self.assertLessEqual(len(interface["shortDescription"]), 30)
        self.assertEqual(interface["category"], "Finance")
        self.assertEqual(interface["longDescription"], CHATGPT_DESCRIPTION)
        self.assertTrue(interface["defaultPrompt"][0].startswith("Update my books"))
        self.assertEqual(interface["composerIcon"], "./assets/icon.png")
        self.assertEqual(interface["logo"], "./assets/icon.png")
        self.assertEqual(interface["composerIconDark"], "./assets/icon-dark.svg")
        self.assertEqual(interface["logoDark"], "./assets/icon-dark.svg")
        self.assertEqual(self.png_dimensions("assets/icon.png"), (512, 512))
        self.assertEqual(manifest["author"]["email"], "hi@slashbooks.co")
        self.assertNotIn("Cloud", manifest["description"])
        self.assertIn("Requires a Slashbooks Cloud account.", interface["longDescription"])
        self.assertNotIn("—", interface["longDescription"])

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
        # A second live Claude manifest nested in this repo breaks marketplace sync.
        self.assertFalse((PACKAGE / ".claude-plugin/plugin.json").exists())
        manifest = self.load("claude-plugin.template.json")
        self.assertEqual(manifest["name"], "slashbooks-cloud")
        self.assertEqual(manifest["version"], "0.9.2")
        self.assertEqual(manifest["description"], CLAUDE_DESCRIPTION)
        self.assertEqual(manifest["icon"], "./assets/icon.png")
        self.assertIn("Requires a Slashbooks Cloud account.", manifest["description"])
        self.assertNotIn("—", manifest["description"])
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
        interface = manifest["extensions"]["com.openai"]["interface"]
        for field in ("composerIcon", "composerIconDark", "logo", "logoDark"):
            self.assertTrue((PACKAGE / interface[field].removeprefix("./")).is_file())
        for skill in (PACKAGE / "skills").glob("*/SKILL.md"):
            text = skill.read_text()
            self.assertTrue(text.startswith("---\n"))
            self.assertRegex(text, r"(?m)^name: [a-z0-9-]+$")
            self.assertRegex(text, r"(?m)^description: .+$")

    def test_generated_procedures_are_current_and_package_local(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/hosted_plugin.py"), "check"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_skills_preserve_authorized_work_and_person_only_boundaries(self):
        getting_started = (PACKAGE / "skills/get-started/SKILL.md").read_text()
        books = (PACKAGE / "skills/books/SKILL.md").read_text()
        combined = getting_started + books
        self.assertNotIn("Ask whether the user is", combined)
        self.assertIn("Continue the user's requested task when it is known", getting_started)
        self.assertIn("Continue routine actions already allowed", books)
        self.assertIn("person-only", combined)
        self.assertIn("optional adjustments permission", combined)
        self.assertIn("OAuth step-up flow", books)

    def test_skills_route_company_bound_mcp_workflows(self):
        getting_started = (PACKAGE / "skills/get-started/SKILL.md").read_text()
        books = (PACKAGE / "skills/books/SKILL.md").read_text()
        combined = getting_started + books

        self.assertIn("get_bookkeeping_context", combined)
        self.assertIn("workflow_navigation.next_actions", combined)
        self.assertIn("start_authorized_bookkeeping", books)
        self.assertIn("get_bookkeeping_work", books)
        self.assertIn("check_close_readiness", books)
        self.assertNotRegex(combined, re.compile(r"(?is)find\s+the companies.*access"))
        self.assertNotRegex(combined, re.compile(r"(?i)more than one company"))

    def test_hosted_onboarding_requires_durable_prerequisites_or_handoff(self):
        getting_started = (PACKAGE / "skills/get-started/SKILL.md").read_text()
        books = (PACKAGE / "skills/books/SKILL.md").read_text()
        for text in (getting_started, books):
            skill = " ".join(text.split())
            self.assertIn("durably recorded", skill)
            self.assertIn("complete_bookkeeping_onboarding", skill)
            self.assertIn("explicitly confirms completion", skill)
            self.assertIn("read back", skill)
            self.assertIn("handoff_to_slashbooks", skill)
            self.assertNotIn("do not select or call onboarding completion", skill)
        self.assertIn("Do not invent a protected choice", getting_started)


if __name__ == "__main__":
    unittest.main()
