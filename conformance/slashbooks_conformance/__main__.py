"""Command line: python3 -m slashbooks_conformance run --agent scripted"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .runner import run_suite, to_json
from .scenarios import all_scenarios

REPO_ROOT = Path(__file__).resolve().parents[2]


def build_agent(args: argparse.Namespace):
    if args.agent == "scripted":
        from .agents.scripted import ScriptedAgent
        return ScriptedAgent(set(args.flaw or ()))
    if args.agent in ("claude-code", "codex"):
        from .agents.cli_agent import CliAgent
        return CliAgent(args.agent, plugin_dir=Path(args.plugin_dir), model=args.model,
                        timeout=args.timeout)
    if args.agent == "api":
        from .agents.api_agent import ApiAgent
        return ApiAgent(plugin_dir=Path(args.plugin_dir), model=args.model, timeout=args.timeout)
    raise SystemExit(f"unknown agent: {args.agent}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slashbooks_conformance")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List scenarios")
    run = sub.add_parser("run", help="Run the suite against an agent")
    run.add_argument("--agent", default="scripted",
                     choices=["scripted", "claude-code", "codex", "api"])
    run.add_argument("--scenario", action="append", help="Scenario id (repeatable). Default: all")
    run.add_argument("--plugin-dir", default=str(REPO_ROOT),
                     help="Plugin build to test (a checkout or an installed plugin directory)")
    run.add_argument("--model", default=os.environ.get("CONFORMANCE_MODEL"))
    run.add_argument("--timeout", type=int, default=600, help="Seconds per scenario")
    run.add_argument("--min-score", type=float, default=1.0,
                     help="Minimum score from 0 to 1. A critical failure always fails the run.")
    run.add_argument("--json", dest="json_path", help="Write the JSON report to this path")
    run.add_argument("--flaw", action="append",
                     help="Scripted agent only: break one rule on purpose (repeatable)")
    args = parser.parse_args(argv)

    if args.command == "list":
        for s in all_scenarios():
            print(f"{s.id}: {s.title}")
        return 0

    report = run_suite(build_agent(args), args.scenario, args.min_score)
    print(report["text"])
    if args.json_path:
        Path(args.json_path).write_text(to_json(report) + "\n", encoding="utf-8")
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
