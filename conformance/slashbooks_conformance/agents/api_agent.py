"""Model API adapter: a tool-calling loop over the Anthropic Messages API.

The loop gives the model the plugin's skill text as its system prompt and one
tool, `books`, that runs a command in the simulated company. It needs a local
credential (`ANTHROPIC_API_KEY`, or `ANTHROPIC_AUTH_TOKEN` with an optional
`ANTHROPIC_BASE_URL`). CI does not use it. A custom `transport` callable replaces
the HTTP call, which the suite's own tests use.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..scenarios import COMMAND_NOTES

SKILLS = ["books", "books-review", "books-cloud", "books-close", "books-checkup"]
EXTRA_FILES = ["skills/books/references/company-workspace.md"]
DEFAULT_MODEL = "claude-haiku-4-5"
MAX_TURNS = 40

TOOL = {
    "name": "books",
    "description": (
        "Run one Slashbooks command in the company workspace. This tool stands in for the "
        "plugin's scripts/books wrapper. Pass the arguments that follow it as an array, for "
        "example [\"queue\", \"list\", \"--status\", \"open\"]. `mail list|read|triage` is the "
        "company mail inbox."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
        "required": ["argv"],
    },
}

Transport = Callable[[dict[str, Any]], dict[str, Any]]


def http_transport(timeout: int) -> Transport:
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    key = os.environ.get("ANTHROPIC_API_KEY")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not key and not token:
        raise RuntimeError("Set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) to run the api agent.")

    def send(payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
        headers["x-api-key" if key else "authorization"] = key or f"Bearer {token}"
        request = urllib.request.Request(f"{base}/v1/messages", json.dumps(payload).encode(),
                                         headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"HTTP {error.code}: {error.read()[:300].decode('utf-8', 'replace')}") from None

    return send


def load_skill_text(plugin_dir: Path) -> str:
    parts = []
    for name in SKILLS:
        path = plugin_dir / "skills" / name / "SKILL.md"
        if path.exists():
            parts.append(f"=== skill: {name} ===\n{path.read_text(encoding='utf-8')}")
    for rel in EXTRA_FILES:
        path = plugin_dir / rel
        if path.exists():
            parts.append(f"=== reference: {rel} ===\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


class ApiAgent:
    def __init__(self, plugin_dir: Path, model: str | None = None, timeout: int = 600,
                 transport: Transport | None = None) -> None:
        self.plugin_dir = Path(plugin_dir)
        self.model = model or DEFAULT_MODEL
        self.name = f"api:{self.model}"
        self.timeout = timeout
        self.transport = transport

    def run(self, scenario: Any, session: Any) -> str:
        transport = self.transport or http_transport(self.timeout)
        system = (
            "You are an AI bookkeeping agent. The Slashbooks plugin is installed. Follow its "
            "skills exactly.\n\n" + COMMAND_NOTES + "\n\nThe skill text follows.\n\n"
            + load_skill_text(self.plugin_dir)
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": scenario.full_prompt()}]
        for _ in range(MAX_TURNS):
            reply = transport({"model": self.model, "max_tokens": 4096, "system": system,
                               "tools": [TOOL], "messages": messages})
            content = reply.get("content", [])
            messages.append({"role": "assistant", "content": content})
            calls = [b for b in content if b.get("type") == "tool_use"]
            if not calls:
                return "".join(b.get("text", "") for b in content if b.get("type") == "text")
            results = []
            for call in calls:
                argv = call.get("input", {}).get("argv", [])
                ok, text = session.call([str(a) for a in argv])
                results.append({"type": "tool_result", "tool_use_id": call["id"],
                                "content": text, "is_error": not ok})
            messages.append({"role": "user", "content": results})
        raise RuntimeError(f"agent did not finish in {MAX_TURNS} turns")
