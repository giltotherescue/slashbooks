"""Sandbox command shim.

Usage: python3 shim.py <state.json> <books arguments...>

It stands in for `scripts/books` inside a conformance sandbox. It loads the
saved world, runs one command, saves the world, and prints the result.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slashbooks_conformance.world import World  # noqa: E402


def _is_discovery(argv: list[str]) -> bool:
    """Allow only engine help and versioned procedure reads outside the world."""
    help_routes = [[], ["queue"], ["queue", "confirm"], ["cloud"],
                   ["cloud", "questions"], ["procedures"], ["procedures", "show"]]
    if any(argv == [*route, "--help"] for route in help_routes):
        return True
    if argv == ["procedures", "list"]:
        return True
    if argv[:2] != ["procedures", "show"]:
        return False
    tail = argv[2:]
    if len(tail) == 3 and tail[1] == "--format" and tail[2] in ("md", "json"):
        tail = tail[:1]
    return len(tail) == 1 and (
        tail[0] == "--all" or re.fullmatch(r"[a-z][a-z0-9-]*", tail[0]) is not None
    )


def run_command(state_path: Path, argv: list[str]) -> tuple[bool, str]:
    if _is_discovery(argv):
        root = Path(__file__).resolve().parents[2]
        # Resolve the package from this checkout, never an installed engine or caller PYTHONPATH.
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONPATH", "PYTHONHOME"}}
        env["PYTHONPATH"] = str(root / "src")
        proc = subprocess.run([sys.executable, "-m", "bookkeeping.cli", *argv],
                              cwd=root, env=env, capture_output=True, text=True, timeout=30)
        return proc.returncode == 0, proc.stdout or proc.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    ok, text = World(state).execute(argv)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return ok, text


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: shim.py <state.json> <books arguments...>", file=sys.stderr)
        return 2
    ok, text = run_command(Path(sys.argv[1]), sys.argv[2:])
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
