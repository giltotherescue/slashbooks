from __future__ import annotations

import argparse
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bookkeeping.cli import build_parser
from bookkeeping.remote_contract import ContractError, command_spec, parse_command, relative_path, rewrite_paths, virtual_path


class RemoteContractTests(unittest.TestCase):
    def test_every_ordinary_parser_leaf_has_complete_metadata(self):
        seen = set()

        def value(action):
            if action.choices:
                return str(next(iter(action.choices)))
            if action.type is int:
                return "2026"
            if action.type is float:
                return "1"
            if "date" in action.dest or action.dest in {"cutover", "as_of", "today", "coverage_from", "coverage_to"}:
                return "2026-01-01"
            return "placeholder"

        def walk(parser, argv, path):
            argv = list(argv)
            selector = None
            actions = []
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    selector = action
                elif not isinstance(action, argparse._HelpAction):
                    actions.append(action)
                    if action.required or not action.option_strings:
                        if action.option_strings:
                            argv.append(action.option_strings[0])
                        if action.nargs != 0:
                            argv.append(value(action))
            # Mercury's required mutually-exclusive group has optional members.
            for group in parser._mutually_exclusive_groups:
                if group.required:
                    action = group._group_actions[0]
                    argv.extend([action.option_strings[0], value(action)])
            if selector:
                for name, child in selector.choices.items():
                    if not path and name in {"cloud", "hosted", "remote", "files"}:
                        continue
                    walk(child, argv + [name], path + (name,))
                return
            with self.subTest(command=path):
                spec = command_spec(argv)
                self.assertEqual(spec["path"], path)
                self.assertEqual(set(spec), {"path", "entity_fields", "input_fields", "output_fields", "mutates", "network"})
                fields = set(spec["entity_fields"] + spec["input_fields"] + spec["output_fields"])
                self.assertTrue({a.dest for a in actions if a.type is Path} <= fields)
                # Reconstruction must remain parseable and preserve values.
                before, _, _ = parse_command(argv)
                after, _, _ = parse_command(rewrite_paths(argv, {}))
                self.assertEqual(vars(before), vars(after))
                seen.add(path)

        walk(build_parser(), [], ())
        self.assertGreaterEqual(len(seen), 60)
        for family in ("entity", "demo", "connector", "ingest", "ledger", "qb", "report", "ask", "reconcile", "reconcile-resolve", "backtest", "compare", "queue", "quarterly-review", "sanity-check", "export"):
            self.assertTrue(any(p[0] == family for p in seen), family)

    def test_read_permissions_are_not_artifact_permissions(self):
        commands = [
            ["queue", "list", "--entity", "@entity"], ["queue", "show", "--entity", "@entity", "--item", "x"],
            ["queue", "summary", "--entity", "@entity"], ["queue", "split-template-list", "--entity", "@entity"],
            ["queue", "transfer-candidates", "--entity", "@entity"], ["queue", "transfer-exceptions", "--entity", "@entity"],
            ["quarterly-review", "--entity", "@entity", "--quarter", "Q1", "--year", "2026"],
            ["export", "--entity", "@entity", "--from", "2026-01-01", "--to", "2026-03-31", "--output-dir", "@output/export"],
            ["report", "pnl", "--entity", "@entity", "--from", "2026-01-01", "--to", "2026-03-31", "--output", "@output/pnl.json"],
        ]
        for argv in commands:
            with self.subTest(argv=argv):
                self.assertFalse(command_spec(argv)["mutates"])

    def test_rewrite_only_paths_not_business_text(self):
        argv = ["ask", "--entity", "@entity", "@entity"]
        args, _, _ = parse_command(rewrite_paths(argv, {"@entity": "/tmp/company"}))
        self.assertEqual(args.entity, "/tmp/company")
        self.assertEqual(args.question, "@entity")
        argv = ["backtest", "run", "--entity=@entity", "--qb-folder=@input/qb", "--from=2026-01-01", "--to=2026-02-01", "--banksync-json", "@input/a.json", "@input/b.json"]
        args, _, _ = parse_command(rewrite_paths(argv, {"@input/a.json": "/tmp/a.json"}))
        self.assertEqual(args.banksync_json, [Path("/tmp/a.json"), Path("@input/b.json")])

    def test_unsafe_paths_and_commands(self):
        for value in ("../a", "/etc/passwd", "a//b", "a/./b", "a\\b", "C:/x", "%2e%2e/x", ".", "a/../b", "a\x00b"):
            with self.subTest(path=value), self.assertRaises(ContractError):
                relative_path(value)
        for argv in (["hosted", "status"], ["queue", "list", "--ent", "x"], ["report", "--help"], [], ["nonexistent"]):
            with self.subTest(argv=argv), self.assertRaises(ContractError):
                command_spec(argv)
        self.assertEqual(virtual_path("@entity"), ("entity", ""))
        self.assertEqual(virtual_path("@input/a.json"), ("input", "a.json"))


if __name__ == "__main__":
    unittest.main()
