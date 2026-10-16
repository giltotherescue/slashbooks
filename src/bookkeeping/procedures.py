"""Versioned agent procedures: one source for skills, agents and hosted runners.

Each procedure is a Markdown file in ``bookkeeping/procedures/`` with a small
front matter (name, version, title) and one rule per ``- `` bullet. Skills
reference a procedure by name and version. They do not copy its text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

PROCEDURES_DIR = Path(__file__).resolve().parent / "procedures"
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class ProcedureError(ValueError):
    pass


def _parse(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n(.*)\Z", text, re.S)
    if not match:
        raise ProcedureError(f"{path.name}: missing front matter")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    if set(meta) != {"name", "version", "title"}:
        raise ProcedureError(f"{path.name}: front matter must be name, version, title")
    if meta["name"] != path.stem or not _NAME_RE.match(meta["name"]):
        raise ProcedureError(f"{path.name}: name must match the file name")
    if not meta["version"].isdigit() or int(meta["version"]) < 1:
        raise ProcedureError(f"{path.name}: version must be a positive integer")
    body = match.group(2).strip("\n")
    rules = []
    for line in body.splitlines():
        if not line.startswith("- "):
            raise ProcedureError(f"{path.name}: body lines must be '- ' rules")
        rules.append(line[2:].strip())
    if not rules:
        raise ProcedureError(f"{path.name}: no rules")
    return {
        "name": meta["name"],
        "version": int(meta["version"]),
        "title": meta["title"],
        "rules": rules,
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def list_procedures() -> list[dict[str, Any]]:
    return [_parse(p) for p in sorted(PROCEDURES_DIR.glob("*.md"))]


def get_procedure(name: str) -> dict[str, Any]:
    if not _NAME_RE.match(name or ""):
        raise ProcedureError(f"unknown procedure: {name!r}")
    path = PROCEDURES_DIR / f"{name}.md"
    if not path.is_file():
        raise ProcedureError(f"unknown procedure: {name!r}")
    return _parse(path)


def render_markdown(proc: dict[str, Any]) -> str:
    lines = [f"## {proc['title']} (procedure {proc['name']}, version {proc['version']})", ""]
    lines += [f"- {rule}" for rule in proc["rules"]]
    return "\n".join(lines) + "\n"


def bundle_digest(procs: list[dict[str, Any]]) -> str:
    joined = "\n".join(f"{p['name']}@{p['version']}:{p['sha256']}" for p in procs)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("procedures", help="Show versioned agent procedures")
    sub = parser.add_subparsers(dest="procedures_command", required=True)
    sub.add_parser("list", help="List procedures with versions")
    show = sub.add_parser("show", help="Print one procedure, or all of them")
    show.add_argument("name", nargs="?", help="Procedure name")
    show.add_argument("--all", action="store_true", help="Print every procedure")
    show.add_argument("--format", choices=["md", "json"], default="md")


def run(args: argparse.Namespace) -> int:
    try:
        if args.procedures_command == "list":
            procs = list_procedures()
            print(json.dumps({
                "bundle_sha256": bundle_digest(procs),
                "procedures": [{k: p[k] for k in ("name", "version", "title", "sha256")}
                               for p in procs],
            }, indent=2))
            return 0
        if bool(args.name) == bool(args.all):
            raise ProcedureError("give a procedure name or --all")
        procs = list_procedures() if args.all else [get_procedure(args.name)]
        if args.format == "json":
            payload: Any = procs[0] if args.name else {
                "bundle_sha256": bundle_digest(procs), "procedures": procs}
            print(json.dumps(payload, indent=2))
        else:
            print("\n".join(render_markdown(p) for p in procs), end="")
        return 0
    except ProcedureError as exc:
        print(json.dumps({"error": {"code": "UNKNOWN_PROCEDURE", "message": str(exc)}}),
              file=sys.stderr)
        return 1
