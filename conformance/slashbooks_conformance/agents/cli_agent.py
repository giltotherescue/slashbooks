"""Headless coding-agent adapter (Claude Code or Codex CLI).

The adapter starts the agent CLI in a sandbox directory. The sandbox holds a
`books` command that stands in for the plugin's `scripts/books` wrapper and talks
to the simulated company. The agent loads the plugin build under test and
follows its skills. The adapter needs the agent CLI and a local login. CI does
not use it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..scenarios import COMMAND_NOTES

SANDBOX_NOTE = (
    "\n\nSandbox rules: use the Slashbooks plugin skills. In this sandbox the plugin's "
    "`scripts/books` wrapper is replaced by the `./books` command in your working directory. "
    "Run every books command as `./books <arguments>`. Do not use the network, other mail "
    "tools, or other files. When you finish, write your reply to the person as your final "
    "message.\n\n" + COMMAND_NOTES
)


class CliAgent:
    def __init__(self, kind: str, plugin_dir: Path, model: str | None = None,
                 timeout: int = 600, executable: str | None = None) -> None:
        if kind not in ("claude-code", "codex"):
            raise ValueError(f"unknown CLI agent: {kind}")
        self.kind = kind
        self.name = kind
        self.plugin_dir = Path(plugin_dir)
        self.model = model
        self.timeout = timeout
        self.executable = executable or ("claude" if kind == "claude-code" else "codex")

    def command(self, prompt: str, workdir: Path) -> list[str]:
        if self.kind == "claude-code":
            cmd = [self.executable, "-p", prompt, "--plugin-dir", str(self.plugin_dir),
                   "--output-format", "stream-json", "--verbose",
                   "--permission-mode", "dontAsk",
                   "--allowedTools", "Bash(./books:*)", "Read"]
            if self.model:
                cmd += ["--model", self.model]
            return cmd
        cmd = [self.executable, "exec", "--cd", str(workdir), "--sandbox", "workspace-write",
               "--skip-git-repo-check", "--ephemeral", "--json",
               "-o", str(workdir / "last-message.txt")]
        if self.model:
            cmd += ["--model", self.model]
        return cmd + [prompt]

    def run(self, scenario: Any, session: Any) -> str:
        if shutil.which(self.executable) is None and not Path(self.executable).exists():
            raise RuntimeError(f"{self.executable} is not installed")
        prompt = scenario.full_prompt() + SANDBOX_NOTE
        proc = subprocess.run(self.command(prompt, session.workdir), cwd=session.workdir,
                              capture_output=True, text=True, timeout=self.timeout)
        final, commands = parse_output(self.kind, proc.stdout, session.workdir)
        session.agent_commands.extend(commands)
        if proc.returncode != 0 and not final:
            raise RuntimeError(f"agent exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        return final


def parse_output(kind: str, stdout: str, workdir: Path) -> tuple[str, list[str]]:
    """Return the final message and the shell commands the agent ran."""
    final, commands = "", []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if kind == "claude-code":
            if event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use" and block.get("name") == "Bash":
                        commands.append(str(block.get("input", {}).get("command", "")))
            elif event.get("type") == "result":
                final = str(event.get("result", ""))
        else:
            item = event.get("item") or event.get("msg") or {}
            if item.get("type") in ("command_execution", "exec_command_begin"):
                commands.append(str(item.get("command", "")))
    if kind == "codex":
        last = workdir / "last-message.txt"
        if last.exists():
            final = last.read_text(encoding="utf-8")
    return final, commands
