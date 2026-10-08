"""Transport metadata for the ordinary CLI; no accounting or I/O logic."""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
from typing import Any


class ContractError(ValueError):
    """Invalid command or unsafe transport path."""


def _error(message: str) -> None:
    raise ContractError(message)


def parse_command(argv: list[str]) -> tuple[argparse.Namespace, list[Any], tuple[str, ...]]:
    # Import lazily: cli imports this module when routing ordinary commands.
    from .cli import build_parser

    if (not isinstance(argv, list) or not argv or len(argv) > 1024
            or any(not isinstance(v, str) or len(v) > 16384 or "\x00" in v for v in argv)):
        raise ContractError("Expected a bounded, nonempty argv array of strings.")
    if any(v in {"-h", "--help"} for v in argv):
        raise ContractError("Help is handled locally, not by remote execution.")
    parser = build_parser()

    def prepare(p: argparse.ArgumentParser) -> None:
        p.allow_abbrev = False
        p.error = _error
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    prepare(child)

    prepare(parser)
    args = parser.parse_args(argv)
    levels, path = [], []
    while True:
        levels.append(parser)
        selectors = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        if not selectors:
            break
        action = selectors[0]
        selected = getattr(args, action.dest)
        path.append(selected)
        parser = action.choices[selected]
    return args, levels, tuple(path)


def command_spec(argv: list[str]) -> dict[str, Any]:
    args, levels, path = parse_command(argv)
    if path[0] in {"hosted", "remote", "files"}:
        raise ContractError("Transport management is not an engine command.")
    entity = [name for name in ("entity", "entity_path") if hasattr(args, name)]
    if path[0] in {"entity", "demo"}:
        entity.append("path")
    inputs = [name for name in ("input", "file", "folder", "qb_folder", "banksync_json", "balances_json")
              if hasattr(args, name)]
    outputs = [name for name in ("output", "output_dir") if hasattr(args, name)]
    if path == ("ledger", "snapshot"):
        inputs.append("store")
    elif path == ("ledger", "migrate"):
        outputs.append("store")
    classified = set(entity + inputs + outputs)
    for level in levels:
        for action in level._actions:
            if action.type is Path and action.dest not in classified:
                raise ContractError(f"Path field {action.dest} has no transport classification.")
    network = path[0] == "connector" and path[1] != "csv"
    read = (path[0] in {"report", "ask", "sanity-check", "quarterly-review", "export"}
            or path in {("qb", "inventory"), ("entity", "related-entity", "list"),
                        ("connector", "csv", "inspect"), ("connector", "csv", "propose-mapping"),
                        ("connector", "csv", "parse"), ("ledger", "snapshot"),
                        ("queue", "list"), ("queue", "show"), ("queue", "summary"),
                        ("queue", "split-template-list"), ("queue", "transfer-candidates"),
                        ("queue", "transfer-exceptions")})
    mutates = not read and not network
    if path == ("ledger", "migrate") and args.dry_run:
        mutates = False
    if path == ("qb", "repair-opening") and not args.apply:
        mutates = False
    # mutates is a business-write permission, not an artifact/cache-write flag.
    return dict(path=path, entity_fields=entity, input_fields=inputs,
                output_fields=outputs, mutates=mutates, network=network)


def rewrite_paths(argv: list[str], replacements: dict[str, str]) -> list[str]:
    """Rebuild parsed argv, replacing only path fields (never narration/options).

    Explicit defaults freeze dates and other parser defaults for retry safety.
    Parent options remain before their subcommand, as argparse requires.
    """
    args, levels, path = parse_command(argv)
    spec = command_spec(argv)
    fields = set(spec["entity_fields"] + spec["input_fields"] + spec["output_fields"])
    result: list[str] = []

    def render(value: Any, field: str) -> str:
        value = str(value)
        return replacements.get(value, value) if field in fields else value

    for index, level in enumerate(levels):
        for action in level._actions:
            if isinstance(action, (argparse._SubParsersAction, argparse._HelpAction)):
                continue
            value = getattr(args, action.dest, None)
            if value is None:
                continue
            option = action.option_strings[-1] if action.option_strings else None
            if isinstance(action, argparse._StoreTrueAction):
                if value:
                    result.append(option)
            elif isinstance(action, argparse._StoreFalseAction):
                if not value:
                    result.append(option)
            elif isinstance(action, argparse._StoreConstAction):
                if value == action.const and value != action.default:
                    result.append(option)
            elif isinstance(action, argparse._AppendAction):
                for item in value:
                    result.extend([option, render(item, action.dest)])
            else:
                values = value if isinstance(value, list) else [value]
                if option:
                    # Equals syntax preserves values beginning with a dash.
                    if len(values) == 1 and action.nargs not in {"*", "+"}:
                        result.append(option + "=" + render(values[0], action.dest))
                        continue
                    result.append(option)
                result.extend(render(item, action.dest) for item in values)
        if index < len(path):
            result.append(path[index])
    return result


def relative_path(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or "\\" in value or ":" in value or "%" in value
            or value.startswith(("/", "~", "@"))):
        raise ContractError("Expected a plain company-relative path.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or len(part) > 255 for part in parts):
        raise ContractError("Empty, dot and traversal path components are forbidden.")
    return str(PurePosixPath(*parts))


def virtual_path(value: str) -> tuple[str, str]:
    if value == "@entity":
        return "entity", ""
    for root in ("entity", "input", "output"):
        prefix = "@" + root + "/"
        if isinstance(value, str) and value.startswith(prefix):
            return root, relative_path(value[len(prefix):])
    raise ContractError("Paths must use @entity, @input/relative or @output/relative.")
