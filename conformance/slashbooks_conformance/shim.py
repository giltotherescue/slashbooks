"""Sandbox command shim.

Usage: python3 shim.py <state.json> <books arguments...>

It stands in for `scripts/books` inside a conformance sandbox. It loads the
saved world, runs one command, saves the world, and prints the result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slashbooks_conformance.world import World  # noqa: E402


def run_command(state_path: Path, argv: list[str]) -> tuple[bool, str]:
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
